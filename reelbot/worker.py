"""Durable local render worker.

Paid Higgsfield generation is deliberately separate: this worker assembles only
approved, already available source assets. A synthetic presenter request is
sent to the exception queue until a verified MCP operation and quote exist.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from pathlib import Path

from .ai import Director
from .config import Settings
from .media import MediaError, RenderRequest, inspect_media, render_reel
from .store import Store
from .telegram import TelegramAPI

LOG = logging.getLogger(__name__)


def make_cover(video: Path, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "1.0",
         "-i", str(video), "-frames:v", "1", "-q:v", "2", str(output)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode or not output.is_file():
        raise MediaError("Could not extract cover from final video")
    return output


def _srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3_600_000)
    minutes, ms = divmod(ms, 60_000)
    whole, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{whole:02},{ms:03}"


def make_subtitles(script: str, duration: float, output: Path) -> Path:
    # Sentence-level subtitles keep the first implementation readable without
    # claiming word-perfect alignment. The voice recording is never modified.
    clauses = [item.strip() for item in re.split(r"(?<=[.!?؟。؛])\s+|\n+", script) if item.strip()]
    if not clauses:
        clauses = [script.strip()]
    total_chars = sum(max(1, len(item)) for item in clauses)
    cursor = 0.0
    lines: list[str] = []
    for index, item in enumerate(clauses, 1):
        span = duration * max(1, len(item)) / total_chars
        end = min(duration, cursor + span)
        lines.append(f"{index}\n{_srt_time(cursor)} --> {_srt_time(end)}\n{item}\n")
        cursor = end
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


class RenderWorker:
    def __init__(self, settings: Settings, store: Store, telegram: TelegramAPI, director: Director):
        self.settings = settings
        self.store = store
        self.telegram = telegram
        self.director = director

    async def process_next(self) -> bool:
        job = self.store.claim_job()
        if job is None:
            return False
        try:
            if job["kind"] != "render":
                raise ValueError(f"Unsupported job kind: {job['kind']}")
            await self._render(job)
            self.store.finish_job(job["id"], True)
        except Exception as exc:
            LOG.exception("Render job %s failed", job["id"])
            self.store.finish_job(job["id"], False, str(exc))
            reel = self.store.get_reel(job["reel_id"])
            if reel:
                too_short_or_long_voice = isinstance(exc, MediaError) and "Narration is" in str(exc)
                next_status = "awaiting_voice" if too_short_or_long_voice else "needs_operator"
                self.store.update_reel(reel["id"], status=next_status)
                try:
                    await self.telegram.send_message(
                        int(reel["chat_id"]),
                        ("مدت ویس برای ریل یک‌دقیقه‌ای مناسب نیست. لطفاً همان متن را دوباره با گفتار ۴۵ تا ۷۵ ثانیه‌ای بخوانید."
                         if too_short_or_long_voice else
                         "ساخت خودکار این ریل به بررسی نیاز دارد. درخواست ثبت شد و نتیجه به شما اطلاع داده می‌شود."),
                    )
                    if self.settings.operator_chat_id and not too_short_or_long_voice:
                        await self.telegram.send_message(
                            self.settings.operator_chat_id,
                            f"نیاز به بررسی ریل #{reel['id']} (زبان {reel['language']}): {type(exc).__name__}: {exc}",
                        )
                except Exception:
                    LOG.exception("Could not notify about failed reel %s", reel["id"])
        return True

    async def _render(self, job: dict) -> None:
        reel = self.store.get_reel(job["reel_id"])
        if not reel or reel["status"] != "queued":
            raise ValueError("Reel is not queued")
        if not self.store.approval_valid(reel["id"], reel["plan_hash"]):
            raise ValueError("Current creative plan is not approved")
        plan = json.loads(reel["plan_json"])
        if plan.get("presence") != "none":
            # The existing subscription's OAuth/tool map has not been proved on
            # this host. Never silently replace an approved presenter shot.
            raise MediaError("Presenter shot needs a verified generation or source-video workflow")
        source_assets = self.store.list_assets(reel["id"])
        planned_indices = [shot["asset_index"] for shot in plan.get("shotlist", [])
                           if isinstance(shot, dict) and isinstance(shot.get("asset_index"), int)]
        if planned_indices:
            assets = [Path(source_assets[index]["path"]) for index in planned_indices
                      if 0 <= index < len(source_assets) and source_assets[index]["kind"] in {"photo", "video"}]
        else:
            assets = [Path(asset["path"]) for asset in source_assets
                      if asset["kind"] in {"photo", "video"}]
        if not assets:
            raise MediaError("No visual source assets were provided")
        if reel["language"] == "en":
            if not self.store.reserve_spend(reel["id"], "openai", 0.08, self.settings.monthly_openai_limit_usd):
                raise ValueError("OpenAI monthly budget exhausted before English narration")
            narration = self.settings.data_dir / "audio" / str(reel["id"]) / "narration.mp3"
            await self.director.synthesize_english(reel["script"], str(narration))
        else:
            if not reel["voice_path"]:
                raise ValueError("Approved owner voice is missing")
            narration = Path(reel["voice_path"])
        audio_info = await asyncio.to_thread(inspect_media, narration)
        duration = audio_info.duration_seconds or 0.0
        if duration < 45.0 or duration > 75.0:
            raise MediaError(
                f"Narration is {duration:.1f}s; an approved one-minute reel needs 45–75s of speech"
            )
        subtitle_path = self.settings.data_dir / "work" / str(reel["id"]) / "captions.srt"
        make_subtitles(reel["script"], duration, subtitle_path)
        output = self.settings.data_dir / "final" / str(reel["id"]) / "reel.mp4"
        result = await asyncio.to_thread(
            render_reel,
            RenderRequest(assets=assets, narration=narration, output=output,
                          subtitles=subtitle_path, target_seconds=duration,
                          video_bitrate="4M"),
        )
        cover = await asyncio.to_thread(make_cover, result.path, result.path.with_name("cover.jpg"))
        result.path.with_name("caption.txt").write_text(plan.get("caption", ""), encoding="utf-8")
        # A plan can be edited while the render is in flight. Reject stale work.
        current = self.store.get_reel(reel["id"])
        if not current or not self.store.approval_valid(reel["id"], reel["plan_hash"]):
            raise ValueError("Plan changed while render was running")
        updated = self.store.update_reel(
            reel["id"], final_path=str(result.path), final_hash=result.sha256,
            status="awaiting_final_approval",
        )
        await self.telegram.send_video(
            int(updated["chat_id"]), result.path,
            caption="پیش‌نمایش ریل آماده است. لطفاً همین نسخه را بررسی کنید.",
            buttons=[[("تأیید تحویل", f"deliver:{reel['id']}:{result.sha256[:12]}")],
                     [("اصلاح ویدیو", f"revise_video:{reel['id']}")]],
        )


async def run_worker(worker: RenderWorker) -> None:
    interrupted = worker.store.recover_interrupted_jobs()
    for reel_id in interrupted:
        reel = worker.store.get_reel(reel_id)
        if not reel:
            continue
        try:
            await worker.telegram.send_message(
                int(reel["chat_id"]), "ساخت ریل هنگام خاموشی سیستم متوقف شد؛ برای جلوگیری از هزینهٔ تکراری به صف بررسی رفت."
            )
            if worker.settings.operator_chat_id:
                await worker.telegram.send_message(
                    worker.settings.operator_chat_id,
                    f"Job ریل #{reel_id} پس از restart نیمه‌کاره مانده؛ با reelbot-admin retry {reel_id} دوباره اجرا کنید.",
                )
        except Exception:
            LOG.exception("Could not notify about interrupted reel %s", reel_id)
    while True:
        try:
            worked = await worker.process_next()
            if not worked:
                await asyncio.sleep(2)
        except Exception:
            LOG.exception("Render worker loop failed")
            await asyncio.sleep(5)
