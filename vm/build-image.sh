#!/bin/bash
# Builds out/tv-stick.img: Debian 13 for x86-64 PCs that boots from a USB stick
# straight into the TV home screen. Runs as root inside the "tvbuild" Lima VM,
# an arm64 Debian where Rosetta runs the amd64 parts. Start it with ./build.sh.
set -euo pipefail

SRC=$(cd "$(dirname "$0")/.." && pwd)
OUT=$SRC/out
WORK=/var/tmp/tvbuild
ROOT=$WORK/rootfs
CACHE=/var/tmp/tvbuild-cache
SUITE=trixie
MIRROR=http://deb.debian.org/debian
SECURITY=http://security.debian.org/debian-security
COMPONENTS="main contrib non-free non-free-firmware"
TV_HOSTNAME=tvstick
TIMEZONE=Europe/Berlin
MOZILLA_KEY_FPR=35BAA0B33E9EB396F59CA838C0BA5CE6DC6315A3
ESP_MB=256

log() { printf '\n==> %s\n' "$*"; }
in_root() {
    chroot "$ROOT" /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin HOME=/root \
        LANG=C.UTF-8 DEBIAN_FRONTEND=noninteractive "$@"
}
unmount_all() {
    for m in run sys proc dev/pts dev; do
        if mountpoint -q "$ROOT/$m" 2>/dev/null; then umount -l "$ROOT/$m"; fi
    done
}

[ "$(id -u)" = 0 ] || { echo "Run as root."; exit 1; }
[ -s "$OUT/authorized_keys" ] || { echo "Missing $OUT/authorized_keys. Run ./build.sh on the Mac."; exit 1; }

log "Build tools"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq mmdebstrap arch-test dosfstools mtools e2fsprogs gdisk curl \
    ca-certificates gpg python3 unzip >/dev/null
arch-test amd64 >/dev/null || { echo "This VM can't run amd64 programs. Start it with --rosetta."; exit 1; }

mount_all() {
    mount --bind /dev "$ROOT/dev"
    mount --bind /dev/pts "$ROOT/dev/pts"
    mount -t proc proc "$ROOT/proc"
    mount -t sysfs sys "$ROOT/sys"
    mount -t tmpfs tmpfs "$ROOT/run"
    rm -f "$ROOT/etc/resolv.conf"
    cp -L /etc/resolv.conf "$ROOT/etc/resolv.conf"
    printf '#!/bin/sh\nexit 101\n' > "$ROOT/usr/sbin/policy-rc.d"
    chmod 755 "$ROOT/usr/sbin/policy-rc.d"
}

trap unmount_all EXIT
unmount_all
rm -rf "$WORK"
mkdir -p "$WORK" "$OUT" "$CACHE"

# The base system (Debian packages, Firefox, uBlock Origin) is cached, so changing
# the TV files only takes a minute. ./build.sh --fresh starts from scratch.
KEY=$(sha256sum "$SRC/vm/packages.txt" | cut -c1-16)
BASE_TAR="$CACHE/base-$KEY.tar"
if [ "${1:-}" != "--fresh" ] && [ -s "$BASE_TAR" ]; then
    log "Reusing the cached base system"
    mkdir -p "$ROOT"
    tar -C "$ROOT" --xattrs --xattrs-include='*' --numeric-owner -xpf "$BASE_TAR"
    mount_all
else
    log "Debian base system for amd64 (the slow part)"
    PKGS=$(sed -e 's/#.*//' "$SRC/vm/packages.txt" | xargs | tr ' ' ',')
    mmdebstrap --mode=root --arch=amd64 --variant=minbase \
        --include="$PKGS" \
        --dpkgopt='path-exclude=/usr/share/man/*' \
        --dpkgopt='path-exclude=/usr/share/info/*' \
        --dpkgopt='path-exclude=/usr/share/doc/*' \
        --dpkgopt='path-include=/usr/share/doc/*/copyright' \
        "$SUITE" "$ROOT" \
        "deb $MIRROR $SUITE $COMPONENTS" \
        "deb $MIRROR $SUITE-updates $COMPONENTS" \
        "deb $SECURITY $SUITE-security $COMPONENTS"
    mount_all

    log "Firefox from Mozilla's repository"
    install -d -m 755 "$ROOT/etc/apt/keyrings"
    curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg \
        -o "$ROOT/etc/apt/keyrings/packages.mozilla.org.asc"
    fpr=$(gpg --show-keys --with-colons "$ROOT/etc/apt/keyrings/packages.mozilla.org.asc" 2>/dev/null \
        | awk -F: '/^fpr/ {print $10; exit}')
    [ "$fpr" = "$MOZILLA_KEY_FPR" ] || { echo "Mozilla key is $fpr, expected $MOZILLA_KEY_FPR"; exit 1; }
    echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
        > "$ROOT/etc/apt/sources.list.d/mozilla.list"
    printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > "$ROOT/etc/apt/preferences.d/mozilla"
    in_root apt-get update -qq
    in_root apt-get install -y -qq --no-install-recommends firefox

    log "uBlock Origin"
    install -d "$ROOT/usr/local/share/tv"
    curl -fsSL -o "$ROOT/usr/local/share/tv/ublock-origin.xpi" \
        https://addons.mozilla.org/firefox/downloads/latest/ublock-origin/latest.xpi
    unzip -p "$ROOT/usr/local/share/tv/ublock-origin.xpi" manifest.json | python3 -c '
import json, sys
m = json.load(sys.stdin)
gecko = (m.get("browser_specific_settings") or m.get("applications") or {}).get("gecko", {})
assert gecko.get("id") == "uBlock0@raymondhill.net", gecko
print("uBlock Origin", m["version"])'

    log "Saving the base system for faster rebuilds"
    in_root apt-get clean
    unmount_all
    rm -f "$CACHE"/base-*.tar
    tar -C "$ROOT" --xattrs --xattrs-include='*' --numeric-owner -cf "$BASE_TAR.part" .
    mv "$BASE_TAR.part" "$BASE_TAR"
    mount_all
fi

log "TV files"
tar -C "$SRC/rootfs" --owner=0 --group=0 --numeric-owner -cf - $(ls -A "$SRC/rootfs") \
    | tar -C "$ROOT" -xpf - --no-overwrite-dir
chmod 755 "$ROOT"/usr/local/bin/* "$ROOT"/usr/local/sbin/* "$ROOT"/usr/local/lib/tvd/tvd.py \
    "$ROOT"/usr/local/lib/tvd/apply-scale "$ROOT"/usr/local/lib/tvd/fullscreen-keeper \
    "$ROOT"/usr/local/lib/tvd/audio-auto

log "System settings"
echo "$TV_HOSTNAME" > "$ROOT/etc/hostname"
printf '127.0.0.1\tlocalhost\n127.0.1.1\t%s\n::1\t\tlocalhost ip6-localhost ip6-loopback\n' \
    "$TV_HOSTNAME" > "$ROOT/etc/hosts"
echo "en_US.UTF-8 UTF-8" > "$ROOT/etc/locale.gen"
in_root locale-gen >/dev/null
echo "LANG=en_US.UTF-8" > "$ROOT/etc/default/locale"
ln -sf "/usr/share/zoneinfo/$TIMEZONE" "$ROOT/etc/localtime"
echo "$TIMEZONE" > "$ROOT/etc/timezone"
date -u +%Y-%m-%d > "$ROOT/etc/tv-build"

for g in video render audio; do in_root sh -c "getent group $g >/dev/null || groupadd -r $g"; done
in_root sh -c 'id tv >/dev/null 2>&1 || useradd --create-home --uid 1000 --shell /bin/bash --groups video,render,audio tv' 2>&1 \
    | grep -v "already exists\|Not copying" || true
in_root id tv
in_root usermod -p '*' tv
in_root usermod -p '*' root
chown -R 1000:1000 "$ROOT/home/tv"
chmod 750 "$ROOT/home/tv"
install -d -m 700 "$ROOT/root/.ssh"
install -m 600 "$OUT/authorized_keys" "$ROOT/root/.ssh/authorized_keys"
install -d -m 755 "$ROOT/var/lib/tv" "$ROOT/var/log/journal" "$ROOT/boot/efi"

in_root systemctl enable tvd.service tv-firstboot.service ssh.service avahi-daemon.service \
    NetworkManager.service systemd-timesyncd.service
in_root systemctl set-default multi-user.target
in_root plymouth-set-default-theme bgrt

ROOT_UUID=$(cat /proc/sys/kernel/random/uuid)
ESP_ID=$(od -An -N4 -tx4 /dev/urandom | tr -d ' \n' | tr a-f A-F)
ESP_UUID="${ESP_ID:0:4}-${ESP_ID:4:4}"
cat > "$ROOT/etc/fstab" <<EOF
# Root filesystem, and the EFI boot partition (mounted only when something needs it).
UUID=$ROOT_UUID  /          ext4  noatime,commit=60,errors=remount-ro  0  1
UUID=$ESP_UUID   /boot/efi  vfat  umask=0077,noauto,x-systemd.automount,x-systemd.idle-timeout=60  0  2
EOF
echo "RESUME=none" > "$ROOT/etc/initramfs-tools/conf.d/resume"

log "Kernel and initramfs"
KVER=$(ls "$ROOT"/boot/vmlinuz-* | sed 's#.*/vmlinuz-##' | sort -V | tail -1)
in_root linux-update-symlinks install "$KVER" "/boot/vmlinuz-$KVER"
in_root update-initramfs -u -k all
[ -e "$ROOT/vmlinuz" ] && [ -e "$ROOT/initrd.img" ] || { echo "/vmlinuz or /initrd.img missing"; exit 1; }

log "Signed boot loader (shim and GRUB, works with Secure Boot on)"
mkdir -p "$WORK/efi-debs" "$WORK/efi"
in_root sh -c 'cd /tmp && apt-get download -qq shim-signed shim-helpers-amd64-signed grub-efi-amd64-signed' 2>&1 \
    | grep -v unsandboxed || true
mv "$ROOT"/tmp/*.deb "$WORK/efi-debs/"
for d in "$WORK"/efi-debs/*.deb; do dpkg-deb -x "$d" "$WORK/efi"; done
SHIM=$(find "$WORK/efi" -name shimx64.efi.signed | head -1)
MOKMGR=$(find "$WORK/efi" -name mmx64.efi.signed | head -1)
GRUB=$(find "$WORK/efi" -name grubx64.efi.signed | head -1)
for f in "$SHIM" "$MOKMGR" "$GRUB"; do [ -s "$f" ] || { echo "A signed EFI file is missing"; exit 1; }; done

CMDLINE="root=UUID=$ROOT_UUID ro quiet splash loglevel=3 rd.udev.log_level=3 vt.global_cursor_default=0"
cat > "$WORK/grub.cfg" <<EOF
# TV stick boot menu. It stays hidden: hold Shift or press Esc while the PC starts to see it.
set default=0
set timeout_style=hidden
set timeout=1
insmod part_gpt
insmod ext2
search --no-floppy --fs-uuid --set=root $ROOT_UUID
set gfxpayload=keep

menuentry "TV" {
    linux /vmlinuz $CMDLINE
    initrd /initrd.img
}
menuentry "TV (previous kernel)" {
    linux /vmlinuz.old $CMDLINE
    initrd /initrd.img.old
}
menuentry "TV (text console, for fixing problems)" {
    linux /vmlinuz root=UUID=$ROOT_UUID ro tv.console
    initrd /initrd.img
}
menuentry "PC firmware settings (BIOS)" {
    fwsetup
}
EOF

log "Clean up"
FIREFOX_VERSION=$(sed -n 's/^Version=//p' "$ROOT/usr/lib/firefox/application.ini" | head -1)
in_root apt-get clean
unmount_all
rm -f "$ROOT/usr/sbin/policy-rc.d" "$ROOT/etc/resolv.conf"
rm -rf "$ROOT"/var/lib/apt/lists/* "$ROOT"/tmp/* "$ROOT"/var/tmp/* "$ROOT"/var/cache/apt/*.bin
rm -f "$ROOT"/etc/ssh/ssh_host_*
: > "$ROOT/etc/machine-id"
rm -f "$ROOT/var/lib/dbus/machine-id"

log "Disk image"
USED_MB=$(du -sm "$ROOT" | cut -f1)
ROOT_MB=$(( (USED_MB * 125 / 100 + 1024 + 3) / 4 * 4 ))
mkfs.ext4 -q -F -L tvroot -U "$ROOT_UUID" -m 1 -O ^orphan_file,^metadata_csum_seed \
    -E root_owner=0:0 -d "$ROOT" "$WORK/root.img" "${ROOT_MB}M"

export MTOOLS_SKIP_CHECK=1
mkfs.vfat -F 32 -n TVBOOT -i "$ESP_ID" -C "$WORK/esp.img" $((ESP_MB * 1024)) >/dev/null
mmd -i "$WORK/esp.img" ::/EFI ::/EFI/BOOT ::/EFI/debian
mcopy -i "$WORK/esp.img" "$SHIM" ::/EFI/BOOT/BOOTX64.EFI
mcopy -i "$WORK/esp.img" "$GRUB" ::/EFI/BOOT/grubx64.efi
mcopy -i "$WORK/esp.img" "$MOKMGR" ::/EFI/BOOT/mmx64.efi
mcopy -i "$WORK/esp.img" "$WORK/grub.cfg" ::/EFI/BOOT/grub.cfg
mcopy -i "$WORK/esp.img" "$WORK/grub.cfg" ::/EFI/debian/grub.cfg

IMG="$WORK/tv-stick.img"
TOTAL_MB=$((1 + ESP_MB + ROOT_MB + 1))
rm -f "$IMG"
truncate -s "${TOTAL_MB}M" "$IMG"
sgdisk -a 2048 -n "1:2048:+${ESP_MB}M" -t 1:EF00 -c "1:EFI system" \
    -n "2:0:+${ROOT_MB}M" -t 2:8304 -c "2:TV root" "$IMG" >/dev/null
start=$(sgdisk -i 2 "$IMG" | awk '/First sector/ {print $3}')
[ "$start" = $(((1 + ESP_MB) * 2048)) ] || { echo "Partition 2 starts at $start, not where expected"; exit 1; }
dd if="$WORK/esp.img" of="$IMG" bs=1M seek=1 conv=notrunc status=none
dd if="$WORK/root.img" of="$IMG" bs=1M seek=$((1 + ESP_MB)) conv=notrunc,sparse status=none
sgdisk -v "$IMG" | tail -1

log "Copy to the Mac"
cp --sparse=always "$IMG" "$OUT/tv-stick.img.part"
mv "$OUT/tv-stick.img.part" "$OUT/tv-stick.img"
(cd "$OUT" && sha256sum tv-stick.img > tv-stick.img.sha256)
cat > "$OUT/build-info.txt" <<EOF
built=$(cat "$ROOT/etc/tv-build")
kernel=$KVER
firefox=$FIREFOX_VERSION
root_uuid=$ROOT_UUID
esp_uuid=$ESP_UUID
size_mb=$TOTAL_MB
used_mb=$USED_MB
EOF
cat "$OUT/build-info.txt"
log "Done: $OUT/tv-stick.img"
