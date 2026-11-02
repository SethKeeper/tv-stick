#!/bin/bash
# SSH into the test VM (test/run-vm.sh), or into the real stick with: test/ssh.sh tvstick.local
host=${1:-127.0.0.1}
shift || true
port=22
[ "$host" = "127.0.0.1" ] && port=2222
exec ssh -i "$HOME/.ssh/tv_stick_ed25519" -p "$port" -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10 "root@$host" "$@"
