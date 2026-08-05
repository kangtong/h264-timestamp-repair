# Video Integrity Repair

[中文文档](https://github.com/kangtong/h264-timestamp-repair/blob/main/README.md)

A general-purpose Docker and Linux service that detects and losslessly repairs H.264 B-frame timeline errors and invalid chapter metadata. Video and audio are never re-encoded.

## Supported issues

- MP4: missing or incorrectly ordered B-frame presentation timestamps.
- MKV: container timecodes incorrectly follow decode order, causing repeated, skipped, or uneven playback.
- MP4: a single untitled, empty chapter spanning the full duration.
- Optional Emby notification after repair to refresh media information and chapter thumbnails.

MP4 and MKV share the same timeline-integrity core. The final decision is based on whether decoded frame PTS values increase strictly in presentation order. Container-specific logic is limited to fast pre-screening and remux adapters.

## Version 3.1 features

- Watches create, write, move, rename, and delete events without repeatedly probing the full library.
- Performs a daily directory and file-identity reconciliation; unchanged files hit the SQLite cache directly.
- Chinese and English Web UI that follows the browser language on first use and remembers manual selection.
- Manual scan, per-file recheck, single or batch repair, and failed-task retry controls.
- Live progress for analysis, extraction, timestamp rebuilding, remuxing, validation, copying, and replacement.
- Persistent manual-task, scan, and media-library refresh queues in SQLite.
- Atomically replaces the original only after timeline, bitstream hash, stream-property, and sample-decode validation pass.
- Web API exposes stable status, reason, and task-stage codes with Chinese or English labels.

### Version 3.1.0

- The public project now contains only generic Docker deployment files, with no device- or platform-specific configuration.
- Added complete English documentation, bilingual container metadata, and automatic Docker Hub description synchronization.
- Added browser-language detection, manual language switching, and local persistence.
- Added `/api/i18n`, the API `lang` parameter, and stable `current_stage_code` values.

> The Web UI has no login authentication. Expose it only on a trusted LAN, never directly to the Internet.

## Quick start

```bash
mkdir -p video-integrity-repair/config video-integrity-repair/work
cd video-integrity-repair
curl -O https://raw.githubusercontent.com/kangtong/h264-timestamp-repair/main/docker-compose.hub.yml
```

Create `.env`:

```dotenv
MEDIA_HOST_PATH=/srv/media
DOCKER_IMAGE=kangtong1993/h264-timestamp-repair:3.1.0
TZ=Asia/Shanghai
AUTO_REPAIR=false
REPAIR_MKV_TIMESTAMPS=true
REPAIR_EMPTY_FULL_CHAPTERS=true
MIN_FILE_AGE_SECONDS=3600
FILE_SETTLE_SECONDS=60
RECONCILE_LOCAL_TIME=04:00
WEB_UI_HOST_PORT=8080
```

Start the service:

```bash
docker compose -f docker-compose.hub.yml up -d
```

Start with `AUTO_REPAIR=false` to review detection results. Set it to `true` and recreate the container when ready. The Web UI is available at `http://server-address:8080/`.

## Automatic Emby refresh

Store the API key in `config/emby-api-key` instead of committing it to Git:

```dotenv
EMBY_URL=http://media-server:8096/emby
EMBY_MEDIA_ROOT=/path-seen-by-emby
EMBY_API_KEY_FILE=/config/emby-api-key
EMBY_REFRESH_RETRY_SECONDS=60
EMBY_REFRESH_MAX_RETRY_SECONDS=21600
```

`MEDIA_ROOT` is the path visible inside this container, while `EMBY_MEDIA_ROOT` is the corresponding root visible to Emby. A refresh is requested only when the complete path has one exact match. Refresh failures retry independently and do not change the file-repair result.

## Main configuration

| Variable | Default | Description |
|---|---:|---|
| `MEDIA_HOST_PATH` | required | Media directory on the Docker host |
| `AUTO_REPAIR` | `false` | Automatically repair every confirmed candidate |
| `REPAIR_MKV_TIMESTAMPS` | `true` | Enable MKV timestamp detection and repair |
| `REPAIR_EMPTY_FULL_CHAPTERS` | `true` | Detect and remove invalid full-duration empty chapters |
| `MIN_FILE_AGE_SECONDS` | `3600` | Minimum file age before processing |
| `FILE_SETTLE_SECONDS` | `60` | Quiet period after the last file event |
| `RECONCILE_LOCAL_TIME` | `04:00` | Daily file-identity reconciliation time |
| `SAMPLE_SECONDS` | `8` | Seconds sampled near the start, middle, and end |
| `MINIMUM_PACKETS` | `60` | Minimum valid samples for an automatic decision |
| `KEEP_TEMP_ON_FAILURE` | `false` | Keep work files after a failure |
| `WEB_UI_HOST_PORT` | `8080` | Host Web port |
| `WEB_SHOW_FULL_PATHS` | `false` | Show full paths in the Web UI and API |

## Web API

- `GET /api/i18n?lang=zh-CN|en` returns the complete language catalog.
- `GET /api/status`, `/api/files`, `/api/history`, and `/api/tasks` accept an optional `lang` parameter.
- Requests without `lang` default to `zh-CN` for backward compatibility.
- Responses retain existing code fields and add `locale` plus localized labels.
- The status heartbeat returns both `current_stage_code` and `current_stage_label`.

## Persistent data and upgrades

- `config/state.sqlite3`: file identities, analysis results, scan queue, manual tasks, and media-library refresh queue.
- `config/heartbeat.json`: health information and current-task state.
- `work/job-*`: temporary repair files, removed automatically after success.

The database migrates automatically from older releases. Unchanged MP4 and MKV files continue to use cached results. Back up `config/state.sqlite3` before upgrading.

## Safety boundaries

- Automatic timeline repair applies only to a single primary H.264 video stream with B-frames and a fixed or clearly known frame rate.
- Files with too few samples, variable frame rate, conflicting conclusions, or streams that cannot be copied safely are not repaired automatically.
- Every picture-carrying H.264 VCL NAL must match before and after repair; SPS, PPS, SEI, and other parameter units are also validated by type.
- Manual repair controls cannot bypass detection, compatibility, or integrity validation.
- Project and test data use generic names and contain no user media paths, content names, keys, or device details.

Images are published for `linux/amd64` and `linux/arm64` and do not depend on a particular storage device, media server, or content type.
