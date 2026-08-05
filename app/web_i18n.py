"""Shared localization catalog for the Web UI and JSON API."""

from __future__ import annotations

from typing import Any


STATUS_LABELS = {
    "zh-CN": {
        "Healthy": "正常", "Candidate": "待修复", "Repaired": "已修复",
        "Skipped": "已跳过", "Uncertain": "需要人工确认", "Failed": "处理失败",
        "QueuedRepair": "等待自动修复", "QueuedRecheck": "等待自动复检",
        "WaitingStable": "等待文件写入完成", "MediaRefreshQueued": "等待媒体库刷新",
        "MediaRefreshWaiting": "等待媒体入库", "MediaRefreshDeferred": "媒体库刷新重试",
        "MediaRefreshed": "媒体库已刷新", "Migration": "数据升级",
    },
    "en": {
        "Healthy": "Healthy", "Candidate": "Repair available", "Repaired": "Repaired",
        "Skipped": "Skipped", "Uncertain": "Manual review", "Failed": "Failed",
        "QueuedRepair": "Queued for repair", "QueuedRecheck": "Queued for recheck",
        "WaitingStable": "Waiting for file to settle", "MediaRefreshQueued": "Media refresh queued",
        "MediaRefreshWaiting": "Waiting for media import", "MediaRefreshDeferred": "Media refresh retrying",
        "MediaRefreshed": "Media refreshed", "Migration": "Data migration",
    },
}

ISSUE_LABELS = {
    "zh-CN": {
        "none": "无问题", "timeline": "时间轴异常", "chapter": "章节元数据异常",
        "multiple": "多项异常", "unsupported": "无法自动处理", "failed": "处理失败",
        "other": "其他",
    },
    "en": {
        "none": "No issue", "timeline": "Timeline issue", "chapter": "Chapter metadata issue",
        "multiple": "Multiple issues", "unsupported": "Cannot process automatically", "failed": "Processing failed",
        "other": "Other",
    },
}

REASON_LABELS = {
    "zh-CN": {
        "timeline_bframe_pts": "H.264 B 帧显示时间轴顺序异常",
        "timeline_missing_mp4": "H.264 B 帧显示时间轴顺序异常",
        "timeline_order_mkv": "H.264 B 帧显示时间轴顺序异常",
        "chapter_full_duration": "存在覆盖整段视频的无效空章节",
        "timeline_and_chapter": "同时存在时间轴和章节元数据异常",
        "timestamps_present": "显示时间戳正常", "no_b_frames": "视频不包含需要重排的 B 帧",
        "unsupported_codec": "主视频编码不在自动修复范围内", "no_video": "没有找到主视频流",
        "too_few_samples": "有效时间戳样本不足", "ambiguous_timeline": "抽样结果不一致，无法安全自动判断",
        "validation_failed": "修复结果未通过完整性验证，原文件未被覆盖",
        "variable_fps": "可变或不明确的帧率不能自动修复", "unsupported_streams": "存在无法安全复制的媒体流",
        "repaired_timeline": "已无损重建视频显示时间戳", "repaired_chapter": "已移除无效的整段空章节",
        "repaired_multiple": "已修复时间轴和章节元数据异常", "processing_failed": "处理过程中发生错误",
    },
    "en": {
        "timeline_bframe_pts": "H.264 B-frame presentation timestamps are out of order",
        "timeline_missing_mp4": "H.264 B-frame presentation timestamps are out of order",
        "timeline_order_mkv": "H.264 B-frame presentation timestamps are out of order",
        "chapter_full_duration": "An invalid empty chapter spans the full video",
        "timeline_and_chapter": "Timeline and chapter metadata issues are both present",
        "timestamps_present": "Presentation timestamps are valid", "no_b_frames": "The video has no B-frames that require reordering",
        "unsupported_codec": "The primary video codec is outside the automatic repair scope", "no_video": "No primary video stream was found",
        "too_few_samples": "Not enough valid timestamp samples", "ambiguous_timeline": "Samples disagree, so an automatic decision is unsafe",
        "validation_failed": "The repaired output failed integrity validation; the original file was preserved",
        "variable_fps": "Variable or indeterminate frame rate cannot be repaired automatically", "unsupported_streams": "Some media streams cannot be copied safely",
        "repaired_timeline": "Video presentation timestamps were rebuilt without re-encoding", "repaired_chapter": "The invalid full-duration empty chapter was removed",
        "repaired_multiple": "Timeline and chapter metadata issues were repaired", "processing_failed": "An error occurred during processing",
    },
}

STAGE_SOURCE_TO_CODE = {
    "": "idle",
    "正在执行手动操作": "manual_command",
    "正在分析文件": "analyzing_file",
    "正在记录非视频流指纹": "fingerprinting_auxiliary_streams",
    "正在无损抽取视频": "extracting_video",
    "正在重建显示时间戳": "rebuilding_timestamps",
    "显示时间戳重建完成": "timestamps_rebuilt",
    "正在重新封装媒体流": "remuxing_streams",
    "正在验证视频码流": "verifying_video_bitstream",
    "正在比较画面码流指纹": "comparing_video_fingerprints",
    "正在校验原始画面码流": "hashing_original_video",
    "正在校验修复后画面码流": "hashing_repaired_video",
    "正在验证时间轴和媒体流": "validating_timeline_and_streams",
    "正在安全替换原文件": "replacing_original",
    "正在复制修复文件": "copying_repaired_file",
    "正在计算修复文件哈希": "hashing_repaired_file",
    "正在验证暂存文件哈希": "verifying_staged_file",
    "修复完成": "repair_complete",
}

STAGE_LABELS = {
    "zh-CN": {
        "idle": "服务空闲", "processing": "正在处理", "manual_command": "正在执行手动操作",
        "analyzing_file": "正在分析文件", "fingerprinting_auxiliary_streams": "正在记录非视频流指纹",
        "extracting_video": "正在无损抽取视频", "rebuilding_timestamps": "正在重建显示时间戳",
        "timestamps_rebuilt": "显示时间戳重建完成", "remuxing_streams": "正在重新封装媒体流",
        "verifying_video_bitstream": "正在验证视频码流", "comparing_video_fingerprints": "正在比较画面码流指纹",
        "hashing_original_video": "正在校验原始画面码流", "hashing_repaired_video": "正在校验修复后画面码流",
        "validating_timeline_and_streams": "正在验证时间轴和媒体流", "replacing_original": "正在安全替换原文件",
        "copying_repaired_file": "正在复制修复文件", "hashing_repaired_file": "正在计算修复文件哈希",
        "verifying_staged_file": "正在验证暂存文件哈希", "repair_complete": "修复完成",
    },
    "en": {
        "idle": "Service idle", "processing": "Processing", "manual_command": "Running manual action",
        "analyzing_file": "Analyzing file", "fingerprinting_auxiliary_streams": "Fingerprinting non-video streams",
        "extracting_video": "Extracting video losslessly", "rebuilding_timestamps": "Rebuilding presentation timestamps",
        "timestamps_rebuilt": "Presentation timestamps rebuilt", "remuxing_streams": "Remuxing media streams",
        "verifying_video_bitstream": "Verifying video bitstream", "comparing_video_fingerprints": "Comparing video fingerprints",
        "hashing_original_video": "Hashing original video", "hashing_repaired_video": "Hashing repaired video",
        "validating_timeline_and_streams": "Validating timeline and media streams", "replacing_original": "Safely replacing original file",
        "copying_repaired_file": "Copying repaired file", "hashing_repaired_file": "Hashing repaired file",
        "verifying_staged_file": "Verifying staged file", "repair_complete": "Repair complete",
    },
}

UI = {
    "zh-CN": {
        "app.title": "视频完整性修复", "app.subtitle": "无损检测与修复", "nav.label": "功能菜单",
        "nav.overview": "概览", "nav.problems": "问题文件", "nav.files": "全部文件", "nav.tasks": "任务中心", "nav.settings": "运行设置",
        "security.lan": "界面无登录验证，请仅在可信局域网使用。", "header.eyebrow": "视频时间轴与元数据",
        "state.connecting": "正在连接", "action.scan": "立即扫描变化", "action.refresh": "刷新页面",
        "summary.label": "状态汇总", "summary.total": "已检查文件", "summary.totalHint": "MP4 与 MKV", "summary.candidate": "待修复",
        "summary.candidateHint": "安全候选文件", "summary.repaired": "已修复", "summary.repairedHint": "无损修复完成",
        "summary.failed": "处理失败", "summary.failedHint": "可以手动重试", "work.current": "当前工作",
        "work.file": "当前文件", "work.progress": "进度", "work.waiting": "等待状态", "work.speed": "速度与剩余时间", "work.queue": "等待队列",
        "issues.kicker": "问题分类", "issues.attention": "需要关注", "action.viewAll": "查看全部", "events.kicker": "重要事件", "events.recent": "最近处理",
        "problems.kicker": "按原因分类", "filter.allIssues": "全部问题", "filter.timeline": "时间轴异常", "filter.chapter": "章节异常",
        "filter.multiple": "多项异常", "filter.unsupported": "无法自动处理", "filter.failed": "处理失败",
        "selection.none": "尚未选择文件", "action.recheck": "重新检测", "action.repair": "无损修复", "action.retry": "重试失败任务",
        "files.kicker": "媒体记录", "search.label": "搜索", "search.placeholder": "文件名或原因", "filter.status": "状态", "filter.allStatuses": "全部状态",
        "filter.format": "格式", "filter.allFormats": "全部格式", "tasks.kicker": "工作队列", "settings.kicker": "只读配置",
        "settings.formats": "支持格式", "settings.autoRepair": "自动修复", "settings.mkv": "MKV 时间戳修复", "settings.reconcile": "每日校准",
        "settings.settle": "文件静默期", "settings.minAge": "最小文件年龄", "settings.mediaRefresh": "媒体库刷新", "settings.paths": "路径显示",
        "settings.note": "配置由 Docker 环境变量管理。修改后需要重新创建容器才能生效。", "footer.refresh": "工作时自动快速刷新", "footer.never": "尚未更新",
        "language.label": "界面语言", "language.zh": "中文", "language.en": "English", "empty.events": "暂无重要事件", "empty.tasks": "暂无手动任务",
        "empty.files": "没有符合条件的文件", "common.unknown": "未知", "common.systemTask": "系统任务", "common.on": "已开启", "common.off": "已关闭",
        "common.enabledRetry": "已启用，失败自动重试", "common.notConfigured": "未配置", "common.fullPaths": "显示完整路径", "common.fileNames": "仅显示文件名",
        "common.daily": "每天 {value}", "common.hours": "{value} 小时", "common.minutes": "{value} 分钟", "common.seconds": "{value} 秒",
        "work.runningStage": "正在执行当前阶段", "work.noTask": "没有正在处理的任务", "work.remaining": "剩余约 {value}", "work.calculating": "计算中",
        "work.queueText": "{files} 个文件；{refreshes} 个媒体库刷新", "state.normal": "监听正常", "state.degraded": "降级运行", "state.stale": "服务状态过期",
        "mode.repair": "自动修复", "mode.scan": "仅检测", "files.resultCount": "显示 {shown} / {total}", "files.select": "选择 {path}",
        "files.samples": "逐帧样本 {transitions}，顺序异常 {errors}", "files.checked": "检查时间 {value}", "action.recheckShort": "复检", "action.repairShort": "修复", "action.retryShort": "重试",
        "task.reconcile": "扫描变化", "task.recheck": "重新检测", "task.repair": "无损修复", "task.retry": "重试失败任务",
        "task.queued": "等待执行", "task.running": "正在执行", "task.succeeded": "已完成", "task.failed": "失败", "task.queuedDetail": "已加入持久化任务队列",
        "task.scanCompleted": "已完成新增和变化文件扫描", "task.filesQueued": "已加入队列：{count} 个文件",
        "selection.count": "已选择 {count} 个文件", "dialog.selectFirst": "请先选择文件", "dialog.confirm": "确认{action}所选的 {count} 个文件吗？",
        "dialog.repair": "无损修复并在校验通过后覆盖原文件", "dialog.queued": "操作已加入任务队列", "dialog.scan": "立即扫描媒体目录中的新增和变化文件吗？",
        "error.request": "请求失败：{status}", "error.load": "加载失败：{message}", "footer.updated": "更新于 {time}",
    },
    "en": {
        "app.title": "Video Integrity Repair", "app.subtitle": "Lossless detection and repair", "nav.label": "Feature menu",
        "nav.overview": "Overview", "nav.problems": "Problem files", "nav.files": "All files", "nav.tasks": "Task center", "nav.settings": "Settings",
        "security.lan": "This interface has no login. Use it only on a trusted LAN.", "header.eyebrow": "Video timeline and metadata",
        "state.connecting": "Connecting", "action.scan": "Scan for changes", "action.refresh": "Refresh",
        "summary.label": "Status summary", "summary.total": "Files checked", "summary.totalHint": "MP4 and MKV", "summary.candidate": "Repair available",
        "summary.candidateHint": "Safe repair candidates", "summary.repaired": "Repaired", "summary.repairedHint": "Lossless repair completed",
        "summary.failed": "Failed", "summary.failedHint": "Manual retry available", "work.current": "Current task",
        "work.file": "Current file", "work.progress": "Progress", "work.waiting": "Waiting for status", "work.speed": "Speed and time remaining", "work.queue": "Queue",
        "issues.kicker": "Issue categories", "issues.attention": "Needs attention", "action.viewAll": "View all", "events.kicker": "Important events", "events.recent": "Recent activity",
        "problems.kicker": "Grouped by cause", "filter.allIssues": "All issues", "filter.timeline": "Timeline", "filter.chapter": "Chapters",
        "filter.multiple": "Multiple", "filter.unsupported": "Cannot auto-process", "filter.failed": "Failed",
        "selection.none": "No files selected", "action.recheck": "Recheck", "action.repair": "Lossless repair", "action.retry": "Retry failed tasks",
        "files.kicker": "Media records", "search.label": "Search", "search.placeholder": "File name or reason", "filter.status": "Status", "filter.allStatuses": "All statuses",
        "filter.format": "Format", "filter.allFormats": "All formats", "tasks.kicker": "Work queue", "settings.kicker": "Read-only configuration",
        "settings.formats": "Supported formats", "settings.autoRepair": "Automatic repair", "settings.mkv": "MKV timestamp repair", "settings.reconcile": "Daily reconciliation",
        "settings.settle": "File quiet period", "settings.minAge": "Minimum file age", "settings.mediaRefresh": "Media library refresh", "settings.paths": "Path display",
        "settings.note": "Configuration is managed by Docker environment variables. Recreate the container after changes.", "footer.refresh": "Refreshes faster while working", "footer.never": "Not updated yet",
        "language.label": "Interface language", "language.zh": "中文", "language.en": "English", "empty.events": "No important events", "empty.tasks": "No manual tasks",
        "empty.files": "No matching files", "common.unknown": "Unknown", "common.systemTask": "System task", "common.on": "Enabled", "common.off": "Disabled",
        "common.enabledRetry": "Enabled; failures retry automatically", "common.notConfigured": "Not configured", "common.fullPaths": "Full paths", "common.fileNames": "File names only",
        "common.daily": "Daily at {value}", "common.hours": "{value} hours", "common.minutes": "{value} minutes", "common.seconds": "{value} seconds",
        "work.runningStage": "Running current stage", "work.noTask": "No active task", "work.remaining": "About {value} remaining", "work.calculating": "Calculating",
        "work.queueText": "{files} files; {refreshes} media refreshes", "state.normal": "Watcher active", "state.degraded": "Degraded mode", "state.stale": "Service status is stale",
        "mode.repair": "Automatic repair", "mode.scan": "Scan only", "files.resultCount": "Showing {shown} / {total}", "files.select": "Select {path}",
        "files.samples": "Frame samples {transitions}; ordering errors {errors}", "files.checked": "Checked {value}", "action.recheckShort": "Recheck", "action.repairShort": "Repair", "action.retryShort": "Retry",
        "task.reconcile": "Scan for changes", "task.recheck": "Recheck", "task.repair": "Lossless repair", "task.retry": "Retry failed tasks",
        "task.queued": "Queued", "task.running": "Running", "task.succeeded": "Completed", "task.failed": "Failed", "task.queuedDetail": "Added to the persistent task queue",
        "task.scanCompleted": "Scan for new and changed files completed", "task.filesQueued": "Queued {count} files",
        "selection.count": "{count} files selected", "dialog.selectFirst": "Select at least one file", "dialog.confirm": "Confirm {action} for {count} selected files?",
        "dialog.repair": "losslessly repair and replace originals only after validation", "dialog.queued": "Action added to the task queue", "dialog.scan": "Scan the media folder for new and changed files now?",
        "error.request": "Request failed: {status}", "error.load": "Load failed: {message}", "footer.updated": "Updated at {time}",
    },
}


def normalize_locale(value: str | None) -> str:
    """Return one of the two supported locale identifiers."""
    return "zh-CN" if not value or value.lower().startswith("zh") else "en"


def catalog(locale: str | None) -> dict[str, Any]:
    selected = normalize_locale(locale)
    return {
        "locale": selected,
        "ui": UI[selected],
        "statuses": STATUS_LABELS[selected],
        "issues": ISSUE_LABELS[selected],
        "reasons": REASON_LABELS[selected],
        "stages": STAGE_LABELS[selected],
    }


def stage_code(source_label: str, active: bool = False) -> str:
    return STAGE_SOURCE_TO_CODE.get(source_label, "processing" if active else "idle")
