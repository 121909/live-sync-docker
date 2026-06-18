#!/bin/sh
set -eu

if [ "${SSHD_ENABLED:-1}" = "1" ]; then
    SSHD_USER="${SSHD_USER:-debug}"
    SSHD_PASSWORD="${SSHD_PASSWORD:-live-sync}"

    if [ -z "$SSHD_USER" ] || [ -z "$SSHD_PASSWORD" ]; then
        echo "SSHD_USER and SSHD_PASSWORD must not be empty when SSHD_ENABLED=1" >&2
        exit 1
    fi

    if ! id "$SSHD_USER" >/dev/null 2>&1; then
        useradd --create-home --shell /bin/bash "$SSHD_USER"
    fi

    echo "$SSHD_USER:$SSHD_PASSWORD" | chpasswd
    mkdir -p /run/sshd /etc/ssh/sshd_config.d
    ssh-keygen -A >/dev/null

    {
        echo "Port 22"
        echo "ListenAddress 0.0.0.0"
        echo "PermitRootLogin yes"
        echo "PasswordAuthentication yes"
        echo "KbdInteractiveAuthentication no"
        echo "PermitEmptyPasswords no"
        echo "AllowUsers $SSHD_USER"
    } > /etc/ssh/sshd_config.d/99-live-sync-debug.conf

    /usr/sbin/sshd
fi

exec "$@"
