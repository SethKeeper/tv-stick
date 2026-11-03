#!/usr/bin/env python3
"""tvd: the TV stick's home screen service.

Serves the home screen on 127.0.0.1:8765 and does the system jobs the screen
needs: Wi-Fi, Bluetooth, the VPN, sound output, text size, power, restarting
into Windows, and the list of site tiles. It runs as root, so it only answers
requests addressed to 127.0.0.1 or localhost (which blocks DNS rebinding) and
every API call must carry the per-boot token that is written into the home
screen page.

To try the page on another computer with made-up data:
    TVD_FAKE=1 TVD_STATE=/tmp/tvd python3 tvd.py
"""

import json
import os
import pwd
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("TVD_PORT", "8765"))
FAKE = os.environ.get("TVD_FAKE") == "1"
WWW = Path(os.environ.get("TVD_WWW", str(Path(__file__).resolve().parent / "www")))
STATE = Path(os.environ.get("TVD_STATE", "/var/lib/tv"))
ICONS = STATE / "icons"
TILES_FILE = STATE / "tiles.json"
SETTINGS_FILE = STATE / "settings.json"
BUILD_FILE = Path("/etc/tv-build")
FIREFOX_INI = Path("/usr/lib/firefox/application.ini")
PROFILE = "/home/tv/.mozilla/firefox/tv"
TV_USER = "tv"
TOKEN = secrets.token_urlsafe(24)
ALLOWED_HOSTS = {"127.0.0.1:%d" % PORT, "localhost:%d" % PORT}
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0"
CSP = ("default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
       "script-src 'unsafe-inline'; connect-src 'self'; font-src 'self'; "
       "base-uri 'none'; form-action 'none'")

DEFAULT_TILES = [
    {"name": "Netflix", "url": "https://www.netflix.com/browse"},
    {"name": "YouTube", "url": "https://www.youtube.com/"},
]
TEXT_SIZES = ("small", "medium", "large")

# Large icons for well-known sites, tried before whatever the site's own page lists.
ICON_HINTS = {
    "netflix.com": ["https://assets.nflxext.com/us/ffe/siteui/common/icons/nficon2016.png"],
    "youtube.com": ["https://www.gstatic.com/youtube/img/branding/favicon/favicon_192x192.png",
                    "https://www.gstatic.com/youtube/img/branding/favicon/favicon_144x144.png"],
}

FAKE_NET = {"online": True, "wired": False, "wifi": "Home-5G", "signal": 74,
            "has_wifi": True, "wifi_saved": 1, "ip": "192.168.178.57"}
FAKE_NETWORKS = [
    {"ssid": "Home-5G", "signal": 74, "secure": True, "active": True},
    {"ssid": "Home-2.4G", "signal": 61, "secure": True, "active": False},
    {"ssid": "Cafe Guest", "signal": 38, "secure": False, "active": False},
    {"ssid": "<b>Neighbours</b>", "signal": 22, "secure": True, "active": False},
]
FAKE_VPN = {"installed": True, "logged_in": False, "connected": False, "country": None,
            "city": None, "server": None, "autoconnect": False}
FAKE_COUNTRIES = ["Austria", "Belgium", "Canada", "France", "Germany", "Italy", "Japan",
                  "Netherlands", "Spain", "Sweden", "Switzerland", "United Kingdom", "United States"]
FAKE_BT = {"available": True, "powered": True, "discovering": False, "devices": [
    {"mac": "AA:BB:CC:DD:EE:01", "name": "AirPods Pro", "icon": "audio-headphones", "paired": True, "connected": False},
    {"mac": "AA:BB:CC:DD:EE:02", "name": "Living Room Speaker", "icon": "audio-card", "paired": False, "connected": False},
    {"mac": "AA:BB:CC:DD:EE:03", "name": "Xbox Wireless Controller", "icon": "input-gaming", "paired": False, "connected": False},
]}
FAKE_AUDIO = [
    {"id": "sink:hdmi", "name": "Projector (HDMI)", "active": True, "available": True},
    {"id": "sink:analog", "name": "Headphones", "active": False, "available": False},
    {"id": "profile:card|output:hdmi-stereo-extra1", "name": "Digital Stereo (HDMI 2) Output",
     "active": False, "available": True},
]


def tv_uid():
    try:
        return pwd.getpwnam(TV_USER).pw_uid
    except KeyError:
        return 1000


def run(cmd, timeout=15, as_tv=False, env=None, answer=None):
    """Runs a command and returns (exit code, stdout, stderr). Never raises.
    stdin is /dev/null unless `answer` is given, so a tool that asks a question can't wait."""
    if as_tv:
        cmd = ["runuser", "-u", TV_USER, "--", "env",
               "XDG_RUNTIME_DIR=/run/user/%d" % tv_uid()] + list(cmd)
    kwargs = {"input": answer} if answer is not None else {"stdin": subprocess.DEVNULL}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, **kwargs)
        return p.returncode, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return -1, "", str(e)


def to_int(s, default=0):
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


# ---------- saved state ----------

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def get_settings():
    s = load_json(SETTINGS_FILE, {})
    if not isinstance(s, dict):
        s = {}
    if s.get("text_size") not in TEXT_SIZES:
        s["text_size"] = "medium"
    return s


def normalize_url(raw):
    raw = (raw or "").strip()
    if not raw or len(raw) > 2000:
        return None
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = "https://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    if "." not in host and host != "localhost":
        return None
    return urllib.parse.urlunsplit(parts)


def clean_tiles(raw):
    tiles = []
    for t in raw if isinstance(raw, list) else []:
        if not isinstance(t, dict):
            continue
        name = " ".join(str(t.get("name", "")).split())[:40]
        url = normalize_url(str(t.get("url", "")))
        if name and url:
            tiles.append({"name": name, "url": url})
    return tiles[:40]


def get_tiles():
    tiles = load_json(TILES_FILE, None)
    if isinstance(tiles, list):
        return clean_tiles(tiles)
    return [dict(t) for t in DEFAULT_TILES]


# ---------- network ----------

def nm_split(line):
    """Splits one line of `nmcli -t` output on unescaped colons."""
    fields, cur, escaped = [], [], False
    for ch in line:
        if escaped:
            cur.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    fields.append("".join(cur))
    return fields


def local_ip():
    _, out, _ = run(["ip", "-4", "-o", "addr", "show", "scope", "global"], timeout=3)
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def net_status():
    if FAKE:
        return dict(FAKE_NET)
    _, out, _ = run(["nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device"], timeout=5)
    wired, wifi_conn, has_wifi = False, None, False
    for line in out.splitlines():
        typ, state, conn = (nm_split(line) + ["", "", ""])[:3]
        if typ == "wifi":
            has_wifi = True
        if state.startswith("connected"):
            if typ == "ethernet":
                wired = True
            elif typ == "wifi":
                wifi_conn = conn
    ssid, signal = wifi_conn, None
    if wifi_conn:
        _, out, _ = run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL", "device", "wifi",
                         "list", "--rescan", "no"], timeout=5)
        for line in out.splitlines():
            inuse, name, sig = (nm_split(line) + ["", "", ""])[:3]
            if inuse == "*":
                ssid, signal = name or ssid, to_int(sig)
    _, out, _ = run(["nmcli", "-t", "-f", "TYPE", "connection", "show"], timeout=5)
    saved = sum(1 for line in out.splitlines() if line.strip() == "802-11-wireless")
    return {"online": wired or bool(wifi_conn), "wired": wired, "wifi": ssid,
            "signal": signal, "has_wifi": has_wifi, "wifi_saved": saved, "ip": local_ip()}


def wifi_scan():
    if FAKE:
        time.sleep(1.2)
        return FAKE_NETWORKS
    _, out, _ = run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi",
                     "list", "--rescan", "yes"], timeout=25)
    best = {}
    for line in out.splitlines():
        inuse, ssid, sig, sec = (nm_split(line) + ["", "", "", ""])[:4]
        if not ssid:
            continue
        net = {"ssid": ssid, "signal": to_int(sig), "secure": sec not in ("", "--"),
               "active": inuse == "*"}
        old = best.get(ssid)
        if old is None or net["active"] or (not old["active"] and net["signal"] > old["signal"]):
            best[ssid] = net
    return sorted(best.values(), key=lambda n: (not n["active"], -n["signal"]))


WIFI_ERRORS = [
    ("secrets were required", "Incorrect password. Check it and try again."),
    ("802-11-wireless-security.psk", "Incorrect password. Wi-Fi passwords have at least 8 characters."),
    ("no network with ssid", "Can't find that network. Move closer and try again."),
    ("timeout", "The network didn't respond. Try again."),
]


def wifi_connect(ssid, password):
    if FAKE:
        time.sleep(1.5)
        if password == "wrong":
            return False, WIFI_ERRORS[0][1]
        FAKE_NET.update(online=True, wired=False, wifi=ssid, signal=70)
        return True, ""
    if not ssid or len(ssid.encode()) > 32:
        return False, "That network name isn't valid."
    cmd = ["nmcli", "--wait", "30", "device", "wifi", "connect", ssid]
    if password:
        cmd += ["password", password]
    code, out, err = run(cmd, timeout=45)
    if code == 0:
        return True, ""
    # Drop the half-made profile so the next try starts clean.
    run(["nmcli", "connection", "delete", "id", ssid], timeout=10)
    text = (err or out).strip()
    for needle, message in WIFI_ERRORS:
        if needle in text.lower():
            return False, message
    return False, "Couldn't join: " + (text.splitlines()[-1] if text else "unknown error")


# ---------- sound ----------

def pactl_json(*args):
    code, out, _ = run(["pactl", "-f", "json"] + list(args), timeout=8, as_tv=True)
    if code != 0:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def sink_available(sink):
    active = sink.get("active_port")
    for port in sink.get("ports") or []:
        if port.get("name") == active:
            return port.get("availability") != "not available"
    return True


def audio_outputs():
    if FAKE:
        return FAKE_AUDIO
    sinks = pactl_json("list", "sinks")
    _, default, _ = run(["pactl", "get-default-sink"], timeout=5, as_tv=True)
    default = default.strip()
    outputs, present = [], set()
    for s in sinks:
        name = s.get("name", "")
        present.add(name.rsplit(".", 1)[-1])
        outputs.append({"id": "sink:" + name, "name": s.get("description") or name,
                        "active": name == default, "available": sink_available(s)})
    # Outputs that need the sound card switched to another profile first (HDMI, mostly).
    for card in pactl_json("list", "cards"):
        for pname, prof in (card.get("profiles") or {}).items():
            if not pname.startswith("output:") or "+" in pname:
                continue
            if prof.get("available") in (False, "no") or not prof.get("sinks"):
                continue
            if pname.split(":", 1)[1] in present:
                continue
            outputs.append({"id": "profile:%s|%s" % (card.get("name", ""), pname),
                            "name": prof.get("description") or pname,
                            "active": False, "available": True})
    return outputs


def set_audio(choice):
    if FAKE:
        for o in FAKE_AUDIO:
            o["active"] = o["id"] == choice
        return True, ""
    if choice.startswith("sink:"):
        code, _, err = run(["pactl", "set-default-sink", choice[5:]], as_tv=True)
    elif choice.startswith("profile:"):
        card, _, profile = choice[8:].partition("|")
        code, _, err = run(["pactl", "set-card-profile", card, profile], as_tv=True)
        suffix = "." + profile.split(":", 1)[-1]
        for _ in range(15):
            if code != 0:
                break
            time.sleep(0.3)
            match = [s["name"] for s in pactl_json("list", "sinks")
                     if s.get("name", "").endswith(suffix)]
            if match:
                code, _, err = run(["pactl", "set-default-sink", match[0]], as_tv=True)
                break
    else:
        return False, "Unknown output."
    if code != 0:
        return False, "Couldn't switch the sound. " + err.strip()
    s = get_settings()
    s["audio_manual"] = True
    save_json(SETTINGS_FILE, s)
    return True, ""


# ---------- power and browser ----------

_windows = {"checked": False, "entry": None}


def windows_entry():
    if FAKE:
        return "0000"
    if not _windows["checked"]:
        _, out, _ = run(["efibootmgr"], timeout=5)
        for line in out.splitlines():
            m = re.match(r"Boot([0-9A-Fa-f]{4})\*?\s+(.*)", line)
            if m and "windows boot manager" in m.group(2).lower():
                _windows["entry"] = m.group(1)
                break
        _windows["checked"] = True
    return _windows["entry"]


def power(action):
    if action not in ("off", "restart", "windows"):
        return False, "Unknown action."
    if action == "windows":
        entry = windows_entry()
        if not entry:
            return False, "There's no Windows on this PC to start."
        if not FAKE:
            # One-time boot of Windows' own boot manager. Firmware starts it directly,
            # exactly like a normal Windows start.
            code, _, err = run(["efibootmgr", "--bootnext", entry], timeout=10)
            if code != 0:
                return False, "Couldn't choose Windows for the next start. " + err.strip()
    if not FAKE:
        verb = "poweroff" if action == "off" else "reboot"
        threading.Timer(0.8, lambda: run(["systemctl", verb])).start()
    return True, ""


def restart_browser():
    if not FAKE:
        run(["pkill", "-TERM", "-u", TV_USER, "-f", "--", "--profile " + PROFILE], timeout=5)


def update_pending():
    """True when Firefox was updated on disk after the running copy started."""
    stamp = Path("/run/user/%d/tv-browser.stamp" % tv_uid())
    try:
        return FIREFOX_INI.stat().st_ctime > stamp.stat().st_mtime + 2
    except OSError:
        return False


# ---------- Bluetooth (BlueZ over D-Bus) ----------

BT_ADAPTER = "/org/bluez/hci0"
MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
_bt_lock = threading.Lock()
_bt_scan = {"proc": None}
BT_ERRORS = [
    ("page timeout", "The device didn't answer. Make sure it's on and close by."),
    ("authentication", "Pairing was refused. Put the device in pairing mode and try again."),
    ("in progress", "Still busy with that device. Try again in a moment."),
    ("not ready", "Bluetooth isn't ready yet. Try again in a moment."),
    ("profile-unavailable", "This TV can't use that kind of device."),
    ("does not exist", "That device is gone. Search again."),
]


def bt_path(mac):
    return BT_ADAPTER + "/dev_" + mac.replace(":", "_")


def bt_busctl(*args, timeout=15):
    """One busctl call against BlueZ. Returns (ok, friendly error)."""
    code, out, err = run(["busctl", "--timeout=%d" % timeout] + list(args), timeout=timeout + 5)
    if code == 0:
        return True, ""
    text = (err or out).strip().lower()
    for needle, message in BT_ERRORS:
        if needle in text:
            return False, message
    return False, "Bluetooth said: " + ((err or out).strip().splitlines() or ["unknown error"])[-1]


DEVICE_LINE = re.compile(r"^Device ([0-9A-F]{2}(?::[0-9A-F]{2}){5}) (.*)$")
_bt_icons = {}  # MAC -> icon name; `info` costs a call per device, and icons don't change


def btctl(*args, timeout=10):
    """bluetoothctl as text. (`busctl --json` can't be used: it fails outright as soon as any
    nearby device advertises manufacturer data, whose dictionary has number keys.)"""
    code, out, _ = run(["bluetoothctl"] + list(args), timeout=timeout)
    return code, ANSI.sub("", out)


def bt_list(*which):
    """[(MAC, alias)] from `bluetoothctl devices [Paired|Connected]`."""
    found = []
    for line in btctl("devices", *which)[1].splitlines():
        m = DEVICE_LINE.match(line.strip())
        if m:
            found.append((m.group(1), m.group(2).strip()))
    return found


def bt_icon(mac):
    if mac not in _bt_icons:
        _bt_icons[mac] = key_values(btctl("info", mac)[1]).get("icon", "")
    return _bt_icons[mac]


def bt_status():
    if FAKE:
        return json.loads(json.dumps(FAKE_BT))
    proc = _bt_scan["proc"]
    scanning = proc is not None and proc.poll() is None  # poll() also reaps a finished scan
    code, out = btctl("show")
    if code != 0 or "Powered:" not in out:
        return {"available": False}
    show = key_values(out)
    paired = {mac for mac, _ in bt_list("Paired")}
    connected = {mac for mac, _ in bt_list("Connected")}
    devices = []
    for mac, alias in bt_list():
        # Without a name, BlueZ shows the address with dashes. Nobody can recognise those,
        # so they're only listed once paired.
        named = bool(alias) and alias != mac.replace(":", "-")
        if not named and mac not in paired:
            continue
        devices.append({"mac": mac, "name": alias if named else mac, "icon": bt_icon(mac),
                        "paired": mac in paired, "connected": mac in connected})
    devices.sort(key=lambda d: (not d["connected"], not d["paired"], d["name"].lower()))
    return {"available": True, "powered": show.get("powered") == "yes",
            "discovering": show.get("discovering") == "yes" or scanning, "devices": devices[:60]}


def bt_summary():
    b = bt_status()
    return {"available": b.get("available", False), "powered": b.get("powered", False),
            "connected": [d["name"] for d in b.get("devices", []) if d["connected"]]}


def bt_fake_device(mac):
    return next((d for d in FAKE_BT["devices"] if d["mac"] == mac), None)


def bt_power(on):
    if FAKE:
        FAKE_BT["powered"] = bool(on)
        return True, ""
    # BlueZ powers the adapter on again at every start, so "off" is an rfkill block, which
    # systemd restores at boot. Unblocking first lets "on" work after such a block.
    if on:
        run(["rfkill", "unblock", "bluetooth"], timeout=5)
        time.sleep(1)
    ok, err = bt_busctl("set-property", "org.bluez", BT_ADAPTER, "org.bluez.Adapter1",
                        "Powered", "b", "true" if on else "false")
    if not on:
        run(["rfkill", "block", "bluetooth"], timeout=5)
    return ok, err


def bt_scan():
    """Searches for about 45 seconds. Discovery belongs to the process that asked for it,
    so a bluetoothctl stays running for that long."""
    if FAKE:
        FAKE_BT["discovering"] = True
        threading.Timer(6, lambda: FAKE_BT.update(discovering=False)).start()
        return True, ""
    proc = _bt_scan["proc"]
    if proc is None or proc.poll() is not None:
        # Since BlueZ 5.71 a bare "scan on" quits within a second, and the search ends with
        # it; --timeout keeps bluetoothctl (and so the search) running for 45 seconds.
        _bt_scan["proc"] = subprocess.Popen(["bluetoothctl", "--timeout", "45", "scan", "on"],
                                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
    return True, ""


def bt_stop_scan():
    proc = _bt_scan["proc"]
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def bt_pair(mac):
    if FAKE:
        time.sleep(1.5)
        d = bt_fake_device(mac)
        if not d:
            return False, "That device is gone. Search again."
        d.update(paired=True, connected=True)
        return True, ""
    bt_stop_scan()  # pairing while searching fails on many adapters
    path = bt_path(mac)
    ok, err = bt_busctl("call", "org.bluez", path, "org.bluez.Device1", "Pair", timeout=40)
    if not ok and "already" not in err.lower():
        return False, err
    bt_busctl("set-property", "org.bluez", path, "org.bluez.Device1", "Trusted", "b", "true")
    return bt_connect(mac)


def bt_connect(mac):
    if FAKE:
        time.sleep(1)
        d = bt_fake_device(mac)
        if d:
            d["connected"] = True
        return True, ""
    ok, err = bt_busctl("call", "org.bluez", bt_path(mac), "org.bluez.Device1", "Connect", timeout=30)
    if ok:
        bt_use_for_sound(mac)
    return ok, err


def bt_use_for_sound(mac):
    """Sends sound to a Bluetooth speaker or headphones that just connected. The TV pins
    sound to HDMI at startup, so new Bluetooth audio would otherwise stay silent."""
    dev = next((d for d in bt_status().get("devices", []) if d["mac"] == mac), {})
    if dev.get("icon") and not dev["icon"].startswith("audio"):
        return  # a controller or keyboard: nothing to do for sound
    want = "bluez_output." + mac.replace(":", "_")
    for _ in range(16):
        sinks = [s.get("name", "") for s in pactl_json("list", "sinks")]
        match = [n for n in sinks if n.startswith(want)]
        if match:
            run(["pactl", "set-default-sink", match[0]], as_tv=True)
            s = get_settings()
            s["audio_manual"] = True
            save_json(SETTINGS_FILE, s)
            return
        time.sleep(0.5)  # not an audio device, or its sound profile isn't up yet


def bt_disconnect(mac):
    if FAKE:
        d = bt_fake_device(mac)
        if d:
            d["connected"] = False
        return True, ""
    return bt_busctl("call", "org.bluez", bt_path(mac), "org.bluez.Device1", "Disconnect", timeout=20)


def bt_forget(mac):
    if FAKE:
        FAKE_BT["devices"] = [d for d in FAKE_BT["devices"] if d["mac"] != mac]
        return True, ""
    return bt_busctl("call", "org.bluez", BT_ADAPTER, "org.bluez.Adapter1", "RemoveDevice", "o", bt_path(mac))


# ---------- VPN (NordVPN's official Linux app) ----------

NORDVPN = "/usr/bin/nordvpn"
# Firefox hands Nord's sign-in return link (nordvpn://...) to tv-vpn-callback, which runs
# as the tv user and drops it here. tvd (root) finishes the sign-in with it.
CALLBACK_DIR = Path(os.environ.get("TVD_RUN", "/run/tvd"))
CALLBACK_FILE = CALLBACK_DIR / "nordvpn-callback"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
COUNTRY_RE = re.compile(r"^[A-Za-z][A-Za-z_' .()-]{1,40}$")
TOKEN_RE = re.compile(r"^[a-f0-9]{16,1024}$")  # nordvpnd only accepts lowercase hex
LOGIN_URL_RE = re.compile(r"Continue in the browser: (https://api\.nordvpn\.com/v1/users/oauth/login-redirect\?attempt=[0-9a-f-]{36})")
_vpn_lock = threading.Lock()
# finishing: Nord's return link is here and the sign-in is being completed with it.
# signed_in: the last answer from vpn_signed_in(), for the 15-second poll (None until known).
_vpn_login = {"started": 0.0, "url": None, "error": None, "watching": False, "finishing": False,
              "signed_in": None}
_vpn_countries = {"at": 0.0, "list": []}


def nordvpn(*args, timeout=30):
    """Runs the NordVPN CLI as root and returns (exit code, cleaned text). The CLI quits at
    once without a HOME, prints all messages (errors too) to stdout, and draws colors and a
    spinner only on a terminal; the env makes sure of the latter anyway."""
    env = dict(os.environ, HOME=os.environ.get("HOME") or "/root", NO_COLOR="1", DISABLE_TUI_LOADER="1")
    # Logins ask an analytics question when consent is unset; "n" answers it (consent off).
    answer = "n\n" if args and args[0] == "login" else None
    code, out, err = run([NORDVPN] + list(args), timeout=timeout, env=env, answer=answer)
    lines = []
    for raw in ANSI.sub("", out + "\n" + err).split("\n"):
        # The CLI draws a spinner with carriage returns even without a terminal.
        line = raw.split("\r")[-1].strip()
        if line and line not in ("-", "\\", "|", "/"):
            lines.append(line)
    return code, "\n".join(lines)


def key_values(text):
    """'Key: value' lines as a dict with lower-case keys."""
    out = {}
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if sep:
            out[key.strip().lower()] = val.strip()
    return out


def last_line(text, fallback):
    lines = [l for l in text.splitlines() if l.strip()]
    return lines[-1] if lines else fallback


def vpn_status(full=True):
    if FAKE:
        info = dict(FAKE_VPN, login_pending=False, login_error=None)
    elif not os.path.exists(NORDVPN):
        return {"installed": False}
    else:
        _, text = nordvpn("status", timeout=15)  # local, cheap: fine to poll
        st = key_values(text)
        state_word = st.get("status", "").lower()
        connected = state_word == "connected"
        info = {"installed": True, "connected": connected, "connecting": state_word == "connecting",
                "country": st.get("country") if connected else None,
                "city": st.get("city") if connected else None,
                "server": (st.get("hostname") or st.get("server")) if connected else None}
        if full:
            info["logged_in"] = vpn_signed_in() is not False
            settings = key_values(nordvpn("settings", timeout=15)[1])
            info["autoconnect"] = settings.get("auto-connect", "").lower().startswith("enabled")
            # Only once Nord's return link is here. Someone who left Nord's page without
            # signing in gets the Sign In buttons back instead of a spinner.
            info["login_pending"] = not info["logged_in"] and (
                _vpn_login["finishing"] or (_vpn_login["watching"] and CALLBACK_FILE.exists()))
            info["login_error"] = _vpn_login["error"]
    if full:
        _vpn_login["signed_in"] = info["logged_in"]
    else:
        # The poll skips the account check (it asks Nord's servers) and reports the last
        # answer instead. Connected means signed in.
        on = info["connected"] or info.get("connecting")
        info["logged_in"] = True if on else _vpn_login["signed_in"]
    return info


def vpn_countries():
    """Country names for the picker. The CLI prints one Title_Case_Token per line when it
    isn't on a terminal; underscores become spaces for display."""
    if FAKE:
        return FAKE_COUNTRIES
    if time.time() - _vpn_countries["at"] < 3600 and _vpn_countries["list"]:
        return _vpn_countries["list"]
    code, text = nordvpn("countries", timeout=30)
    if code != 0:  # e.g. the server list isn't loaded yet; never parse an error sentence
        return _vpn_countries["list"]
    names = {word.replace("_", " ") for word in text.split() if COUNTRY_RE.match(word)}
    if names:
        _vpn_countries.update(at=time.time(), list=sorted(names))
    return sorted(names)


def vpn_target(country):
    """The CLI's token for a country (spaces as underscores), or None if NordVPN doesn't offer it."""
    return country.replace(" ", "_") if country in vpn_countries() else None


def vpn_signed_in():
    """True, False, or None when it can't tell (offline). `account` asks Nord's servers,
    so only an exact "not logged in" counts as signed out."""
    if FAKE:
        return FAKE_VPN["logged_in"]
    code, text = nordvpn("account", timeout=20)
    if code == 0:
        return True
    if "not logged in" in text.lower():
        return False
    code, text = nordvpn("token", timeout=15)  # local check, used when the API can't be reached
    if code == 0:
        return True
    return False if "not logged in" in text.lower() else None


def vpn_wait_connected(seconds):
    for _ in range(seconds):
        if vpn_status(full=False).get("connected"):
            return True
        time.sleep(1)
    return False


def vpn_connect(country):
    if FAKE:
        time.sleep(1.5)
        FAKE_VPN.update(connected=True, country=country or "Germany",
                        city="Frankfurt" if (country or "Germany") == "Germany" else None)
    else:
        args = ["connect"]
        if country:
            target = vpn_target(country)
            if not target:
                return False, "NordVPN doesn't offer that country."
            args.append(target)
        code, text = nordvpn(*args, timeout=90)
        # Exit codes don't match outcomes ("already in progress" exits 0), so read the state back.
        if not vpn_wait_connected(30 if "in progress" in text.lower() else 3):
            return False, "Couldn't connect. " + last_line(text, "NordVPN didn't say why.")
    s = get_settings()
    s["vpn_country"] = country
    save_json(SETTINGS_FILE, s)
    if vpn_status(full=True).get("autoconnect"):
        vpn_set_autoconnect(True)  # follow the new country at the next start too
    return True, ""


def vpn_disconnect():
    if FAKE:
        FAKE_VPN.update(connected=False, country=None, city=None)
        return True, ""
    code, text = nordvpn("disconnect", timeout=30)
    return (True, "") if code == 0 else (False, last_line(text, "Couldn't disconnect."))


def vpn_set_autoconnect(on):
    if FAKE:
        FAKE_VPN["autoconnect"] = bool(on)
        return True, ""
    if on:
        target = vpn_target(get_settings().get("vpn_country") or "")
        code, text = nordvpn(*(["set", "autoconnect", "on"] + ([target] if target else [])), timeout=20)
    else:
        code, text = nordvpn("set", "autoconnect", "off", timeout=20)
    return (True, "") if code == 0 else (False, last_line(text, "Couldn't change that setting."))


def vpn_logout():
    if FAKE:
        FAKE_VPN.update(logged_in=False, connected=False, country=None, city=None, autoconnect=False)
        return True, ""
    nordvpn("set", "autoconnect", "off", timeout=20)  # logout alone leaves auto-connect on
    s = get_settings()
    revoke = ["--revoke-token"] if s.get("vpn_token_login") else []
    code, text = nordvpn("logout", *revoke, timeout=30)
    if code == 0 or "not logged in" in text.lower():
        _vpn_login.update(url=None, error=None)
        s["vpn_token_login"] = False
        save_json(SETTINGS_FILE, s)
        return True, ""
    return False, last_line(text, "Couldn't sign out.")


def vpn_consent():
    """Answers NordVPN's analytics question ahead of time. Without an answer, every login
    asks it on stdin and, with nobody to reply, never finishes."""
    nordvpn("set", "analytics", "off", timeout=20)  # "already set" is fine too


def vpn_token_login(token):
    token = re.sub(r"\s+", "", token or "").lower()
    if not TOKEN_RE.match(token):
        return False, "That doesn't look like a NordVPN token. It's a long string of numbers and letters a to f."
    if FAKE:
        if token.startswith("dead"):
            return False, "That token didn't work."
        FAKE_VPN["logged_in"] = True
        return True, ""
    vpn_consent()
    code, text = nordvpn("login", "--token", token, timeout=60)
    if vpn_signed_in():  # the CLI can exit 0 without having logged in
        s = get_settings()
        s["vpn_token_login"] = True  # so Sign Out revokes it; NordVPN 6 keeps tokens valid otherwise
        save_json(SETTINGS_FILE, s)
        return True, ""
    return False, "That token didn't work. " + last_line(text, "")


def vpn_login_start():
    """Starts Nord's browser sign-in and returns its URL for Firefox to open."""
    if FAKE:
        FAKE_VPN["logged_in"] = True
        return "/#vpn", None
    # A second "nordvpn login" replaces the pending one, so reuse it while it's fresh.
    if _vpn_login["url"] and time.time() - _vpn_login["started"] < 600:
        return _vpn_login["url"], None
    prepare_callback_dir()
    try:
        CALLBACK_FILE.unlink()
    except OSError:
        pass
    vpn_consent()
    code, text = nordvpn("login", timeout=30)
    m = LOGIN_URL_RE.search(text)
    if not m:
        if "already logged in" in text.lower():
            return None, "You're already signed in."
        return None, last_line(text, "NordVPN didn't give a sign-in link.")
    _vpn_login.update(started=time.time(), url=m.group(1), error=None)
    if not _vpn_login["watching"]:
        _vpn_login["watching"] = True
        threading.Thread(target=_vpn_watch_callback, daemon=True).start()
    return m.group(1), None


def _vpn_finish_login(link):
    """Completes a browser sign-in from Nord's return link, or records Nord's error."""
    if "exchange_token=" not in link:
        # Nord's error link looks like nordvpn://login?code=<n>&status=error
        code = re.search(r"[?&]code=(\d+)", link)
        _vpn_login["error"] = "Nord couldn't finish it%s. Try again." % (
            " (code %s)" % code.group(1) if code else "")
        return
    with _vpn_lock:
        _, text = nordvpn("login", "--callback", link, timeout=90)
        ok = vpn_signed_in() is True  # the CLI can say Welcome even when it failed
    if ok:
        _vpn_login["signed_in"] = True
        s = get_settings()
        s["vpn_token_login"] = False
        save_json(SETTINGS_FILE, s)
    _vpn_login["error"] = None if ok else last_line(text, "The sign-in didn't finish.")


def _vpn_watch_callback():
    try:
        while time.time() - _vpn_login["started"] < 900:
            if CALLBACK_FILE.exists():
                _vpn_login["finishing"] = True  # before the file goes, so the page sees one or the other
                try:
                    link = CALLBACK_FILE.read_text(errors="replace").strip()
                    CALLBACK_FILE.unlink()
                    if link.startswith("nordvpn://login?") and len(link) <= 4096 and not re.search(r"\s", link):
                        _vpn_finish_login(link)
                        _vpn_login["url"] = None
                        return
                finally:
                    _vpn_login["finishing"] = False
            time.sleep(0.5)
    finally:
        _vpn_login["watching"] = False


def _vpn_learn_sign_in():
    """Gives the 15-second poll its first sign-in answer once NordVPN's service is up, so
    Settings can say Not Signed In before anyone opens the VPN screen."""
    for _ in range(30):
        if _vpn_login["signed_in"] is not None:
            return
        answer = vpn_signed_in()
        if answer is not None:
            if _vpn_login["signed_in"] is None:  # a newer answer may have come in meanwhile
                _vpn_login["signed_in"] = answer
            return
        time.sleep(10)


# ---------- tile icons ----------

class IconLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = []
        self.manifest = None

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        rel = a.get("rel", "").lower().split()
        href = a.get("href", "").strip()
        if not href:
            return
        if "manifest" in rel:
            self.manifest = href
        elif "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel:
            self.found.append((icon_size(a.get("sizes", ""), href, 180), href))
        elif "icon" in rel:
            self.found.append((icon_size(a.get("sizes", ""), href, 32), href))


def icon_size(sizes, href, fallback):
    best = 0
    for s in sizes.lower().split():
        if s == "any":
            best = max(best, 256)
        m = re.match(r"(\d+)x\d+", s)
        if m:
            best = max(best, int(m.group(1)))
    if not best:
        best = 256 if href.lower().split("?")[0].endswith(".svg") else fallback
    return best


def http_get(url, limit):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=8) as r:
        data = r.read(limit + 1)
        if len(data) > limit:
            raise ValueError("too large")
        return r.geturl(), r.headers.get_content_type(), data


def sniff_image(ctype, data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if b"<svg" in data[:1000].lower():
        return "image/svg+xml"
    return ctype if ctype.startswith("image/") and ctype != "image/svg+xml" else None


def find_icon(site):
    host = (urllib.parse.urlsplit(site).hostname or "").lower()
    candidates, base = [], site
    for domain, urls in ICON_HINTS.items():
        if host == domain or host.endswith("." + domain):
            candidates += [(1000 - i, u) for i, u in enumerate(urls)]
    try:
        base, _, html = http_get(site, 3000000)
        links = IconLinks()
        links.feed(html.decode("utf-8", "replace"))
        candidates += [(size, urllib.parse.urljoin(base, href)) for size, href in links.found]
        if links.manifest:
            murl = urllib.parse.urljoin(base, links.manifest)
            try:
                _, _, mdata = http_get(murl, 500000)
                for ic in json.loads(mdata.decode("utf-8", "replace")).get("icons", []):
                    if isinstance(ic, dict) and ic.get("src"):
                        candidates.append((icon_size(str(ic.get("sizes", "")), ic["src"], 192),
                                           urllib.parse.urljoin(murl, ic["src"])))
            except Exception:
                pass
    except Exception:
        pass
    root = "%s://%s/" % tuple(urllib.parse.urlsplit(base)[:2])
    candidates += [(180, root + "apple-touch-icon.png"), (16, root + "favicon.ico")]
    candidates.sort(key=lambda c: -min(c[0], 1000))
    seen = set()
    for _, url in candidates:
        if url in seen or not url.startswith(("http://", "https://")):
            continue
        seen.add(url)
        try:
            _, ctype, data = http_get(url, 1500000)
        except Exception:
            continue
        kind = sniff_image(ctype, data) if len(data) > 64 else None
        if kind:
            return kind, data
    return None


_icon_locks = {}
_icon_guard = threading.Lock()


def icon_for(host, site):
    safe = re.sub(r"[^a-z0-9.-]", "_", host.lower())
    data_f, type_f, fail_f = (ICONS / (safe + ext) for ext in (".img", ".type", ".fail"))
    with _icon_guard:
        lock = _icon_locks.setdefault(safe, threading.Lock())
    with lock:
        if data_f.exists() and type_f.exists():
            return type_f.read_text().strip(), data_f.read_bytes()
        if fail_f.exists() and time.time() - fail_f.stat().st_mtime < 1800:
            return None
        got = find_icon(site)
        ICONS.mkdir(parents=True, exist_ok=True)
        if got:
            data_f.write_bytes(got[1])
            type_f.write_text(got[0])
        else:
            fail_f.touch()
        return got


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "tvd"

    def log_message(self, fmt, *args):
        pass

    def reply(self, code, body=b"", ctype="application/json", headers=None, cache="no-store"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def host_ok(self):
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def do_GET(self):
        if not self.host_ok():
            return self.reply(403, "forbidden", "text/plain")
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        if path == "/healthz":
            return self.reply(200, "ok", "text/plain")
        if path in ("/", "/index.html"):
            html = (WWW / "index.html").read_text(encoding="utf-8").replace("__TV_TOKEN__", TOKEN)
            return self.reply(200, html, "text/html; charset=utf-8",
                              {"Content-Security-Policy": CSP, "X-Frame-Options": "DENY",
                               "Referrer-Policy": "no-referrer"})
        if path.startswith("/icon/"):
            if urllib.parse.parse_qs(url.query).get("t", [""])[0] != TOKEN:
                return self.reply(403, "forbidden", "text/plain")
            host = urllib.parse.unquote(path[len("/icon/"):]).lower()
            site = next((t["url"] for t in get_tiles()
                         if (urllib.parse.urlsplit(t["url"]).hostname or "") == host), None)
            got = icon_for(host, site) if site else None
            if not got:
                return self.reply(404, "", "text/plain")
            return self.reply(200, got[1], got[0], cache="private, max-age=86400")
        if path.startswith("/api/"):
            if self.headers.get("X-TV-Token") != TOKEN:
                return self.reply(403, {"error": "forbidden"})
            return self.api(path, None)
        return self.reply(404, "not found", "text/plain")

    def do_POST(self):
        if not self.host_ok() or self.headers.get("X-TV-Token") != TOKEN:
            return self.reply(403, {"error": "forbidden"})
        length = to_int(self.headers.get("Content-Length"), 0)
        if length > 65536:
            return self.reply(413, {"error": "too large"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self.reply(400, {"error": "bad request"})
        return self.api(urllib.parse.urlsplit(self.path).path, body)

    def api(self, path, body):
        get = body is None
        if path == "/api/state" and get:
            build = BUILD_FILE.read_text().strip() if BUILD_FILE.exists() else "development copy"
            return self.reply(200, {
                "tiles": get_tiles(), "settings": get_settings(), "net": net_status(),
                "vpn": vpn_status(full=False), "bt": bt_summary(),
                "windows": bool(windows_entry()), "update_pending": update_pending(),
                "host": "tvstick" if FAKE else socket.gethostname(), "build": build})
        if path.startswith("/api/vpn"):
            return self.vpn_api(path, body)
        if path.startswith("/api/bt"):
            return self.bt_api(path, body)
        if path == "/api/wifi":
            if get:
                return self.reply(200, {"networks": wifi_scan()})
            ok, err = wifi_connect(str(body.get("ssid", "")), str(body.get("password", "")))
            return self.reply(200, {"ok": ok, "error": err})
        if path == "/api/audio":
            if get:
                return self.reply(200, {"outputs": audio_outputs()})
            ok, err = set_audio(str(body.get("id", "")))
            return self.reply(200, {"ok": ok, "error": err})
        if path == "/api/tiles" and not get:
            tiles = clean_tiles(body.get("tiles"))
            save_json(TILES_FILE, tiles)
            return self.reply(200, {"ok": True, "tiles": tiles})
        if path == "/api/settings" and not get:
            size = body.get("text_size")
            if size not in TEXT_SIZES:
                return self.reply(200, {"ok": False, "error": "Unknown text size."})
            s = get_settings()
            changed = s.get("text_size") != size
            s["text_size"] = size
            save_json(SETTINGS_FILE, s)
            if changed:
                threading.Timer(1.2, restart_browser).start()
            return self.reply(200, {"ok": True, "restarting": changed})
        if path == "/api/power" and not get:
            ok, err = power(str(body.get("action", "")))
            return self.reply(200, {"ok": ok, "error": err})
        if path == "/api/browser/restart" and not get:
            threading.Timer(0.5, restart_browser).start()
            return self.reply(200, {"ok": True})
        return self.reply(404, {"error": "not found"})

    def bt_api(self, path, body):
        if body is None:
            return self.reply(200, bt_status()) if path == "/api/bt" else self.reply(404, {"error": "not found"})
        mac = str(body.get("mac", "")).upper()
        needs_mac = {"/api/bt/pair": bt_pair, "/api/bt/connect": bt_connect,
                     "/api/bt/disconnect": bt_disconnect, "/api/bt/forget": bt_forget}
        if path == "/api/bt/power":
            action = lambda: bt_power(bool(body.get("on")))
        elif path == "/api/bt/scan":
            action = bt_scan
        elif path in needs_mac:
            if not MAC_RE.match(mac):
                return self.reply(200, {"ok": False, "error": "That isn't a Bluetooth address."})
            action = lambda: needs_mac[path](mac)
        else:
            return self.reply(404, {"error": "not found"})
        if not _bt_lock.acquire(timeout=1):
            return self.reply(200, {"ok": False, "error": "Still busy with the last Bluetooth change."})
        try:
            ok, err = action()
        finally:
            _bt_lock.release()
        return self.reply(200, {"ok": ok, "error": err, "bt": bt_status()})

    def vpn_api(self, path, body):
        get = body is None
        if get and path == "/api/vpn":
            return self.reply(200, vpn_status(full=True))
        if get and path == "/api/vpn/countries":
            return self.reply(200, {"countries": vpn_countries()})
        if get:
            return self.reply(404, {"error": "not found"})
        if path == "/api/vpn/login":
            url, err = vpn_login_start()
            return self.reply(200, {"ok": bool(url), "url": url, "error": err})
        actions = {
            "/api/vpn/token": lambda: vpn_token_login(str(body.get("token", "")).strip()),
            "/api/vpn/connect": lambda: vpn_connect(str(body.get("country", "")).strip()),
            "/api/vpn/disconnect": vpn_disconnect,
            "/api/vpn/autoconnect": lambda: vpn_set_autoconnect(bool(body.get("on"))),
            "/api/vpn/logout": vpn_logout,
        }
        if path not in actions:
            return self.reply(404, {"error": "not found"})
        if not FAKE and not os.path.exists(NORDVPN):
            return self.reply(200, {"ok": False, "error": "NordVPN isn't installed."})
        if not _vpn_lock.acquire(timeout=1):
            return self.reply(200, {"ok": False, "error": "Still busy with the last VPN change."})
        try:
            ok, err = actions[path]()
        finally:
            _vpn_lock.release()
        return self.reply(200, {"ok": ok, "error": err, "vpn": vpn_status(full=True)})


def prepare_callback_dir():
    """/run/tvd, where tv-vpn-callback (running as tv) drops the sign-in link."""
    CALLBACK_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(CALLBACK_DIR, 0, pwd.getpwnam(TV_USER).pw_gid)
    os.chmod(CALLBACK_DIR, 0o770)


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    if not FAKE:
        prepare_callback_dir()
    threading.Thread(target=windows_entry, daemon=True).start()
    if FAKE or os.path.exists(NORDVPN):
        threading.Thread(target=_vpn_learn_sign_in, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print("tvd listening on 127.0.0.1:%d%s" % (PORT, " (fake data)" if FAKE else ""), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
