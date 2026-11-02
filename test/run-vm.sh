#!/bin/bash
# Boots a copy of out/tv-stick.img in an emulated x86-64 PC (UEFI firmware, image
# attached as a USB stick) to check the boot and the home screen on this Mac.
# Emulation is slow: give it a few minutes. Screen: test/qmp.py shot out/test/screen.png
# SSH: ssh -i ~/.ssh/tv_stick_ed25519 -p 2222 root@127.0.0.1   Stop: test/run-vm.sh stop
set -euo pipefail
cd "$(dirname "$0")/.."
T=out/test
QEMU_SHARE=$(brew --prefix qemu)/share/qemu
mkdir -p "$T"
if [ -f "$T/qemu.pid" ]; then
    kill "$(cat "$T/qemu.pid")" 2>/dev/null || true
    rm -f "$T/qemu.pid"
    sleep 1
fi
[ "${1:-}" = "stop" ] && exit 0

rm -f "$T/stick.img" "$T/serial.log"
cp -c out/tv-stick.img "$T/stick.img"  # APFS clone, instant
truncate -s 16G "$T/stick.img"         # a bigger "stick", so first boot has to grow the partition
cp "$QEMU_SHARE/edk2-i386-vars.fd" "$T/vars.fd"

qemu-system-x86_64 \
    -machine q35 -accel tcg,thread=multi,tb-size=2048 -cpu max -smp 4 -m 4096 \
    -drive if=pflash,format=raw,readonly=on,file="$QEMU_SHARE/edk2-x86_64-code.fd" \
    -drive if=pflash,format=raw,file="$T/vars.fd" \
    -device qemu-xhci,id=xhci \
    -drive if=none,id=stick,format=raw,file="$T/stick.img" \
    -device usb-storage,bus=xhci.0,drive=stick,removable=on,bootindex=0 \
    -device usb-tablet,bus=xhci.0 -device usb-kbd,bus=xhci.0 \
    -device virtio-vga,xres=1920,yres=1080 \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 -device virtio-net-pci,netdev=net0 \
    -audiodev none,id=snd -device intel-hda -device hda-output,audiodev=snd \
    -display none -vnc 127.0.0.1:7 \
    -qmp unix:"$PWD/$T/qmp.sock",server=on,wait=off \
    -serial file:"$PWD/$T/serial.log" \
    -daemonize -pidfile "$PWD/$T/qemu.pid"
echo "Booting. VNC 127.0.0.1:5907, SSH port 2222, screenshots with test/qmp.py shot FILE"
