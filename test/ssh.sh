#!/bin/bash
# SSH into the test VM (test/run-vm.sh), or into the real stick with: test/ssh.sh tvstick.local
host=${1:-127.0.0.1}
shift || true
port=22
# The stick's host key is saved on the first connection and checked after that, so another
# device answering as tvstick.local gets refused. write-stick.sh clears it for a new stick.
# The VM gets new keys with every image, so it isn't checked.
check=(-o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="$HOME/.ssh/known_hosts_tvstick")
if [ "$host" = "127.0.0.1" ]; then
    port=2222
    check=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)
fi
exec ssh -i "$HOME/.ssh/tv_stick_ed25519" -p "$port" "${check[@]}" -o LogLevel=ERROR \
    -o ConnectTimeout=10 "root@$host" "$@"
