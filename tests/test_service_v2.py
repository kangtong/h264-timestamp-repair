from __future__ import annotations

import importlib
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


APP_ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_ROOT))
os.environ.setdefault("MEDIA_ROOT", tempfile.gettempdir())
os.environ.setdefault("CONFIG_ROOT", tempfile.gettempdir())
os.environ.setdefault("WORK_ROOT", tempfile.gettempdir())
service = importlib.import_module("repair_service")


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = service.State(self.root / "state.sqlite3")

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def test_exact_identity_cache_hit_and_same_name_replacement_miss(self) -> None:
        media = self.root / "same-name.mp4"
        media.write_bytes(b"old-content")
        original = media.stat()
        self.state.save(media, original, "Healthy", "ok")
        self.assertTrue(service.cache_matches(self.state.get(media), media.stat()))

        replacement = self.root / "replacement.mp4"
        replacement.write_bytes(b"new-content")
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(replacement, media)
        self.assertFalse(service.cache_matches(self.state.get(media), media.stat()))

    def test_analysis_signature_change_invalidates_cache(self) -> None:
        media = self.root / "video.mp4"
        media.write_bytes(b"x")
        self.state.save(media, media.stat(), "Healthy", "ok")
        self.state.db.execute("UPDATE files SET analysis_signature='old'")
        self.state.db.commit()
        self.assertFalse(service.cache_matches(self.state.get(media), media.stat()))

    def test_queue_coalesces_events_and_preserves_force(self) -> None:
        media = self.root / "video.mp4"
        first = time.time() + 10
        second = time.time() + 20
        self.state.enqueue(media, "created", first, True)
        self.state.enqueue(media, "closed", second, False)
        row = self.state.db.execute("SELECT * FROM pending").fetchone()
        self.assertEqual(1, self.state.pending_count())
        self.assertEqual("closed", row["event_kind"])
        self.assertEqual(second, row["eligible_at"])
        self.assertEqual(1, row["force"])

    def test_paired_rename_transfers_cache_only_for_same_inode(self) -> None:
        source = self.root / "before.mp4"
        destination = self.root / "after.mp4"
        source.write_bytes(b"content")
        self.state.save(source, source.stat(), "Healthy", "ok")
        source.rename(destination)
        self.assertTrue(self.state.transfer_rename(source, destination))
        self.assertIsNone(self.state.get(source))
        self.assertIsNotNone(self.state.get(destination))

    def test_reconcile_skips_unchanged_terminal_file(self) -> None:
        media = self.root / "cached.mp4"
        media.write_bytes(b"content")
        self.state.save(media, media.stat(), "Healthy", "ok")
        runtime = service.Runtime()
        with mock.patch.object(service, "enumerate_media", return_value=[media]):
            service.reconcile(self.state, runtime)
        self.assertEqual(0, self.state.pending_count())


class MigrationTests(unittest.TestCase):
    def test_legacy_migration_preserves_valid_cache_and_requeues_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            healthy = root / "healthy.mp4"
            failed = root / "failed.mp4"
            healthy.write_bytes(b"healthy")
            failed.write_bytes(b"failed")
            database = root / "state.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE files (
                  path TEXT PRIMARY KEY,size INTEGER,mtime_ns INTEGER,stable_count INTEGER,
                  status TEXT,reason TEXT,checked_at REAL,comparable INTEGER,different INTEGER,
                  dropped_data INTEGER,sha256 TEXT
                )
                """
            )
            for media, status, reason in (
                (healthy, "Healthy", "ok"),
                (failed, "Failed", "failure" * 200000),
            ):
                stat = media.stat()
                connection.execute(
                    "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (str(media), stat.st_size, stat.st_mtime_ns, 1, status, reason, time.time(), 0, 0, 0, ""),
                )
            connection.commit()
            connection.close()
            for name in ("history.jsonl", "latest.csv", "latest.tmp", "web-password.txt"):
                (root / name).write_text("legacy", encoding="utf-8")

            state = service.State(database)
            try:
                self.assertEqual("Healthy", state.get(healthy)["status"])
                self.assertEqual(1, state.pending_count())
                self.assertEqual(str(failed), state.due()[0]["path"])
                self.assertEqual("ok", state.db.execute("PRAGMA integrity_check").fetchone()[0])
            finally:
                state.close()
            for name in ("history.jsonl", "latest.csv", "latest.tmp", "web-password.txt"):
                self.assertFalse((root / name).exists())


class UtilityTests(unittest.TestCase):
    def test_error_is_plain_and_bounded(self) -> None:
        value = "\x1b[31m" + "x" * 10000
        compact = service.compact_error(value)
        self.assertLessEqual(len(compact), service.MAX_ERROR_LENGTH)
        self.assertNotIn("\x1b", compact)

    def test_mp4box_uses_work_job_as_temp_directory(self) -> None:
        source = Path(service.__file__).read_text(encoding="utf-8")
        self.assertIn('[MP4BOX, "-tmp", str(job), "-add"', source)

    def test_deterministic_repair_validation_uses_distinct_error(self) -> None:
        original = service.Analysis(
            "Candidate", "missing timestamps", {"streams": []},
            {"codec_name": "h264"}, 60, 0,
        )
        fixed = service.Analysis(
            "Candidate", "timestamps still missing", {"streams": []},
            {"codec_name": "h264"}, 60, 0,
        )
        with self.assertRaises(service.RepairValidationError):
            service.compare_streams(original, fixed)


if __name__ == "__main__":
    unittest.main()
