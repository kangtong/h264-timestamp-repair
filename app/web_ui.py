#!/usr/bin/env python3
"""Anonymous LAN dashboard and safe control API for the repair service."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from web_i18n import ISSUE_LABELS, REASON_LABELS, STATUS_LABELS, UI, catalog, normalize_locale, stage_code, STAGE_LABELS


LOG = logging.getLogger("timestamp-repair.web")

def _display_path(value: str, show_full_paths: bool) -> str:
    return value if show_full_paths else Path(value).name


@dataclass
class WebService:
    server: ThreadingHTTPServer
    thread: threading.Thread

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
    show_full_paths: bool,
    settings: dict[str, Any],
    wake_callback: Any | None = None,
) -> WebService:
    """Start the anonymous LAN dashboard."""
    static_root = Path(__file__).resolve().parent / "web"
    if not static_root.is_dir():
        raise RuntimeError(f"Web assets directory is missing: {static_root}")

    state_path = config_root / "state.sqlite3"
    heartbeat_path = config_root / "heartbeat.json"

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "VideoIntegrityRepair/3.1.1"

        def log_message(self, format_text: str, *args: Any) -> None:
            LOG.debug("%s - %s", self.address_string(), format_text % args)

        def _send_headers(
            self,
            status: int,
            content_type: str,
            length: int,
            *,
            cache: str = "no-store",
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
            self.end_headers()

        def _send_bytes(
            self,
            payload: bytes,
            content_type: str,
            status: int = HTTPStatus.OK,
            *,
            cache: str = "no-store",
        ) -> None:
            self._send_headers(status, content_type, len(payload), cache=cache)
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self._send_bytes(body, "application/json; charset=utf-8", status)

        def _connect(self, *, write: bool = False) -> sqlite3.Connection | None:
            if not state_path.exists():
                return None
            connection = sqlite3.connect(state_path, timeout=5)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            if not write:
                connection.execute("PRAGMA query_only=ON")
            return connection

        def _summary(self) -> dict[str, int]:
            connection = self._connect()
            if connection is None:
                return {"total": 0}
            try:
                rows = connection.execute(
                    "SELECT CASE "
                    "WHEN p.path IS NOT NULL AND p.requested_action='repair' THEN 'QueuedRepair' "
                    "WHEN p.path IS NOT NULL THEN 'QueuedRecheck' ELSE f.status END AS effective_status, "
                    "COUNT(*) AS count FROM files f LEFT JOIN pending p ON p.path=f.path "
                    "GROUP BY effective_status"
                ).fetchall()
                result = {str(row["effective_status"]): int(row["count"]) for row in rows}
                result["total"] = sum(result.values())
                result["pending"] = int(connection.execute("SELECT COUNT(*) FROM pending").fetchone()[0])
                result["media_refresh_pending"] = int(
                    connection.execute("SELECT COUNT(*) FROM media_refresh_queue").fetchone()[0]
                )
                issue_rows = connection.execute(
                    "SELECT f.issue_category,COUNT(*) AS count FROM files f "
                    "LEFT JOIN pending p ON p.path=f.path "
                    "WHERE f.status IN ('Candidate','Uncertain','Failed') AND p.path IS NULL "
                    "GROUP BY f.issue_category"
                ).fetchall()
                result["issues"] = {str(row["issue_category"]): int(row["count"]) for row in issue_rows}
                return result
            finally:
                connection.close()

        def _heartbeat(self, locale: str) -> dict[str, Any]:
            try:
                payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                return {}
            current = str(payload.get("current_path", ""))
            if current:
                payload["current_path"] = _display_path(current, show_full_paths)
            active = bool(payload.get("current_action") and payload.get("current_action") != "idle")
            code = stage_code(str(payload.get("current_stage", "")), active)
            payload["current_stage_code"] = code
            payload["current_stage_label"] = STAGE_LABELS[locale].get(code, str(payload.get("current_stage", "")))
            return payload

        def _status_payload(self, locale: str) -> dict[str, Any]:
            heartbeat = self._heartbeat(locale)
            age = None
            if heartbeat.get("time") is not None:
                try:
                    age = max(0.0, time.time() - float(heartbeat["time"]))
                except (TypeError, ValueError):
                    age = None
            return {
                "locale": locale,
                "now": time.time(),
                "heartbeat_age_seconds": age,
                "service_healthy": age is not None and age <= 600,
                "heartbeat": heartbeat,
                "summary": self._summary(),
                "config": settings,
            }

        def _files_payload(self, query: dict[str, list[str]], locale: str) -> dict[str, Any]:
            status = query.get("status", [""])[0].strip()
            issue = query.get("issue", [""])[0].strip()
            container = query.get("container", [""])[0].strip()
            problems_only = query.get("problems", [""])[0].strip() == "1"
            search = query.get("q", [""])[0].strip()
            try:
                limit = min(200, max(1, int(query.get("limit", ["50"])[0])))
                offset = max(0, int(query.get("offset", ["0"])[0]))
            except ValueError:
                limit, offset = 200, 0

            clauses: list[str] = []
            params: list[Any] = []
            if status:
                clauses.append(
                    "CASE WHEN p.path IS NOT NULL AND p.requested_action='repair' THEN 'QueuedRepair' "
                    "WHEN p.path IS NOT NULL THEN 'QueuedRecheck' ELSE f.status END = ?"
                )
                params.append(status)
            if issue:
                clauses.append("f.issue_category = ?")
                params.append(issue)
            if container:
                clauses.append("f.container = ?")
                params.append(container)
            if problems_only:
                clauses.append("f.status IN ('Candidate','Uncertain','Failed') AND p.path IS NULL")
            if search:
                clauses.append("(f.path LIKE ? ESCAPE '\\' OR f.reason LIKE ? ESCAPE '\\')")
                escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                params.extend([f"%{escaped}%", f"%{escaped}%"])
            where = " WHERE " + " AND ".join(clauses) if clauses else ""

            connection = self._connect()
            if connection is None:
                return {"locale": locale, "total": 0, "offset": offset, "limit": limit, "items": []}
            try:
                from_clause = " FROM files f LEFT JOIN pending p ON p.path=f.path"
                total = int(connection.execute("SELECT COUNT(*)" + from_clause + where, params).fetchone()[0])
                rows = connection.execute(
                    "SELECT f.file_id,f.path,f.status,f.reason,f.checked_at,f.comparable,f.different,"
                    "f.dropped_data,f.container,f.issue_category,f.reason_code,f.diagnostics_json,"
                    "p.requested_action " + from_clause + where +
                    " ORDER BY f.checked_at DESC,f.path LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                ).fetchall()
                items = []
                for row in rows:
                    item = dict(row)
                    item["path"] = _display_path(str(item["path"]), show_full_paths)
                    requested_action = item.pop("requested_action")
                    effective_status = (
                        "QueuedRepair" if requested_action == "repair"
                        else "QueuedRecheck" if requested_action is not None
                        else str(item["status"])
                    )
                    item["status_code"] = effective_status
                    item["status_label"] = STATUS_LABELS[locale].get(effective_status, effective_status)
                    item["issue_label"] = ISSUE_LABELS[locale].get(str(item["issue_category"]), ISSUE_LABELS[locale]["other"])
                    item["reason_label"] = REASON_LABELS[locale].get(str(item["reason_code"]), str(item["reason"]))
                    try:
                        item["diagnostics"] = json.loads(str(item.pop("diagnostics_json")))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        item["diagnostics"] = {}
                    items.append(item)
                return {"locale": locale, "total": total, "offset": offset, "limit": limit, "items": items}
            finally:
                connection.close()

        def _history_payload(self, query: dict[str, list[str]], locale: str) -> dict[str, Any]:
            try:
                limit = min(200, max(1, int(query.get("limit", ["30"])[0])))
            except ValueError:
                limit = 30
            connection = self._connect()
            if connection is None:
                return {"locale": locale, "items": []}
            try:
                rows = connection.execute(
                    "SELECT e.event_time,e.path,e.status,e.reason,p.requested_action,f.reason_code "
                    "FROM events e LEFT JOIN pending p ON p.path=e.path LEFT JOIN files f ON f.path=e.path "
                    "ORDER BY e.id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return {
                    "locale": locale,
                    "items": [
                        (lambda effective_status: {
                            "time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(row["event_time"])),
                            "path": _display_path(str(row["path"]), show_full_paths) if row["path"] else "",
                            "status": effective_status, "reason": row["reason"],
                            "reason_code": row["reason_code"] or "",
                            "reason_label": REASON_LABELS[locale].get(str(row["reason_code"]), str(row["reason"])),
                            "status_label": STATUS_LABELS[locale].get(effective_status, effective_status),
                        })(
                            "QueuedRepair" if row["requested_action"] == "repair"
                            else "QueuedRecheck" if row["requested_action"] is not None
                            else str(row["status"])
                        )
                        for row in rows
                    ]
                }
            finally:
                connection.close()

        def _tasks_payload(self, query: dict[str, list[str]], locale: str) -> dict[str, Any]:
            try:
                limit = min(200, max(1, int(query.get("limit", ["50"])[0])))
            except ValueError:
                limit = 50
            connection = self._connect()
            if connection is None:
                return {"locale": locale, "items": []}
            try:
                rows = connection.execute(
                    "SELECT id,action,requested_at,started_at,finished_at,state,result_code,result_detail "
                    "FROM control_commands ORDER BY id DESC LIMIT ?", (limit,),
                ).fetchall()
                action_keys = {"reconcile": "task.reconcile", "recheck": "task.recheck", "repair": "task.repair", "retry": "task.retry"}
                state_keys = {"queued": "task.queued", "running": "task.running", "succeeded": "task.succeeded", "failed": "task.failed"}
                items = []
                for row in rows:
                    item = dict(row)
                    item["action_label"] = UI[locale].get(action_keys.get(str(item["action"]), ""), str(item["action"]))
                    item["state_label"] = UI[locale].get(state_keys.get(str(item["state"]), ""), str(item["state"]))
                    if item["result_code"] == "queued" and item["action"] == "reconcile":
                        item["result_detail_label"] = UI[locale]["task.scanCompleted"]
                    elif item["result_code"] == "queued":
                        match = re.search(r"\d+", str(item["result_detail"]))
                        item["result_detail_label"] = UI[locale]["task.filesQueued"].replace("{count}", match.group(0) if match else "0")
                    else:
                        item["result_detail_label"] = str(item["result_detail"])
                    items.append(item)
                return {"locale": locale, "items": items}
            finally:
                connection.close()

        def _file_payload(self, file_id: str, locale: str) -> dict[str, Any] | None:
            connection = self._connect()
            if connection is None:
                return None
            try:
                row = connection.execute(
                    "SELECT f.*,p.requested_action FROM files f LEFT JOIN pending p ON p.path=f.path "
                    "WHERE f.file_id=?", (file_id,),
                ).fetchone()
                if row is None:
                    return None
                item = dict(row)
                item["path"] = _display_path(str(item["path"]), show_full_paths)
                requested_action = item.pop("requested_action")
                effective_status = (
                    "QueuedRepair" if requested_action == "repair"
                    else "QueuedRecheck" if requested_action is not None
                    else str(item["status"])
                )
                item["status_code"] = effective_status
                item["locale"] = locale
                item["status_label"] = STATUS_LABELS[locale].get(effective_status, effective_status)
                item["issue_label"] = ISSUE_LABELS[locale].get(str(item["issue_category"]), ISSUE_LABELS[locale]["other"])
                item["reason_label"] = REASON_LABELS[locale].get(str(item["reason_code"]), str(item["reason"]))
                try:
                    item["diagnostics"] = json.loads(str(item.pop("diagnostics_json")))
                except (TypeError, ValueError, json.JSONDecodeError):
                    item["diagnostics"] = {}
                return item
            finally:
                connection.close()

        def _queue_command(self, action: str, file_ids: list[str]) -> int:
            connection = self._connect(write=True)
            if connection is None:
                raise RuntimeError("状态数据库尚未准备完成")
            try:
                cursor = connection.execute(
                    "INSERT INTO control_commands(action,file_ids_json,requested_at,state) VALUES(?,?,?,'queued')",
                    (action, json.dumps(file_ids[:100], ensure_ascii=False), time.time()),
                )
                connection.commit()
                if wake_callback is not None:
                    wake_callback()
                return int(cursor.lastrowid)
            finally:
                connection.close()

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            try:
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query, keep_blank_values=True)
                locale = normalize_locale(query.get("lang", [""])[0])
                if path in {"/", "/index.html"}:
                    self._send_bytes((static_root / "index.html").read_bytes(), "text/html; charset=utf-8", cache="no-cache")
                elif path == "/assets/style.css":
                    self._send_bytes((static_root / "style.css").read_bytes(), "text/css; charset=utf-8", cache="public,max-age=300")
                elif path == "/assets/app.js":
                    self._send_bytes((static_root / "app.js").read_bytes(), "text/javascript; charset=utf-8", cache="public,max-age=300")
                elif path == "/assets/icon.png":
                    self._send_bytes((static_root / "icon.png").read_bytes(), "image/png", cache="public,max-age=86400")
                elif path == "/assets/favicon.png":
                    self._send_bytes((static_root / "favicon.png").read_bytes(), "image/png", cache="public,max-age=86400")
                elif path == "/api/i18n":
                    self._send_json(catalog(locale))
                elif path == "/api/status":
                    self._send_json(self._status_payload(locale))
                elif path == "/api/files":
                    self._send_json(self._files_payload(query, locale))
                elif path == "/api/history":
                    self._send_json(self._history_payload(query, locale))
                elif path == "/api/tasks":
                    self._send_json(self._tasks_payload(query, locale))
                elif path.startswith("/api/files/"):
                    payload = self._file_payload(path.rsplit("/", 1)[-1], locale)
                    message = "文件记录不存在" if locale == "zh-CN" else "File record not found"
                    self._send_json(payload if payload is not None else {"locale": locale, "error": message}, HTTPStatus.OK if payload else HTTPStatus.NOT_FOUND)
                else:
                    message = "接口不存在" if locale == "zh-CN" else "Endpoint not found"
                    self._send_json({"locale": locale, "error": message}, HTTPStatus.NOT_FOUND)
            except (OSError, sqlite3.Error, ValueError) as exc:
                LOG.exception("Dashboard request failed")
                self._send_json({"error": "dashboard data is temporarily unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            locale = "zh-CN"
            try:
                parsed = urlparse(self.path)
                locale = normalize_locale(parse_qs(parsed.query).get("lang", [""])[0])
                origin = self.headers.get("Origin", "")
                if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
                    message = "拒绝跨站请求" if locale == "zh-CN" else "Cross-origin request rejected"
                    self._send_json({"locale": locale, "error": message}, HTTPStatus.FORBIDDEN)
                    return
                if "application/json" not in self.headers.get("Content-Type", ""):
                    message = "请求必须使用 JSON" if locale == "zh-CN" else "The request must use JSON"
                    self._send_json({"locale": locale, "error": message}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                    return
                length = min(64 * 1024, max(0, int(self.headers.get("Content-Length", "0"))))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                path = parsed.path
                actions = {
                    "/api/actions/reconcile": "reconcile",
                    "/api/files/actions/recheck": "recheck",
                    "/api/files/actions/repair": "repair",
                    "/api/files/actions/retry": "retry",
                }
                action = actions.get(path)
                if action is None:
                    message = "接口不存在" if locale == "zh-CN" else "Endpoint not found"
                    self._send_json({"locale": locale, "error": message}, HTTPStatus.NOT_FOUND)
                    return
                file_ids = [] if action == "reconcile" else [str(v) for v in payload.get("file_ids", []) if str(v)]
                if action != "reconcile" and not file_ids:
                    message = "请选择至少一个文件" if locale == "zh-CN" else "Select at least one file"
                    self._send_json({"locale": locale, "error": message}, HTTPStatus.BAD_REQUEST)
                    return
                if len(file_ids) > 100:
                    message = "单次最多操作 100 个文件" if locale == "zh-CN" else "A single action can include at most 100 files"
                    self._send_json({"locale": locale, "error": message}, HTTPStatus.BAD_REQUEST)
                    return
                command_id = self._queue_command(action, file_ids)
                self._send_json({"locale": locale, "accepted": True, "command_id": command_id}, HTTPStatus.ACCEPTED)
            except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
                LOG.exception("Dashboard control request failed")
                message = "操作暂时不可用" if locale == "zh-CN" else "The action is temporarily unavailable"
                self._send_json({"locale": locale, "error": message}, HTTPStatus.SERVICE_UNAVAILABLE)

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="timestamp-repair-web", daemon=True)
    thread.start()
    LOG.warning("Anonymous Web UI listening on http://%s:%s; do not expose it directly to the Internet", host, server.server_address[1])
    return WebService(server=server, thread=thread)
