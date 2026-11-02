#!/bin/bash
# Writes out/tv-stick.img to a USB stick and checks what was written.
#   ./write-stick.sh disk6
# Refuses anything that isn't a removable USB disk smaller than 300 GB, so the
# Mac's own drives and the external Work drive can't be hit by a typo.
set -euo pipefail
cd "$(dirname "$0")"
DISK=${1:?Usage: ./write-stick.sh diskN (see: diskutil list external physical)}
IMG=$PWD/out/tv-stick.img
[[ "$DISK" =~ ^disk[0-9]+$ ]] || { echo "Give a whole disk like disk6, not $DISK"; exit 1; }
[ -s "$IMG" ] || { echo "No image yet. Run ./build.sh first."; exit 1; }

info=$(diskutil info "$DISK")
grep -q "Protocol: *USB" <<<"$info" || { echo "$DISK is not a USB disk. Stopping."; exit 1; }
grep -q "Removable Media: *Removable" <<<"$info" || { echo "$DISK is not removable media. Stopping."; exit 1; }
bytes=$(sed -n 's/.*Disk Size:.*(\([0-9]*\) Bytes).*/\1/p' <<<"$info")
[ -n "$bytes" ] && [ "$bytes" -lt 300000000000 ] || { echo "$DISK is bigger than 300 GB. Stopping."; exit 1; }
img_bytes=$(stat -f %z "$IMG")
[ "$img_bytes" -lt "$bytes" ] || { echo "The image doesn't fit on $DISK."; exit 1; }
echo "Writing to $DISK: $(sed -n 's/.*Device \/ Media Name: *//p' <<<"$info"), $((bytes / 1000000000)) GB"

diskutil unmountDisk "/dev/$DISK"
mb=$((img_bytes / 1048576))
want=$(head -c "$img_bytes" "$IMG" | shasum -a 256 | cut -d' ' -f1)
# Raw disk access needs an administrator; macOS asks for the password in its own dialog.
got=$(osascript -e "do shell script \"/bin/dd if='$IMG' of=/dev/r$DISK bs=1m && /bin/sync && /bin/dd if=/dev/r$DISK bs=1m count=$mb 2>/dev/null | /usr/bin/shasum -a 256 | /usr/bin/cut -d' ' -f1\" with administrator privileges")
if [ "$got" != "$want" ]; then
    echo "Check failed: the stick doesn't match the image ($got vs $want)."
    exit 1
fi
echo "Written and checked ($mb MB)."
diskutil eject "/dev/$DISK" >/dev/null && echo "Ejected. You can pull the stick out."
