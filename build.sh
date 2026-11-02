#!/bin/bash
# Builds out/tv-stick.img inside a Linux VM on this Mac (Lima, "tvbuild").
# ./build.sh --fresh rebuilds the cached Debian base too.
set -euo pipefail
cd "$(dirname "$0")"
VM=tvbuild
KEY=~/.ssh/tv_stick_ed25519

mkdir -p out
[ -f "$KEY" ] || ssh-keygen -q -t ed25519 -N "" -C "tv-stick remote help" -f "$KEY"
cp "$KEY.pub" out/authorized_keys

if ! limactl list -q 2>/dev/null | grep -qx "$VM"; then
    limactl start --name="$VM" --tty=false --vm-type=vz --rosetta --cpus=8 --memory=16 --disk=60 \
        --mount-only="$PWD:w" template:debian-13
elif [ "$(limactl list --format '{{.Status}}' "$VM")" != "Running" ]; then
    limactl start --tty=false "$VM"
fi

limactl shell --workdir / "$VM" sudo bash "$PWD/vm/build-image.sh" "$@"
