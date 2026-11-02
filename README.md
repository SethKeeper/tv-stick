# TV stick

A USB stick that turns the projector's mini PC into a TV box. With the stick in, the PC boots
straight into a full-screen home screen with Netflix, YouTube and any sites you pin, running in
Firefox with uBlock Origin. Pull the stick out and the PC starts its normal Windows, which the
stick never touches.

## What's on it

- Debian 13 for x86-64 PCs. It boots through Debian's signed shim, so Secure Boot can stay on.
- Firefox from Mozilla's own repository, with uBlock Origin and Netflix's copy protection
  (Widevine) turned on. Netflix plays in Full HD on Linux; 4K needs Windows.
- A home screen in the style of Apple TV (`rootfs/usr/local/lib/tvd/www/index.html`), served on
  127.0.0.1:8765 by a small Python service (`tvd.py`). It also handles Wi-Fi, sound output,
  text size, power, and restarting into Windows for one boot.
- sway as a bare window manager that keeps Firefox full screen, automatic login, and
  security updates that install on their own.

## Build, test, write

```bash
./build.sh                 # builds out/tv-stick.img in a Lima VM (15 min first time, then 1 min)
test/run-vm.sh             # boots a copy in an emulated PC; test/qmp.py takes screenshots
./write-stick.sh disk6     # writes and checks the stick; macOS asks for your password
```

`./build.sh --fresh` also rebuilds the cached Debian base, which picks up new Firefox and
Debian versions. Sticks already in use update themselves.

The home screen runs on a Mac with made-up data, no stick needed:

```bash
TVD_FAKE=1 TVD_STATE=out/fake-state TVD_PORT=8766 python3 rootfs/usr/local/lib/tvd/tvd.py
```

Then open http://127.0.0.1:8766.

## Remote help

The stick runs SSH with key login only, for the key in `~/.ssh/tv_stick_ed25519` on the Mac
that built it. With the stick running on the same network: `test/ssh.sh tvstick.local`.

## Boot menu

Hidden by default. Hold Shift or press Esc while the PC starts to get it. It has the previous
kernel, a text console for fixing problems, and a shortcut into the PC's firmware settings.
