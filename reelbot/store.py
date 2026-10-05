"""Durable, single-host state for the Telegram reel workflow.

Each Store call owns its transaction.  In particular, approvals, job claims and
spend reservations are serialized with ``BEGIN IMMEDIATE`` so duplicate Telegram
updates and parallel workers cannot start unapproved or over-budget work.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any


_REEL_FIELDS = {
    "chat_id",
    "language",
    "status",
    "brief",
    "plan_json",
    "plan_hash",
    "script",
    "voice_path",
    "final_path",
    "final_hash",
    "video_feedback",
}
_PLAN_FIELDS = {"language", "brief", "plan_json", "plan_hash", "script"}


class Store:
    """SQLite store; make one instance per process or worker.

    Monetary amounts are stored as integer microdollars to avoid floating-point
    ceiling errors; category ceilings reset by UTC calendar month.  ``plan_version``
    and ``voice_version`` are monotonic and
    managed by the store; callers must never set them directly.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                language TEXT NOT NULL CHECK(language IN ('fa', 'ar', 'en')),
                status TEXT NOT NULL DEFAULT 'collecting',
                brief TEXT,
                plan_json TEXT,
                plan_hash TEXT,
                plan_version INTEGER NOT NULL DEFAULT 0,
                script TEXT,
                voice_path TEXT,
                voice_version INTEGER NOT NULL DEFAULT 0,
                final_path TEXT,
                final_hash TEXT,
                video_feedback TEXT,
                approval_hash TEXT,
                approval_version INTEGER,
                approved_at TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_reels_chat ON reels(chat_id, id DESC);
            CREATE TABLE IF NOT EXISTS assets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reel_id INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                telegram_file_id TEXT,
                transcript TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_assets_reel ON assets(reel_id, id);
            CREATE TABLE IF NOT EXISTS telegram_updates (
                update_id INTEGER PRIMARY KEY,
                payload_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'complete',
                received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reel_id INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK(status IN ('queued','running','succeeded','failed')),
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                started_at TEXT,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, id);
            CREATE TABLE IF NOT EXISTS spend_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reel_id INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
                category TEXT NOT NULL,
                amount_microusd INTEGER NOT NULL CHECK(amount_microusd >= 0),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_spend_category ON spend_reservations(category);
            CREATE TABLE IF NOT EXISTS credit_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reel_id INTEGER NOT NULL REFERENCES reels(id) ON DELETE CASCADE,
                plan_hash TEXT NOT NULL,
                credits_milli INTEGER NOT NULL CHECK(credits_milli > 0),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                UNIQUE(reel_id, plan_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_credit_reservations_month
                ON credit_reservations(created_at);
            """
        )
        asset_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(assets)")}
        if "transcript" not in asset_columns:
            self._conn.execute("ALTER TABLE assets ADD COLUMN transcript TEXT")
        reel_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(reels)")}
        if "video_feedback" not in reel_columns:
            self._conn.execute("ALTER TABLE reels ADD COLUMN video_feedback TEXT")
        # Existing installations created before the durable inbox retain their
        # already-deduplicated update IDs, marked complete by default.
        update_columns = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(telegram_updates)")
        }
        if "payload_json" not in update_columns:
            self._conn.execute(
                "ALTER TABLE telegram_updates ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'"
            )
        if "status" not in update_columns:
            self._conn.execute(
                "ALTER TABLE telegram_updates ADD COLUMN status TEXT NOT NULL DEFAULT 'complete'"
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _reel(self, reel_id: int) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM reels WHERE id = ?", (reel_id,)).fetchone()
        if row is None:
            raise KeyError(f"reel {reel_id} does not exist")
        return dict(row)

    def create_reel(self, chat_id: int, language: str) -> dict[str, Any]:
        if language not in {"fa", "ar", "en"}:
            raise ValueError("language must be fa, ar or en")
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO reels(chat_id, language) VALUES (?, ?)", (chat_id, language)
            )
            return self._reel(cursor.lastrowid)

    def get_reel(self, reel_id: int) -> dict[str, Any] | None:
        return self._row(
            self._conn.execute("SELECT * FROM reels WHERE id = ?", (reel_id,)).fetchone()
        )

    def list_reels(self, chat_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT * FROM reels WHERE chat_id = ? ORDER BY id DESC", (chat_id,)
            )
        ]

    def get_active_reel(self, chat_id: int) -> dict[str, Any] | None:
        return self._row(
            self._conn.execute(
                "SELECT * FROM reels WHERE chat_id = ? "
                "AND status NOT IN ('delivered', 'cancelled') ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        )

    def update_reel(self, reel_id: int, **fields: Any) -> dict[str, Any]:
        unknown = set(fields) - _REEL_FIELDS
        if unknown:
            raise ValueError(f"unsupported reel fields: {', '.join(sorted(unknown))}")
        if not fields:
            return self._reel(reel_id)
        if "language" in fields and fields["language"] not in {"fa", "ar", "en"}:
            raise ValueError("language must be fa, ar or en")
        if "plan_json" in fields and not isinstance(fields["plan_json"], (str, type(None))):
            fields["plan_json"] = json.dumps(fields["plan_json"], ensure_ascii=False)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = self._reel(reel_id)
            plan_changed = any(
                key in fields and fields[key] != current[key] for key in _PLAN_FIELDS
            )
            plan_body_changed = any(
                key in fields and fields[key] != current[key]
                for key in _PLAN_FIELDS - {"plan_hash"}
            )
            voice_changed = "voice_path" in fields and fields["voice_path"] != current["voice_path"]
            changes = dict(fields)
            if plan_changed:
                if plan_body_changed and changes.get("plan_hash") == current["plan_hash"]:
                    # A hash that still identifies the old approval card is unsafe.
                    changes["plan_hash"] = None
                elif plan_body_changed and "plan_hash" not in changes:
                    changes["plan_hash"] = None
                changes["plan_version"] = current["plan_version"] + 1
                changes["approval_hash"] = None
                changes["approval_version"] = None
                changes["approved_at"] = None
                # A new plan cannot inherit a voice or final video from an old one.
                if "voice_path" not in changes:
                    changes["voice_path"] = None
                    voice_changed = current["voice_path"] is not None
                changes["final_path"] = None
                changes["final_hash"] = None
            if voice_changed:
                changes["voice_version"] = current["voice_version"] + 1
                changes["final_path"] = None
                changes["final_hash"] = None
            changes["updated_at"] = "__CURRENT_TIME__"
            assignments = ", ".join(
                f"{key} = " + ("strftime('%Y-%m-%dT%H:%M:%fZ','now')" if key == "updated_at" else "?")
                for key in changes
            )
            values = [value for key, value in changes.items() if key != "updated_at"]
            self._conn.execute(
                f"UPDATE reels SET {assignments} WHERE id = ?", (*values, reel_id)
            )
            result = self._reel(reel_id)
            self._conn.commit()
            return result
        except BaseException:
            self._conn.rollback()
            raise

    def add_asset(
        self, reel_id: int, kind: str, path: str, telegram_file_id: str | None = None
    ) -> dict[str, Any]:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO assets(reel_id, kind, path, telegram_file_id) VALUES (?, ?, ?, ?)",
                (reel_id, kind, path, telegram_file_id),
            )
            return dict(
                self._conn.execute("SELECT * FROM assets WHERE id = ?", (cursor.lastrowid,)).fetchone()
            )

    def list_assets(self, reel_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self._conn.execute(
                "SELECT * FROM assets WHERE reel_id = ? ORDER BY id", (reel_id,)
            )
        ]

    def set_asset_transcript(self, asset_id: int, transcript: str) -> None:
        if not transcript.strip():
            raise ValueError("transcript must be nonempty")
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE assets SET transcript = ? WHERE id = ?", (transcript, asset_id)
            )
            if cursor.rowcount != 1:
                raise KeyError(f"asset {asset_id} does not exist")

    def record_update(self, update_id: int) -> bool:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO telegram_updates(update_id) VALUES (?)", (update_id,)
            )
            return cursor.rowcount == 1

    def begin_update(self, update_id: int, payload: dict[str, Any]) -> bool:
        """Persist a Telegram update before processing it; return true if new.

        A process may die after this call.  On restart ``pending_updates``
        returns the original payload until ``complete_update`` is called.
        """
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO telegram_updates(update_id, payload_json, status) "
                "VALUES (?, ?, 'pending')",
                (update_id, encoded),
            )
            if cursor.rowcount == 0:
                original = self._conn.execute(
                    "SELECT payload_json, status FROM telegram_updates WHERE update_id = ?",
                    (update_id,),
                ).fetchone()
                if original["status"] == "pending" and original["payload_json"] != encoded:
                    raise ValueError("update ID already has a different payload")
            return cursor.rowcount == 1

    def pending_updates(self) -> list[dict[str, Any]]:
        return [
            {
                "update_id": row["update_id"],
                "payload": json.loads(row["payload_json"]),
                "received_at": row["received_at"],
            }
            for row in self._conn.execute(
                "SELECT update_id, payload_json, received_at FROM telegram_updates "
                "WHERE status = 'pending' ORDER BY update_id"
            )
        ]

    def complete_update(self, update_id: int) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE telegram_updates SET status = 'complete' "
                "WHERE update_id = ? AND status = 'pending'",
                (update_id,),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"update {update_id} is not pending")

    @staticmethod
    def _job(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def enqueue_job(
        self, reel_id: int, kind: str, idempotency_key: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO jobs(reel_id, kind, idempotency_key, payload_json) "
                "VALUES (?, ?, ?, ?)",
                (reel_id, kind, idempotency_key, encoded),
            )
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            assert row is not None
            if row["reel_id"] != reel_id or row["kind"] != kind or row["payload_json"] != encoded:
                raise ValueError("idempotency key already belongs to another job")
            self._conn.commit()
            return self._job(row)
        except BaseException:
            self._conn.rollback()
            raise

    def claim_job(self) -> dict[str, Any] | None:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                self._conn.commit()
                return None
            self._conn.execute(
                "UPDATE jobs SET status = 'running', attempts = attempts + 1, "
                "started_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (row["id"],),
            )
            claimed = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
            self._conn.commit()
            return self._job(claimed)
        except BaseException:
            self._conn.rollback()
            raise

    def recover_interrupted_jobs(self) -> list[int]:
        """Move jobs left running by a process crash to the operator queue.

        We do not replay them automatically: the last external side effect may
        have happened before the crash, so an automatic retry could spend twice.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self._conn.execute(
                "SELECT DISTINCT reel_id FROM jobs WHERE status = 'running' ORDER BY reel_id"
            ).fetchall()
            reel_ids = [int(row["reel_id"]) for row in rows]
            self._conn.execute(
                "UPDATE jobs SET status = 'failed', error = 'interrupted by process restart', "
                "finished_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE status = 'running'"
            )
            if reel_ids:
                self._conn.executemany(
                    "UPDATE reels SET status = 'needs_operator', "
                    "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ? AND status = 'queued'",
                    [(reel_id,) for reel_id in reel_ids],
                )
            self._conn.commit()
            return reel_ids
        except BaseException:
            self._conn.rollback()
            raise

    def finish_job(self, job_id: int, success: bool, error: str | None = None) -> None:
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE jobs SET status = ?, error = ?, "
                "finished_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE id = ? AND status = 'running'",
                ("succeeded" if success else "failed", None if success else error, job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"job {job_id} is not running")

    def reserve_spend(
        self, reel_id: int, category: str, amount_usd: float, ceiling_usd: float
    ) -> bool:
        if not all(math.isfinite(value) and value >= 0 for value in (amount_usd, ceiling_usd)):
            raise ValueError("amount and ceiling must be finite nonnegative dollars")
        amount = round(amount_usd * 1_000_000)
        ceiling = round(ceiling_usd * 1_000_000)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._reel(reel_id)
            spent = self._conn.execute(
                "SELECT COALESCE(SUM(amount_microusd), 0) FROM spend_reservations "
                "WHERE category = ? AND substr(created_at, 1, 7) = strftime('%Y-%m','now')",
                (category,),
            ).fetchone()[0]
            if spent + amount > ceiling:
                self._conn.commit()
                return False
            self._conn.execute(
                "INSERT INTO spend_reservations(reel_id, category, amount_microusd) "
                "VALUES (?, ?, ?)",
                (reel_id, category, amount),
            )
            self._conn.commit()
            return True
        except BaseException:
            self._conn.rollback()
            raise

    def total_spend(self, category: str) -> float:
        total = self._conn.execute(
            "SELECT COALESCE(SUM(amount_microusd), 0) FROM spend_reservations "
            "WHERE category = ? AND substr(created_at, 1, 7) = strftime('%Y-%m','now')",
            (category,),
        ).fetchone()[0]
        return total / 1_000_000

    @staticmethod
    def _credit_amount(value: float, *, allow_zero: bool = False) -> int:
        if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
            raise ValueError("credits must be finite and nonnegative")
        milli = round(value * 1_000)
        if (milli <= 0 and not allow_zero) or abs(value * 1_000 - milli) > 1e-7:
            raise ValueError("credits must be specified to at most three decimal places")
        return milli

    def reserve_credits(
        self, reel_id: int, plan_hash: str, credits: float, ceiling: float
    ) -> dict[str, Any] | None:
        """Reserve the approved plan's total Higgsfield credits this UTC month.

        Repeated calls for the same reel and plan return the existing reservation;
        changing its size requires a new plan and approval.  No paid generation
        should start unless ``verify_credit_reservation`` then passes.
        """
        amount = self._credit_amount(credits)
        limit = self._credit_amount(ceiling, allow_zero=True)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            reel = self._reel(reel_id)
            if not (
                plan_hash
                and reel["plan_hash"] == plan_hash
                and reel["approval_hash"] == plan_hash
                and reel["approval_version"] == reel["plan_version"]
            ):
                raise ValueError("credit reservation requires the approved current plan")
            existing = self._conn.execute(
                "SELECT * FROM credit_reservations WHERE reel_id = ? AND plan_hash = ?",
                (reel_id, plan_hash),
            ).fetchone()
            if existing is not None:
                if existing["credits_milli"] != amount:
                    raise ValueError("existing reservation has a different credit amount")
                self._conn.commit()
                return self._credit_row(existing)
            used = self._conn.execute(
                "SELECT COALESCE(SUM(credits_milli), 0) FROM credit_reservations "
                "WHERE substr(created_at, 1, 7) = strftime('%Y-%m','now')"
            ).fetchone()[0]
            if used + amount > limit:
                self._conn.commit()
                return None
            cursor = self._conn.execute(
                "INSERT INTO credit_reservations(reel_id, plan_hash, credits_milli) "
                "VALUES (?, ?, ?)",
                (reel_id, plan_hash, amount),
            )
            row = self._conn.execute(
                "SELECT * FROM credit_reservations WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            self._conn.commit()
            return self._credit_row(row)
        except BaseException:
            self._conn.rollback()
            raise

    @staticmethod
    def _credit_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["credits"] = result.pop("credits_milli") / 1_000
        return result

    def verify_credit_reservation(
        self, reel_id: int, plan_hash: str, reservation_id: int, credits: float
    ) -> bool:
        try:
            amount = self._credit_amount(credits)
        except ValueError:
            return False
        row = self._conn.execute(
            "SELECT c.credits_milli FROM credit_reservations c "
            "JOIN reels r ON r.id = c.reel_id "
            "WHERE c.id = ? AND c.reel_id = ? AND c.plan_hash = ? "
            "AND c.credits_milli = ? AND r.plan_hash = ? AND r.approval_hash = ? "
            "AND r.approval_version = r.plan_version",
            (reservation_id, reel_id, plan_hash, amount, plan_hash, plan_hash),
        ).fetchone()
        return row is not None

    def set_approval(self, reel_id: int, plan_hash: str) -> dict[str, Any]:
        if not plan_hash:
            raise ValueError("plan_hash must be nonempty")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = self._reel(reel_id)
            if current["plan_hash"] != plan_hash or not current["plan_json"]:
                raise ValueError("cannot approve a missing or outdated plan")
            self._conn.execute(
                "UPDATE reels SET approval_hash = ?, approval_version = plan_version, "
                "approved_at = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (plan_hash, reel_id),
            )
            result = self._reel(reel_id)
            self._conn.commit()
            return result
        except BaseException:
            self._conn.rollback()
            raise

    def approval_valid(self, reel_id: int, plan_hash: str) -> bool:
        reel = self.get_reel(reel_id)
        return bool(
            reel
            and plan_hash
            and reel["plan_hash"] == plan_hash
            and reel["approval_hash"] == plan_hash
            and reel["approval_version"] == reel["plan_version"]
        )
