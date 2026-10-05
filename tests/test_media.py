from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from reelbot.higgsfield import (GenerationAuthorization, GenerationDenied,
                                GenerationPlan, GenerationUnavailable, HiggsfieldMCP)
from reelbot.media import MediaError, MediaInfo, RenderRequest, build_render_command, plan_shots, sha256_file


class RenderCommandTests(unittest.TestCase):
    def test_voice_is_not_truncated_and_outputs_vertical(self):
        video = MediaInfo(Path("clip.mp4"), 4.0, True, True, 1920, 1080, "mp4")
        audio = MediaInfo(Path("voice.wav"), 68.0, False, True, None, None, "wav")
        command = build_render_command(RenderRequest([Path("clip.mp4")], Path("voice.wav"), Path("out.mp4")),
                                       asset_info=[video], narration_info=audio)
        joined = " ".join(command)
        self.assertIn("scale=1080:1920", joined)
        self.assertIn("scale=1040:1880:force_original_aspect_ratio=decrease", joined)
        self.assertIn("boxblur=24:1", joined)
        self.assertIn("atrim=duration=68.000000", joined)
        self.assertIn("-t 68.000000", joined)
        self.assertIn("-stream_loop -1", joined)
        self.assertIn("-maxrate 4M", joined)
        self.assertIn("-b:a 128k", joined)
        self.assertGreater(joined.count("overlay=x="), 1)

    def test_short_beats_and_still_motion(self):
        shots = plan_shots(60, 2)
        self.assertEqual(sum(shot.duration_seconds for shot in shots), 60)
        self.assertTrue(all(2.5 <= shot.duration_seconds <= 6.5 for shot in shots))
        self.assertEqual({shot.asset_index for shot in shots}, {0, 1})
        image = MediaInfo(Path("room.jpg"), None, True, False, 2000, 1300, "jpeg")
        voice = MediaInfo(Path("voice.wav"), 60, False, True, None, None, "wav")
        command = build_render_command(RenderRequest([Path("room.jpg")], Path("voice.wav"), Path("out.mp4")),
                                       asset_info=[image], narration_info=voice)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("10*sin(t*0.65)", graph)
        self.assertIn("force_original_aspect_ratio=decrease", graph)
        self.assertIn("concat=n=", graph)

    def test_rejects_missing_narration_stream(self):
        image = MediaInfo(Path("image.jpg"), None, True, False, 1200, 800, "jpeg")
        with self.assertRaises(MediaError):
            build_render_command(RenderRequest([Path("image.jpg")], Path("bad.mp3"), Path("out.mp4")),
                                 asset_info=[image], narration_info=image)

    def test_sha256(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "f"
            path.write_bytes(b"abc")
            self.assertEqual(sha256_file(path), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


class HiggsfieldSafetyTests(unittest.TestCase):
    def setUp(self):
        self.plan = GenerationPlan("reel-1", "verified-model", "verified-tool", {"prompt": "room"}, Decimal("30"))
        self.auth = GenerationAuthorization(self.plan.plan_hash, "reservation-1", Decimal("30"))

    def test_requires_exact_approval_and_reservation(self):
        adapter = HiggsfieldMCP(lambda *_: "job", verified_operations=frozenset({"verified-tool"}),
                                verify_reservation=lambda *_: True)
        with self.assertRaises(GenerationDenied):
            adapter.generate(self.plan, GenerationAuthorization("wrong", "reservation-1", Decimal("30")))
        with self.assertRaises(GenerationDenied):
            adapter.generate(self.plan, GenerationAuthorization(self.plan.plan_hash, "reservation-1", Decimal("29")))
        self.assertEqual(adapter.generate(self.plan, self.auth), "job")

    def test_persisted_reservation_must_be_verified(self):
        adapter = HiggsfieldMCP(lambda *_: "job", verified_operations=frozenset({"verified-tool"}),
                                verify_reservation=lambda *_: False)
        with self.assertRaises(GenerationDenied):
            adapter.generate(self.plan, self.auth)

    def test_no_unverified_credit_bearing_call(self):
        with self.assertRaises(GenerationUnavailable):
            HiggsfieldMCP().generate(self.plan, self.auth)


if __name__ == "__main__":
    unittest.main()
