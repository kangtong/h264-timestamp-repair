#!/usr/bin/env python3
"""Continuously detect and repair broken H.264 B-frame timestamps in MP4 files."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
SCAN_INTERVAL = max(60, int(os.getenv("SCAN_INTERVAL_SECONDS", "1800")))
MIN_FILE_AGE = max(0, int(os.getenv("MIN_FILE_AGE_SECONDS", "3600")))
STABLE_SCANS = max(1, int(os.getenv("STABLE_SCANS", "1")))
SAMPLE_SECONDS = max(3, int(os.getenv("SAMPLE_SECONDS", "8")))
MINIMUM_PACKETS = max(20, int(os.getenv("MINIMUM_PACKETS", "60")))
RETRY_FAILED_AFTER = max(300, int(os.getenv("RETRY_FAILED_AFTER_SECONDS", "86400")))
KEEP_TEMP_ON_FAILURE = env_bool("KEEP_TEMP_ON_FAILURE", False)
FFMPEG = os.getenv("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_BIN", "ffprobe")
MP4BOX = os.getenv("MP4BOX_BIN", "MP4Box")

STOP_REQUESTED = False
CURRENT_CHILD: subprocess.Popen[str] | None = None

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


def request_stop(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
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
        details = stderr_path.read_text(encoding="utf-8", errors="replace")
        raise RuntimeError(f"{label} failed (exit {code}): {details.strip()}")


def media_info(path: Path) -> dict[str, Any]:
    return json.loads(
        run_capture(
            [FFPROBE, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
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
    if not videos:
        return Analysis("Skipped", "No primary video stream", info, None)
    video = videos[0]
    if video.get("codec_name") != "h264":
        return Analysis("Skipped", "Primary video is not H.264", info, video)
    if int(video.get("has_b_frames", 0) or 0) <= 0:
        return Analysis("Healthy", "H.264 stream does not declare B-frames", info, video)
    duration = float(info.get("format", {}).get("duration", 0.0) or 0.0)
    comparable, different = timestamp_sample(path, duration)
    if comparable < MINIMUM_PACKETS:
        return Analysis("Uncertain", "Too few comparable packet timestamps", info, video, comparable, different)
    if different == 0:
        return Analysis(
            "Candidate",
            "B-frames present but all sampled packets have PTS equal to DTS",
            info,
            video,
            comparable,
            different,
        )
    return Analysis("Healthy", "Composition timestamps are present", info, video, comparable, different)


def compatibility_reason(analysis: Analysis) -> str:
    videos = primary_videos(analysis.info)
    if len(videos) != 1:
        return "Exactly one primary video stream is required"
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
    if fixed.status != "Healthy" or fixed.different <= 0:
        raise RuntimeError(f"Timestamp repair validation failed: {fixed.reason}")
    assert original.video is not None and fixed.video is not None
    for key in ("codec_name", "profile", "width", "height", "pix_fmt"):
        if str(original.video.get(key)) != str(fixed.video.get(key)):
            raise RuntimeError(f"Video property changed unexpectedly: {key}")
    before_frames = original.video.get("nb_frames")
    after_frames = fixed.video.get("nb_frames")
    if before_frames and after_frames and int(before_frames) != int(after_frames):
        raise RuntimeError(f"Video frame count changed: {before_frames} -> {after_frames}")

    before_audio = [s for s in original.info.get("streams", []) if s.get("codec_type") == "audio"]
    after_audio = [s for s in fixed.info.get("streams", []) if s.get("codec_type") == "audio"]
    if len(before_audio) != len(after_audio):
        raise RuntimeError("Audio stream count changed unexpectedly")
    for index, (before, after) in enumerate(zip(before_audio, after_audio)):
        for key in ("codec_name", "sample_rate", "channels"):
            if str(before.get(key)) != str(after.get(key)):
                raise RuntimeError(f"Audio stream {index} property changed unexpectedly: {key}")


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
    token = uuid.uuid4().hex
    stage = original.parent / f"TimestampRepairStage-{token}.mp4"
    backup = original.parent / f"TimestampRepairBackup-{token}.mp4"
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
            if final.status != "Healthy" or final.different <= 0:
                raise RuntimeError(f"Post-install timestamp validation failed: {final.reason}")
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
            [MP4BOX, "-add", f"{raw}:fps={nominal_fps}", "-new", str(video_only)],
            job / "mp4box",
            "MP4Box timestamp rebuild",
        )
        if not KEEP_TEMP_ON_FAILURE:
            raw.unlink(missing_ok=True)

        LOG.info("Copying audio, subtitles, chapters and metadata: %s", path)
        run_logged(
            [
                FFMPEG,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-y",
                "-i",
                str(video_only),
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-map",
                "1:a?",
                "-map",
                "1:s?",
                "-c",
                "copy",
                "-map_metadata",
                "1",
                "-map_chapters",
                "1",
                "-map_metadata:s:v:0",
                "1:s:v:0",
                "-movflags",
                "+faststart",
                "-avoid_negative_ts",
                "disabled",
                str(repaired),
            ],
            job / "remux",
            "final remux",
        )
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
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                stable_count INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                checked_at REAL NOT NULL,
                comparable INTEGER NOT NULL DEFAULT 0,
                different INTEGER NOT NULL DEFAULT 0,
                dropped_data INTEGER NOT NULL DEFAULT 0,
                sha256 TEXT NOT NULL DEFAULT ''
            )
            """
        )
        self.db.commit()

    def get(self, path: Path) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM files WHERE path = ?", (str(path),)).fetchone()

    def save(
        self,
        path: Path,
        stat: os.stat_result,
        status: str,
        reason: str,
        stable_count: int,
        comparable: int = 0,
        different: int = 0,
        dropped_data: int = 0,
        final_hash: str = "",
    ) -> None:
        self.db.execute(
            """
            INSERT INTO files(path,size,mtime_ns,stable_count,status,reason,checked_at,comparable,different,dropped_data,sha256)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              size=excluded.size,mtime_ns=excluded.mtime_ns,stable_count=excluded.stable_count,
              status=excluded.status,reason=excluded.reason,checked_at=excluded.checked_at,
              comparable=excluded.comparable,different=excluded.different,
              dropped_data=excluded.dropped_data,sha256=excluded.sha256
            """,
            (
                str(path),
                stat.st_size,
                stat.st_mtime_ns,
                stable_count,
                status,
                reason,
                time.time(),
                comparable,
                different,
                dropped_data,
                final_hash,
            ),
        )
        self.db.commit()

    def export_csv(self, target: Path) -> None:
        temporary = target.with_suffix(".tmp")
        rows = self.db.execute("SELECT * FROM files ORDER BY path").fetchall()
        with temporary.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(rows[0].keys() if rows else ["path", "status", "reason"])
            for row in rows:
                writer.writerow(tuple(row))
        os.replace(temporary, target)


def record_history(path: Path, status: str, reason: str, **extra: Any) -> None:
    event = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "path": str(path), "status": status, "reason": reason}
    event.update(extra)
    with (CONFIG_ROOT / "history.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def enumerate_media() -> list[Path]:
    files = []
    for root, directories, names in os.walk(MEDIA_ROOT):
        directories[:] = [d for d in directories if d not in {"@eaDir", "#recycle", ".Trash"}]
        for name in names:
            if not name.lower().endswith(".mp4"):
                continue
            if name.startswith(("TimestampRepairStage-", "TimestampRepairBackup-")):
                continue
            if NAME_CONTAINS and NAME_CONTAINS.casefold() not in name.casefold():
                continue
            files.append(Path(root) / name)
    files.sort(key=lambda p: str(p).casefold())
    return files


def should_use_cache(row: sqlite3.Row, stat: os.stat_result) -> bool:
    unchanged = row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns
    if not unchanged:
        return False
    if row["status"] in {"Healthy", "Skipped", "Uncertain", "Repaired"}:
        return True
    if row["status"] == "Candidate" and not AUTO_REPAIR:
        return True
    if row["status"] == "Failed" and time.time() - row["checked_at"] < RETRY_FAILED_AFTER:
        return True
    return False


def write_heartbeat(cycle_started: float, files: int, complete: bool) -> None:
    payload = {
        "pid": os.getpid(),
        "time": time.time(),
        "cycle_started": cycle_started,
        "files_seen": files,
        "cycle_complete": complete,
        "auto_repair": AUTO_REPAIR,
    }
    target = CONFIG_ROOT / "heartbeat.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def scan_cycle(state: State) -> None:
    cycle_started = time.time()
    files = enumerate_media()
    LOG.info("Scan cycle found %d matching MP4 files (auto_repair=%s)", len(files), AUTO_REPAIR)
    write_heartbeat(cycle_started, len(files), False)
    for index, path in enumerate(files, start=1):
        if STOP_REQUESTED:
            break
        try:
            stat = path.stat()
            row = state.get(path)
            stable_count = 1
            if row and row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns:
                stable_count = int(row["stable_count"]) + 1
            if should_use_cache(row, stat) if row else False:
                continue
            age = time.time() - stat.st_mtime
            if age < MIN_FILE_AGE or stable_count < STABLE_SCANS:
                reason = f"Waiting for file stability (age={int(age)}s, stable_scans={stable_count})"
                state.save(path, stat, "WaitingStable", reason, stable_count)
                continue

            LOG.info("[%d/%d] Inspecting %s", index, len(files), path)
            result = analyze(path)
            dropped_data = sum(1 for s in result.info.get("streams", []) if s.get("codec_type") == "data")
            final_hash = ""
            if result.status == "Candidate" and AUTO_REPAIR:
                final_hash, dropped_data = repair_one(path, result)
                stat = path.stat()
                result = Analysis(
                    "Repaired",
                    "Composition timestamps rebuilt without video re-encoding; original replaced after validation",
                    result.info,
                    result.video,
                    result.comparable,
                    result.different,
                )
            state.save(
                path,
                stat,
                result.status,
                result.reason,
                stable_count,
                result.comparable,
                result.different,
                dropped_data,
                final_hash,
            )
            record_history(path, result.status, result.reason, sha256=final_hash, dropped_data=dropped_data)
        except Exception as exc:  # continue with other files; original replacement has rollback
            LOG.exception("Failed while processing %s", path)
            try:
                state.save(path, path.stat(), "Failed", str(exc), 1)
                record_history(path, "Failed", str(exc))
            except OSError:
                pass
        finally:
            state.export_csv(CONFIG_ROOT / "latest.csv")
            write_heartbeat(cycle_started, len(files), False)
    state.export_csv(CONFIG_ROOT / "latest.csv")
    write_heartbeat(cycle_started, len(files), not STOP_REQUESTED)
    LOG.info("Scan cycle complete")


def healthcheck() -> int:
    heartbeat = CONFIG_ROOT / "heartbeat.json"
    try:
        data = json.loads(heartbeat.read_text(encoding="utf-8"))
        maximum_age = max(1800, SCAN_INTERVAL * 2 + 1800)
        return 0 if time.time() - float(data["time"]) <= maximum_age else 1
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return 1


def acquire_lock() -> int:
    lock_path = CONFIG_ROOT / "service.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, BlockingIOError) as exc:
        os.close(descriptor)
        raise RuntimeError("Another repair service instance is already running") from exc
    os.ftruncate(descriptor, 0)
    os.write(descriptor, str(os.getpid()).encode("ascii"))
    return descriptor


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one scan cycle and exit")
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

    lock_descriptor = acquire_lock()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    state = State(CONFIG_ROOT / "state.sqlite3")
    LOG.info(
        "Service started: media=%s name_contains=%r auto_repair=%s interval=%ss min_age=%ss",
        MEDIA_ROOT,
        NAME_CONTAINS,
        AUTO_REPAIR,
        SCAN_INTERVAL,
        MIN_FILE_AGE,
    )
    try:
        while not STOP_REQUESTED:
            scan_cycle(state)
            if args.once or STOP_REQUESTED:
                break
            deadline = time.time() + SCAN_INTERVAL
            while time.time() < deadline and not STOP_REQUESTED:
                time.sleep(min(5, max(0, deadline - time.time())))
    finally:
        os.close(lock_descriptor)
    LOG.info("Service stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        LOG.exception("Fatal service error")
        sys.exit(1)
