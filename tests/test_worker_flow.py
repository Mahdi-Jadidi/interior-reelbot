import json

import pytest

from reelbot.config import Settings
from reelbot.store import Store
from reelbot.worker import RenderWorker, make_subtitles


class FakeTelegram:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text, buttons=None):
        self.messages.append(text)


@pytest.mark.asyncio
async def test_presenter_job_never_falls_back_to_different_video(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    reel = store.create_reel(3, "en")
    plan = {"presence": "throughout", "script": "hello"}
    reel = store.update_reel(reel["id"], plan_json=json.dumps(plan), plan_hash="hash", script="hello",
                             status="awaiting_plan_approval")
    store.set_approval(reel["id"], "hash")
    store.update_reel(reel["id"], status="queued")
    store.enqueue_job(reel["id"], "render", "render:1:hash", {})
    telegram = FakeTelegram()
    worker = RenderWorker(Settings("bot", "key", tmp_path, frozenset({3})), store, telegram, object())
    assert await worker.process_next() is True
    assert store.get_reel(reel["id"])["status"] == "needs_operator"
    assert store.get_reel(reel["id"])["final_path"] is None
    assert telegram.messages
    store.close()


def test_subtitles_cover_narration_without_changing_script(tmp_path):
    path = make_subtitles("جملهٔ اول. جملهٔ دوم.", 60, tmp_path / "captions.srt")
    content = path.read_text(encoding="utf-8")
    assert "جملهٔ اول" in content and "جملهٔ دوم" in content
    assert "00:01:00,000" in content
