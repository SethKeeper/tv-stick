#!/usr/bin/env python3
"""tvd: the TV stick's home screen service.

Serves the home screen on 127.0.0.1:8765 and does the system jobs the screen
needs: Wi-Fi, sound output, text size, power, restarting into Windows, and the
list of site tiles. It runs as root, so it only answers requests addressed to
127.0.0.1 or localhost (which blocks DNS rebinding) and every API call must
carry the per-boot token that is written into the home screen page.

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


def run(cmd, timeout=15, as_tv=False):
    """Runs a command and returns (exit code, stdout, stderr). Never raises."""
    if as_tv:
        cmd = ["runuser", "-u", TV_USER, "--", "env",
               "XDG_RUNTIME_DIR=/run/user/%d" % tv_uid()] + list(cmd)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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
                "windows": bool(windows_entry()), "update_pending": update_pending(),
                "host": "tvstick" if FAKE else socket.gethostname(), "build": build})
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


def main():
    STATE.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=windows_entry, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print("tvd listening on 127.0.0.1:%d%s" % (PORT, " (fake data)" if FAKE else ""), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
