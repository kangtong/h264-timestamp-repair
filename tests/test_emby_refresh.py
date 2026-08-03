from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path, PurePosixPath
from unittest import mock


APP_ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_ROOT))
os.environ.setdefault("MEDIA_ROOT", tempfile.gettempdir())
os.environ.setdefault("CONFIG_ROOT", tempfile.gettempdir())
os.environ.setdefault("WORK_ROOT", tempfile.gettempdir())
emby = importlib.import_module("emby_refresh")
service = importlib.import_module("repair_service")


class FakeResponse:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class EmbyClientTests(unittest.TestCase):
    def test_exact_path_lookup_then_full_refresh_without_replacing_all_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "folder" / "same-name.mp4"
            calls = []

            def open_request(request: object, timeout: int) -> FakeResponse:
                calls.append((request, timeout))
                if request.get_method() == "GET":
                    return FakeResponse(json.dumps({
                        "Items": [{"Id": "42", "Path": "/Library/folder/same-name.mp4"}],
                    }).encode("utf-8"))
                return FakeResponse(b"")

            client = emby.EmbyRefreshClient(
                "http://server:8096/emby", "secret", root,
                PurePosixPath("/Library"), 15,
            )
            with mock.patch.object(emby.urllib.request, "urlopen", side_effect=open_request):
                self.assertEqual("42", client.refresh(media))

            self.assertEqual(2, len(calls))
            get_request, get_timeout = calls[0]
            self.assertEqual("GET", get_request.get_method())
            self.assertEqual(15, get_timeout)
            get_query = urllib.parse.parse_qs(urllib.parse.urlsplit(get_request.full_url).query)
            self.assertEqual(["/Library/folder/same-name.mp4"], get_query["Path"])
            self.assertIn(("X-emby-token", "secret"), get_request.header_items())

            post_request, _ = calls[1]
            self.assertEqual("POST", post_request.get_method())
            self.assertIn("/Items/42/Refresh?", post_request.full_url)
            post_query = urllib.parse.parse_qs(urllib.parse.urlsplit(post_request.full_url).query)
            self.assertEqual(["false"], post_query["ReplaceAllMetadata"])
            self.assertEqual(["true"], post_query["ReplaceThumbnailImages"])

    def test_non_exact_or_ambiguous_item_is_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = emby.EmbyRefreshClient(
                "http://server/emby", "secret", root, PurePosixPath("/Library"), 15,
            )
            response = FakeResponse(json.dumps({
                "Items": [{"Id": "1", "Path": "/Library/a/different.mp4"}],
            }).encode("utf-8"))
            with mock.patch.object(emby.urllib.request, "urlopen", return_value=response):
                with self.assertRaises(emby.EmbyItemNotReady):
                    client.refresh(root / "a" / "target.mp4")


class RefreshQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = service.State(self.root / "state.sqlite3")

    def tearDown(self) -> None:
        self.state.close()
        self.temporary.cleanup()

    def test_refresh_queue_is_persistent_coalesced_and_separate_from_file_failure(self) -> None:
        media = self.root / "video.mp4"
        self.state.enqueue_media_refresh(media, time.time() - 10)
        self.state.enqueue_media_refresh(media, time.time() + 100)
        self.assertEqual(1, self.state.media_refresh_pending_count())
        row = self.state.due_media_refresh(1)[0]
        self.state.defer_media_refresh(media, time.time() - 1, "not in library yet")
        row = self.state.due_media_refresh(1)[0]
        self.assertEqual(1, row["attempts"])
        self.assertEqual("not in library yet", row["last_error"])
        self.assertIsNone(self.state.get(media))

        self.state.close()
        self.state = service.State(self.root / "state.sqlite3")
        self.assertEqual(1, self.state.media_refresh_pending_count())
        self.state.complete_media_refresh(media)
        self.assertEqual(0, self.state.media_refresh_pending_count())

    def test_not_ready_refresh_uses_backoff_without_marking_media_failed(self) -> None:
        media = self.root / "video.mp4"
        self.state.enqueue_media_refresh(media, time.time() - 1)
        runtime = service.Runtime()
        client = mock.Mock()
        client.refresh.side_effect = emby.EmbyItemNotReady("not ready")
        service.process_media_refresh(self.state, runtime, client, self.state.due_media_refresh(1)[0])
        queued = self.state.db.execute("SELECT * FROM media_refresh_queue").fetchone()
        self.assertEqual(1, queued["attempts"])
        self.assertIsNone(self.state.get(media))
        event = self.state.db.execute("SELECT status FROM events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual("MediaRefreshWaiting", event["status"])

    def test_chapter_repair_backfill_runs_once(self) -> None:
        media = self.root / "already-repaired.mp4"
        media.write_bytes(b"media")
        self.state.save(
            media, media.stat(), "Repaired",
            "invalid full-duration chapter removed; streams copied without re-encoding",
        )
        self.assertEqual(1, self.state.backfill_chapter_media_refreshes())
        self.assertEqual(1, self.state.media_refresh_pending_count())
        self.state.complete_media_refresh(media)
        self.assertEqual(0, self.state.backfill_chapter_media_refreshes())
        self.assertEqual(0, self.state.media_refresh_pending_count())

    def test_successful_media_repair_queues_library_refresh(self) -> None:
        media = self.root / "video.mp4"
        media.write_bytes(b"media")
        self.state.enqueue(media, "test", time.time() - 1, True)
        analysis = service.Analysis(
            "Candidate", "chapter", {"streams": []}, {"codec_name": "h264"},
            chapter_issue=True,
        )
        runtime = service.Runtime()
        with mock.patch.object(service, "AUTO_REPAIR", True), mock.patch.object(
            service, "MIN_FILE_AGE", 0,
        ), mock.patch.object(
            service, "analyze", return_value=analysis,
        ), mock.patch.object(service, "compatibility_reason", return_value=""), mock.patch.object(
            service, "repair_one", return_value=("ABC", 1),
        ):
            service.process_pending(
                self.state, runtime, self.state.due(1)[0], media_refresh_enabled=True,
            )
        self.assertEqual("Repaired", self.state.get(media)["status"])
        self.assertEqual(1, self.state.media_refresh_pending_count())
        event_statuses = [
            row[0] for row in self.state.db.execute("SELECT status FROM events ORDER BY id")
        ]
        self.assertIn("MediaRefreshQueued", event_statuses)


if __name__ == "__main__":
    unittest.main()
