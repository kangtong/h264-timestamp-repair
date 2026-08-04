from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_ROOT))
os.environ.setdefault("MEDIA_ROOT", tempfile.gettempdir())
os.environ.setdefault("CONFIG_ROOT", tempfile.gettempdir())
os.environ.setdefault("WORK_ROOT", tempfile.gettempdir())
service_module = importlib.import_module("repair_service")
web_module = importlib.import_module("web_ui")


class AnonymousWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = service_module.State(self.root / "state.sqlite3")
        media = self.root / "private-path" / "sample.mp4"
        media.parent.mkdir()
        media.write_bytes(b"x")
        self.state.save(media, media.stat(), "Healthy", "ok")
        self.state.record(media, "Healthy", "ok")
        (self.root / "heartbeat.json").write_text(
            json.dumps({
                "time": time.time(), "watcher_active": True, "pending_count": 0,
                "current_path": str(media), "current_action": "idle",
            }),
            encoding="utf-8",
        )
        self.web = web_module.start_web_server(
            self.root, "127.0.0.1", 0, False,
            {"auto_repair": True, "reconcile_local_time": "04:00"},
        )
        self.base = f"http://127.0.0.1:{self.web.port}"

    def tearDown(self) -> None:
        self.web.stop()
        self.state.close()
        self.temporary.cleanup()

    def get(self, path: str) -> tuple[int, bytes]:
        with urllib.request.urlopen(self.base + path, timeout=5) as response:
            return response.status, response.read()

    def post(self, path: str, payload: dict, *, origin: str = "") -> tuple[int, bytes]:
        request = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", **({"Origin": origin} if origin else {})},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()

    def test_page_and_apis_are_anonymous_and_paths_are_redacted(self) -> None:
        self.assertEqual(200, self.get("/")[0])
        status, body = self.get("/api/status")
        self.assertEqual(200, status)
        self.assertTrue(json.loads(body)["service_healthy"])
        _, body = self.get("/api/files")
        item = json.loads(body)["items"][0]
        self.assertEqual("sample.mp4", item["path"])
        self.assertNotIn("private-path", body.decode("utf-8"))
        self.assertEqual("正常", item["status_label"])
        self.assertEqual(200, self.get("/api/history")[0])

    def test_manual_actions_are_persisted_and_cross_origin_is_rejected(self) -> None:
        item = json.loads(self.get("/api/files")[1])["items"][0]
        status, body = self.post("/api/files/actions/recheck", {"file_ids": [item["file_id"]]})
        self.assertEqual(202, status)
        self.assertTrue(json.loads(body)["accepted"])
        row = self.state.db.execute("SELECT action,state FROM control_commands").fetchone()
        self.assertEqual(("recheck", "queued"), tuple(row))
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.post("/api/actions/reconcile", {}, origin="https://example.invalid")
        self.assertEqual(403, raised.exception.code)

    def test_csv_endpoint_is_removed(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.get("/report.csv")
        self.assertEqual(404, raised.exception.code)

    def test_queued_problem_is_not_reported_as_manual_confirmation(self) -> None:
        media = self.root / "private-path" / "queued.mkv"
        media.write_bytes(b"queued")
        analysis = service_module.Analysis(
            "Uncertain", "old validation result", {}, {}, 100, 50,
            container="mkv", issue_category="unsupported", reason_code="ambiguous_timeline",
        )
        self.state.save(
            media, media.stat(), analysis.status, analysis.reason,
            analysis.comparable, analysis.different, analysis=analysis,
        )
        self.state.record(media, analysis.status, analysis.reason)
        self.state.enqueue(media, "signature-recheck", time.time(), True, "repair")

        summary = json.loads(self.get("/api/status")[1])["summary"]
        self.assertEqual(1, summary["QueuedRepair"])
        self.assertNotIn("unsupported", summary["issues"])

        payload = json.loads(self.get("/api/files?status=QueuedRepair")[1])
        self.assertEqual(1, payload["total"])
        self.assertEqual("QueuedRepair", payload["items"][0]["status_code"])
        self.assertEqual("等待自动修复", payload["items"][0]["status_label"])
        self.assertEqual(0, json.loads(self.get("/api/files?problems=1")[1])["total"])

        history = json.loads(self.get("/api/history")[1])["items"][0]
        self.assertEqual("QueuedRepair", history["status"])
        self.assertEqual("等待自动修复", history["status_label"])


if __name__ == "__main__":
    unittest.main()
