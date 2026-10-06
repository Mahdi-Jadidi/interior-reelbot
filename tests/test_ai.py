import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reelbot.ai import AIDirector


class FakeResponses:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        value = self.values.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=value if isinstance(value, str) else json.dumps(value)))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        )


class FakeSpeech:
    async def write_to_file(self, path):
        Path(path).write_bytes(b"fake mp3")


class FakeClient:
    def __init__(self, values):
        self.chat = SimpleNamespace(completions=FakeResponses(values))
        self.audio = SimpleNamespace(
            transcriptions=SimpleNamespace(create=self.transcribe),
            speech=SimpleNamespace(create=self.speech),
        )
        self.transcription_calls = []
        self.speech_calls = []

    async def transcribe(self, **kwargs):
        self.transcription_calls.append(kwargs)
        return SimpleNamespace(text="  سلام  ")

    async def speech(self, **kwargs):
        self.speech_calls.append(kwargs)
        return FakeSpeech()


def plan(**overrides):
    value = {
        "idea": "تحول فضای کوچک", "hook": "این گوشه را ببینید", "story": "شروع، جزئیات، پایان",
        "script": " ".join(["این فضا آرام‌تر می‌شود", "نور پنجره به جزئیات جان می‌دهد", "هر گوشه فرصتی برای آسایش است", "طراحی خوب زندگی روزمره را دلنشین‌تر می‌کند"] * 4), "shotlist": [
            {"asset_index": 0, "description": "نمای ورودی"},
            {"asset_index": 0, "description": "نمای جزئیات"},
        ],
        "considered_hooks": ["این گوشه را ببینید", "هوک دوم", "هوک سوم"],
        "selection_reason": "بهترین تطبیق با تصاویر موجود",
        "character": "none", "presence": "none", "caption": "جزئیات خانه",
        "language": "fa", "source_facts": ["آشپزخانه کوچک"], "clarifying_question": None,
        "claim_support": [],
    }
    value.update(overrides)
    return value


class DirectorTests(unittest.TestCase):
    def test_structured_result_accepts_fenced_json_only(self):
        director = AIDirector("test", client=FakeClient(['Here is the result:\n```json\n{"category":"question"}\n```']))
        result = asyncio.run(director._structured(
            "test", "test-model", "Return JSON", {}, {"type": "object"}
        ))
        self.assertEqual(result, {"category": "question"})

    def test_structured_result_accepts_python_quoted_mapping_from_gateway(self):
        director = AIDirector("test", client=FakeClient(["{'category': 'question'}"]))
        result = asyncio.run(director._structured(
            "test", "test-model", "Return JSON", {}, {"type": "object"}
        ))
        self.assertEqual(result, {"category": "question"})

    def test_structured_gateway_timeout_is_bounded_and_actionable(self):
        class SlowResponses(FakeResponses):
            async def create(self, **kwargs):
                await asyncio.sleep(0.05)

        client = FakeClient([])
        client.chat.completions = SlowResponses([])
        director = AIDirector("test", client=client, request_timeout_seconds=0.005)
        with self.assertRaisesRegex(TimeoutError, "exceeded 0.005s"):
            asyncio.run(director._structured(
                "test", "test-model", "Return JSON", {}, {"type": "object"}
            ))

    def test_client_uses_miarouter_chat_endpoint(self):
        with patch("reelbot.ai.AsyncOpenAI") as client_factory:
            AIDirector("masked", base_url="https://miarouter.online/v1")
        client_factory.assert_called_once_with(
            api_key="masked", base_url="https://miarouter.online/v1"
        )

    def test_structured_plan_and_usage(self):
        client = FakeClient([plan()])
        director = AIDirector("test", client=client)
        result = asyncio.run(director.propose_plan(
            "آشپزخانه کوچک", "fa", {}, [{"kind": "photo", "path": "missing.jpg"}]
        ))
        self.assertIsNone(result["estimated_higgsfield_credits"])
        self.assertEqual(result["source_facts"], ["آشپزخانه کوچک"])
        self.assertTrue(client.chat.completions.calls[0]["response_format"]["json_schema"]["strict"])
        self.assertEqual(director.total_cost_usd, 0.25)
        self.assertEqual(director.usage_events[0]["estimated_cost_usd"], 0.25)
        self.assertGreater(director.usage_events[0]["input_tokens"], 0)

    def test_rejects_unsupported_fact_and_wrong_language(self):
        for fake in [
            plan(source_facts=["متریال مرمر ایتالیایی"]),
            plan(language="en"),
            plan(claim_support=[{"claim": "این فضا آرام‌تر می‌شود", "evidence": "سنگ مرمر ایتالیایی"}]),
            plan(script="خیلی کوتاه"),
        ]:
            director = AIDirector("test", client=FakeClient([fake]))
            with self.assertRaises(ValueError):
                asyncio.run(director.propose_plan(
                    "آشپزخانه کوچک", "fa", {}, [{"kind": "photo", "path": "missing.jpg"}]
                ))

    def test_classify_voice_match_and_unavailable_speech_endpoints(self):
        client = FakeClient([{"category": "feedback"}, {"matches": False}])
        director = AIDirector("test", client=client)
        self.assertEqual(asyncio.run(director.classify_message("هوک را عوض کن")), "feedback")
        self.assertFalse(asyncio.run(director.voice_matches_script("چوب گردو", "چوب بلوط")))
        self.assertTrue(asyncio.run(director.voice_matches_script("سلام، دنیا!", "سلام دنیا")))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "voice.wav"
            source.write_bytes(b"fake wav")
            output = Path(directory) / "speech.mp3"
            with self.assertRaises(NotImplementedError):
                asyncio.run(director.transcribe_voice(str(source)))
            with self.assertRaises(NotImplementedError):
                asyncio.run(director.synthesize_english("Welcome home", str(output)))
        self.assertEqual(client.speech_calls, [])
        self.assertEqual({event["purpose"] for event in director.usage_events}, {
            "message_classification", "voice_script_match"
        })

    def test_empty_message_does_not_spend_and_bad_inputs(self):
        client = FakeClient([])
        director = AIDirector("test", client=client)
        self.assertEqual(asyncio.run(director.classify_message("  ")), "other")
        self.assertEqual(director.usage_events, [])
        with self.assertRaises(ValueError):
            asyncio.run(director.propose_plan("x", "de", {}, []))
        with self.assertRaises(NotImplementedError):
            asyncio.run(director.synthesize_english("hello", "speech.wav"))

    def test_visual_analysis_is_bounded_and_strips_private_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / f"private-project-{index}.mp4" for index in range(6)]
            for path in paths:
                path.write_bytes(b"raw video must not be uploaded")
            assets = [{"kind": "video", "path": str(path), "file_id": "telegram-secret"} for path in paths]
            client = FakeClient([plan(source_facts=["آشپزخانه کوچک"], shotlist=[
                {"asset_index": 0, "description": "نمای آشپزخانه"}
            ])])
            director = AIDirector("test", client=client)
            with patch("reelbot.ai._small_jpeg", return_value=b"bounded-jpeg") as preview:
                result = asyncio.run(director.propose_plan("آشپزخانه کوچک", "fa", {}, assets))
            self.assertEqual(preview.call_count, 4)
            self.assertEqual(result["idea"], "تحول فضای کوچک")
            self.assertEqual(len(client.chat.completions.calls), 1)
            vision_content = client.chat.completions.calls[0]["messages"][1]["content"]
            self.assertEqual(sum(item["type"] == "image_url" for item in vision_content), 4)
            self.assertTrue(all(item["image_url"]["detail"] == "low" for item in vision_content if item["type"] == "image_url"))
            visible_payload = json.dumps(client.chat.completions.calls, ensure_ascii=False)
            self.assertNotIn("private-project", visible_payload)
            self.assertNotIn("telegram-secret", visible_payload)
            self.assertNotIn("raw video must not be uploaded", visible_payload)

    def test_rejects_malformed_or_unsupported_shotlists(self):
        invalid_plans = [
            plan(shotlist=["نمای ورودی"]),
            plan(shotlist=[{"asset_index": 8, "description": "نمای ورودی"}]),
            plan(shotlist=[{"asset_index": 0, "description": ""}]),
            plan(considered_hooks=["یک", "یک", "سه"]),
        ]
        for fake in invalid_plans:
            director = AIDirector("test", client=FakeClient([fake]))
            with self.assertRaises(ValueError):
                asyncio.run(director.propose_plan(
                    "آشپزخانه کوچک", "fa", {}, [{"kind": "photo", "path": "missing.jpg"}]
                ))

    def test_unavailable_visual_does_not_invent_description(self):
        director = AIDirector("test", client=FakeClient([]))
        result = asyncio.run(director.analyze_assets([{"kind": "photo", "path": "missing.jpg"}]))
        self.assertEqual(result[0]["analysis_status"], "unavailable")
        self.assertEqual(result[0]["description"], "")
