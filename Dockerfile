FROM ubuntu:24.04

LABEL org.opencontainers.image.title="视频完整性修复 | Video Integrity Repair" \
      org.opencontainers.image.description="检测并无损修复视频时间轴与章节元数据问题 | Detect and losslessly repair video timeline and chapter metadata issues" \
      org.opencontainers.image.source="https://github.com/kangtong/h264-timestamp-repair"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/config

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        gpac \
        python3 \
        python3-watchdog \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ /app/

EXPOSE 8080

ENTRYPOINT ["python3", "/app/repair_service.py"]
