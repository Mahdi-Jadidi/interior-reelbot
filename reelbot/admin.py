"""Local operator actions for exceptions; never grants creative approval."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import uuid
from pathlib import Path

from .config import Settings
from .media import verify_reel
from .store import Store
from .telegram import TelegramAPI
from .worker import make_cover


def retry_reel(store: Store, reel_id: int) -> dict:
    reel = store.get_reel(reel_id)
    if not reel:
        raise ValueError("Reel does not exist")
    if reel["status"] != "needs_operator":
        raise ValueError("Only an exception reel can be retried")
    if not store.approval_valid(reel_id, reel["plan_hash"]):
        raise ValueError("Current plan is not approved")
    job = store.enqueue_job(
        reel_id, "render", f"render:{reel_id}:{reel['plan_hash']}:retry:{uuid.uuid4().hex}", {}
    )
    store.update_reel(reel_id, status="queued")
    return job


async def complete_reel(settings: Settings, store: Store, reel_id: int, source: Path) -> Path:
    reel = store.get_reel(reel_id)
    if not reel or not store.approval_valid(reel_id, reel["plan_hash"]):
        raise ValueError("An approved current plan is required")
    if reel["status"] not in {"needs_operator", "queued"}:
        raise ValueError("Reel is not waiting for operator completion")
    verified = await asyncio.to_thread(verify_reel, source)
    target = settings.data_dir / "final" / str(reel_id) / "reel.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    cover = await asyncio.to_thread(make_cover, target, target.with_name("cover.jpg"))
    plan = json.loads(reel["plan_json"])
    target.with_name("caption.txt").write_text(plan.get("caption", ""), encoding="utf-8")
    current = store.get_reel(reel_id)
    if not store.approval_valid(reel_id, reel["plan_hash"]) or current["status"] not in {"needs_operator", "queued"}:
        raise ValueError("Plan changed during operator completion")
    telegram = TelegramAPI(settings.telegram_bot_token)
    try:
        await telegram.send_video(
            int(reel["chat_id"]), target,
            caption="پیش‌نمایش ریل آماده است. لطفاً همین نسخه را بررسی کنید.",
            buttons=[[("تأیید تحویل", f"deliver:{reel_id}:{verified.sha256[:12]}")],
                     [("اصلاح ویدیو", f"revise_video:{reel_id}")]],
        )
    finally:
        await telegram.close()
    store.update_reel(reel_id, final_path=str(target), final_hash=verified.sha256,
                      status="awaiting_final_approval")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Reelbot operator commands")
    sub = parser.add_subparsers(dest="action", required=True)
    report = sub.add_parser("report", help="current spend reservations")
    retry = sub.add_parser("retry", help="retry an approved exception reel")
    retry.add_argument("reel_id", type=int)
    complete = sub.add_parser("complete", help="submit a polished final MP4 for client review")
    complete.add_argument("reel_id", type=int)
    complete.add_argument("path", type=Path)
    args = parser.parse_args()
    settings = Settings.from_env()
    with Store(settings.data_dir / "reelbot.sqlite3") as store:
        if args.action == "report":
            print(json.dumps({"openai_reserved_usd": store.total_spend("openai")}, indent=2))
        elif args.action == "retry":
            print(json.dumps(retry_reel(store, args.reel_id), ensure_ascii=False, indent=2))
        elif args.action == "complete":
            if not settings.telegram_bot_token:
                raise ValueError("TELEGRAM_BOT_TOKEN is required")
            print(asyncio.run(complete_reel(settings, store, args.reel_id, args.path)))


if __name__ == "__main__":
    main()
