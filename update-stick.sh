#!/bin/bash
# Copies the stick's system files (rootfs/etc and rootfs/usr) to a running stick over
# SSH and restarts the home screen, so small changes don't need a rewrite.
#   ./update-stick.sh              the stick on the home network (tvstick.local)
#   ./update-stick.sh 127.0.0.1    the test VM
set -euo pipefail
cd "$(dirname "$0")"
HOST=${1:-tvstick.local}
tar -C rootfs --owner=0 --group=0 --numeric-owner -cf - etc usr | test/ssh.sh "$HOST" '
    tar -C / -xpf - --no-overwrite-dir &&
    systemctl daemon-reload &&
    systemctl restart tvd &&
    { pkill -TERM -u tv -f -- "--profile /home/tv/.mozilla/firefox/tv" || true; }'
echo "Updated $HOST. The browser restarts on its own."
