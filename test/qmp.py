#!/usr/bin/env python3
"""Drives the test VM through QEMU's monitor socket.

    test/qmp.py shot out/test/screen.png   screenshot as PNG
    test/qmp.py key down down ret          key presses (QEMU key names, ctrl-alt-f2 style combos)
    test/qmp.py type "hello world"         types lowercase text
    test/qmp.py move 960 540               moves the pointer (pixels on the 1920x1080 screen)
    test/qmp.py click 960 540              moves and left-clicks
"""
import json
import os
import socket
import sys
import time

SOCK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "out", "test", "qmp.sock")
WIDTH, HEIGHT = 1920, 1080
CHARS = {" ": "spc", ".": "dot", "-": "minus", "/": "slash", ":": "shift-semicolon", ",": "comma"}


def send(f, cmd, **args):
    f.write(json.dumps({"execute": cmd, "arguments": args} if args else {"execute": cmd}) + "\n")
    f.flush()
    while True:
        msg = json.loads(f.readline())
        if "error" in msg:
            raise SystemExit("QEMU: %s" % msg["error"].get("desc"))
        if "return" in msg:
            return msg["return"]


def keys(f, combo):
    send(f, "send-key", keys=[{"type": "qcode", "data": k} for k in combo.split("-")])
    time.sleep(0.12)


def pointer(f, x, y):
    send(f, "input-send-event", events=[
        {"type": "abs", "data": {"axis": "x", "value": int(float(x) * 32767 / WIDTH)}},
        {"type": "abs", "data": {"axis": "y", "value": int(float(y) * 32767 / HEIGHT)}}])


def main():
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    f = s.makefile("rw")
    f.readline()
    send(f, "qmp_capabilities")
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "shot":
        send(f, "screendump", filename=os.path.abspath(args[0]), format="png")
    elif cmd == "key":
        for combo in args:
            keys(f, combo)
    elif cmd == "type":
        for ch in " ".join(args):
            keys(f, CHARS.get(ch, ch))
    elif cmd in ("move", "click"):
        # Wiggle first so the guest sees real movement, then settle on the spot.
        pointer(f, float(args[0]) + 3, float(args[1]) + 3)
        time.sleep(0.05)
        pointer(f, args[0], args[1])
        if cmd == "click":
            time.sleep(0.15)
            for down in (True, False):
                send(f, "input-send-event", events=[{"type": "btn", "data": {"down": down, "button": "left"}}])
                time.sleep(0.08)
    else:
        raise SystemExit(__doc__)


main()
