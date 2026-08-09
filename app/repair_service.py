#!/usr/bin/env python3
"""Continuously detect and losslessly repair broken video timelines."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mmap
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
from fractions import Fraction
from pathlib import Path
from typing import Any

from emby_refresh import EmbyItemNotReady, EmbyRefreshClient
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
REPAIR_MKV_TIMESTAMPS = env_bool("REPAIR_MKV_TIMESTAMPS", True)
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
EMBY_REFRESH_RETRY_SECONDS = max(30, int(os.getenv("EMBY_REFRESH_RETRY_SECONDS", "60")))
EMBY_REFRESH_MAX_RETRY_SECONDS = max(
    EMBY_REFRESH_RETRY_SECONDS,
    int(os.getenv("EMBY_REFRESH_MAX_RETRY_SECONDS", "21600")),
)
MP4_ANALYSIS_SIGNATURE = f"mp4:4:{SAMPLE_SECONDS}:{MINIMUM_PACKETS}:{int(REPAIR_EMPTY_FULL_CHAPTERS)}"
MKV_ANALYSIS_SIGNATURE = f"mkv:2:{SAMPLE_SECONDS}:{MINIMUM_PACKETS}"
ANALYSIS_SIGNATURE = MP4_ANALYSIS_SIGNATURE
LEGACY_ENV_NAMES = ("SCAN_INTERVAL_SECONDS", "STABLE_SCANS", "WEB_USERNAME", "WEB_PASSWORD")
MAX_ERROR_LENGTH = 4096

STOP_REQUESTED = False
CURRENT_CHILD: subprocess.Popen[str] | None = None
ACTIVE_RUNTIME: Any = None
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
    container: str = "mp4"
    issue_category: str = "none"
    reason_code: str = "unknown"
    diagnostics: dict[str, Any] | None = None


STATUS_LABELS = {
    "Healthy": "正常",
    "Candidate": "待修复",
    "Repaired": "已修复",
    "Skipped": "已跳过",
    "Uncertain": "需要人工确认",
    "WaitingStable": "等待文件写入完成",
    "Failed": "处理失败",
    "MediaRefreshQueued": "等待媒体库刷新",
    "MediaRefreshWaiting": "等待媒体入库",
    "MediaRefreshDeferred": "媒体库刷新重试",
    "MediaRefreshed": "媒体库已刷新",
}

ISSUE_LABELS = {
    "none": "无问题",
    "timeline": "时间轴异常",
    "chapter": "章节元数据异常",
    "multiple": "多项异常",
    "unsupported": "无法自动处理",
    "failed": "处理失败",
}

REASON_LABELS = {
    "timeline_bframe_pts": "H.264 B 帧显示时间轴顺序异常",
    "timeline_missing_mp4": "H.264 B 帧显示时间轴顺序异常",
    "timeline_order_mkv": "H.264 B 帧显示时间轴顺序异常",
    "chapter_full_duration": "存在覆盖整段视频的无效空章节",
    "timeline_and_chapter": "同时存在时间轴和章节元数据异常",
    "timestamps_present": "显示时间戳正常",
    "no_b_frames": "视频不包含需要重排的 B 帧",
    "unsupported_codec": "主视频编码不在自动修复范围内",
    "no_video": "没有找到主视频流",
    "too_few_samples": "有效时间戳样本不足",
    "ambiguous_timeline": "抽样结果不一致，无法安全自动判断",
    "validation_failed": "修复结果未通过完整性验证，原文件未被覆盖",
    "variable_fps": "可变或不明确的帧率不能自动修复",
    "unsupported_streams": "存在无法安全复制到目标封装的流",
    "repaired_timeline": "已无损重建视频显示时间戳",
    "repaired_chapter": "已移除无效的整段空章节",
    "repaired_multiple": "已修复时间轴和章节元数据异常",
    "processing_failed": "处理过程中发生错误",
}


def container_kind(path: Path, info: dict[str, Any] | None = None) -> str:
    suffix = path.suffix.lower()
    if suffix == ".mkv":
        return "mkv"
    if suffix == ".mp4":
        return "mp4"
    names = str((info or {}).get("format", {}).get("format_name", ""))
    return "mkv" if "matroska" in names else "mp4"


def analysis_signature_for(path: Path) -> str:
    return MKV_ANALYSIS_SIGNATURE if path.suffix.lower() == ".mkv" else MP4_ANALYSIS_SIGNATURE


def file_identifier(path: Path) -> str:
    return hashlib.sha256(str(path).encode("utf-8", errors="surrogatepass")).hexdigest()[:24]


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


def report_task(
    stage: str,
    *,
    stage_progress: float | None = None,
    overall_progress: float | None = None,
    speed: str = "",
    eta_seconds: float | None = None,
) -> None:
    runtime = ACTIVE_RUNTIME
    if runtime is None:
        return
    with runtime.lock:
        runtime.current_stage = stage
        runtime.stage_progress = stage_progress
        runtime.overall_progress = overall_progress
        runtime.current_speed = speed
        runtime.eta_seconds = eta_seconds


def run_ffmpeg_progress(
    args: list[str],
    log_base: Path,
    label: str,
    duration: float,
    stage: str,
    overall_start: float,
    overall_end: float,
) -> None:
    """Run ffmpeg and publish media-time based progress."""
    global CURRENT_CHILD
    command = list(args)
    command[1:1] = ["-progress", "pipe:1", "-nostats"]
    stdout_path = log_base.with_suffix(".stdout.log")
    stderr_path = log_base.with_suffix(".stderr.log")
    report_task(stage, stage_progress=0.0, overall_progress=overall_start)
    last_progress = 0.0
    speed = ""
    with stdout_path.open("w", encoding="utf-8") as stdout_log, stderr_path.open("w", encoding="utf-8") as stderr:
        CURRENT_CHILD = subprocess.Popen(
            command, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=stderr,
        )
        assert CURRENT_CHILD.stdout is not None
        for raw_line in CURRENT_CHILD.stdout:
            stdout_log.write(raw_line)
            key, _, value = raw_line.strip().partition("=")
            if key == "speed":
                speed = value
            elif key in {"out_time_us", "out_time_ms"}:
                try:
                    seconds = float(value) / 1_000_000.0
                except ValueError:
                    continue
                if duration > 0:
                    last_progress = min(1.0, max(last_progress, seconds / duration))
                    overall = overall_start + (overall_end - overall_start) * last_progress
                    try:
                        numeric_speed = float(speed.rstrip("x")) if speed.endswith("x") else 0.0
                    except ValueError:
                        numeric_speed = 0.0
                    eta = ((duration - seconds) / numeric_speed) if numeric_speed > 0 else None
                    report_task(
                        stage, stage_progress=last_progress * 100, overall_progress=overall,
                        speed=speed, eta_seconds=eta,
                    )
        code = CURRENT_CHILD.wait()
        CURRENT_CHILD = None
    if code != 0:
        with stderr_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            length = handle.tell()
            handle.seek(max(0, length - 16 * 1024))
            details = handle.read().decode("utf-8", errors="replace")
        details = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", details).strip()
        raise RuntimeError(f"{label} failed (exit {code}): {details}"[:MAX_ERROR_LENGTH])
    report_task(stage, stage_progress=100.0, overall_progress=overall_end, speed=speed, eta_seconds=0.0)


def media_info(path: Path) -> dict[str, Any]:
    return json.loads(
        run_capture(
            [
                FFPROBE, "-v", "error", "-show_streams", "-show_chapters",
                "-show_format", "-show_data_hash", "sha256", "-of", "json", str(path),
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
    # MP4 chapter metadata can end at the shortest media stream while the
    # container duration includes a slightly longer video tail. Accept a
    # small relative tail difference, but cap it so a genuinely shorter
    # chapter is never mistaken for the synthetic full-file chapter.
    tolerance = max(0.1, min(5.0, duration * 0.001))
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


def decoded_timeline_sample(
    path: Path,
    duration: float,
    frame_rate: float,
) -> tuple[int, int, str, dict[str, Any]]:
    """Validate display-order PTS after decoding, independent of the container."""
    positions: list[float] = []
    for candidate in (0.0, max(0.0, duration / 2.0), max(0.0, duration - SAMPLE_SECONDS - 2.0)):
        rounded = round(candidate, 3)
        if rounded not in positions:
            positions.append(rounded)

    expected = 1.0 / frame_rate if frame_rate > 0 else 0.0
    total_transitions = 0
    non_increasing = 0
    large_gaps = 0
    usable_segments = 0
    bad_segments = 0
    segments: list[dict[str, Any]] = []
    for position in positions:
        interval = f"{position:.3f}%+{SAMPLE_SECONDS}"
        output = run_capture(
            [
                FFPROBE, "-v", "error", "-select_streams", "v:0",
                "-read_intervals", interval, "-show_frames", "-show_entries",
                "frame=pts_time", "-of", "json", str(path),
            ],
            "ffprobe decoded frame sampling",
        )
        timestamps = []
        for frame in json.loads(output).get("frames", []):
            try:
                timestamps.append(float(frame["pts_time"]))
            except (KeyError, TypeError, ValueError):
                continue
        segment_total = max(0, len(timestamps) - 1)
        segment_bad = 0
        segment_gaps = 0
        for before, after in zip(timestamps, timestamps[1:]):
            delta = after - before
            if delta <= 0:
                segment_bad += 1
            elif expected > 0 and delta > expected * 1.8:
                segment_gaps += 1
        if segment_total >= 20:
            usable_segments += 1
            threshold = max(3, int(segment_total * 0.05))
            if segment_bad >= threshold:
                bad_segments += 1
        total_transitions += segment_total
        non_increasing += segment_bad
        large_gaps += segment_gaps
        segments.append({
            "position": position,
            "transitions": segment_total,
            "non_increasing": segment_bad,
            "large_gaps": segment_gaps,
        })

    diagnostics = {
        "detector": "decoded_display_pts",
        "transitions": total_transitions,
        "non_increasing": non_increasing,
        "large_gaps": large_gaps,
        "usable_segments": usable_segments,
        "bad_segments": bad_segments,
        "segments": segments,
    }
    if total_transitions < MINIMUM_PACKETS or usable_segments == 0:
        return total_transitions, non_increasing, "uncertain", diagnostics
    noise_limit = max(1, int(total_transitions * 0.005))
    diagnostics["noise_limit"] = noise_limit
    required_segments = 1 if duration <= SAMPLE_SECONDS * 3 else 2
    if bad_segments >= required_segments:
        return total_transitions, non_increasing, "issue", diagnostics
    if bad_segments == 0 and non_increasing <= noise_limit:
        diagnostics["low_level_noise_ignored"] = non_increasing > 0
        return total_transitions, non_increasing, "healthy", diagnostics
    return total_transitions, non_increasing, "uncertain", diagnostics


def analyze(path: Path) -> Analysis:
    info = media_info(path)
    kind = container_kind(path, info)
    videos = primary_videos(info)
    chapter_issue = kind == "mp4" and has_empty_full_duration_chapter(info)
    if not videos:
        return Analysis(
            "Skipped", REASON_LABELS["no_video"], info, None,
            container=kind, issue_category="unsupported", reason_code="no_video",
        )
    video = videos[0]
    comparable = different = 0
    timestamp_issue = False
    timestamp_reason_code = ""
    diagnostics: dict[str, Any] = {}
    if video.get("codec_name") == "h264" and int(video.get("has_b_frames", 0) or 0) > 0:
        duration = float(info.get("format", {}).get("duration", 0.0) or 0.0)
        frame_rate = fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        if kind == "mp4":
            packet_total, packet_offsets = timestamp_sample(path, duration)
            diagnostics["packet_timestamps"] = {
                "comparable": packet_total, "different": packet_offsets,
            }
        comparable, different, verdict, frame_diagnostics = decoded_timeline_sample(
            path, duration, frame_rate,
        )
        diagnostics.update(frame_diagnostics)
        if verdict == "issue":
            timestamp_issue = True
        elif verdict == "uncertain":
            timestamp_reason_code = "too_few_samples" if comparable < MINIMUM_PACKETS else "ambiguous_timeline"

    problems = []
    if timestamp_issue:
        problems.append(REASON_LABELS["timeline_bframe_pts"])
    if chapter_issue:
        problems.append(REASON_LABELS["chapter_full_duration"])
    if problems:
        category = "multiple" if len(problems) > 1 else ("timeline" if timestamp_issue else "chapter")
        reason_code = (
            "timeline_and_chapter" if len(problems) > 1
            else ("timeline_bframe_pts" if timestamp_issue else "chapter_full_duration")
        )
        return Analysis(
            "Candidate", "; ".join(problems), info, video, comparable, different,
            timestamp_issue, chapter_issue, kind, category, reason_code, diagnostics,
        )
    if timestamp_reason_code:
        return Analysis(
            "Uncertain", REASON_LABELS[timestamp_reason_code], info, video, comparable, different,
            container=kind, issue_category="unsupported", reason_code=timestamp_reason_code,
            diagnostics=diagnostics,
        )
    if video.get("codec_name") != "h264":
        return Analysis(
            "Skipped", REASON_LABELS["unsupported_codec"], info, video,
            container=kind, issue_category="unsupported", reason_code="unsupported_codec",
        )
    if int(video.get("has_b_frames", 0) or 0) <= 0:
        return Analysis(
            "Healthy", REASON_LABELS["no_b_frames"], info, video,
            container=kind, reason_code="no_b_frames",
        )
    return Analysis(
        "Healthy", REASON_LABELS["timestamps_present"], info, video, comparable, different,
        container=kind, reason_code="timestamps_present", diagnostics=diagnostics,
    )


def compatibility_reason(analysis: Analysis) -> str:
    if analysis.container == "mkv" and analysis.timestamp_issue and not REPAIR_MKV_TIMESTAMPS:
        return "MKV 时间戳修复已由配置关闭"
    videos = primary_videos(analysis.info)
    if len(videos) != 1:
        return "自动修复要求文件中只有一个主视频流"
    if analysis.timestamp_issue:
        avg = fraction(analysis.video.get("avg_frame_rate") if analysis.video else None)
        nominal = fraction(analysis.video.get("r_frame_rate") if analysis.video else None)
        if avg <= 0 or nominal <= 0 or abs(avg - nominal) > max(0.001, nominal * 0.005):
            return "可变或不明确的帧率暂不支持自动修复"
    bad_subtitles = [
        stream
        for stream in analysis.info.get("streams", [])
        if analysis.container == "mp4"
        and stream.get("codec_type") == "subtitle" and stream.get("codec_name") != "mov_text"
    ]
    if bad_subtitles:
        return "MP4 中的非 mov_text 字幕无法安全无损复制"
    return ""


def sha256(
    path: Path,
    *,
    stage: str = "",
    overall_start: float | None = None,
    overall_end: float | None = None,
) -> str:
    digest = hashlib.sha256()
    total = max(1, path.stat().st_size)
    processed = 0
    with path.open("rb") as source:
        while block := source.read(8 * 1024 * 1024):
            digest.update(block)
            processed += len(block)
            if stage and overall_start is not None and overall_end is not None:
                ratio = min(1.0, processed / total)
                report_task(
                    stage, stage_progress=ratio * 100,
                    overall_progress=overall_start + (overall_end - overall_start) * ratio,
                )
    return digest.hexdigest().upper()


def annexb_nal_fingerprints(
    path: Path,
    *,
    stage: str = "",
    overall_start: float | None = None,
    overall_end: float | None = None,
) -> dict[str, Any]:
    """Fingerprint H.264 NAL payloads while ignoring safe cross-type reordering."""
    type_digests: dict[int, Any] = {}
    type_counts: dict[int, int] = {}
    vcl_digest = hashlib.sha256()
    vcl_count = 0

    with path.open("rb") as source:
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
            total = len(data)

            def find_start(position: int) -> tuple[int, int]:
                marker = data.find(b"\x00\x00\x01", position)
                if marker < 0:
                    return -1, -1
                start = marker
                while start > 0 and data[start - 1] == 0:
                    start -= 1
                return start, marker + 3

            start, payload_start = find_start(0)
            if start < 0:
                raise RepairValidationError("H.264 Annex-B 码流中没有找到 NAL 单元")
            last_report = 0
            while start >= 0:
                next_start, next_payload = find_start(payload_start)
                payload_end = next_start if next_start >= 0 else total
                while payload_end > payload_start and data[payload_end - 1] == 0:
                    payload_end -= 1
                if payload_end > payload_start:
                    payload = data[payload_start:payload_end]
                    nal_type = payload[0] & 0x1F
                    digest = type_digests.setdefault(nal_type, hashlib.sha256())
                    type_counts[nal_type] = type_counts.get(nal_type, 0) + 1
                    length = len(payload).to_bytes(8, "big")
                    digest.update(length)
                    digest.update(payload)
                    if nal_type in {1, 2, 3, 4, 5}:
                        vcl_digest.update(length)
                        vcl_digest.update(payload)
                        vcl_count += 1
                if stage and overall_start is not None and overall_end is not None:
                    if payload_end - last_report >= 64 * 1024 * 1024 or next_start < 0:
                        ratio = min(1.0, payload_end / max(1, total))
                        report_task(
                            stage, stage_progress=ratio * 100,
                            overall_progress=overall_start + (overall_end - overall_start) * ratio,
                        )
                        last_report = payload_end
                if next_start < 0:
                    break
                start, payload_start = next_start, next_payload

    if vcl_count == 0:
        raise RepairValidationError("H.264 Annex-B 码流中没有找到画面 NAL 单元")
    return {
        "vcl": (vcl_count, vcl_digest.hexdigest()),
        "types": {
            str(nal_type): (type_counts[nal_type], type_digests[nal_type].hexdigest())
            for nal_type in sorted(type_digests)
        },
    }


def copyfile_progress(
    source: Path,
    destination: Path,
    *,
    overall_start: float,
    overall_end: float,
) -> None:
    total = max(1, source.stat().st_size)
    processed = 0
    with source.open("rb") as reader, destination.open("wb") as writer:
        while block := reader.read(8 * 1024 * 1024):
            writer.write(block)
            processed += len(block)
            ratio = min(1.0, processed / total)
            report_task(
                "正在复制修复文件", stage_progress=ratio * 100,
                overall_progress=overall_start + (overall_end - overall_start) * ratio,
            )


def packet_payload_fingerprints(path: Path, info: dict[str, Any]) -> dict[str, tuple[int, str]]:
    """Hash the ordered compressed packet payloads for every non-video stream."""
    global CURRENT_CHILD
    stream_keys: dict[int, str] = {}
    ordinals: dict[str, int] = {}
    for stream in info.get("streams", []):
        stream_type = str(stream.get("codec_type", "unknown"))
        if stream_type == "video":
            continue
        ordinal = ordinals.get(stream_type, 0)
        ordinals[stream_type] = ordinal + 1
        stream_keys[int(stream.get("index", -1))] = f"{stream_type}:{ordinal}"
    if not stream_keys:
        return {}
    digests = {key: hashlib.sha256() for key in stream_keys.values()}
    counts = {key: 0 for key in stream_keys.values()}
    CURRENT_CHILD = subprocess.Popen(
        [
            FFPROBE, "-v", "error", "-show_packets", "-show_data_hash", "sha256",
            "-show_entries", "packet=stream_index,data_hash", "-of", "compact=p=0:nk=0",
            str(path),
        ],
        text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert CURRENT_CHILD.stdout is not None
    for line in CURRENT_CHILD.stdout:
        fields = dict(part.split("=", 1) for part in line.strip().split("|") if "=" in part)
        try:
            stream_index = int(fields.get("stream_index", "-1"))
            data_hash = fields.get("data_hash", "").split(":", 1)[1]
            key = stream_keys[stream_index]
            digests[key].update(bytes.fromhex(data_hash))
            counts[key] += 1
        except (KeyError, ValueError, IndexError):
            continue
    stderr = CURRENT_CHILD.stderr.read() if CURRENT_CHILD.stderr is not None else ""
    code = CURRENT_CHILD.wait()
    CURRENT_CHILD = None
    if code != 0:
        raise RuntimeError(f"ffprobe packet payload hashing failed (exit {code}): {stderr.strip()}"[:MAX_ERROR_LENGTH])
    return {key: (counts[key], digests[key].hexdigest()) for key in sorted(digests)}


def copied_payload_fingerprints(
    fingerprints: dict[str, tuple[int, str]],
    container: str,
) -> dict[str, tuple[int, str]]:
    """Keep payloads the remux path promises to copy; reject non-empty dropped MP4 data."""
    if container != "mp4":
        return fingerprints
    copied: dict[str, tuple[int, str]] = {}
    for key, value in fingerprints.items():
        stream_type = key.split(":", 1)[0]
        if stream_type in {"audio", "subtitle"}:
            copied[key] = value
        elif int(value[0]) > 0:
            raise RepairValidationError(
                f"MP4 {stream_type} stream contains packet payloads that cannot be dropped safely"
            )
    return copied


def mp4box_fps(value: Any, duration: float) -> str:
    """Bound pathological FPS fractions without materially changing total duration."""
    try:
        numerator_text, denominator_text = str(value).split("/", 1)
        original = Fraction(int(numerator_text), int(denominator_text))
    except (ValueError, ZeroDivisionError):
        return str(value)
    if original <= 0:
        return str(value)
    if abs(original.numerator) <= 1_000_000 and original.denominator <= 100_000:
        return f"{original.numerator}/{original.denominator}"
    for maximum_denominator in (1_000, 10_000, 100_000, 1_000_000):
        candidate = original.limit_denominator(maximum_denominator)
        duration_drift = max(0.0, duration) * abs(float(original / candidate) - 1.0)
        if duration_drift <= 0.005:
            return f"{candidate.numerator}/{candidate.denominator}"
    return f"{original.numerator}/{original.denominator}"


def assert_free_space(input_size: int) -> None:
    required = int(input_size * (3.2 if KEEP_TEMP_ON_FAILURE else 2.25) + 1024**3)
    free = shutil.disk_usage(WORK_ROOT).free
    if free < required:
        raise RuntimeError(
            f"Insufficient work space: need about {required / 1024**3:.1f} GiB, "
            f"available {free / 1024**3:.1f} GiB"
        )


def compare_streams(original: Analysis, fixed: Analysis) -> None:
    if original.timestamp_issue and (fixed.timestamp_issue or fixed.status != "Healthy"):
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

    for stream_type in ("attachment", "data"):
        before_streams = [s for s in original.info.get("streams", []) if s.get("codec_type") == stream_type]
        after_streams = [s for s in fixed.info.get("streams", []) if s.get("codec_type") == stream_type]
        if original.container == "mkv" and len(before_streams) != len(after_streams):
            raise RepairValidationError(f"{stream_type.title()} stream count changed unexpectedly")
        for index, (before, after) in enumerate(zip(before_streams, after_streams)):
            if str(before.get("codec_name")) != str(after.get("codec_name")):
                raise RepairValidationError(f"{stream_type.title()} stream {index} codec changed unexpectedly")
            if stream_type == "attachment":
                if str(before.get("extradata_hash")) != str(after.get("extradata_hash")):
                    raise RepairValidationError(f"Attachment {index} content changed unexpectedly")
                for tag in ("filename", "mimetype"):
                    before_value = str((before.get("tags") or {}).get(tag, ""))
                    after_value = str((after.get("tags") or {}).get(tag, ""))
                    if before_value != after_value:
                        raise RepairValidationError(f"Attachment {index} metadata changed unexpectedly: {tag}")

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
    suffix = original.suffix.lower()
    stage = original.parent / f"MediaRepairStage-{token}{suffix}"
    backup = original.parent / f"MediaRepairBackup-{token}{suffix}"
    original_stat = original.stat()
    installed = False
    try:
        copyfile_progress(repaired, stage, overall_start=88.0, overall_end=94.0)
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
        local_hash = sha256(
            repaired, stage="正在计算修复文件哈希", overall_start=94.0, overall_end=96.0,
        )
        if sha256(stage, stage="正在验证暂存文件哈希", overall_start=96.0, overall_end=98.0) != local_hash:
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
    duration = float(original.info.get("format", {}).get("duration", 0.0) or 0.0)
    assert_free_space(size)
    job = Path(tempfile.mkdtemp(prefix="job-", dir=WORK_ROOT))
    raw = job / "video.h264"
    video_only = job / "video-fixed.mp4"
    repaired = job / f"repaired{path.suffix.lower()}"
    verified_raw = job / "video-verified.h264"
    success = False
    try:
        report_task("正在记录非视频流指纹", stage_progress=None, overall_progress=2.0)
        original_payloads = copied_payload_fingerprints(
            packet_payload_fingerprints(path, original.info), original.container,
        )
        if original.timestamp_issue:
            nominal_fps = str(original.video.get("r_frame_rate") or original.video.get("avg_frame_rate"))
            rebuild_fps = mp4box_fps(nominal_fps, duration)
            LOG.info("Extracting H.264 without re-encoding: %s", path)
            run_ffmpeg_progress(
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
                duration,
                "正在无损抽取视频",
                5.0,
                25.0,
            )
            LOG.info("Rebuilding composition timestamps: %s", path)
            report_task("正在重建显示时间戳", stage_progress=None, overall_progress=30.0)
            run_logged(
                [MP4BOX, "-tmp", str(job), "-add", f"{raw}:fps={rebuild_fps}", "-new", str(video_only)],
                job / "mp4box",
                "MP4Box timestamp rebuild",
            )
            report_task("显示时间戳重建完成", stage_progress=100.0, overall_progress=40.0)
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
            if original.container == "mkv":
                remux.extend(["-map", "1:t?", "-map", "1:d?"])
                attachments = [
                    stream for stream in original.info.get("streams", [])
                    if stream.get("codec_type") == "attachment"
                ]
                for attachment_index, stream in enumerate(attachments):
                    tags = stream.get("tags") or {}
                    for tag in ("filename", "mimetype"):
                        value = str(tags.get(tag, ""))
                        if value:
                            remux.extend([f"-metadata:s:t:{attachment_index}", f"{tag}={value}"])
        else:
            remux.extend([
                "-i", str(path), "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
                "-map_metadata", "0", "-map_metadata:s:v:0", "0:s:v:0",
                "-map_chapters", "-1" if original.chapter_issue else "0",
            ])
        remux.extend(["-c", "copy"])
        if original.container == "mp4":
            remux.extend(["-movflags", "+faststart"])
        remux.extend(["-avoid_negative_ts", "disabled", str(repaired)])
        run_ffmpeg_progress(
            remux, job / "remux", "final remux", duration,
            "正在重新封装媒体流", 40.0, 62.0,
        )
        if original.timestamp_issue:
            run_ffmpeg_progress(
                [
                    FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(repaired), "-map", "0:v:0", "-c:v", "copy",
                    "-bsf:v", "h264_mp4toannexb", "-f", "h264", str(verified_raw),
                ],
                job / "verify-bitstream",
                "video bitstream verification extraction",
                duration,
                "正在验证视频码流",
                62.0,
                72.0,
            )
            report_task("正在比较画面码流指纹", stage_progress=None, overall_progress=72.0)
            before_nals = annexb_nal_fingerprints(
                raw, stage="正在校验原始画面码流", overall_start=72.0, overall_end=75.0,
            )
            after_nals = annexb_nal_fingerprints(
                verified_raw, stage="正在校验修复后画面码流", overall_start=75.0, overall_end=78.0,
            )
            if before_nals["vcl"] != after_nals["vcl"]:
                raise RepairValidationError("修复前后的 H.264 画面 NAL 内容不一致")
            if before_nals["types"] != after_nals["types"]:
                raise RepairValidationError("修复前后的 H.264 参数 NAL 内容不一致")
        report_task("正在验证时间轴和媒体流", stage_progress=None, overall_progress=78.0)
        fixed = analyze(repaired)
        repaired_payloads = copied_payload_fingerprints(
            packet_payload_fingerprints(repaired, fixed.info), fixed.container,
        )
        if repaired_payloads != original_payloads:
            raise RepairValidationError("A copied non-video stream packet payload changed unexpectedly")
        compare_streams(original, fixed)
        duration = float(fixed.info.get("format", {}).get("duration", 0.0) or 0.0)
        validate_decode(repaired, duration, job)
        report_task("正在安全替换原文件", stage_progress=None, overall_progress=88.0)
        final_hash = safe_replace(path, repaired, original)
        report_task("修复完成", stage_progress=100.0, overall_progress=100.0, eta_seconds=0.0)
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
        analysis: Analysis | None = None,
    ) -> None:
        reason = compact_error(reason)
        kind = analysis.container if analysis else container_kind(path)
        category = analysis.issue_category if analysis else ("failed" if status == "Failed" else "none")
        reason_code = analysis.reason_code if analysis else ("processing_failed" if status == "Failed" else "unknown")
        diagnostics = json.dumps(analysis.diagnostics or {}, ensure_ascii=False, separators=(",", ":")) if analysis else "{}"
        self.db.execute(
            """
            INSERT INTO files(
              path,size,mtime_ns,ctime_ns,device,inode,status,reason,checked_at,
              comparable,different,dropped_data,sha256,analysis_signature,
              file_id,container,issue_category,reason_code,diagnostics_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              size=excluded.size,mtime_ns=excluded.mtime_ns,ctime_ns=excluded.ctime_ns,
              device=excluded.device,inode=excluded.inode,status=excluded.status,
              reason=excluded.reason,checked_at=excluded.checked_at,
              comparable=excluded.comparable,different=excluded.different,
              dropped_data=excluded.dropped_data,sha256=excluded.sha256,
              analysis_signature=excluded.analysis_signature,file_id=excluded.file_id,
              container=excluded.container,issue_category=excluded.issue_category,
              reason_code=excluded.reason_code,diagnostics_json=excluded.diagnostics_json
            """,
            (
                str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                stat.st_dev, stat.st_ino, status, reason, time.time(), comparable,
                different, dropped_data, final_hash, analysis_signature_for(path),
                file_identifier(path), kind, category, reason_code, diagnostics,
            ),
        )
        self.db.commit()

    def remove(self, path: Path) -> None:
        value = str(path)
        self.db.execute("DELETE FROM pending WHERE path = ?", (value,))
        self.db.execute("DELETE FROM media_refresh_queue WHERE path = ?", (value,))
        self.db.execute("DELETE FROM files WHERE path = ?", (value,))
        self.db.commit()

    def remove_tree(self, path: Path) -> None:
        prefix = str(path).rstrip("/\\") + os.sep
        for table in ("pending", "media_refresh_queue", "files"):
            self.db.execute(
                f"DELETE FROM {table} WHERE substr(path,1,?)=?",
                (len(prefix), prefix),
            )
        self.db.commit()

    def enqueue(
        self,
        path: Path,
        kind: str,
        eligible_at: float,
        force: bool,
        requested_action: str = "inspect",
    ) -> None:
        now = time.time()
        self.db.execute(
            """
            INSERT INTO pending(path,event_kind,queued_at,eligible_at,force,attempts,requested_action)
            VALUES(?,?,?,?,?,0,?)
            ON CONFLICT(path) DO UPDATE SET
              event_kind=excluded.event_kind,queued_at=excluded.queued_at,
              eligible_at=excluded.eligible_at,force=MAX(pending.force,excluded.force),
              requested_action=CASE
                WHEN pending.requested_action='repair' OR excluded.requested_action='repair' THEN 'repair'
                ELSE 'inspect' END
            """,
            (str(path), kind, now, eligible_at, int(force), requested_action),
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

    def enqueue_media_refresh(self, path: Path, eligible_at: float | None = None) -> None:
        now = time.time()
        self.db.execute(
            """
            INSERT INTO media_refresh_queue(path,queued_at,eligible_at,attempts,last_error)
            VALUES(?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET
              queued_at=excluded.queued_at,eligible_at=MIN(media_refresh_queue.eligible_at,excluded.eligible_at),
              last_error=''
            """,
            (str(path), now, now if eligible_at is None else eligible_at, 0, ""),
        )
        self.db.commit()
        WAKE_EVENT.set()

    def due_media_refresh(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM media_refresh_queue WHERE eligible_at <= ? ORDER BY eligible_at,path LIMIT ?",
            (time.time(), limit),
        ).fetchall()

    def defer_media_refresh(self, path: Path, eligible_at: float, error: str) -> None:
        self.db.execute(
            "UPDATE media_refresh_queue SET eligible_at=?,attempts=attempts+1,last_error=? WHERE path=?",
            (eligible_at, compact_error(error), str(path)),
        )
        self.db.commit()

    def complete_media_refresh(self, path: Path) -> None:
        self.db.execute("DELETE FROM media_refresh_queue WHERE path=?", (str(path),))
        self.db.commit()

    def media_refresh_pending_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM media_refresh_queue").fetchone()[0])

    def next_media_refresh_due(self) -> float | None:
        value = self.db.execute("SELECT MIN(eligible_at) FROM media_refresh_queue").fetchone()[0]
        return float(value) if value is not None else None

    def backfill_chapter_media_refreshes(self) -> int:
        marker = "media_refresh_chapter_backfill_v1"
        if self.meta_get(marker) == "complete":
            return 0
        now = time.time()
        self.db.execute(
            """
            INSERT OR IGNORE INTO media_refresh_queue(path,queued_at,eligible_at,attempts,last_error)
            SELECT path,?,?,0,'' FROM files
            WHERE status='Repaired' AND reason LIKE '%invalid full-duration chapter removed%'
            """,
            (now, now),
        )
        added = int(self.db.execute("SELECT changes()").fetchone()[0])
        self.db.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (marker, "complete"),
        )
        self.db.commit()
        if added:
            WAKE_EVENT.set()
        return added

    def backfill_chapter_tolerance_rechecks(self) -> int:
        """Recheck only prior MP4 timeline repairs that may retain a bogus chapter."""
        marker = "chapter_tolerance_backfill_v1"
        if self.meta_get(marker) == "complete":
            return 0
        now = time.time()
        self.db.execute(
            """
            INSERT OR IGNORE INTO pending(
              path,event_kind,queued_at,eligible_at,force,attempts,requested_action
            )
            SELECT path,'chapter-rule-upgrade',?,?,1,0,'inspect' FROM files
            WHERE container='mp4' AND status='Repaired'
              AND reason_code='repaired_timeline' AND dropped_data>0
            """,
            (now, now),
        )
        added = int(self.db.execute("SELECT changes()").fetchone()[0])
        self.db.execute(
            "INSERT INTO metadata(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (marker, "complete"),
        )
        self.db.commit()
        if added:
            WAKE_EVENT.set()
        return added

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

    def due_control_command(self) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM control_commands WHERE state='queued' ORDER BY id LIMIT 1"
        ).fetchone()

    def start_control_command(self, command_id: int) -> None:
        self.db.execute(
            "UPDATE control_commands SET state='running',started_at=? WHERE id=? AND state='queued'",
            (time.time(), command_id),
        )
        self.db.commit()

    def finish_control_command(self, command_id: int, state: str, code: str, detail: str) -> None:
        self.db.execute(
            "UPDATE control_commands SET state=?,finished_at=?,result_code=?,result_detail=? WHERE id=?",
            (state, time.time(), code, compact_error(detail), command_id),
        )
        self.db.execute(
            "DELETE FROM control_commands WHERE id IN "
            "(SELECT id FROM control_commands ORDER BY id DESC LIMIT -1 OFFSET 1000)"
        )
        self.db.commit()

    def paths_for_file_ids(self, file_ids: list[str]) -> list[Path]:
        if not file_ids:
            return []
        placeholders = ",".join("?" for _ in file_ids)
        rows = self.db.execute(
            f"SELECT path FROM files WHERE file_id IN ({placeholders})",
            file_ids,
        ).fetchall()
        return [Path(str(row["path"])) for row in rows]

    def transfer_rename(self, source: Path, destination: Path) -> bool:
        row = self.get(source)
        if row is None or not destination.exists():
            self.remove(source)
            return False
        stat = destination.stat()
        same_file = (
            row["device"] == stat.st_dev and row["inode"] == stat.st_ino
            and row["size"] == stat.st_size and row["mtime_ns"] == stat.st_mtime_ns
            and row["analysis_signature"] == analysis_signature_for(destination)
            and cacheable_status(str(row["status"]))
        )
        if not same_file:
            self.remove(source)
            return False
        self.db.execute("DELETE FROM pending WHERE path IN (?,?)", (str(source), str(destination)))
        self.db.execute("DELETE FROM media_refresh_queue WHERE path=?", (str(destination),))
        self.db.execute(
            "UPDATE media_refresh_queue SET path=? WHERE path=?",
            (str(destination), str(source)),
        )
        self.db.execute("DELETE FROM files WHERE path=?", (str(destination),))
        self.db.execute(
            "UPDATE files SET path=?,file_id=?,ctime_ns=?,container=? WHERE path=?",
            (
                str(destination), file_identifier(destination), stat.st_ctime_ns,
                container_kind(destination), str(source),
            ),
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
        CREATE TABLE IF NOT EXISTS media_refresh_queue (
          path TEXT PRIMARY KEY,
          queued_at REAL NOT NULL,
          eligible_at REAL NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_media_refresh_due ON media_refresh_queue(eligible_at);
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
        CREATE TABLE IF NOT EXISTS control_commands (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          action TEXT NOT NULL,
          file_ids_json TEXT NOT NULL DEFAULT '[]',
          requested_at REAL NOT NULL,
          started_at REAL,
          finished_at REAL,
          state TEXT NOT NULL DEFAULT 'queued',
          result_code TEXT NOT NULL DEFAULT '',
          result_detail TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_control_commands_state ON control_commands(state,id);
        """
    )
    file_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(files)")}
    for name, definition in (
        ("file_id", "TEXT NOT NULL DEFAULT ''"),
        ("container", "TEXT NOT NULL DEFAULT 'mp4'"),
        ("issue_category", "TEXT NOT NULL DEFAULT 'none'"),
        ("reason_code", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("diagnostics_json", "TEXT NOT NULL DEFAULT '{}'")
    ):
        if name not in file_columns:
            connection.execute(f"ALTER TABLE files ADD COLUMN {name} {definition}")
    pending_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(pending)")}
    if "requested_action" not in pending_columns:
        connection.execute("ALTER TABLE pending ADD COLUMN requested_action TEXT NOT NULL DEFAULT 'inspect'")
    connection.execute("UPDATE files SET file_id=substr(lower(hex(randomblob(12))),1,24) WHERE file_id='' ")
    connection.execute("UPDATE files SET container='mkv' WHERE lower(path) LIKE '%.mkv'")
    connection.execute(
        "UPDATE files SET analysis_signature=? WHERE container='mp4' AND analysis_signature LIKE '3:%'",
        (MP4_ANALYSIS_SIGNATURE,),
    )
    connection.execute(
        "UPDATE files SET issue_category='timeline',reason_code='timeline_bframe_pts' "
        "WHERE reason_code='unknown' AND reason LIKE '%composition timestamp%'"
    )
    connection.execute(
        "UPDATE files SET issue_category='chapter',reason_code='chapter_full_duration' "
        "WHERE reason_code='unknown' AND reason LIKE '%full-duration chapter%'"
    )
    connection.execute(
        "UPDATE files SET issue_category='failed',reason_code='processing_failed' "
        "WHERE status='Failed' AND reason_code='unknown'"
    )
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_files_file_id ON files(file_id)")
    connection.execute(
        "INSERT INTO metadata(key,value) VALUES('schema_version','3') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
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
    target: sqlite3.Connection | None = None
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
                    """INSERT INTO files(
                       path,size,mtime_ns,ctime_ns,device,inode,status,reason,checked_at,
                       comparable,different,dropped_data,sha256,analysis_signature,
                       file_id,container,issue_category,reason_code,diagnostics_json
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        str(media), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
                        stat.st_dev, stat.st_ino, status, compact_error(str(row["reason"])),
                        float(row["checked_at"]), int(row["comparable"]), int(row["different"]),
                        int(row["dropped_data"]), str(row["sha256"]), analysis_signature_for(media),
                        file_identifier(media), container_kind(media), "none", "unknown", "{}",
                    ),
                )
                migrated += 1
            else:
                target.execute(
                    "INSERT INTO pending(path,event_kind,queued_at,eligible_at,force,attempts,requested_action) VALUES(?,?,?,?,?,?,'inspect')",
                    (str(media), "migration-retry", now, now, 1, 0),
                )
                queued += 1
        target.execute(
            "INSERT INTO events(event_time,path,status,reason) VALUES(?,?,?,?)",
            (now, "", "Migration", f"Imported {migrated} cached records; queued {queued} files"),
        )
        target.commit()
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"New state database failed integrity check: {integrity}")
        target.close()
        target = None
    except Exception:
        if target is not None:
            target.close()
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
        and row["analysis_signature"] == analysis_signature_for(Path(str(row["path"])))
        and row["size"] == stat.st_size
        and row["mtime_ns"] == stat.st_mtime_ns
        and row["ctime_ns"] == stat.st_ctime_ns
        and row["device"] == stat.st_dev
        and row["inode"] == stat.st_ino
    )


def matches_media(path: Path) -> bool:
    name = path.name
    if path.suffix.lower() not in {".mp4", ".mkv"}:
        return False
    if name.startswith(("TimestampRepairStage-", "TimestampRepairBackup-", "MediaRepairStage-", "MediaRepairBackup-")):
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
        self.current_stage = ""
        self.stage_progress: float | None = None
        self.overall_progress: float | None = None
        self.current_speed = ""
        self.eta_seconds: float | None = None
        self.task_started_at = 0.0
        self.last_reconcile = 0.0
        self.next_reconcile = 0.0
        self.processed_session = 0
        self.pending_count = 0
        self.media_refresh_pending_count = 0
        self.media_refresh_enabled = False

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "pid": os.getpid(), "time": time.time(), "auto_repair": AUTO_REPAIR,
                "watcher_active": self.watcher_active, "watcher_error": self.watcher_error,
                "current_path": self.current_path, "current_action": self.current_action,
                "current_stage": self.current_stage,
                "stage_progress": self.stage_progress,
                "overall_progress": self.overall_progress,
                "speed": self.current_speed,
                "eta_seconds": self.eta_seconds,
                "task_started_at": self.task_started_at,
                "last_reconcile": self.last_reconcile, "next_reconcile": self.next_reconcile,
                "processed_session": self.processed_session, "pending_count": self.pending_count,
                "media_refresh_pending_count": self.media_refresh_pending_count,
                "media_refresh_enabled": self.media_refresh_enabled,
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
        with runtime.lock:
            active = runtime.current_action != "idle"
        runtime.stop_event.wait(1 if active else 10)
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


def process_control_command(state: State, runtime: Runtime, row: sqlite3.Row) -> None:
    command_id = int(row["id"])
    action = str(row["action"])
    state.start_control_command(command_id)
    with runtime.lock:
        runtime.current_action = "manual-command"
        runtime.current_stage = "正在执行手动操作"
        runtime.task_started_at = time.time()
    try:
        if action == "reconcile":
            reconcile(state, runtime)
            detail = "已完成新增和变化文件扫描"
        elif action in {"recheck", "repair", "retry"}:
            raw_ids = json.loads(str(row["file_ids_json"]) or "[]")
            file_ids = [str(value) for value in raw_ids if str(value)][:100]
            paths = state.paths_for_file_ids(file_ids)
            requested_action = "repair" if action in {"repair", "retry"} else "inspect"
            for path in paths:
                if path.exists() and matches_media(path):
                    state.enqueue(path, f"web-{action}", time.time(), True, requested_action)
            detail = f"已加入队列：{len(paths)} 个文件"
        else:
            raise ValueError(f"Unsupported control action: {action}")
    except Exception as exc:
        state.finish_control_command(command_id, "failed", "command_failed", str(exc))
        LOG.exception("Manual command failed: %s", action)
    else:
        state.finish_control_command(command_id, "succeeded", "queued", detail)
    finally:
        with runtime.lock:
            runtime.current_action = "idle"
            runtime.current_stage = ""
            runtime.task_started_at = 0.0
            runtime.pending_count = state.pending_count()


def process_pending(
    state: State,
    runtime: Runtime,
    row: sqlite3.Row,
    *,
    media_refresh_enabled: bool = False,
) -> None:
    path = Path(str(row["path"]))
    with runtime.lock:
        runtime.current_action = "process"
        runtime.current_path = str(path)
        runtime.current_stage = "正在分析文件"
        runtime.stage_progress = None
        runtime.overall_progress = 0.0
        runtime.current_speed = ""
        runtime.eta_seconds = None
        runtime.task_started_at = time.time()
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
        manual_repair = str(row["requested_action"] or "inspect") == "repair"
        if result.status == "Candidate" and (AUTO_REPAIR or manual_repair):
            incompatible = compatibility_reason(result)
            if incompatible:
                result = Analysis(
                    "Uncertain", incompatible, result.info, result.video,
                    result.comparable, result.different,
                    container=result.container, issue_category="unsupported",
                    reason_code="unsupported_streams", diagnostics=result.diagnostics,
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
                        container=result.container, issue_category="unsupported",
                        reason_code="validation_failed", diagnostics=result.diagnostics,
                    )
                else:
                    stat = path.stat()
                    remember_internal_change(path, stat)
                    if repaired_timestamp and repaired_chapter:
                        reason_code = "repaired_multiple"
                    elif repaired_timestamp:
                        reason_code = "repaired_timeline"
                    else:
                        reason_code = "repaired_chapter"
                    result = Analysis(
                        "Repaired",
                        REASON_LABELS[reason_code] + "；所有媒体流均未重新编码，并已在完整校验后替换原文件",
                        result.info, result.video, result.comparable, result.different,
                        container=result.container, issue_category=(
                            "multiple" if repaired_timestamp and repaired_chapter
                            else "timeline" if repaired_timestamp else "chapter"
                        ), reason_code=reason_code, diagnostics=result.diagnostics,
                    )
        state.save(
            path, stat, result.status, result.reason, result.comparable, result.different,
            dropped_data, final_hash, analysis=result,
        )
        state.record(path, result.status, result.reason)
        if result.status == "Repaired" and media_refresh_enabled:
            state.enqueue_media_refresh(path)
            state.record(path, "MediaRefreshQueued", "修复完成，已加入媒体库刷新队列")
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
            runtime.current_stage = ""
            runtime.stage_progress = None
            runtime.overall_progress = None
            runtime.current_speed = ""
            runtime.eta_seconds = None
            runtime.task_started_at = 0.0
            runtime.pending_count = state.pending_count()
            runtime.media_refresh_pending_count = state.media_refresh_pending_count()


def media_refresh_retry_delay(attempts: int) -> int:
    return min(EMBY_REFRESH_MAX_RETRY_SECONDS, EMBY_REFRESH_RETRY_SECONDS * (2 ** min(attempts, 10)))


def process_media_refresh(
    state: State,
    runtime: Runtime,
    client: EmbyRefreshClient,
    row: sqlite3.Row,
) -> None:
    path = Path(str(row["path"]))
    with runtime.lock:
        runtime.current_action = "media-refresh"
        runtime.current_path = str(path)
    try:
        client.refresh(path)
    except Exception as exc:
        reason = compact_error(str(exc))
        attempts = int(row["attempts"])
        delay = media_refresh_retry_delay(attempts)
        state.defer_media_refresh(path, time.time() + delay, reason)
        status = "MediaRefreshWaiting" if isinstance(exc, EmbyItemNotReady) else "MediaRefreshDeferred"
        state.record(path, status, f"{reason}; retry in {delay} seconds")
        if isinstance(exc, EmbyItemNotReady):
            LOG.info("Media-library item is not ready; refresh deferred for %s seconds: %s", delay, path)
        else:
            LOG.warning("Media-library refresh failed; retrying in %s seconds: %s", delay, reason)
    else:
        state.complete_media_refresh(path)
        state.record(path, "MediaRefreshed", "Full media-library refresh requested after repair")
        LOG.info("Media-library refresh requested after repair: %s", path)
    finally:
        with runtime.lock:
            runtime.current_path = ""
            runtime.current_action = "idle"
            runtime.media_refresh_pending_count = state.media_refresh_pending_count()


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
    global ACTIVE_RUNTIME
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
            LOG.warning("Legacy setting %s is ignored by version 3.0", name)

    lock_descriptor = acquire_lock()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    state = State(CONFIG_ROOT / "state.sqlite3")
    runtime = Runtime()
    ACTIVE_RUNTIME = runtime
    media_refresh_client = EmbyRefreshClient.from_environment(MEDIA_ROOT)
    runtime.media_refresh_enabled = media_refresh_client is not None
    chapter_rechecks = state.backfill_chapter_tolerance_rechecks()
    if chapter_rechecks:
        LOG.info("Queued %d prior MP4 repairs for targeted chapter recheck", chapter_rechecks)
    if media_refresh_client is not None:
        backfilled = state.backfill_chapter_media_refreshes()
        if backfilled:
            LOG.info("Queued %d earlier chapter repairs for one-time media-library refresh", backfilled)
    runtime.media_refresh_pending_count = state.media_refresh_pending_count()
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
                    "repair_mkv_timestamps": REPAIR_MKV_TIMESTAMPS,
                    "repair_empty_full_chapters": REPAIR_EMPTY_FULL_CHAPTERS,
                    "supported_formats": ["MP4", "MKV"],
                    "media_refresh_enabled": media_refresh_client is not None,
                    "file_settle_seconds": FILE_SETTLE_SECONDS,
                    "min_file_age_seconds": MIN_FILE_AGE,
                    "reconcile_local_time": RECONCILE_LOCAL_TIME,
                    "name_filter_enabled": bool(NAME_CONTAINS),
                    "show_full_paths": WEB_SHOW_FULL_PATHS,
                },
                wake_callback=WAKE_EVENT.set,
            )
        except Exception:
            LOG.exception("Web UI failed to start; repair service will continue without it")

    LOG.info(
        "Service 3.1.1 started: media=%s auto_repair=%s mkv_timestamps=%s empty_full_chapters=%s media_refresh=%s reconcile=%s",
        MEDIA_ROOT, AUTO_REPAIR, REPAIR_MKV_TIMESTAMPS, REPAIR_EMPTY_FULL_CHAPTERS,
        media_refresh_client is not None, RECONCILE_LOCAL_TIME,
    )
    try:
        reconcile(state, runtime)
        while not STOP_REQUESTED:
            drain_events(state, events)
            command = state.due_control_command()
            if command is not None:
                process_control_command(state, runtime, command)
                if not args.once:
                    continue
            if time.time() >= runtime.next_reconcile:
                reconcile(state, runtime)
            refresh_due = state.due_media_refresh(100 if args.once else 1) if media_refresh_client else []
            if refresh_due:
                for refresh_row in refresh_due:
                    process_media_refresh(state, runtime, media_refresh_client, refresh_row)
                if not args.once:
                    continue
            due = state.due(1 if not args.once else 100)
            if due:
                for pending in due:
                    if STOP_REQUESTED:
                        break
                    process_pending(
                        state, runtime, pending,
                        media_refresh_enabled=media_refresh_client is not None,
                    )
                if args.once:
                    if media_refresh_client:
                        for refresh_row in state.due_media_refresh(100):
                            process_media_refresh(state, runtime, media_refresh_client, refresh_row)
                    break
                continue
            if args.once:
                break
            with runtime.lock:
                runtime.pending_count = state.pending_count()
            next_due = state.next_due()
            refresh_next_due = state.next_media_refresh_due() if media_refresh_client else None
            deadlines = [runtime.next_reconcile]
            if next_due is not None:
                deadlines.append(next_due)
            if refresh_next_due is not None:
                deadlines.append(refresh_next_due)
            deadline = min(deadlines)
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
        ACTIVE_RUNTIME = None
        os.close(lock_descriptor)
    LOG.info("Service stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        LOG.exception("Fatal service error")
        sys.exit(1)
