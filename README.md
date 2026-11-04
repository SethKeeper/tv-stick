# TV stick

A USB stick that turns a mini PC into a TV box. Once the PC is set to start from the stick (a
one-time BIOS change, see [SETUP.md](SETUP.md)), it boots straight into a full-screen home
screen with Netflix, YouTube and any sites you pin, running in Firefox with uBlock Origin. Pull
the stick out and the PC starts its normal Windows, which the stick never touches.

The stick was made for an Intel N5105 mini PC (Realtek RTL8821CE Wi-Fi and Bluetooth card)
connected to an Epson projector at 1280x800.

## What's on it

- Debian 13 for x86-64 PCs. It boots through Debian's signed shim, so Secure Boot can stay on.
- Firefox from Mozilla's own repository, with uBlock Origin and Netflix's copy protection
  (Widevine) turned on. Netflix plays in Full HD on Linux; 4K needs Windows.
- A home screen in the style of Apple TV (`rootfs/usr/local/lib/tvd/www/index.html`), served on
  127.0.0.1:8765 by a small Python service (`tvd.py`). It also handles Wi-Fi, Bluetooth, the
  VPN, sound output, text size, power, and restarting into Windows for one boot.
- Bluetooth pairing from Settings, with no PIN to type. Headphones and speakers that connect
  there take over the sound. The TV only accepts a new pairing while Settings is pairing one,
  so nothing nearby can pair with it on its own.
- NordVPN's Linux app from Nord's own repository (you need a NordVPN account). You sign in on
  Nord's website, so the home screen never handles your password. Then you switch the VPN on
  in Settings, pick a country and choose whether it connects at startup.
- sway as a bare window manager that keeps Firefox full screen, automatic login, and
  security updates that install on their own.

## Build, test, write, update

You need an Apple Silicon Mac with Rosetta and Lima, plus Homebrew's `qemu` for the test VM.
The stick should hold 8 GB or more, and the PC has to boot in UEFI mode. `TIMEZONE` in
`vm/build-image.sh` sets the time zone (Europe/Berlin).

```bash
./build.sh                 # builds out/tv-stick.img in a Lima VM (about a minute once cached)
test/run-vm.sh             # boots a copy in an emulated PC; test/qmp.py takes screenshots
./write-stick.sh diskN     # writes and checks the stick; macOS asks for your password
./update-stick.sh          # copies rootfs/etc and rootfs/usr to the running stick over SSH
```

Look up the stick's disk number (`diskN`) with `diskutil list external physical` first, because
writing erases that disk. The script refuses anything but a removable USB disk under 300 GB.

`./build.sh --fresh` also rebuilds the cached Debian base, which picks up new Debian, Firefox,
uBlock Origin and NordVPN versions. A change to `vm/packages.txt` rebuilds it too. The cache
lives in the Lima VM, which keeps running after a build (`limactl stop tvbuild` stops it).

Sticks already in use install Debian's security updates and new Firefox and NordVPN versions on
their own. After copying, `./update-stick.sh` restarts the home screen and Firefox; other
services pick up their changes at the stick's next start. New packages or services,
`rootfs/home` and boot changes need a new image, and writing one starts the stick over: Wi-Fi,
sign-ins, Bluetooth pairings and pinned sites have to be set up again.

The home screen also runs on a Mac with made-up data, so you can try it without a stick:

```bash
TVD_FAKE=1 TVD_STATE=out/fake-state TVD_PORT=8766 python3 rootfs/usr/local/lib/tvd/tvd.py
```

Then open http://127.0.0.1:8766.

## Remote help

The stick runs SSH with key login only, for the key in `~/.ssh/tv_stick_ed25519` on the Mac
that built it (`./build.sh` makes it the first time). With the stick running on the same
network, `test/ssh.sh tvstick.local` logs in as root. NordVPN's LAN discovery is turned on,
which is meant to keep this working while the VPN is connected. If `tvstick.local` doesn't
answer, `test/ssh.sh` and `./update-stick.sh` also take the IP address that Settings shows
under About. The first connection saves the stick's host key in `~/.ssh/known_hosts_tvstick`
and later ones check it, so another device answering as `tvstick.local` gets refused.
`./write-stick.sh` clears that file, because a new stick makes new keys.

## Boot menu

The menu is hidden by default. To open it, hold Shift or press Esc while the PC starts. It has
the previous kernel (the same one until the first kernel update), a text console in place of
the TV screen, and a shortcut into the PC's firmware settings. The console shows the boot
messages and logs in as the `tv` user, who has no root access, so system fixes still go
through SSH.
