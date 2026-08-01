# H.264 Timestamp Repair

一个通用的 Docker 服务，用于检测并修复 MP4 文件中缺失或错误的 H.264 合成时间戳。此类文件通常包含 B 帧，但数据包的 PTS 与 DTS 始终相同，可能在直接播放时表现为节奏不均匀、轻微停顿或类似掉帧的现象。

项目不针对任何特定媒体内容、文件命名方式、播放器品牌或 NAS 系统。

## 核心特性

- 递归扫描媒体目录中的 MP4 文件，默认不限制文件名。
- 对视频开头、中间和结尾进行数据包时间戳采样，减少误判。
- 仅处理单路主 H.264 视频、恒定帧率且包含 B 帧的安全候选文件。
- 使用 MP4Box 重建合成时间戳，不重新编码视频码流。
- 音频、兼容字幕、章节和元数据通过码流复制保留。
- 修复后检查轨道、视频属性、帧数、时间戳和多段解码结果。
- 在媒体目录中暂存并校验 SHA-256，然后原子替换原文件；失败时自动回滚。
- 使用 SQLite 增量缓存，未变化的文件不会重复分析。
- 支持 `linux/amd64` 和 `linux/arm64`。

## 快速部署

公开镜像：

```text
kangtong1993/h264-timestamp-repair:1.0.0
```

复制示例配置：

```bash
cp .env.example .env
```

编辑 `.env`，至少设置 Docker 主机上的媒体目录绝对路径：

```env
MEDIA_HOST_PATH=/srv/media
```

建议先保持扫描模式：

```env
AUTO_REPAIR=false
```

启动并查看报告，确认识别结果符合预期：

```bash
docker compose -f docker-compose.hub.yml up -d
docker compose -f docker-compose.hub.yml logs -f --tail=100
```

报告保存在 `config/latest.csv`。确认候选文件后，将 `.env` 改为：

```env
AUTO_REPAIR=true
```

然后重新创建服务：

```bash
docker compose -f docker-compose.hub.yml up -d --force-recreate
```

任何支持 Docker Compose 的 Linux 服务器、家用服务器或 NAS 都可以采用相同方式部署。

## 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MEDIA_HOST_PATH` | 无 | Docker 主机上的媒体目录绝对路径 |
| `DOCKER_IMAGE` | `kangtong1993/h264-timestamp-repair:1.0.0` | 使用的容器镜像 |
| `AUTO_REPAIR` | `false` | `false` 仅扫描，`true` 自动修复并覆盖原文件 |
| `NAME_CONTAINS` | 空 | 可选文件名过滤；为空时扫描所有 MP4 |
| `SCAN_INTERVAL_SECONDS` | `1800` | 两轮扫描之间的等待时间 |
| `MIN_FILE_AGE_SECONDS` | `3600` | 新文件至少需要稳定存在的时间 |
| `STABLE_SCANS` | `1` | 文件大小和修改时间需要保持不变的扫描次数 |
| `SAMPLE_SECONDS` | `8` | 每个时间戳采样区间的秒数 |
| `MINIMUM_PACKETS` | `60` | 判定所需的最少可比较数据包数 |
| `RETRY_FAILED_AFTER_SECONDS` | `86400` | 失败文件的重试间隔 |
| `KEEP_TEMP_ON_FAILURE` | `false` | 失败时是否保留工作文件用于诊断 |

## 判定逻辑

文件需要同时满足以下条件才会成为自动修复候选：

1. 容器中只有一路主视频轨道，且编码为 H.264。
2. 视频轨道声明包含 B 帧。
3. 开头、中间和结尾的采样中有足够多可比较数据包。
4. 所有采样数据包均表现为 `PTS = DTS`，即缺少正常的合成时间偏移。
5. 平均帧率与标称帧率一致，不属于可变或含糊帧率。
6. 不包含无法安全复制回 MP4 的字幕轨道。

其他文件会标记为 `Healthy`、`Skipped` 或 `Uncertain`，不会自动修改。

## 修复与安全机制

修复过程会先从源文件提取 H.264 码流，再用原始标称帧率重建 MP4 视频轨道，最后复制其他兼容轨道和元数据。视频不会重新编码，因此不会产生有损画质变化。

容器需要对媒体目录具有读写权限。替换前会验证输出文件，并在源文件所在目录执行暂存、备份和原子改名。任何安装后验证失败都会恢复原文件。即使如此，首次使用前仍建议准备独立备份，并先运行扫描模式。

处理期间，工作目录通常需要至少约源文件大小 2.25 倍的可用空间；另预留 1 GiB 安全余量。

## 状态与日志

- `config/latest.csv`：当前文件状态汇总。
- `config/history.jsonl`：每次新分析或修复事件。
- `config/state.sqlite3`：增量扫描缓存。
- `config/heartbeat.json`：健康检查和当前扫描状态。
- `work/`：修复期间的临时文件。

## 更新镜像

固定版本标签适合稳定部署：

```env
DOCKER_IMAGE=kangtong1993/h264-timestamp-repair:1.0.0
```

拉取更新并重新创建服务：

```bash
docker compose -f docker-compose.hub.yml pull
docker compose -f docker-compose.hub.yml up -d --force-recreate
```

## 自行构建与发布

本地构建：

```bash
docker compose up -d --build
```

公开仓库包含手动触发的 GitHub Actions 工作流，可发布 `amd64` 和 `arm64` 镜像。需要配置：

- Repository variable：`DOCKERHUB_USERNAME`
- Repository secret：`DOCKERHUB_TOKEN`

也可以在装有 Docker Buildx 的环境中运行 `publish-to-dockerhub.sh`。
