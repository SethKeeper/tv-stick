# tv-stick

Builds a Debian USB stick image that boots Seth's projector mini PC into a TV home screen.
The PC's internal Windows must never be touched: no installer on the stick, nothing mounts
internal disks, and Windows is only reached through `efibootmgr --bootnext` (Restart in Windows).

- Build: `./build.sh` starts the Lima VM "tvbuild" (Debian arm64, Rosetta runs amd64) and runs
  `vm/build-image.sh` as root. The Debian base with Firefox and uBlock Origin is cached in the VM
  under /var/tmp/tvbuild-cache, keyed on `vm/packages.txt`; `--fresh` rebuilds it.
- Test: `test/run-vm.sh` boots a copy under QEMU (TCG, UEFI, USB storage, 16 GB so first-boot
  growing runs). `test/qmp.py shot|key|type|click`, `test/ssh.sh` (port 2222). Only the first
  Firefox start is meaningful there: later starts sit blank for minutes because Firefox holds
  page loads until uBlock Origin's background has started, which TCG makes very slow. Checked
  2026-11-02 by moving uBO out of the profile (home screen then appears). Not a stick bug.
- Running stick: `./update-stick.sh` pushes rootfs/etc and rootfs/usr over SSH and restarts
  the home screen, no rewrite needed.
- Write: `./write-stick.sh diskN` only accepts a removable USB disk under 300 GB. The Mac's
  Work drive is an external disk too. Always check `diskutil list external physical` first.
- Home screen: `rootfs/usr/local/lib/tvd/tvd.py` (root service: Host check against DNS
  rebinding, per-boot token for every API call) and `www/index.html` (single file, sizes in vw,
  works with a pointer or arrow keys). Preview with fake data: launch config "tv-home (fake data)".
- Design: Apple TV look on request. Glass panels, grouped lists, tiles that lift and tilt.
- UI copy and docs follow the humanizer rules (no em dashes).
