"""
Lyrion Music Server (LMS) <-> CamillaDSP volume sync.

Based on the original work by mbrennwa:
    https://github.com/mbrennwa/camilla_LMS_volume_sync
Many thanks to the author. This version has been heavily modified:
the ALSA Dummy intermediate and the multi-thread mute worker have
been removed, the player MAC is now detected at startup (or set
manually via PLAYER_MANUAL), and the volume mapping uses a
psychoacoustic curve instead of a linear dB scale.
"""

import logging
import os
import re
import select
import signal
import socket
import sys
import time
from urllib.parse import quote, unquote

from camilladsp import CamillaClient

# --- Debug ---
DEBUG = False

# --- Configuration ---
LMS_ADDR = ("lyrionserver.local", 9090)  # echo "192.168.1.81 lyrionserver.local" | sudo tee -a /etc/hosts
CAMILLA_ADDR = ("127.0.0.1", 1234)

# Player MAC.
#   - leave empty ""  -> automatic detection (MAC of the default network interface)
#   - set a MAC       -> e.g. "dc:a6:32:3c:1c:21" (raw format, lowercase)
PLAYER_MANUAL = ""


def detect_player_mac():
    """MAC of the default network interface, raw format (dc:a6:32:3c:1c:21)."""
    with os.popen("ip route show default") as route_info:
        default_iface = next((line.split()[4] for line in route_info), None)
    if not default_iface:
        raise RuntimeError("No default network interface found")
    with open(f"/sys/class/net/{default_iface}/address") as f:
        return f.read().strip().lower()


PLAYER = PLAYER_MANUAL or detect_player_mac()

log = logging.getLogger("camilla_lms").info

# --- Curve ---
# Strictly increasing in both % and dB, spanning 0..100 %.
CURVE_POINTS = [
    (0, -99.0), (5, -80.0), (10, -65.0), (15, -50.0), (20, -44.1),
    (25, -36.9), (30, -34.5), (35, -31.8), (40, -30.0), (45, -27.3),
    (50, -24.6), (55, -21.9), (60, -19.8), (65, -17.1), (70, -14.7),
    (75, -12.3), (80, -9.6), (85, -7.2), (90, -4.8), (95, -2.4), (100, 0.0),
]


# --- Interpolation ---
def _interp(x, pts):
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (x - x0) / (x1 - x0) * (y1 - y0)
    return pts[-1][1]


_INV = [(d, p) for p, d in CURVE_POINTS]
pct_to_db = lambda p: _interp(p, CURVE_POINTS)
db_to_pct = lambda d: round(_interp(d, _INV))


# --- Timing / LMS regex ---
POLL_S = 0.25
RESTORE_TIMEOUT_S = 0.5   # after an unmute: LMS may report a wrong volume for this long

PQ = quote(PLAYER, safe="")
_P = re.escape(PLAYER)
RE_VOL = re.compile(rf"{_P} prefset server volume (-?\d+(?:\.\d+)?)$", re.I)
RE_MUTE = re.compile(rf"{_P} prefset server mute ([01])$", re.I)


def run():
    cdsp = CamillaClient(*CAMILLA_ADDR)
    cdsp.connect()
    log("Connected to CamillaDSP")
    sock = None
    try:
        sock = socket.create_connection(LMS_ADDR, timeout=5)
        sock.settimeout(None)
        log("Connected to LMS CLI")
        send = lambda cmd: sock.sendall((cmd + "\n").encode())

        # --- Bridge state ---
        vol = cdsp.volume
        pct = db_to_pct(vol.main_volume())
        muted = bool(vol.main_mute())
        restore_until = 0.0   # monotonic deadline of the post-unmute window (0 = none)
        prev = None           # previous Camilla reading (debounce)

        def set_mute(new, origin, apply):
            # After an unmute LMS may report a wrong volume (+/- keys restart from 0/5 %),
            # or nothing at all (icon click). During the restore window, correct any wrong
            # volume it reports and push nothing from Camilla.
            nonlocal muted, restore_until
            muted = new
            log(f"{origin} mute={new}")
            apply(new)
            restore_until = 0.0 if new else time.monotonic() + RESTORE_TIMEOUT_S

        def on_lms_volume(p):
            nonlocal pct, restore_until
            if time.monotonic() < restore_until:
                if p != pct:
                    log(f"LMS unmute: restore LMS to {pct}%")
                    send(f"{PQ} mixer volume {pct}")
                else:
                    restore_until = 0.0
                return
            if p != pct:   # real user action (an echo of our own push has p == pct)
                pct = p
                log(f"LMS {p}% -> Camilla {pct_to_db(p):.1f} dB")
                vol.set_main_volume(pct_to_db(p))

        # --- Startup: Camilla wins ---
        send("listen 1")
        send(f"{PQ} mixer volume {pct}")
        send(f"{PQ} mixer muting {int(muted)}")
        log(f"Startup: Camilla -> LMS {pct}% muted={muted}")

        buf = b""
        while True:
            # --- LMS -> Camilla ---
            if select.select([sock], [], [], POLL_S)[0]:
                data = sock.recv(4096)
                if not data:
                    raise ConnectionError("LMS closed the connection")
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = unquote(line.decode(errors="replace")).strip()
                    if DEBUG and "prefset server" in line:
                        log(f"<< {line}")
                    if (m := RE_MUTE.match(line)):
                        if (lms_muted := m[1] == "1") != muted:
                            set_mute(lms_muted, "LMS -> Camilla", vol.set_main_mute)
                        continue
                    if (m := RE_VOL.match(line)) and not muted:
                        on_lms_volume(round(abs(float(m[1]))))

            # --- Camilla -> LMS ---
            if (cam_muted := bool(vol.main_mute())) != muted:
                set_mute(cam_muted, "Camilla -> LMS",
                         lambda m: send(f"{PQ} mixer muting {int(m)}"))

            if muted or time.monotonic() < restore_until:
                prev = None
                continue

            p = db_to_pct(vol.main_volume())
            if p != pct and p == prev:   # confirmed by two consecutive reads
                pct, prev = p, None
                log(f"Camilla {p}% -> LMS")
                send(f"{PQ} mixer volume {p}")
            else:
                prev = p
    finally:
        if sock is not None:
            sock.close()
        try:
            cdsp.disconnect()
        except Exception:
            pass


def main():
    # Stdout goes to journalctl under systemd, which already timestamps each line.
    logging.basicConfig(stream=sys.stdout,
                        level=logging.DEBUG if DEBUG else logging.INFO,
                        format="%(message)s")
    # SIGINT already raises KeyboardInterrupt; both end up in run()'s finally block.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    while True:
        try:
            run()
        except Exception as e:
            log(f"Bridge error: {e}, restart in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
