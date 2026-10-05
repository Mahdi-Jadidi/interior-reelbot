import json
from pathlib import Path

import pytest

from reelbot.app import ReelBot
from reelbot.config import Settings
from reelbot.store import Store


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.callbacks = []

    async def send_message(self, chat_id, text, buttons=None):
        self.messages.append((chat_id, text, buttons))
        return {}

    async def answer_callback(self, callback_id, text=""):
        self.callbacks.append((callback_id, text))

    async def download(self, file_id, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake voice")
        return destination


class FakeDirector:
    transcript = "متن گفتار"
    matches = True

    async def propose_plan(self, *, brief, language, brand_profile, assets, feedback=None):
        return {
            "idea": "نمایش نتیجهٔ طراحی", "hook": "این اتاق را ببینید",
            "story": "شروع، جزئیات، نتیجه", "script": "متن گفتار",
            "shotlist": ["نمای باز", "جزئیات"], "character": "بدون کاراکتر",
            "presence": "none", "caption": "یک فضای تازه", "language": language,
            "source_facts": [], "clarifying_question": None,
            "estimated_higgsfield_credits": None,
        }

    async def transcribe_voice(self, path):
        return self.transcript

    async def voice_matches_script(self, script, transcript):
        return self.matches


def message(update_id, chat_id, text=None, voice=None, audio=None):
    payload = {"update_id": update_id, "message": {"chat": {"id": chat_id}}}
    if text is not None:
        payload["message"]["text"] = text
    if voice is not None:
        payload["message"]["voice"] = {"file_id": voice, "file_size": 100}
    if audio is not None:
        payload["message"]["audio"] = {"file_id": audio, "file_size": 100}
    return payload


def callback(update_id, chat_id, data):
    return {"update_id": update_id, "callback_query": {
        "id": f"cb-{update_id}", "data": data, "message": {"chat": {"id": chat_id}}
    }}


@pytest.fixture
def setup_bot(tmp_path):
    settings = Settings("token", "key", tmp_path, frozenset({123}))
    store = Store(tmp_path / "state.db")
    telegram = FakeTelegram()
    director = FakeDirector()
    bot = ReelBot(settings, store, telegram, director)
    yield bot, store, telegram, director
    store.close()


@pytest.mark.asyncio
async def test_english_plan_requires_current_explicit_approval(setup_bot):
    bot, store, telegram, _ = setup_bot
    await bot.handle_update(message(1, 123, "/new"))
    reel_id = store.get_active_reel(123)["id"]
    await bot.handle_update(callback(2, 123, f"lang:{reel_id}:en"))
    await bot.handle_update(message(3, 123, "پروژهٔ اتاق نشیمن"))
    store.add_asset(reel_id, "photo", "sample.jpg")
    await bot.handle_update(callback(4, 123, f"finish:{reel_id}"))
    reel = store.get_reel(reel_id)
    assert reel["status"] == "awaiting_plan_approval"
    assert store.claim_job() is None
    await bot.handle_update(callback(5, 123, f"approve:{reel_id}:0"))
    assert store.claim_job() is None
    await bot.handle_update(callback(6, 123, f"approve:{reel_id}:{reel['plan_version']}"))
    assert store.approval_valid(reel_id, reel["plan_hash"])
    assert store.claim_job()["kind"] == "render"
    await bot.handle_update(callback(6, 123, f"approve:{reel_id}:{reel['plan_version']}"))
    assert store.claim_job() is None


@pytest.mark.asyncio
async def test_persian_voice_waits_for_operator_until_mia_has_asr(setup_bot):
    bot, store, telegram, director = setup_bot
    await bot.handle_update(message(10, 123, "/new"))
    reel_id = store.get_active_reel(123)["id"]
    await bot.handle_update(message(11, 123, "پروژهٔ آشپزخانه"))
    store.add_asset(reel_id, "photo", "sample.jpg")
    await bot.handle_update(callback(12, 123, f"finish:{reel_id}"))
    reel = store.get_reel(reel_id)
    await bot.handle_update(callback(13, 123, f"approve:{reel_id}:{reel['plan_version']}"))
    assert store.get_reel(reel_id)["status"] == "awaiting_voice"
    assert store.claim_job() is None
    await bot.handle_update(message(14, 123, voice="voice-1"))
    assert store.get_reel(reel_id)["status"] == "needs_operator"
    assert store.get_reel(reel_id)["voice_path"]
    assert store.claim_job() is None


@pytest.mark.asyncio
async def test_voice_is_not_auto_approved_without_asr(setup_bot):
    bot, store, telegram, director = setup_bot
    director.matches = False
    director.transcript = "متن گفتار تغییر کرده"
    await bot.handle_update(message(20, 123, "/new"))
    reel_id = store.get_active_reel(123)["id"]
    await bot.handle_update(message(21, 123, "پروژهٔ نشیمن"))
    store.add_asset(reel_id, "photo", "sample.jpg")
    await bot.handle_update(callback(22, 123, f"finish:{reel_id}"))
    before = store.get_reel(reel_id)
    await bot.handle_update(callback(23, 123, f"approve:{reel_id}:{before['plan_version']}"))
    await bot.handle_update(message(24, 123, voice="voice-2"))
    after = store.get_reel(reel_id)
    assert after["status"] == "needs_operator"
    assert after["plan_version"] == before["plan_version"]
    assert store.approval_valid(reel_id, before["plan_hash"])
    assert store.claim_job() is None


@pytest.mark.asyncio
async def test_rejects_unknown_chat(setup_bot):
    bot, store, telegram, _ = setup_bot
    await bot.handle_update(message(30, 999, "/new"))
    assert store.list_reels(999) == []
    assert telegram.messages == []


@pytest.mark.asyncio
async def test_intake_voice_is_kept_but_mia_router_text_api_does_not_transcribe(setup_bot):
    bot, store, telegram, director = setup_bot
    await bot.handle_update(message(40, 123, "/new"))
    reel_id = store.get_active_reel(123)["id"]
    await bot.handle_update(message(41, 123, "پروژهٔ ورودی خانه"))
    store.add_asset(reel_id, "photo", "sample.jpg")
    await bot.handle_update(message(42, 123, audio="brief-audio"))
    await bot.handle_update(callback(43, 123, f"finish:{reel_id}"))
    reel = store.get_reel(reel_id)
    assert reel["brief"] == "پروژهٔ ورودی خانه"
    assert store.list_assets(reel_id)[1]["transcript"] is None
    assert any("گفتاربه‌متن ندارد" in item[1] for item in telegram.messages)


@pytest.mark.asyncio
async def test_video_feedback_preserves_approved_plan_for_operator_revision(setup_bot):
    bot, store, telegram, _ = setup_bot
    await bot.handle_update(message(50, 123, "/new"))
    reel_id = store.get_active_reel(123)["id"]
    await bot.handle_update(message(51, 123, "پروژه"))
    store.add_asset(reel_id, "photo", "sample.jpg")
    await bot.handle_update(callback(52, 123, f"finish:{reel_id}"))
    reel = store.get_reel(reel_id)
    await bot.handle_update(callback(53, 123, f"approve:{reel_id}:{reel['plan_version']}"))
    # Simulate the preview being ready, then ask for an edit.
    store.update_reel(reel_id, status="awaiting_final_approval", final_path="final.mp4", final_hash="abc123")
    await bot.handle_update(callback(54, 123, f"revise_video:{reel_id}"))
    await bot.handle_update(message(55, 123, "زیرنویس را بزرگ‌تر کن"))
    updated = store.get_reel(reel_id)
    assert updated["status"] == "needs_operator"
    assert updated["video_feedback"] == "زیرنویس را بزرگ‌تر کن"
    assert store.approval_valid(reel_id, reel["plan_hash"])
