from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import logging
import uuid
from pathlib import Path
from typing import Any

from .ai import Director
from .config import Settings
from .store import Store
from .telegram import TelegramAPI
from .upload import make_upload_link
from .worker import RenderWorker, run_worker

LOG = logging.getLogger(__name__)
LANGUAGES = {"fa": "فارسی", "ar": "عربی", "en": "انگلیسی"}
CHARACTERS = {"none": "بدون کاراکتر", "owner": "چهرهٔ کارفرما", "fictional": "شخصیت ساختگی"}
PRESENCE = {"none": "بدون حضور چهره", "cameo": "حضور کوتاه", "intermittent": "چند بخش", "throughout": "در سراسر ویدیو"}
LOG = logging.getLogger(__name__)


def plan_hash(plan: dict[str, Any]) -> str:
    raw = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def plan_card(plan: dict[str, Any]) -> str:
    shots = plan.get("shotlist") or []
    if isinstance(shots, list):
        shot_text = "، ".join(
            shot.get("description", "") if isinstance(shot, dict) else str(shot)
            for shot in shots[:5]
        )
    else:
        shot_text = str(shots)
    return (
        "طرح پیشنهادی ریل 🎬\n\n"
        f"زبان: {LANGUAGES.get(plan.get('language'), plan.get('language', '—'))}\n"
        f"ایده: {plan.get('idea', '—')}\n"
        f"هوک: {plan.get('hook', '—')}\n"
        f"داستان: {plan.get('story', '—')}\n"
        f"متن گفتار:\n{plan.get('script', '—')}\n\n"
        f"نماها: {shot_text or '—'}\n"
        f"شخصیت: {CHARACTERS.get(plan.get('character'), plan.get('character', '—'))}\n"
        f"میزان حضور: {PRESENCE.get(plan.get('presence'), plan.get('presence', '—'))}\n"
        f"کپشن: {plan.get('caption', '—')}\n\n"
        "اگر این طرح را تأیید کنید، ساخت ویدیوی همین نسخه آغاز می‌شود."
    )


class ReelBot:
    def __init__(self, settings: Settings, store: Store, telegram: TelegramAPI, director: Director):
        self.settings = settings
        self.store = store
        self.telegram = telegram
        self.director = director

    def _brand_profile(self) -> dict[str, Any]:
        path = self.settings.brand_profile_path
        if path is None or not path.is_file():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Brand profile must be a JSON object")
        return value

    def _enqueue_render(self, reel: dict[str, Any]) -> None:
        key = f"render:{reel['id']}:{reel['plan_hash']}:voice:{reel['voice_version']}"
        self.store.enqueue_job(reel["id"], "render", key, {})

    def _allowed(self, chat_id: int) -> bool:
        return chat_id in self.settings.allowed_chat_ids

    async def handle_update(self, update: dict[str, Any]) -> None:
        update_id = int(update["update_id"])
        if not self.store.begin_update(update_id, update):
            return
        await self._dispatch_update(update)
        self.store.complete_update(update_id)

    async def _dispatch_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if callback:
            chat_id = int(callback["message"]["chat"]["id"])
            if not self._allowed(chat_id):
                await self.telegram.answer_callback(callback["id"], "دسترسی ندارید")
                return
            await self._handle_callback(chat_id, callback)
            return
        message = update.get("message")
        if not message:
            return
        chat_id = int(message["chat"]["id"])
        if (message.get("text") or "").strip() == "/whoami":
            await self.telegram.send_message(chat_id, f"شناسهٔ عددی این گفت‌وگو: {chat_id}")
            return
        if not self._allowed(chat_id):
            if (message.get("text") or "").strip() == "/start":
                await self.telegram.send_message(chat_id, f"برای فعال‌سازی خصوصی بات، شناسهٔ {chat_id} را در ALLOWED_TELEGRAM_CHAT_IDS فایل .env وارد کنید و بات را دوباره اجرا کنید.")
            return
        await self._handle_message(chat_id, message)

    async def _handle_message(self, chat_id: int, message: dict[str, Any]) -> None:
        text = (message.get("text") or message.get("caption") or "").strip()
        if text in {"/start", "/help"}:
            await self.telegram.send_message(
                chat_id,
                "سلام! عکس‌ها، ویدیوها و توضیح پروژه را بفرستید. برای شروع ریل تازه «/new» را بزنید. "
                "وقتی فایل‌ها تمام شد، دکمهٔ «فایل‌ها تمام شد» را انتخاب کنید.",
                [[("ویدیوی جدید", "new")], [("وضعیت", "status")]],
            )
            return
        if text == "/new":
            await self._new_reel(chat_id)
            return
        if text == "/status":
            await self._status(chat_id)
            return
        reel = self.store.get_active_reel(chat_id)
        if reel is None:
            await self.telegram.send_message(chat_id, "برای شروع، «/new» را بزنید.")
            return
        if reel["status"] == "awaiting_voice" and (message.get("voice") or message.get("audio")):
            await self._receive_voice(reel, message)
            return
        if reel["status"] == "awaiting_feedback" and text:
            await self._make_plan(reel, feedback=text)
            return
        if reel["status"] == "awaiting_feedback" and (message.get("voice") or message.get("audio")):
            item = message.get("voice") or message.get("audio")
            if int(item.get("file_size", 0)) > 20_000_000:
                await self.telegram.send_message(chat_id, "این ویس بزرگ است؛ لطفاً کوتاه‌تر بفرستید.")
                return
            if not self.store.reserve_spend(reel["id"], "openai", 0.02, self.settings.monthly_openai_limit_usd):
                self.store.update_reel(reel["id"], status="needs_operator")
                await self.telegram.send_message(chat_id, "سقف هزینهٔ پردازش پر شده است؛ درخواست برای بررسی ثبت شد.")
                return
            destination = self.settings.data_dir / "feedback" / str(reel["id"]) / f"{uuid.uuid4().hex}.ogg"
            await self.telegram.download(item["file_id"], destination)
            feedback = await self.director.transcribe_voice(str(destination))
            await self._make_plan(reel, feedback=feedback)
            return
        if reel["status"] == "awaiting_clarification" and text:
            updated = self.store.update_reel(reel["id"], brief=(reel.get("brief") or "") + "\nپاسخ کارفرما: " + text, status="collecting")
            await self._make_plan(updated)
            return
        if reel["status"] == "awaiting_video_feedback" and text:
            self.store.update_reel(reel["id"], status="needs_operator", video_feedback=text)
            await self.telegram.send_message(chat_id, "درخواست اصلاح ثبت شد و برای آماده‌سازی نسخهٔ تازه بررسی می‌شود.")
            if self.settings.operator_chat_id:
                await self.telegram.send_message(
                    self.settings.operator_chat_id,
                    f"اصلاح ریل #{reel['id']} ({reel['language']}): {text}\nطرح: {reel.get('plan_hash')}\nفایل‌های پروژه: {self.settings.data_dir / 'raw' / str(reel['id'])}",
                )
            return
        if reel["status"] != "collecting":
            await self.telegram.send_message(chat_id, "درخواست فعلی در حال بررسی است. برای ریل جداگانه «/new» را بزنید.")
            return
        attachment = self._attachment(message)
        if attachment:
            kind, file_id, size, suffix = attachment
            if size and size > 20_000_000:
                await self.telegram.send_message(
                    chat_id,
                    "این فایل برای دریافت مستقیم بات بزرگ است. لینک آپلود خصوصی پروژه را از دکمهٔ «آپلود فایل بزرگ» بگیرید.",
                    [[("آپلود فایل بزرگ", f"upload:{reel['id']}")]],
                )
                return
            destination = self.settings.data_dir / "raw" / str(reel["id"]) / f"{uuid.uuid4().hex}{suffix}"
            await self.telegram.download(file_id, destination)
            self.store.add_asset(reel["id"], kind, str(destination), file_id)
        if text:
            brief = "\n".join(filter(None, [reel.get("brief") or "", text]))
            self.store.update_reel(reel["id"], brief=brief)
        if attachment or text:
            await self.telegram.send_message(
                chat_id,
                "دریافت شد. فایل یا توضیح دیگری دارید بفرستید؛ وقتی تمام شد ادامه دهید.",
                [[("فایل‌ها تمام شد", f"finish:{reel['id']}")]],
            )

    @staticmethod
    def _attachment(message: dict[str, Any]) -> tuple[str, str, int, str] | None:
        for kind in ("video", "voice", "audio", "document"):
            if kind in message:
                item = message[kind]
                if kind == "document":
                    suffix = Path(item.get("file_name") or "").suffix.lower()
                    image_types = {".jpg", ".jpeg", ".png", ".webp"}
                    video_types = {".mp4", ".mov", ".m4v", ".webm"}
                    audio_types = {".mp3", ".wav", ".m4a", ".ogg"}
                    if suffix in image_types:
                        return "photo", item["file_id"], int(item.get("file_size", 0)), suffix
                    if suffix in video_types:
                        return "video", item["file_id"], int(item.get("file_size", 0)), suffix
                    if suffix in audio_types:
                        return "audio", item["file_id"], int(item.get("file_size", 0)), suffix
                    return None
                suffix = {"video": ".mp4", "voice": ".ogg", "audio": ".mp3"}[kind]
                return kind, item["file_id"], int(item.get("file_size", 0)), suffix
        photos = message.get("photo") or []
        if photos:
            item = photos[-1]
            return "photo", item["file_id"], int(item.get("file_size", 0)), ".jpg"
        return None

    async def _handle_callback(self, chat_id: int, callback: dict[str, Any]) -> None:
        data = callback.get("data", "")
        await self.telegram.answer_callback(callback["id"])
        if data == "new":
            await self._new_reel(chat_id)
            return
        if data == "status":
            await self._status(chat_id)
            return
        if data.startswith("upload:"):
            reel_id = int(data.split(":", 1)[1])
            reel = self.store.get_reel(reel_id)
            if not self._owns(reel, chat_id) or reel["status"] != "collecting":
                return
            if self.settings.public_base_url.startswith("https://") and self.settings.upload_secret:
                link = make_upload_link(self.settings, reel_id, chat_id)
                await self.telegram.send_message(chat_id, f"فایل بزرگ را از این لینک خصوصی آپلود کنید (اعتبار یک ساعت):\n{link}")
            else:
                await self.telegram.send_message(chat_id, "لینک آپلود مستقیم پس از تنظیم آدرس عمومی هاست فعال می‌شود. فعلاً فایل‌های زیر ۲۰ مگابایت را بفرستید.")
            return
        parts = data.split(":")
        action = parts[0]
        if action == "lang" and len(parts) == 3:
            reel = self.store.get_reel(int(parts[1]))
            if self._owns(reel, chat_id) and reel["status"] == "collecting":
                self.store.update_reel(reel["id"], language=parts[2])
                await self.telegram.send_message(chat_id, "عالی. حالا فایل‌ها و توضیح پروژه را بفرستید.", [[("فایل‌ها تمام شد", f"finish:{reel['id']}")]])
            return
        if len(parts) < 2 or not parts[1].isdigit():
            return
        reel = self.store.get_reel(int(parts[1]))
        if not self._owns(reel, chat_id):
            return
        if action == "finish" and reel["status"] == "collecting":
            if not any(asset["kind"] in {"photo", "video"} for asset in self.store.list_assets(reel["id"])):
                await self.telegram.send_message(chat_id, "برای ساخت ریل، لطفاً دست‌کم یک عکس یا ویدیوی پروژه را بفرستید.")
            else:
                await self._make_plan(reel)
        elif action == "approve" and len(parts) == 3:
            await self._approve(reel, parts[2])
        elif action == "edit" and reel["status"] == "awaiting_plan_approval":
            self.store.update_reel(reel["id"], status="awaiting_feedback")
            await self.telegram.send_message(chat_id, "چه چیزی را در طرح تغییر بدهم؟ ساده و آزاد بنویسید یا ویس بفرستید.")
        elif action == "different" and reel["status"] == "awaiting_plan_approval":
            await self._make_plan(reel, feedback="ایده و هوک دیگری با زاویهٔ کاملاً متفاوت پیشنهاد بده.")
        elif action == "cancel":
            self.store.update_reel(reel["id"], status="cancelled")
            await self.telegram.send_message(chat_id, "این درخواست لغو شد. برای ریل تازه «/new» را بزنید.")
        elif action == "deliver" and len(parts) == 3:
            if reel["status"] == "awaiting_final_approval" and reel.get("final_hash", "").startswith(parts[2]):
                final_path = Path(reel["final_path"])
                cover = final_path.with_name("cover.jpg")
                if cover.is_file():
                    await self.telegram.send_photo(chat_id, cover, caption="کاور همین نسخه")
                plan = json.loads(reel["plan_json"])
                await self.telegram.send_message(chat_id, "کپشن پیشنهادی:\n\n" + plan.get("caption", ""))
                self.store.update_reel(reel["id"], status="delivered")
                await self.telegram.send_message(chat_id, "ویدیوی نهایی تأیید شد ✅ فایل همین نسخه برای انتشار آماده است.")
            else:
                await self.telegram.send_message(chat_id, "این تأیید مربوط به نسخهٔ قدیمی است؛ پیش‌نمایش جدید را بررسی کنید.")
        elif action == "revise_video" and reel["status"] == "awaiting_final_approval":
            self.store.update_reel(reel["id"], status="awaiting_video_feedback")
            await self.telegram.send_message(chat_id, "چه تغییری در ویدیو می‌خواهید؟ اگر مربوط به لحظه‌ای مشخص است، زمان آن را هم بنویسید.")

    def _owns(self, reel: dict | None, chat_id: int) -> bool:
        return bool(reel and int(reel["chat_id"]) == chat_id)

    async def _new_reel(self, chat_id: int) -> None:
        reel = self.store.create_reel(chat_id, "fa")
        await self.telegram.send_message(
            chat_id,
            "زبان این ریل را انتخاب کنید. بعد عکس‌ها، ویدیوها و توضیح پروژه را بفرستید.",
            [[("فارسی", f"lang:{reel['id']}:fa"), ("عربی", f"lang:{reel['id']}:ar"), ("English", f"lang:{reel['id']}:en")]],
        )

    async def _status(self, chat_id: int) -> None:
        reel = self.store.get_active_reel(chat_id)
        if reel:
            await self.telegram.send_message(chat_id, f"درخواست #{reel['id']}: {reel['status']}")
        else:
            await self.telegram.send_message(chat_id, "درخواست فعالی ندارید. برای شروع «/new» را بزنید.")

    async def _make_plan(self, reel: dict, feedback: str | None = None) -> None:
        chat_id = int(reel["chat_id"])
        if not self.settings.openai_api_key or self.director is None:
            await self.telegram.send_message(
                chat_id,
                "فایل‌های پروژه ذخیره شدند. تولید خودکار سناریو پس از افزودن کلید OpenAI API فعال می‌شود؛ "
                "بعداً دوباره «فایل‌ها تمام شد» را بزنید.",
                [[("فایل‌ها تمام شد", f"finish:{reel['id']}")]],
            )
            return
        current_assets = self.store.list_assets(reel["id"])
        if not any(asset["kind"] in {"photo", "video"} for asset in current_assets):
            await self.telegram.send_message(chat_id, "برای ساخت ریل، لطفاً دست‌کم یک عکس یا ویدیوی پروژه را بفرستید.")
            return
        brief = reel.get("brief") or ""
        audio_assets = [asset for asset in current_assets
                        if asset["kind"] in {"voice", "audio"} and not asset.get("transcript")]
        for asset in audio_assets:
            if not self.store.reserve_spend(reel["id"], "openai", 0.02, self.settings.monthly_openai_limit_usd):
                self.store.update_reel(reel["id"], status="needs_operator")
                await self.telegram.send_message(chat_id, "سقف هزینهٔ پردازش پر شده است؛ ویس توضیحی برای بررسی ثبت شد.")
                return
            try:
                transcript = await self.director.transcribe_voice(asset["path"])
            except Exception:
                LOG.exception("Could not transcribe intake audio for reel %s", reel["id"])
                await self.telegram.send_message(chat_id, "ویس توضیحی دریافت شد اما پردازش آن ناموفق بود. لطفاً ویس کوتاه‌تر بفرستید یا توضیح را متنی بنویسید.")
                return
            self.store.set_asset_transcript(asset["id"], transcript)
            brief = "\n".join(filter(None, [brief, "توضیح صوتی کارفرما: " + transcript]))
        if brief != (reel.get("brief") or ""):
            reel = self.store.update_reel(reel["id"], brief=brief)
        if not self.store.reserve_spend(reel["id"], "openai", 0.08, self.settings.monthly_openai_limit_usd):
            await self.telegram.send_message(chat_id, "سقف هزینهٔ ماهانهٔ برنامه‌ریزی پر شده است؛ درخواست برای بررسی ثبت شد.")
            self.store.update_reel(reel["id"], status="needs_operator")
            return
        await self.telegram.send_message(chat_id, "دارم طرح ریل را آماده می‌کنم…")
        try:
            plan = await self.director.propose_plan(
                brief=brief,
                language=reel["language"],
                brand_profile=self._brand_profile(),
                assets=self.store.list_assets(reel["id"]),
                feedback=feedback,
            )
        except Exception:
            LOG.exception("Could not draft a plan for reel %s", reel["id"])
            previous_status = reel.get("status")
            restore_status = previous_status if previous_status in {
                "awaiting_feedback", "awaiting_plan_approval", "collecting", "awaiting_clarification"
            } else "collecting"
            self.store.update_reel(reel["id"], status=restore_status)
            if restore_status == "awaiting_feedback":
                text = "ساخت طرح از نظر فنی ناموفق شد. لطفاً درخواست اصلاح را دوباره بفرستید."
            elif restore_status == "awaiting_plan_approval":
                text = "ایدهٔ جایگزین آماده نشد. طرح قبلی هنوز قابل تأیید است یا دوباره «ایدهٔ دیگر» را بزنید."
            else:
                text = "ساخت طرح موقتاً ناموفق شد. فایل‌ها محفوظ‌اند؛ دوباره «فایل‌ها تمام شد» را بزنید."
            await self.telegram.send_message(chat_id, text)
            return
        if plan.get("clarifying_question"):
            self.store.update_reel(reel["id"], status="awaiting_clarification")
            await self.telegram.send_message(chat_id, plan["clarifying_question"])
            return
        # Each proposal is a distinct approval artifact, even if a model happens
        # to repeat the same wording after an "idea other" request.
        plan["_revision_id"] = uuid.uuid4().hex
        digest = plan_hash(plan)
        updated = self.store.update_reel(
            reel["id"], plan_json=json.dumps(plan, ensure_ascii=False), plan_hash=digest,
            script=plan["script"], status="awaiting_plan_approval",
        )
        version = updated["plan_version"]
        await self.telegram.send_message(
            chat_id, plan_card(plan),
            [[("تأیید طرح و ساخت", f"approve:{reel['id']}:{version}")],
             [("اصلاح طرح", f"edit:{reel['id']}"), ("ایدهٔ دیگر", f"different:{reel['id']}")],
             [("لغو درخواست", f"cancel:{reel['id']}")]],
        )

    async def _approve(self, reel: dict, version: str) -> None:
        chat_id = int(reel["chat_id"])
        if reel["status"] != "awaiting_plan_approval" or str(reel["plan_version"]) != version:
            await self.telegram.send_message(chat_id, "این دکمه برای طرح قدیمی است. آخرین طرح را تأیید کنید.")
            return
        self.store.set_approval(reel["id"], reel["plan_hash"])
        if reel["language"] in {"fa", "ar"}:
            if reel.get("voice_path"):
                queued = self.store.update_reel(reel["id"], status="queued")
                self._enqueue_render(queued)
                await self.telegram.send_message(chat_id, "متن ویس اصلاح‌شده تأیید شد ✅ ساخت ویدیو در صف قرار گرفت.")
            else:
                self.store.update_reel(reel["id"], status="awaiting_voice")
                await self.telegram.send_message(chat_id, "طرح تأیید شد ✅ لطفاً متن زیر را با صدای خودتان بخوانید و ویس بفرستید:\n\n" + reel["script"])
        else:
            queued = self.store.update_reel(reel["id"], status="queued")
            self._enqueue_render(queued)
            await self.telegram.send_message(chat_id, "طرح تأیید شد ✅ ساخت ویدیو در صف قرار گرفت.")

    async def _receive_voice(self, reel: dict, message: dict) -> None:
        chat_id = int(reel["chat_id"])
        item = message.get("voice") or message.get("audio")
        if int(item.get("file_size", 0)) > 20_000_000:
            await self.telegram.send_message(chat_id, "ویس بیش از حد بزرگ است؛ لطفاً آن را کوتاه‌تر بفرستید.")
            return
        if not self.store.reserve_spend(reel["id"], "openai", 0.02, self.settings.monthly_openai_limit_usd):
            self.store.update_reel(reel["id"], status="needs_operator")
            await self.telegram.send_message(chat_id, "ویس دریافت شد ولی سقف هزینهٔ پردازش پر است؛ درخواست برای بررسی ثبت شد.")
            return
        destination = self.settings.data_dir / "voice" / str(reel["id"]) / f"{uuid.uuid4().hex}.ogg"
        await self.telegram.download(item["file_id"], destination)
        try:
            transcript = await self.director.transcribe_voice(str(destination))
            matches = await self.director.voice_matches_script(reel["script"], transcript)
        except Exception:
            LOG.exception("Could not verify voice for reel %s", reel["id"])
            await self.telegram.send_message(chat_id, "ویس دریافت شد اما پردازش آن ناموفق بود. لطفاً دوباره ارسال کنید.")
            return
        if matches:
            queued = self.store.update_reel(reel["id"], voice_path=str(destination), status="queued")
            self._enqueue_render(queued)
            await self.telegram.send_message(chat_id, "ویس دریافت و با متن تأییدشده تطبیق داده شد ✅ ساخت ویدیو آغاز می‌شود.")
        else:
            plan = json.loads(reel["plan_json"])
            plan["script"] = transcript
            digest = plan_hash(plan)
            updated = self.store.update_reel(
                reel["id"], plan_json=json.dumps(plan, ensure_ascii=False), plan_hash=digest,
                script=transcript, voice_path=str(destination), status="awaiting_plan_approval",
            )
            await self.telegram.send_message(
                chat_id,
                "در ویس، متن تغییر کرده است. لطفاً همین نسخهٔ اصلاح‌شده را تأیید کنید:\n\n" + plan_card(plan),
                [[("تأیید طرح و ساخت", f"approve:{reel['id']}:{updated['plan_version']}")], [("اصلاح طرح", f"edit:{reel['id']}")]],
            )


async def run_polling(bot: ReelBot) -> None:
    offset: int | None = None
    for pending in bot.store.pending_updates():
        try:
            await bot._dispatch_update(pending["payload"])
            bot.store.complete_update(pending["update_id"])
        except Exception:
            LOG.exception("Failed to recover Telegram update %s", pending["update_id"])
    while True:
        try:
            updates = await bot.telegram.updates(offset)
            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    await bot.handle_update(update)
                except Exception:
                    LOG.exception("Failed to process Telegram update %s", update.get("update_id"))
        except Exception:
            LOG.exception("Telegram polling failed")
            await asyncio.sleep(5)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env()
    settings.validate()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.data_dir / "reelbot.sqlite3")
    telegram = TelegramAPI(settings.telegram_bot_token)
    director = Director(api_key=settings.openai_api_key) if settings.openai_api_key else None
    bot = ReelBot(settings, store, telegram, director)
    worker = RenderWorker(settings, store, telegram, director)

    async def start() -> None:
        try:
            await asyncio.gather(run_polling(bot), run_worker(worker))
        finally:
            await telegram.close()

    asyncio.run(start())


if __name__ == "__main__":
    main()
