#!/usr/bin/env python3
"""Continuously detect and repair broken H.264 B-frame timestamps in MP4 files."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from web_ui import WebService, start_web_server

try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - covered by container integration
    FileSystemEvent = Any  # type: ignore[assignment,misc]
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", "/media"))
CONFIG_ROOT = Path(os.getenv("CONFIG_ROOT", "/config"))
WORK_ROOT = Path(os.getenv("WORK_ROOT", "/work"))
NAME_CONTAINS = os.getenv("NAME_CONTAINS", "")
AUTO_REPAIR = env_bool("AUTO_REPAIR", False)
REPAIR_EMPTY_FULL_CHAPTERS = env_bool("REPAIR_EMPTY_FULL_CHAPTERS", True)
MIN_FILE_AGE = max(0, int(os.getenv("MIN_FILE_AGE_SECONDS", "3600")))
FILE_SETTLE_SECONDS = max(1, int(os.getenv("FILE_SETTLE_SECONDS", "60")))
RECONCILE_LOCAL_TIME = os.getenv("RECONCILE_LOCAL_TIME", "04:00").strip()
SAMPLE_SECONDS = max(3, int(os.getenv("SAMPLE_SECONDS", "8")))
MINIMUM_PACKETS = max(20, int(os.getenv("MINIMUM_PACKETS", "60")))
RETRY_FAILED_AFTER = max(300, int(os.getenv("RETRY_FAILED_AFTER_SECONDS", "86400")))
KEEP_TEMP_ON_FAILURE = env_bool("KEEP_TEMP_ON_FAILURE", False)
EVENT_HISTORY_LIMIT = max(100, int(os.getenv("EVENT_HISTORY_LIMIT", "5000")))
FFMPEG = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_BIN", "ffprobe")
MP4BOX = os.getenv("MP4BOX_BIN", "MP4Box")
WEB_UI_ENABLED = env_bool("WEB_UI_ENABLED", True)
WEB_UI_HOST = os.getenv("WEB_UI_HOST", "0.0.0.0")
WEB_UI_PORT = min(65535, max(1, int(os.getenv("WEB_UI_PORT", "8080"))))
WEB_SHOW_FULL_PATHS = env_bool("WEB_SHOW_FULL_PATHS", False)
ANALYSIS_SIGNATURE = f"3:{SAMPLE_SECONDS}:{MINIMUM_PACKETS}:{int(REPAIR_EMPTY_FULL_CHAPTERS)}"
LEGACY_ENV_NAMES = ("SCAN_INTERVAL_SECONDS", "STABLE_SCANS", "WEB_USERNAME", "WEB_PASSWORD")
MAX_ERROR_LENGTH = 4096

STOP_REQUESTED = False
CURRENT_CHILD: subprocess.Popen[str] | None = None
WAKE_EVENT = threading.Event()
INTERNAL_IDENTITIES: dict[str, tuple[tuple[int, ...], float]] = {}
INTERNAL_IDENTITIES_LOCK = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
LOG = logging.getLogger("timestamp-repair")


@dataclass
class Analysis:
    status: str
    reason: str
    info: dict[str, Any]
    video: dict[str, Any] | None
    comparable: int = 0
    different: int = 0
    timestamp_issue: bool = False
    chapter_issue: bool = False


class RepairValidationError(RuntimeError):
    """The rebuilt file is unsafe or ineffective, so retrying is not useful."""


def request_stop(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    WAKE_EVENT.set()
    LOG.warning("Stop requested by signal %s; finishing the current safe operation.", signum)


def run_capture(args: list[str], label: str) -> str:
    proc = subprocess.run(args, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{label} failed (exit {proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def run_logged(args: list[str], log_base: Path, label: str) -> None:
    global CURRENT_CHILD
    stdout_path = log_base.with_suffix(".stdout.log")
    stderr_path = log_base.with_suffix(".stderr.log")
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        CURRENT_CHILD = subprocess.Popen(args, text=True, stdout=stdout, stderr=stderr)
        code = CURRENT_CHILD.wait()
        CURRENT_CHILD = None
    if code != 0:
        with stderr_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            length = handle.tell()
            handle.seek(max(0, length - 16 * 1024))
            details = handle.read().decode("utf-8", errors="replace")
        details = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", details).strip()
        if length > 16 * 1024:
            details = "[earlier tool output omitted]\n" + details
        raise RuntimeError(f"{label} failed (exit {code}): {details}"[:MAX_ERROR_LENGTH])


def media_info(path: Path) -> dict[str, Any]:
    return json.loads(
        run_capture(
            [
                FFPROBE, "-v", "error", "-show_streams", "-show_chapters",
                "-show_format", "-of", "json", str(path),
            ],
            "ffprobe stream inspection",
        )
    )


def primary_videos(info: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for stream in info.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        if int(stream.get("disposition", {}).get("attached_pic", 0) or 0) != 0:
            continue
        result.append(stream)
    return result


def fraction(value: Any) -> float:
    try:
        numerator, denominator = str(value).split("/", 1)
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return 0.0


def has_empty_full_duration_chapter(info: dict[str, Any]) -> bool:
    """Return true only for one untitled chapter that spans the complete file."""
    if not REPAIR_EMPTY_FULL_CHAPTERS:
        return False
    chapters = info.get("chapters") or []
    if len(chapters) != 1:
        return False
    try:
        duration = float(info.get("format", {}).get("duration", 0.0) or 0.0)
        start = float(chapters[0].get("start_time", 0.0) or 0.0)
        end = float(chapters[0].get("end_time", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    if duration <= 0:
        return False
    title = str((chapters[0].get("tags") or {}).get("title", "")).strip()
    tolerance = max(0.1, duration * 0.00001)
    return not title and abs(start) <= 0.05 and abs(end - duration) <= tolerance


def timestamp_sample(path: Path, duration: float) -> tuple[int, int]:
    positions: list[float] = []
    for candidate in (0.0, max(0.0, duration / 2.0), max(0.0, duration - SAMPLE_SECONDS - 2.0)):
        rounded = round(candidate, 3)
        if rounded not in positions:
            positions.append(rounded)

    comparable = 0
    different = 0
    for position in positions:
        interval = f"{position:.3f}%+{SAMPLE_SECONDS}"
        output = run_capture(
            [
                FFPROBE,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-read_intervals",
                interval,
                "-show_packets",
                "-show_entries",
                "packet=pts,dts",
                "-of",
                "json",
                str(path),
            ],
            "ffprobe packet sampling",
        )
        for packet in json.loads(output).get("packets", []):
            if packet.get("pts") is None or packet.get("dts") is None:
                continue
            comparable += 1
            if str(packet["pts"]) != str(packet["dts"]):
                different += 1
    return comparable, different


def analyze(path: Path) -> Analysis:
    info = media_info(path)
    videos = primary_videos(info)
    chapter_issue = has_empty_full_duration_chapter(info)
    if not videos:
        return Analysis("Skipped", "No primary video stream", info, None)
    video = videos[0]
    comparable = different = 0
    timestamp_issue = False
    timestamp_reason = ""
    if video.get("codec_name") == "h264" and int(video.get("has_b_frames", 0) or 0) > 0:
        duration = float(info.get("format", {}).get("duration", 0.0) or 0.0)
        comparable, different = timestamp_sample(path, duration)
        if comparable < MINIMUM_PACKETS:
            timestamp_reason = "Too few comparable packet timestamps"
        elif different == 0:
            timestamp_issue = True

    problems = []
    if timestamp_issue:
        problems.append("H.264 B-frame composition timestamps are missing")
    if chapter_issue:
        problems.append("A single untitled chapter spans the full file")
    if problems:
        return Analysis(
            "Candidate", "; ".join(problems), info, video, comparable, different,
            timestamp_issue, chapter_issue,
        )
    if timestamp_reason:
        return Analysis("Uncertain", timestamp_reason, info, video, comparable, different)
    if video.get("codec_name") != "h264":
        return Analysis("Skipped", "Primary video is not H.264 and no chapter issue was found", info, video)
    if int(video.get("has_b_frames", 0) or 0) <= 0:
        return Analysis("Healthy", "H.264 stream does not declare B-frames", info, video)
    return Analysis("Healthy", "Composition timestamps are present", info, video, comparable, different)


def compatibility_reason(analysis: Analysis) -> str:
    videos = primary_videos(analysis.info)
    if len(videos) != 1:
        return "Exactly one primary video stream is required"
    if analysis.timestamp_issue:
        avg = fraction(analysis.video.get("avg_frame_rate") if analysis.video else None)
        nominal = fraction(analysis.video.get("r_frame_rate") if analysis.video else None)
        if avg <= 0 or nominal <= 0 or abs(avg - nominal) > max(0.001, nominal * 0.005):
            return "Variable or ambiguous frame rate is not repaired automatically"
    bad_subtitles = [
        stream
        for stream in analysis.info.get("streams", [])
        if stream.get("codec_type") == "subtitle" and stream.get("codec_name") != "mov_text"
    ]
    if bad_subtitles:
        return "Non-mov_text subtitles cannot be safely copied into MP4"
    return ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest().upper()


def assert_free_space(input_size: int) -> None:
    required = int(input_size * (3.2 if KEEP_TEMP_ON_FAILURE else 2.25) + 1024**3)
    free = shutil.disk_usage(WORK_ROOT).free
    if free < required:
        raise RuntimeError(
            f"Insufficient work space: need about {required / 1024**3:.1f} GiB, "
            f"available {free / 1024**3:.1f} GiB"
        )


def compare_streams(original: Analysis, fixed: Analysis) -> None:
    if original.timestamp_issue and (fixed.timestamp_issue or fixed.different <= 0):
        raise RepairValidationError(f"Timestamp repair validation failed: {fixed.reason}")
    if original.chapter_issue and has_empty_full_duration_chapter(fixed.info):
        raise RepairValidationError("Invalid full-duration chapter remains after repair")
    assert original.video is not None and fixed.video is not None
    for key in ("codec_name", "profile", "width", "height", "pix_fmt"):
        if str(original.video.get(key)) != str(fixed.video.get(key)):
            raise RepairValidationError(f"Video property changed unexpectedly: {key}")
    before_frames = original.video.get("nb_frames")
    after_frames = fixed.video.get("nb_frames")
    if before_frames and after_frames and int(before_frames) != int(after_frames):
        raise RepairValidationError(f"Video frame count changed: {before_frames} -> {after_frames}")

    before_audio = [s for s in original.info.get("streams", []) if s.get("codec_type") == "audio"]
    after_audio = [s for s in fixed.info.get("streams", []) if s.get("codec_type") == "audio"]
    if len(before_audio) != len(after_audio):
        raise RepairValidationError("Audio stream count changed unexpectedly")
    for index, (before, after) in enumerate(zip(before_audio, after_audio)):
        for key in ("codec_name", "sample_rate", "channels"):
            if str(before.get(key)) != str(after.get(key)):
                raise RepairValidationError(f"Audio stream {index} property changed unexpectedly: {key}")

    before_subtitles = [s for s in original.info.get("streams", []) if s.get("codec_type") == "subtitle"]
    after_subtitles = [s for s in fixed.info.get("streams", []) if s.get("codec_type") == "subtitle"]
    if len(before_subtitles) != len(after_subtitles):
        raise RepairValidationError("Subtitle stream count changed unexpectedly")
    for index, (before, after) in enumerate(zip(before_subtitles, after_subtitles)):
        if str(before.get("codec_name")) != str(after.get("codec_name")):
            raise RepairValidationError(f"Subtitle stream {index} codec changed unexpectedly")

    before_chapters = original.info.get("chapters") or []
    after_chapters = fixed.info.get("chapters") or []
    if original.chapter_issue:
        if after_chapters:
            raise RepairValidationError("Chapters remain after invalid chapter removal")
    elif len(before_chapters) != len(after_chapters):
        raise RepairValidationError("Valid chapter count changed unexpectedly")


def validate_decode(repaired: Path, duration: float, job: Path) -> None:
    for index, position in enumerate((0.0, max(0.0, duration / 2.0), max(0.0, duration - 5.0))):
        run_logged(
            [
                FFMPEG,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{position:.3f}",
                "-i",
                str(repaired),
                "-map",
                "0:v:0",
                "-t",
                "3",
                "-an",
                "-sn",
                "-f",
                "null",
                "-",
            ],
            job / f"decode-{index}",
            f"decode validation {index}",
        )


def safe_replace(original: Path, repaired: Path, original_analysis: Analysis) -> str:
    required_media_space = repaired.stat().st_size + 256 * 1024 * 1024
    media_free = shutil.disk_usage(original.parent).free
    if media_free < required_media_space:
        raise RuntimeError(
            f"Insufficient media volume space: need about {required_media_space / 1024**3:.1f} GiB, "
            f"available {media_free / 1024**3:.1f} GiB"
        )
    token = uuid.uuid4().hex
    stage = original.parent / f"MediaRepairStage-{token}.mp4"
    backup = original.parent / f"MediaRepairBackup-{token}.mp4"
    original_stat = original.stat()
    installed = False
    try:
        shutil.copyfile(repaired, stage)
        shutil.copystat(original, stage, follow_symlinks=True)
        try:
            os.chown(stage, original_stat.st_uid, original_stat.st_gid)
        except (PermissionError, AttributeError):
            pass
        # fsync requires a descriptor opened for writing on some platforms.
        with stage.open("rb+") as handle:
            os.fsync(handle.fileno())
        if stage.stat().st_size != repaired.stat().st_size:
            raise RuntimeError("Staged file length does not match repaired file")
        local_hash = sha256(repaired)
        if sha256(stage) != local_hash:
            raise RuntimeError("Staged file SHA-256 does not match repaired file")

        os.replace(original, backup)
        try:
            os.replace(stage, original)
            installed = True
            final = analyze(original)
            compare_streams(original_analysis, final)
        except Exception:
            if installed and original.exists():
                original.unlink()
            if backup.exists():
                os.replace(backup, original)
            raise
        backup.unlink()
        return local_hash
    finally:
        if stage.exists():
            stage.unlink()


def repair_one(path: Path, original: Analysis) -> tuple[str, int]:
    reason = compatibility_reason(original)
    if reason:
        raise RuntimeError(reason)
    size = path.stat().st_size
    assert_free_space(size)
    job = Path(tempfile.mkdtemp(prefix="job-", dir=WORK_ROOT))
    raw = job / "video.h264"
    video_only = job / "video-fixed.mp4"
    repaired = job / "repaired.mp4"
    success = False
    try:
        if original.timestamp_issue:
            nominal_fps = str(original.video.get("r_frame_rate") or original.video.get("avg_frame_rate"))
            LOG.info("Extracting H.264 without re-encoding: %s", path)
            run_logged(
                [
                    FFMPEG,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-y",
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-c:v",
                    "copy",
                    "-bsf:v",
                    "h264_mp4toannexb",
                    "-f",
                    "h264",
                    str(raw),
                ],
                job / "extract",
                "H.264 extraction",
            )
            LOG.info("Rebuilding composition timestamps: %s", path)
            run_logged(
                [MP4BOX, "-tmp", str(job), "-add", f"{raw}:fps={nominal_fps}", "-new", str(video_only)],
                job / "mp4box",
                "MP4Box timestamp rebuild",
            )
            if not KEEP_TEMP_ON_FAILURE:
                raw.unlink(missing_ok=True)

        LOG.info(
            "%s: %s",
            "Removing invalid full-duration chapter" if original.chapter_issue else "Preserving valid chapters",
            path,
        )
        remux = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "warning", "-y"]
        if original.timestamp_issue:
            remux.extend([
                "-i", str(video_only), "-i", str(path),
                "-map", "0:v:0", "-map", "1:a?", "-map", "1:s?",
                "-map_metadata", "1", "-map_metadata:s:v:0", "1:s:v:0",
                "-map_chapters", "-1" if original.chapter_issue else "1",
            ])
        else:
            remux.extend([
                "-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
                "-map_metadata", "0", "-map_metadata:s:v:0", "0:s:v:0",
                "-map_chapters", "-1" if original.chapter_issue else "0",
            ])
        remux.extend([
            "-c", "copy", "-movflags", "+faststart", "-avoid_negative_ts", "disabled", str(repaired),
        ])
        run_logged(remux, job / "remux", "final remux")
        fixed = analyze(repaired)
        compare_streams(original, fixed)
        duration = float(fixed.info.get("format", {}).get("duration", 0.0) or 0.0)
        validate_decode(repaired, duration, job)
        final_hash = safe_replace(path, repaired, original)
        success = True
        dropped_data = sum(1 for s in original.info.get("streams", []) if s.get("codec_type") == "data")
        LOG.info("Repaired and replaced: %s SHA256=%s", path, final_hash)
        return final_hash, dropped_data
    finally:
        if success or not KEEP_TEMP_ON_FAILURE:
            shutil.rmtree(job, ignore_errors=True)
        else:
            LOG.warning("Temporary failure files kept at %s", job)


class State:
    TERMINAL = {"Healthy", "Skipped", "Uncertain", "Repaired"}

    def __init__(self, path: Path):
        self.path = path
        migrate_legacy_state(path)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        create_schema(self.db)

    def close(self) -> None:
        self.db.close()

    def get(self, path: Path) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM files WHERE path = ?", (str(path),)).fetchone()

    def all_files(self) -> dict[str, sqlite3.Row]:
        return {str(row["path"]): row for row in self.db.execute("SELECT * FROM files")}

    def save(
        self,
        path: Path,
        stat: os.stat_result,
        status: str,
        reason: str,
        comparable: int = 0,
        different: int = 0,
        dropped_data: int = 0,
        final_hash: str = "",
    ) -> None:
        reason = compact_error(reason)
        self.db.execute(
            """
            INSERT INTO files(
              path,size,mtime_ns,ctime_ns,device,inode,status,reason,checked_at,
              comparable,different,dropped_data,sha256,analysis_signature
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              size=excluded.size,mtime_ns=excluded.mtime_ns,ctime_ns=excluded.ctime_ns,
              device=excluded.device,inode=excluded.inode,status=excluded.status,
              reason=excluded.reason,checked_at=excluded.checked_at,
              comparable=excluded.comparable,different=excluded.different,
              dropped_data=excluded.dropped_data,sha256=excluded.sha256,
              analysis_signature=excluded.analysis_signature
            """,
            (
                str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                stat.st_dev, stat.st_ino, status, reason, time.time(), comparable,
                different, dropped_data, final_hash, ANALYSIS_SIGNATURE,
            ),
        )
        self.db.commit()

    def remove(self, path: Path) -> None:
        value = str(path)
        self.db.execute("DELETE FROM pending WHERE path = ?", (value,))
        self.db.execute("DELETE FROM files WHERE path = ?", (value,))
        self.db.commit()

    def remove_tree(self, path: Path) -> None:
        prefix = str(path).rstrip("/\\") + os.sep
        for table in ("pending", "files"):
            self.db.execute(
                f"DELETE FROM {table} WHERE substr(path,1,?)=?",
                (len(prefix), prefix),
            )
        self.db.commit()

    def enqueue(self, path: Path, kind: str, eligible_at: float, force: bool) -> None:
        now = time.time()
        self.db.execute(
            """
            INSERT INTO pending(path,event_kind,queued_at,eligible_at,force,attempts)
            VALUES(?,?,?,?,?,0)
            ON CONFLICT(path) DO UPDATE SET
              event_kind=excluded.event_kind,queued_at=excluded.queued_at,
              eligible_at=excluded.eligible_at,force=MAX(pending.force,excluded.force)
            """,
            (str(path), kind, now, eligible_at, int(force)),
        )
        self.db.commit()
        WAKE_EVENT.set()

    def defer(self, path: Path, eligible_at: float, *, increment: bool = False) -> None:
        self.db.execute(
            "UPDATE pending SET eligible_at=?, attempts=attempts+? WHERE path=?",
            (eligible_at, int(increment), str(path)),
        )
        self.db.commit()

    def complete(self, path: Path) -> None:
        self.db.execute("DELETE FROM pending WHERE path = ?", (str(path),))
        self.db.commit()

    def due(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM pending WHERE eligible_at <= ? ORDER BY eligible_at,path LIMIT ?",
            (time.time(), limit),
        ).fetchall()

    def pending_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM pending").fetchone()[0])

    def next_due(self) -> float | None:
        value = self.db.execute("SELECT MIN(eligible_at) FROM pending").fetchone()[0]
        return float(value) if value is not None else None

    def record(self, path: Path, status: str, reason: str) -> None:
        self.db.execute(
            "INSERT INTO events(event_time,path,status,reason) VALUES(?,?,?,?)",
            (time.time(), str(path), status, compact_error(reason)),
        )
        self.db.execute(
            "DELETE FROM events WHERE id IN "
            "(SELECT id FROM events ORDER BY id DESC LIMIT -1 OFFSET ?)",
            (EVENT_HISTORY_LIMIT,),
        )
        self.db.commit()

    def meta_get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.db.commit()

    def transfer_rename(self, source: Path, destination: Path) -> bool:
        row = self.get(source)
        if row is None or not destination.exists():
            self.remove(source)
            return False
        stat = destination.stat()
        same_file = (
            row["device"] == stat.st_dev and row["inode"] == stat.st_ino
            and row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns
            and row["analysis_signature"] == ANALYSIS_SIGNATURE
            and cacheable_status(str(row["status"]))
        )
        if not same_file:
            self.remove(source)
            return False
        self.db.execute("DELETE FROM pending WHERE path IN (?,?)", (str(source), str(destination)))
        self.db.execute("DELETE FROM files WHERE path=?", (str(destination),))
        self.db.execute(
            "UPDATE files SET path=?,ctime_ns=? WHERE path=?",
            (str(destination), stat.st_ctime_ns, str(source)),
        )
        self.db.commit()
        return True


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS files (
          path TEXT PRIMARY KEY,
          size INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL,
          ctime_ns INTEGER NOT NULL,
          device INTEGER NOT NULL,
          inode INTEGER NOT NULL,
          status TEXT NOT NULL,
          reason TEXT NOT NULL,
          checked_at REAL NOT NULL,
          comparable INTEGER NOT NULL DEFAULT 0,
          different INTEGER NOT NULL DEFAULT 0,
          dropped_data INTEGER NOT NULL DEFAULT 0,
          sha256 TEXT NOT NULL DEFAULT '',
          analysis_signature TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_files_status ON files(status);
        CREATE INDEX IF NOT EXISTS idx_files_checked ON files(checked_at DESC);
        CREATE TABLE IF NOT EXISTS pending (
          path TEXT PRIMARY KEY,
          event_kind TEXT NOT NULL,
          queued_at REAL NOT NULL,
          eligible_at REAL NOT NULL,
          force INTEGER NOT NULL DEFAULT 0,
          attempts INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_pending_due ON pending(eligible_at);
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_time REAL NOT NULL,
          path TEXT NOT NULL,
          status TEXT NOT NULL,
          reason TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_events_time ON events(event_time DESC);
        CREATE TABLE IF NOT EXISTS metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )
    connection.commit()


def compact_error(value: str) -> str:
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value)).strip()
    if len(value) <= MAX_ERROR_LENGTH:
        return value
    return "[error output truncated]\n" + value[-(MAX_ERROR_LENGTH - 25):]


def migrate_legacy_state(path: Path) -> None:
    if not path.exists():
        return
    source = sqlite3.connect(path)
    source.row_factory = sqlite3.Row
    try:
        columns = {str(row[1]) for row in source.execute("PRAGMA table_info(files)")}
        if "analysis_signature" in columns:
            return
        temporary = path.with_name("state-v2.sqlite3.tmp")
        temporary.unlink(missing_ok=True)
        target = sqlite3.connect(temporary)
        target.row_factory = sqlite3.Row
        create_schema(target)
        now = time.time()
        rows = source.execute(
            "SELECT path,size,mtime_ns,status,"
            "CASE WHEN status='Failed' THEN '' ELSE substr(reason,1,4096) END AS reason,"
            "checked_at,comparable,different,dropped_data,sha256 FROM files"
        )
        migrated = queued = 0
        for row in rows:
            media = Path(str(row["path"]))
            try:
                stat = media.stat()
            except OSError:
                continue
            unchanged = stat.st_size == row["size"] and stat.st_mtime_ns == row["mtime_ns"]
            status = str(row["status"])
            if unchanged and cacheable_status(status):
                target.execute(
                    "INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(media), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                        stat.st_dev, stat.st_ino, status, compact_error(str(row["reason"])),
                        float(row["checked_at"]), int(row["comparable"]), int(row["different"]),
                        int(row["dropped_data"]), str(row["sha256"]), ANALYSIS_SIGNATURE,
                    ),
                )
                migrated += 1
            else:
                target.execute(
                    "INSERT INTO pending VALUES(?,?,?,?,?,?)",
                    (str(media), "migration-retry", now, now, 1, 0),
                )
                queued += 1
        target.execute(
            "INSERT INTO events(event_time,path,status,reason) VALUES(?,?,?,?)",
            (now, "", "Migration", f"Imported {migrated} cached records; queued {queued} files"),
        )
        target.execute("INSERT INTO metadata VALUES('schema_version','2')")
        target.commit()
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"New state database failed integrity check: {integrity}")
        target.close()
    except Exception:
        temporary = path.with_name("state-v2.sqlite3.tmp")
        temporary.unlink(missing_ok=True)
        raise
    finally:
        source.close()

    os.replace(temporary, path)
    for legacy in (
        Path(str(path) + "-wal"), Path(str(path) + "-shm"),
        path.parent / "history.jsonl", path.parent / "latest.csv",
        path.parent / "latest.tmp", path.parent / "web-password.txt",
    ):
        legacy.unlink(missing_ok=True)


def stat_identity(stat: os.stat_result) -> tuple[int, ...]:
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def remember_internal_change(path: Path, stat: os.stat_result) -> None:
    with INTERNAL_IDENTITIES_LOCK:
        INTERNAL_IDENTITIES[str(path)] = (stat_identity(stat), time.time() + 300)


def is_expected_internal(path: Path) -> bool:
    key = str(path)
    with INTERNAL_IDENTITIES_LOCK:
        entry = INTERNAL_IDENTITIES.get(key)
        if entry is None:
            return False
        expected, expires = entry
        if time.time() > expires:
            INTERNAL_IDENTITIES.pop(key, None)
            return False
    try:
        current = stat_identity(path.stat())
    except OSError:
        return True
    if current == expected:
        return True
    with INTERNAL_IDENTITIES_LOCK:
        INTERNAL_IDENTITIES.pop(key, None)
    return False


def cacheable_status(status: str) -> bool:
    return status in State.TERMINAL or (status == "Candidate" and not AUTO_REPAIR)


def cache_matches(row: sqlite3.Row, stat: os.stat_result) -> bool:
    return (
        cacheable_status(str(row["status"]))
        and row["analysis_signature"] == ANALYSIS_SIGNATURE
        and row["size"] == stat.st_size
        and row["mtime_ns"] == stat.st_mtime_ns
        and row["ctime_ns"] == stat.st_ctime_ns
        and row["device"] == stat.st_dev
        and row["inode"] == stat.st_ino
    )


def matches_media(path: Path) -> bool:
    name = path.name
    if not name.lower().endswith(".mp4"):
        return False
    if name.startswith(("TimestampRepairStage-", "TimestampRepairBackup-")):
        return False
    return not NAME_CONTAINS or NAME_CONTAINS.casefold() in name.casefold()


def enumerate_media(root: Path = MEDIA_ROOT) -> list[Path]:
    files: list[Path] = []
    for current, directories, names in os.walk(root):
        directories[:] = [d for d in directories if d not in {"@eaDir", "#recycle", ".Trash"}]
        for name in names:
            candidate = Path(current) / name
            if matches_media(candidate):
                files.append(candidate)
    return files


@dataclass(frozen=True)
class WatchEvent:
    kind: str
    source: Path
    destination: Path | None = None
    is_directory: bool = False


class IncrementalEventHandler(FileSystemEventHandler):
    def __init__(self, events: queue.Queue[WatchEvent]):
        super().__init__()
        self.events = events

    def _put(self, kind: str, event: FileSystemEvent, destination: str | None = None) -> None:
        self.events.put(WatchEvent(kind, Path(event.src_path), Path(destination) if destination else None, event.is_directory))
        WAKE_EVENT.set()

    def on_created(self, event: FileSystemEvent) -> None:
        self._put("created", event)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._put("modified", event)

    def on_closed(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._put("closed", event)

    def on_deleted(self, event: FileSystemEvent) -> None:
        self._put("deleted", event)

    def on_moved(self, event: FileSystemEvent) -> None:
        self._put("moved", event, getattr(event, "dest_path", None))


class Runtime:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.watcher_active = False
        self.watcher_error = ""
        self.current_path = ""
        self.current_action = "idle"
        self.last_reconcile = 0.0
        self.next_reconcile = 0.0
        self.processed_session = 0
        self.pending_count = 0

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "pid": os.getpid(), "time": time.time(), "auto_repair": AUTO_REPAIR,
                "watcher_active": self.watcher_active, "watcher_error": self.watcher_error,
                "current_path": self.current_path, "current_action": self.current_action,
                "last_reconcile": self.last_reconcile, "next_reconcile": self.next_reconcile,
                "processed_session": self.processed_session, "pending_count": self.pending_count,
            }


def write_heartbeat(runtime: Runtime) -> None:
    target = CONFIG_ROOT / "heartbeat.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(runtime.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def heartbeat_loop(runtime: Runtime) -> None:
    while not runtime.stop_event.is_set():
        try:
            write_heartbeat(runtime)
        except OSError:
            LOG.exception("Unable to write heartbeat")
        runtime.stop_event.wait(60)
    try:
        write_heartbeat(runtime)
    except OSError:
        pass


def parse_reconcile_time(value: str) -> tuple[int, int]:
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise RuntimeError("RECONCILE_LOCAL_TIME must use HH:MM") from exc
    return parsed.hour, parsed.minute


def next_reconcile_time(now: datetime | None = None) -> float:
    now = now or datetime.now().astimezone()
    hour, minute = parse_reconcile_time(RECONCILE_LOCAL_TIME)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def reconcile(state: State, runtime: Runtime) -> None:
    started = time.time()
    with runtime.lock:
        runtime.current_action = "reconcile"
        runtime.current_path = ""
    cached = state.all_files()
    seen: set[str] = set()
    queued = skipped = 0
    for path in enumerate_media():
        if STOP_REQUESTED:
            break
        value = str(path)
        seen.add(value)
        try:
            stat = path.stat()
        except OSError:
            continue
        row = cached.get(value)
        if row is not None and cache_matches(row, stat):
            skipped += 1
            continue
        state.enqueue(path, "reconcile", time.time(), False)
        queued += 1
    for value in set(cached) - seen:
        state.remove(Path(value))
    state.meta_set("last_reconcile", str(started))
    with runtime.lock:
        runtime.last_reconcile = started
        runtime.next_reconcile = next_reconcile_time()
        runtime.current_action = "idle"
        runtime.pending_count = state.pending_count()
    LOG.info("Metadata reconciliation complete: cached=%d queued=%d", skipped, queued)


def drain_events(state: State, events: queue.Queue[WatchEvent]) -> None:
    while True:
        try:
            event = events.get_nowait()
        except queue.Empty:
            return
        if not event.is_directory and (is_expected_internal(event.source) or (event.destination is not None and is_expected_internal(event.destination))):
            continue
        if event.kind == "deleted":
            if event.is_directory:
                state.remove_tree(event.source)
            else:
                state.remove(event.source)
            continue
        if event.kind == "moved" and event.destination is not None:
            if event.is_directory:
                for media in enumerate_media(event.destination):
                    state.enqueue(media, "directory-moved", time.time() + FILE_SETTLE_SECONDS, True)
                continue
            if matches_media(event.source):
                if matches_media(event.destination) and state.transfer_rename(event.source, event.destination):
                    continue
                state.remove(event.source)
            if matches_media(event.destination):
                state.enqueue(event.destination, "moved", time.time() + FILE_SETTLE_SECONDS, True)
            continue
        if event.is_directory:
            if event.kind == "created" and event.source.exists():
                for media in enumerate_media(event.source):
                    state.enqueue(media, "directory-created", time.time() + FILE_SETTLE_SECONDS, True)
            continue
        if matches_media(event.source):
            state.enqueue(event.source, event.kind, time.time() + FILE_SETTLE_SECONDS, True)


def process_pending(state: State, runtime: Runtime, row: sqlite3.Row) -> None:
    path = Path(str(row["path"]))
    with runtime.lock:
        runtime.current_action = "process"
        runtime.current_path = str(path)
    try:
        stat = path.stat()
    except OSError:
        state.remove(path)
        return
    cached = state.get(path)
    if not bool(row["force"]) and cached is not None and cache_matches(cached, stat):
        state.complete(path)
        return
    eligible = max(float(row["eligible_at"]), stat.st_mtime + MIN_FILE_AGE)
    if time.time() < eligible:
        state.defer(path, eligible)
        return
    try:
        LOG.info("Inspecting changed file: %s", path)
        result = analyze(path)
        dropped_data = sum(1 for stream in result.info.get("streams", []) if stream.get("codec_type") == "data")
        final_hash = ""
        if result.status == "Candidate" and AUTO_REPAIR:
            incompatible = compatibility_reason(result)
            if incompatible:
                result = Analysis(
                    "Uncertain", incompatible, result.info, result.video,
                    result.comparable, result.different,
                )
            else:
                try:
                    repaired_timestamp = result.timestamp_issue
                    repaired_chapter = result.chapter_issue
                    final_hash, dropped_data = repair_one(path, result)
                except RepairValidationError as exc:
                    result = Analysis(
                        "Uncertain", compact_error(str(exc)), result.info, result.video,
                        result.comparable, result.different,
                    )
                else:
                    stat = path.stat()
                    remember_internal_change(path, stat)
                    completed = []
                    if repaired_timestamp:
                        completed.append("composition timestamps rebuilt")
                    if repaired_chapter:
                        completed.append("invalid full-duration chapter removed")
                    result = Analysis(
                        "Repaired",
                        "; ".join(completed)
                        + "; streams copied without re-encoding and original replaced after validation",
                        result.info, result.video, result.comparable, result.different,
                    )
        state.save(path, stat, result.status, result.reason, result.comparable, result.different, dropped_data, final_hash)
        state.record(path, result.status, result.reason)
        state.complete(path)
        with runtime.lock:
            runtime.processed_session += 1
    except Exception as exc:
        reason = compact_error(str(exc))
        LOG.exception("Failed while processing %s", path)
        try:
            state.save(path, path.stat(), "Failed", reason)
            state.record(path, "Failed", reason)
            state.defer(path, time.time() + RETRY_FAILED_AFTER, increment=True)
        except OSError:
            pass
    finally:
        with runtime.lock:
            runtime.current_path = ""
            runtime.current_action = "idle"
            runtime.pending_count = state.pending_count()


def healthcheck() -> int:
    heartbeat = CONFIG_ROOT / "heartbeat.json"
    try:
        data = json.loads(heartbeat.read_text(encoding="utf-8"))
        return 0 if time.time() - float(data["time"]) <= 600 else 1
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return 1


def acquire_lock() -> int:
    lock_path = CONFIG_ROOT / "service.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        import fcntl
    except ImportError:  # Windows development and tests; production images are Linux
        fcntl = None
    if fcntl is not None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise RuntimeError("Another repair service instance is already running") from exc
    os.ftruncate(descriptor, 0)
    os.write(descriptor, str(os.getpid()).encode("ascii"))
    return descriptor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Reconcile and process currently eligible files, then exit")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        return healthcheck()

    for directory in (CONFIG_ROOT, WORK_ROOT):
        directory.mkdir(parents=True, exist_ok=True)
    if not MEDIA_ROOT.is_dir():
        raise RuntimeError(f"MEDIA_ROOT is not a directory: {MEDIA_ROOT}")
    for tool in (FFMPEG, FFPROBE, MP4BOX):
        if not shutil.which(tool):
            raise RuntimeError(f"Required tool not found: {tool}")
    parse_reconcile_time(RECONCILE_LOCAL_TIME)
    for name in LEGACY_ENV_NAMES:
        if os.getenv(name) is not None:
            LOG.warning("Legacy setting %s is ignored by version 2.0", name)

    lock_descriptor = acquire_lock()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    state = State(CONFIG_ROOT / "state.sqlite3")
    runtime = Runtime()
    runtime.last_reconcile = float(state.meta_get("last_reconcile", "0") or 0)
    runtime.next_reconcile = next_reconcile_time()
    events: queue.Queue[WatchEvent] = queue.Queue()
    observer = None
    if Observer is None:
        runtime.watcher_error = "python watchdog is unavailable"
    else:
        try:
            observer = Observer()
            observer.schedule(IncrementalEventHandler(events), str(MEDIA_ROOT), recursive=True)
            observer.start()
            runtime.watcher_active = True
        except Exception as exc:
            runtime.watcher_error = compact_error(str(exc))
            LOG.exception("Filesystem watcher unavailable; daily reconciliation remains active")

    heartbeat_thread = threading.Thread(target=heartbeat_loop, args=(runtime,), name="heartbeat", daemon=True)
    heartbeat_thread.start()
    web_service: WebService | None = None
    if WEB_UI_ENABLED:
        try:
            web_service = start_web_server(
                config_root=CONFIG_ROOT, host=WEB_UI_HOST, port=WEB_UI_PORT,
                show_full_paths=WEB_SHOW_FULL_PATHS,
                settings={
                    "auto_repair": AUTO_REPAIR,
                    "repair_empty_full_chapters": REPAIR_EMPTY_FULL_CHAPTERS,
                    "file_settle_seconds": FILE_SETTLE_SECONDS,
                    "min_file_age_seconds": MIN_FILE_AGE,
                    "reconcile_local_time": RECONCILE_LOCAL_TIME,
                    "name_filter_enabled": bool(NAME_CONTAINS),
                    "show_full_paths": WEB_SHOW_FULL_PATHS,
                },
            )
        except Exception:
            LOG.exception("Web UI failed to start; repair service will continue without it")

    LOG.info(
        "Service 2.1 started: media=%s auto_repair=%s empty_full_chapters=%s reconcile=%s",
        MEDIA_ROOT, AUTO_REPAIR, REPAIR_EMPTY_FULL_CHAPTERS, RECONCILE_LOCAL_TIME,
    )
    try:
        reconcile(state, runtime)
        while not STOP_REQUESTED:
            drain_events(state, events)
            if time.time() >= runtime.next_reconcile:
                reconcile(state, runtime)
            due = state.due(1 if not args.once else 100)
            if due:
                for pending in due:
                    if STOP_REQUESTED:
                        break
                    process_pending(state, runtime, pending)
                if args.once:
                    break
                continue
            if args.once:
                break
            with runtime.lock:
                runtime.pending_count = state.pending_count()
            next_due = state.next_due()
            deadline = min(runtime.next_reconcile, next_due if next_due is not None else runtime.next_reconcile)
            WAKE_EVENT.wait(max(0.1, min(60.0, deadline - time.time())))
            WAKE_EVENT.clear()
    finally:
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
        if web_service:
            web_service.stop()
        runtime.stop_event.set()
        heartbeat_thread.join(timeout=5)
        state.close()
        os.close(lock_descriptor)
    LOG.info("Service stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        LOG.exception("Fatal service error")
        sys.exit(1)
