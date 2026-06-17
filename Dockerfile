FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      ffmpeg \
      python3 \
      python3-opencv \
      python3-pil \
      tesseract-ocr \
      tini && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY scripts/ /app/scripts/
COPY app/ /app/app/

RUN chmod +x /app/scripts/*.sh /app/scripts/*.py /app/app/server.py && \
    mkdir -p /state /hls /tmp/live_4k_delay

ENV OFFSET_STATE=/state/last_sync_offset.json \
    OUT_DIR=/hls \
    WORK_DIR=/tmp/live_4k_delay \
    STATE_DIR=/state \
    SCRIPTS_ROOT=/app/scripts \
    PORT=18080 \
    MODE=hls

EXPOSE 18080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/app/app/server.py"]
