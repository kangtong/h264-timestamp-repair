FROM ubuntu:24.04

LABEL org.opencontainers.image.title="H.264 Timestamp Repair" \
      org.opencontainers.image.description="Detect and repair missing H.264 composition timestamps in MP4 files without re-encoding video" \
      org.opencontainers.image.source="https://github.com/kangtong/h264-timestamp-repair"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        gpac \
        python3 \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app/ /app/

EXPOSE 8080

ENTRYPOINT ["python3", "/app/repair_service.py"]
