from __future__ import annotations

import importlib
import json
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

    def test_manual_repair_command_is_persistent_and_queues_repair_intent(self) -> None:
        media = self.root / "manual.mkv"
        media.write_bytes(b"content")
        analysis = service.Analysis(
            "Candidate", "timeline", {"streams": []}, {"codec_name": "h264"},
            timestamp_issue=True, container="mkv", issue_category="timeline",
            reason_code="timeline_bframe_pts",
        )
        self.state.save(media, media.stat(), "Candidate", "timeline", analysis=analysis)
        file_id = str(self.state.get(media)["file_id"])
        self.state.db.execute(
            "INSERT INTO control_commands(action,file_ids_json,requested_at,state) VALUES(?,?,?,'queued')",
            ("repair", json.dumps([file_id]), time.time()),
        )
        self.state.db.commit()
        self.state.close()
        self.state = service.State(self.root / "state.sqlite3")
        command = self.state.due_control_command()
        self.assertIsNotNone(command)
        service.process_control_command(self.state, service.Runtime(), command)
        pending = self.state.db.execute("SELECT requested_action FROM pending WHERE path=?", (str(media),)).fetchone()
        self.assertEqual("repair", pending["requested_action"])


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
    @staticmethod
    def media_info_with_chapter(*, title: str = "", start: float = 0.0, end: float = 100.0) -> dict:
        return {
            "format": {"duration": "100.0"},
            "streams": [{
                "codec_type": "video", "codec_name": "h264", "has_b_frames": 1,
                "profile": "High", "width": 1920, "height": 1080, "pix_fmt": "yuv420p",
                "avg_frame_rate": "30000/1001", "r_frame_rate": "30000/1001",
                "disposition": {"attached_pic": 0},
            }],
            "chapters": [{
                "start_time": str(start), "end_time": str(end), "tags": {"title": title},
            }],
        }

    def test_detects_only_untitled_chapter_spanning_the_complete_file(self) -> None:
        self.assertTrue(service.has_empty_full_duration_chapter(self.media_info_with_chapter()))
        self.assertFalse(service.has_empty_full_duration_chapter(self.media_info_with_chapter(title="Feature")))
        self.assertFalse(service.has_empty_full_duration_chapter(self.media_info_with_chapter(start=5.0)))
        self.assertFalse(service.has_empty_full_duration_chapter(self.media_info_with_chapter(end=80.0)))
        multiple = self.media_info_with_chapter()
        multiple["chapters"].append(dict(multiple["chapters"][0]))
        self.assertFalse(service.has_empty_full_duration_chapter(multiple))

    def test_analysis_reports_timestamp_and_chapter_problems_independently(self) -> None:
        info = self.media_info_with_chapter()
        with mock.patch.object(service, "media_info", return_value=info), mock.patch.object(
            service, "timestamp_sample", return_value=(60, 0),
        ), mock.patch.object(
            service, "decoded_timeline_sample", return_value=(60, 30, "issue", {"transitions": 60}),
        ):
            result = service.analyze(Path("sample.mp4"))
        self.assertEqual("Candidate", result.status)
        self.assertTrue(result.timestamp_issue)
        self.assertTrue(result.chapter_issue)
        self.assertEqual("timeline_and_chapter", result.reason_code)

        info["streams"][0]["codec_name"] = "hevc"
        with mock.patch.object(service, "media_info", return_value=info), mock.patch.object(
            service, "timestamp_sample",
        ) as sample:
            result = service.analyze(Path("sample.mp4"))
        self.assertEqual("Candidate", result.status)
        self.assertFalse(result.timestamp_issue)
        self.assertTrue(result.chapter_issue)
        sample.assert_not_called()

    def test_unified_decoded_pts_detector_distinguishes_bad_and_good_mkv(self) -> None:
        bad = {"frames": [{"pts_time": str(value)} for value in (
            0.000, 0.100, 0.067, 0.133, 0.033, 0.167, 0.267, 0.234,
            0.300, 0.200, 0.334, 0.434, 0.401, 0.467, 0.367,
            0.501, 0.601, 0.568, 0.635, 0.535, 0.668, 0.768,
        )]}
        with mock.patch.object(service, "run_capture", side_effect=[json.dumps(bad)] * 3):
            total, anomalies, verdict, _ = service.decoded_timeline_sample(
                Path("bad.mkv"), 100.0, 30.0,
            )
        self.assertGreaterEqual(total, 60)
        self.assertGreaterEqual(anomalies, 9)
        self.assertEqual("issue", verdict)

        good = {"frames": [{"pts_time": f"{index / 30:.6f}"} for index in range(30)]}
        with mock.patch.object(service, "run_capture", side_effect=[json.dumps(good)] * 3):
            _, anomalies, verdict, _ = service.decoded_timeline_sample(
                Path("good.mkv"), 100.0, 30.0,
            )
        self.assertEqual(0, anomalies)
        self.assertEqual("healthy", verdict)

    def test_isolated_sub_threshold_pts_noise_does_not_require_manual_review(self) -> None:
        noisy_values = [index / 30 for index in range(30)]
        noisy_values[12] = noisy_values[11]
        noisy = {"frames": [{"pts_time": f"{value:.6f}"} for value in noisy_values]}
        good = {"frames": [{"pts_time": f"{index / 30:.6f}"} for index in range(30)]}
        with mock.patch.object(
            service, "run_capture", side_effect=[json.dumps(noisy), json.dumps(good), json.dumps(good)],
        ):
            _, anomalies, verdict, diagnostics = service.decoded_timeline_sample(
                Path("minor-noise.mkv"), 100.0, 30.0,
            )
        self.assertEqual(1, anomalies)
        self.assertEqual("healthy", verdict)
        self.assertTrue(diagnostics["low_level_noise_ignored"])

    def test_annexb_fingerprint_accepts_safe_parameter_unit_reordering(self) -> None:
        start = b"\x00\x00\x00\x01"
        sps, pps, sei = b"\x67sps", b"\x68pps", b"\x06sei"
        idr, slice_unit = b"\x65idr-picture", b"\x41predicted-picture"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = root / "before.h264"
            reordered = root / "reordered.h264"
            changed = root / "changed.h264"
            before.write_bytes(start.join((b"", sps, pps, sei, idr, slice_unit)))
            reordered.write_bytes(start.join((b"", sei, sps, pps, idr, slice_unit)))
            changed.write_bytes(start.join((b"", sei, sps, pps, b"\x65changed-picture", slice_unit)))

            before_fingerprint = service.annexb_nal_fingerprints(before)
            reordered_fingerprint = service.annexb_nal_fingerprints(reordered)
            changed_fingerprint = service.annexb_nal_fingerprints(changed)
            raw_hashes_differ = service.sha256(before) != service.sha256(reordered)

        self.assertTrue(raw_hashes_differ)
        self.assertEqual(before_fingerprint, reordered_fingerprint)
        self.assertNotEqual(before_fingerprint["vcl"], changed_fingerprint["vcl"])

    def test_empty_mp4_data_placeholder_does_not_require_manual_review(self) -> None:
        fingerprints = {
            "audio:0": (100, "audio-hash"),
            "data:0": (0, "empty-hash"),
        }
        self.assertEqual(
            {"audio:0": (100, "audio-hash")},
            service.copied_payload_fingerprints(fingerprints, "mp4"),
        )
        self.assertEqual(fingerprints, service.copied_payload_fingerprints(fingerprints, "mkv"))
        with self.assertRaisesRegex(service.RepairValidationError, "cannot be dropped safely"):
            service.copied_payload_fingerprints({"data:0": (1, "payload-hash")}, "mp4")

    def test_pathological_fps_fraction_is_safely_bounded(self) -> None:
        original = service.Fraction(681753313, 27716001)
        normalized = service.mp4box_fps("681753313/27716001", 988.420767)
        self.assertEqual("2263/92", normalized)
        candidate = service.Fraction(normalized)
        duration_drift = 988.420767 * abs(float(original / candidate) - 1.0)
        self.assertLessEqual(duration_drift, 0.005)
        self.assertEqual("30000/1001", service.mp4box_fps("30000/1001", 7200.0))

    def test_mp4_and_mkv_use_the_same_timeline_issue_code(self) -> None:
        info = self.media_info_with_chapter(title="valid", start=5.0, end=80.0)
        for name in ("sample.mp4", "sample.mkv"):
            with self.subTest(name=name), mock.patch.object(
                service, "media_info", return_value=info,
            ), mock.patch.object(
                service, "timestamp_sample", return_value=(60, 0),
            ), mock.patch.object(
                service, "decoded_timeline_sample",
                return_value=(60, 30, "issue", {"transitions": 60}),
            ):
                result = service.analyze(Path(name))
            self.assertEqual("Candidate", result.status)
            self.assertEqual("timeline_bframe_pts", result.reason_code)
            self.assertEqual("timeline", result.issue_category)

    def test_media_matcher_accepts_mp4_and_mkv_only(self) -> None:
        self.assertTrue(service.matches_media(Path("movie.mp4")))
        self.assertTrue(service.matches_media(Path("movie.mkv")))
        self.assertFalse(service.matches_media(Path("movie.avi")))

    def test_error_is_plain_and_bounded(self) -> None:
        value = "\x1b[31m" + "x" * 10000
        compact = service.compact_error(value)
        self.assertLessEqual(len(compact), service.MAX_ERROR_LENGTH)
        self.assertNotIn("\x1b", compact)

    def test_mp4box_uses_work_job_as_temp_directory(self) -> None:
        source = Path(service.__file__).read_text(encoding="utf-8")
        self.assertIn('[MP4BOX, "-tmp", str(job), "-add"', source)
        self.assertIn('f"-metadata:s:t:{attachment_index}"', source)

    def test_mkv_attachment_content_hash_must_be_preserved(self) -> None:
        video = {"codec_type": "video", "codec_name": "h264"}
        original_info = {
            "streams": [video, {
                "codec_type": "attachment", "codec_name": None,
                "extradata_hash": "SHA256:original",
                "tags": {"filename": "font.ttf", "mimetype": "application/x-truetype-font"},
            }],
            "chapters": [],
        }
        fixed_info = json.loads(json.dumps(original_info))
        fixed_info["streams"][1]["extradata_hash"] = "SHA256:changed"
        original = service.Analysis(
            "Candidate", "timeline", original_info, video,
            timestamp_issue=True, container="mkv",
        )
        fixed = service.Analysis("Healthy", "ok", fixed_info, fixed_info["streams"][0], container="mkv")
        with self.assertRaisesRegex(service.RepairValidationError, "Attachment 0 content changed"):
            service.compare_streams(original, fixed)

    def test_deterministic_repair_validation_uses_distinct_error(self) -> None:
        original = service.Analysis(
            "Candidate", "missing timestamps", {"streams": []},
            {"codec_name": "h264"}, 60, 0, timestamp_issue=True,
        )
        fixed = service.Analysis(
            "Candidate", "timestamps still missing", {"streams": []},
            {"codec_name": "h264"}, 60, 0,
        )
        with self.assertRaises(service.RepairValidationError):
            service.compare_streams(original, fixed)


if __name__ == "__main__":
    unittest.main()
