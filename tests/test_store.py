import tempfile
import unittest
import sqlite3
from pathlib import Path

from reelbot.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_reels_assets_and_active_selection(self):
        first = self.store.create_reel(123, "fa")
        second = self.store.create_reel(123, "ar")
        other = self.store.create_reel(456, "en")
        self.assertEqual([second["id"], first["id"]], [r["id"] for r in self.store.list_reels(123)])
        self.assertEqual(second["id"], self.store.get_active_reel(123)["id"])
        self.store.update_reel(second["id"], status="delivered")
        self.assertEqual(first["id"], self.store.get_active_reel(123)["id"])
        self.store.update_reel(first["id"], status="cancelled")
        self.assertIsNone(self.store.get_active_reel(123))
        self.assertEqual(other["id"], self.store.get_active_reel(456)["id"])
        asset = self.store.add_asset(other["id"], "video", "media/clip.mp4", "file-1")
        self.assertEqual("file-1", asset["telegram_file_id"])
        self.assertEqual([asset], self.store.list_assets(other["id"]))
        self.assertIsNone(self.store.get_reel(99999))

    def test_approval_bound_to_plan_version_and_voice_reset(self):
        reel = self.store.create_reel(123, "fa")
        rid = reel["id"]
        self.store.update_reel(rid, plan_json={"idea": "renovation"}, plan_hash="hash-a", script="first")
        with self.assertRaises(ValueError):
            self.store.set_approval(rid, "old-hash")
        approved = self.store.set_approval(rid, "hash-a")
        self.assertTrue(self.store.approval_valid(rid, "hash-a"))
        self.store.update_reel(rid, status="awaiting_voice")
        self.assertTrue(self.store.approval_valid(rid, "hash-a"))
        voiced = self.store.update_reel(rid, voice_path="voice.ogg")
        self.assertEqual(1, voiced["voice_version"])
        self.assertTrue(self.store.approval_valid(rid, "hash-a"))
        self.store.update_reel(rid, final_path="final.mp4", final_hash="video-hash")
        edited = self.store.update_reel(rid, script="revised")
        self.assertGreater(edited["plan_version"], approved["plan_version"])
        self.assertIsNone(edited["voice_path"])
        self.assertIsNone(edited["final_path"])
        self.assertFalse(self.store.approval_valid(rid, "hash-a"))
        self.assertIsNone(edited["plan_hash"])
        with self.assertRaises(ValueError):
            self.store.set_approval(rid, "hash-a")
        self.store.update_reel(rid, plan_hash="hash-b")
        self.store.set_approval(rid, "hash-b")
        self.assertTrue(self.store.approval_valid(rid, "hash-b"))

    def test_duplicate_updates_and_jobs(self):
        rid = self.store.create_reel(123, "en")["id"]
        self.assertTrue(self.store.record_update(800))
        self.assertFalse(self.store.record_update(800))
        queued = self.store.enqueue_job(rid, "render", "render:1", {"shots": [1, 2]})
        self.assertEqual(queued, self.store.enqueue_job(rid, "render", "render:1", {"shots": [1, 2]}))
        with self.assertRaises(ValueError):
            self.store.enqueue_job(rid, "render", "render:1", {"shots": [3]})
        claimed = self.store.claim_job()
        self.assertEqual(queued["id"], claimed["id"])
        self.assertEqual("running", claimed["status"])
        self.assertEqual(1, claimed["attempts"])
        self.assertIsNone(self.store.claim_job())
        self.store.finish_job(claimed["id"], True)
        with self.assertRaises(ValueError):
            self.store.finish_job(claimed["id"], True)
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(self.store.record_update(800))
        self.assertIsNone(self.store.claim_job())

    def test_pending_update_survives_restart_and_completes_once(self):
        payload = {"message": {"chat": {"id": 123}, "text": "پروژه جدید"}}
        self.assertTrue(self.store.begin_update(801, payload))
        self.assertFalse(self.store.begin_update(801, payload))
        with self.assertRaises(ValueError):
            self.store.begin_update(801, {"different": True})
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(
            [{"update_id": 801, "payload": payload, "received_at": self.store.pending_updates()[0]["received_at"]}],
            self.store.pending_updates(),
        )
        self.store.complete_update(801)
        self.assertEqual([], self.store.pending_updates())
        self.assertFalse(self.store.begin_update(801, payload))
        with self.assertRaises(ValueError):
            self.store.complete_update(801)

    def test_old_update_table_migrates_without_reprocessing(self):
        self.store.close()
        self.path.unlink()
        old = sqlite3.connect(self.path)
        old.execute("CREATE TABLE telegram_updates (update_id INTEGER PRIMARY KEY, received_at TEXT)")
        old.execute("INSERT INTO telegram_updates VALUES (77, '2020-01-01T00:00:00Z')")
        old.commit()
        old.close()
        self.store = Store(self.path)
        self.assertEqual([], self.store.pending_updates())
        self.assertFalse(self.store.begin_update(77, {"old": True}))

    def test_credit_reservation_requires_approval_and_is_idempotent(self):
        rid = self.store.create_reel(123, "en")["id"]
        self.store.update_reel(rid, plan_json={"idea": "one"}, plan_hash="p1")
        with self.assertRaises(ValueError):
            self.store.reserve_credits(rid, "p1", 30, 60)
        self.store.set_approval(rid, "p1")
        first = self.store.reserve_credits(rid, "p1", 30, 60)
        self.assertIsNotNone(first)
        self.assertEqual(30, first["credits"])
        self.assertEqual(first, self.store.reserve_credits(rid, "p1", 30, 60))
        self.assertTrue(self.store.verify_credit_reservation(rid, "p1", first["id"], 30))
        self.assertFalse(self.store.verify_credit_reservation(rid, "p1", first["id"], 31))
        with self.assertRaises(ValueError):
            self.store.reserve_credits(rid, "p1", 31, 60)
        second_reel = self.store.create_reel(123, "fa")["id"]
        self.store.update_reel(second_reel, plan_json={"idea": "two"}, plan_hash="p2")
        self.store.set_approval(second_reel, "p2")
        self.assertIsNone(self.store.reserve_credits(second_reel, "p2", 31, 60))
        self.assertIsNone(self.store.reserve_credits(second_reel, "p2", 1, 0))
        self.store.update_reel(rid, script="changed")
        self.assertFalse(self.store.verify_credit_reservation(rid, "p1", first["id"], 30))

    def test_spend_cap_persists_and_is_separate_by_category(self):
        rid = self.store.create_reel(123, "fa")["id"]
        self.assertTrue(self.store.reserve_spend(rid, "openai", 0.6, 1.0))
        self.assertFalse(self.store.reserve_spend(rid, "openai", 0.5, 1.0))
        self.assertTrue(self.store.reserve_spend(rid, "openai", 0.4, 1.0))
        self.assertEqual(1.0, self.store.total_spend("openai"))
        self.assertTrue(self.store.reserve_spend(rid, "higgsfield", 0.5, 0.5))
        self.assertEqual(0.5, self.store.total_spend("higgsfield"))
        self.store._conn.execute(
            "UPDATE spend_reservations SET created_at = '2020-01-01T00:00:00Z' "
            "WHERE category = 'openai'"
        )
        self.assertEqual(0.0, self.store.total_spend("openai"))
        self.assertTrue(self.store.reserve_spend(rid, "openai", 1.0, 1.0))
        with self.assertRaises(ValueError):
            self.store.reserve_spend(rid, "openai", -1, 1)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(1.0, self.store.total_spend("openai"))

    def test_interrupted_job_goes_to_exception_queue_without_replay(self):
        reel = self.store.create_reel(123, "en")
        self.store.update_reel(reel["id"], status="queued")
        job = self.store.enqueue_job(reel["id"], "render", "render:crash", {})
        claimed = self.store.claim_job()
        self.assertEqual(job["id"], claimed["id"])
        self.assertEqual([reel["id"]], self.store.recover_interrupted_jobs())
        self.assertEqual("needs_operator", self.store.get_reel(reel["id"])["status"])
        self.assertIsNone(self.store.claim_job())
        self.assertEqual("failed", self.store._conn.execute("SELECT status FROM jobs WHERE id=?", (job["id"],)).fetchone()["status"])


if __name__ == "__main__":
    unittest.main()
