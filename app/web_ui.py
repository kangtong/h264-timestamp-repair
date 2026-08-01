#!/usr/bin/env python3
"""Read-only authenticated web dashboard for the timestamp repair service."""

from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


LOG = logging.getLogger("timestamp-repair.web")


def _display_path(value: str, show_full_paths: bool) -> str:
    return value if show_full_paths else Path(value).name


def _load_or_create_password(config_root: Path, configured: str) -> tuple[str, Path | None, bool]:
    if configured:
        return configured, None, False
    target = config_root / "web-password.txt"
    if target.exists():
        existing = target.read_text(encoding="utf-8").strip()
        if existing:
            return existing, target, False
    password = secrets.token_urlsafe(18)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(password + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, target)
    return password, target, True


@dataclass
class WebService:
    server: ThreadingHTTPServer
    thread: threading.Thread
    username: str
    password_file: Path | None

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def start_web_server(
    config_root: Path,
    host: str,
    port: int,
    username: str,
    password: str,
    show_full_paths: bool,
    settings: dict[str, Any],
) -> WebService:
    """Start the dashboard in a daemon thread and return its lifecycle handle."""
    resolved_password, password_file, generated = _load_or_create_password(config_root, password)
    static_root = Path(__file__).resolve().parent / "web"
    if not static_root.is_dir():
        raise RuntimeError(f"Web assets directory is missing: {static_root}")

    state_path = config_root / "state.sqlite3"
    heartbeat_path = config_root / "heartbeat.json"
    history_path = config_root / "history.jsonl"

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "H264TimestampRepair/1.1"

        def log_message(self, format_text: str, *args: Any) -> None:
            LOG.debug("%s - %s", self.address_string(), format_text % args)

        def _send_headers(
            self,
            status: int,
            content_type: str,
            length: int,
            *,
            cache: str = "no-store",
            disposition: str | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
            )
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.end_headers()

        def _send_bytes(
            self,
            payload: bytes,
            content_type: str,
            status: int = HTTPStatus.OK,
            *,
            cache: str = "no-store",
            disposition: str | None = None,
        ) -> None:
            self._send_headers(status, content_type, len(payload), cache=cache, disposition=disposition)
            self.wfile.write(payload)

        def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8", status)

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return False
            expected = f"{username}:{resolved_password}"
            return hmac.compare_digest(decoded, expected)

        def _require_auth(self) -> bool:
            if self._authorized():
                return True
            body = b"Authentication required\n"
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="H.264 Timestamp Repair", charset="UTF-8"')
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return False

        def _connect(self) -> sqlite3.Connection | None:
            if not state_path.exists():
                return None
            connection = sqlite3.connect(state_path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA query_only=ON")
            return connection

        def _summary(self) -> dict[str, int]:
            connection = self._connect()
            if connection is None:
                return {"total": 0}
            try:
                rows = connection.execute("SELECT status, COUNT(*) AS count FROM files GROUP BY status").fetchall()
                result = {str(row["status"]): int(row["count"]) for row in rows}
                result["total"] = sum(result.values())
                return result
            finally:
                connection.close()

        def _heartbeat(self) -> dict[str, Any]:
            try:
                payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                return {}
            current = str(payload.get("current_path", ""))
            if current:
                payload["current_path"] = _display_path(current, show_full_paths)
            return payload

        def _status_payload(self) -> dict[str, Any]:
            heartbeat = self._heartbeat()
            age = None
            if heartbeat.get("time") is not None:
                try:
                    age = max(0.0, time.time() - float(heartbeat["time"]))
                except (TypeError, ValueError):
                    age = None
            scan_interval = int(settings.get("scan_interval_seconds", 1800))
            healthy_age = max(1800, scan_interval * 2 + 1800)
            return {
                "now": time.time(),
                "heartbeat_age_seconds": age,
                "service_healthy": age is not None and age <= healthy_age,
                "heartbeat": heartbeat,
                "summary": self._summary(),
                "config": settings,
            }

        def _files_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
            status = query.get("status", [""])[0].strip()
            search = query.get("q", [""])[0].strip()
            try:
                limit = min(500, max(1, int(query.get("limit", ["200"])[0])))
                offset = max(0, int(query.get("offset", ["0"])[0]))
            except ValueError:
                limit, offset = 200, 0

            clauses: list[str] = []
            params: list[Any] = []
            if status:
                clauses.append("status = ?")
                params.append(status)
            if search:
                clauses.append("(path LIKE ? ESCAPE '\\' OR reason LIKE ? ESCAPE '\\')")
                escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                params.extend([f"%{escaped}%", f"%{escaped}%"])
            where = " WHERE " + " AND ".join(clauses) if clauses else ""

            connection = self._connect()
            if connection is None:
                return {"total": 0, "offset": offset, "limit": limit, "items": []}
            try:
                total = int(connection.execute("SELECT COUNT(*) FROM files" + where, params).fetchone()[0])
                rows = connection.execute(
                    "SELECT path,status,reason,checked_at,comparable,different,dropped_data "
                    "FROM files" + where + " ORDER BY checked_at DESC, path LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                ).fetchall()
                items = []
                for row in rows:
                    item = dict(row)
                    item["path"] = _display_path(str(item["path"]), show_full_paths)
                    items.append(item)
                return {"total": total, "offset": offset, "limit": limit, "items": items}
            finally:
                connection.close()

        def _history_payload(self, query: dict[str, list[str]]) -> dict[str, Any]:
            try:
                limit = min(200, max(1, int(query.get("limit", ["30"])[0])))
            except ValueError:
                limit = 30
            if not history_path.exists():
                return {"items": []}
            recent: deque[str] = deque(maxlen=limit)
            with history_path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.strip():
                        recent.append(line)
            items = []
            for line in reversed(recent):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                items.append(
                    {
                        "time": event.get("time", ""),
                        "path": _display_path(str(event.get("path", "")), show_full_paths),
                        "status": event.get("status", ""),
                        "reason": event.get("reason", ""),
                    }
                )
            return {"items": items}

        def _report_csv(self) -> bytes:
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(
                ["path", "status", "reason", "checked_at", "comparable", "different", "dropped_data", "sha256"]
            )
            connection = self._connect()
            if connection is not None:
                try:
                    rows = connection.execute(
                        "SELECT path,status,reason,checked_at,comparable,different,dropped_data,sha256 "
                        "FROM files ORDER BY path"
                    ).fetchall()
                    for row in rows:
                        values = list(row)
                        values[0] = _display_path(str(values[0]), show_full_paths)
                        writer.writerow(values)
                finally:
                    connection.close()
            return ("\ufeff" + output.getvalue()).encode("utf-8")

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if not self._require_auth():
                return
            parsed = urlparse(self.path)
            path = parsed.path
            query = parse_qs(parsed.query, keep_blank_values=True)
            if path in {"/", "/index.html"}:
                payload = (static_root / "index.html").read_bytes()
                self._send_bytes(payload, "text/html; charset=utf-8", cache="no-cache")
            elif path == "/assets/style.css":
                self._send_bytes(
                    (static_root / "style.css").read_bytes(),
                    "text/css; charset=utf-8",
                    cache="public, max-age=300",
                )
            elif path == "/assets/app.js":
                self._send_bytes(
                    (static_root / "app.js").read_bytes(),
                    "text/javascript; charset=utf-8",
                    cache="public, max-age=300",
                )
            elif path == "/api/status":
                self._send_json(self._status_payload())
            elif path == "/api/files":
                self._send_json(self._files_payload(query))
            elif path == "/api/history":
                self._send_json(self._history_payload(query))
            elif path == "/report.csv":
                self._send_bytes(
                    self._report_csv(),
                    "text/csv; charset=utf-8",
                    disposition='attachment; filename="h264-timestamp-report.csv"',
                )
            else:
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="timestamp-repair-web", daemon=True)
    thread.start()
    LOG.info("Web UI listening on http://%s:%s", host, server.server_address[1])
    if generated and password_file:
        LOG.warning("Generated Web UI password for user %s; read it from %s", username, password_file)
    elif password_file:
        LOG.info("Using persisted Web UI password from %s", password_file)
    return WebService(server=server, thread=thread, username=username, password_file=password_file)
