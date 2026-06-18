FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      ffmpeg \
      openssh-server \
      python3 \
      python3-opencv \
      python3-pil \
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
    MODE=hls \
    SSHD_ENABLED=1 \
    SSHD_USER=root \
    SSHD_PASSWORD=live-sync

EXPOSE 18080 22

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/scripts/docker-entrypoint.sh", "python3", "/app/app/server.py"]
