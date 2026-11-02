#!/bin/bash
# Writes out/tv-stick.img to a USB stick and reads it back to check it.
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
[ "$(stat -f %z "$IMG")" -lt "$bytes" ] || { echo "The image doesn't fit on $DISK."; exit 1; }
echo "Writing to $DISK: $(sed -n 's/.*Device \/ Media Name: *//p' <<<"$info"), $((bytes / 1000000000)) GB"

want=$(shasum -a 256 "$IMG" | cut -d' ' -f1)
diskutil unmountDisk "/dev/$DISK"

# macOS only lets authorized tools open a removable disk raw. authopen asks for the
# password in its own dialog and hands back an open descriptor; one password covers
# both writing and the read-back check.
python3 - "$IMG" "/dev/r$DISK" "$want" <<'PY'
import hashlib
import os
import socket
import subprocess
import sys

img, dev, want = sys.argv[1:4]
parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
proc = subprocess.Popen(["/usr/libexec/authopen", "-stdoutpipe", "-o", str(os.O_RDWR), dev], stdout=child)
child.close()
_, fds, _, _ = socket.recv_fds(parent, 16, 1)
proc.wait()
if not fds:
    sys.exit("No access to %s. Was the password dialog cancelled?" % dev)
fd = fds[0]
size = os.path.getsize(img)
chunk = 4 << 20

done = 0
with open(img, "rb") as f:
    while True:
        buf = f.read(chunk)
        if not buf:
            break
        view = memoryview(buf)
        while view:
            view = view[os.write(fd, view):]
        done += len(buf)
        print("\rWriting %3d%%" % (done * 100 // size), end="", flush=True)
os.fsync(fd)

os.lseek(fd, 0, os.SEEK_SET)
digest, left = hashlib.sha256(), size
while left:
    buf = os.read(fd, min(chunk, left))
    if not buf:
        break
    digest.update(buf)
    left -= len(buf)
    print("\rChecking %3d%%" % ((size - left) * 100 // size), end="", flush=True)
os.close(fd)
print()
if digest.hexdigest() != want:
    sys.exit("Check failed: the stick doesn't match the image.")
print("Written and checked (%d MB)." % (size >> 20))
PY

diskutil eject "/dev/$DISK" >/dev/null && echo "Ejected. You can pull the stick out."
