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
# Measured gain sent by LMS to Squeezelite (player model "Squeezebox2"),
# read from the audg packets: dB = 20 * log10(gain / 65536).
#    1 ..  25 %  : ~1.47 dB / step  (-72.25 .. -37 dB)
#   25 .. 100 %  : ~0.49 dB / step  (-37 .. 0 dB)
# 0 % is -100 dB (LMS gain 0); the real mute is handled separately.
# Points 40, 42, 45 and 48 were duplicated/inverted in the raw measurement
# (rounding of the LMS gain), so they are replaced by the mean of their
# neighbours to keep the curve strictly increasing (needed for the inverse).
CURVE_POINTS = [
    (0, -100.0), (1, -75.0), (2, -71.2), (3, -69.5), (4, -68.0),
    (5, -66.5), (6, -65.2), (7, -63.7), (8, -62.2), (9, -60.6),
    (10, -59.2), (11, -57.7), (12, -56.2), (13, -54.8), (14, -53.3),
    (15, -51.8), (16, -50.3), (17, -48.8), (18, -47.4), (19, -45.9),
    (20, -44.4), (21, -42.9), (22, -41.4), (23, -40.0), (24, -38.5),
    (25, -37.0), (26, -36.5), (27, -36.0), (28, -35.5), (29, -35.0),
    (30, -34.5), (31, -34.0), (32, -33.5), (33, -33.0), (34, -32.6),
    (35, -32.1), (36, -31.6), (37, -31.1), (38, -30.6), (39, -30.1),
    (40, -29.6), (41, -29.1), (42, -28.6), (43, -28.2), (44, -27.3),
    (45, -27.0), (46, -26.6), (47, -25.9), (48, -25.6), (49, -25.2),
    (50, -24.6), (51, -24.1), (52, -23.6), (53, -23.1), (54, -22.6),
    (55, -22.1), (56, -21.7), (57, -21.3), (58, -20.6), (59, -20.2),
    (60, -19.9), (61, -19.2), (62, -18.6), (63, -18.3), (64, -17.8),
    (65, -17.3), (66, -16.8), (67, -16.3), (68, -15.7), (69, -15.3),
    (70, -14.7), (71, -14.4), (72, -13.8), (73, -13.4), (74, -12.9),
    (75, -12.3), (76, -11.9), (77, -11.4), (78, -10.9), (79, -10.3),
    (80, -9.9), (81, -9.4), (82, -8.9), (83, -8.4), (84, -7.9),
    (85, -7.4), (86, -6.9), (87, -6.4), (88, -6.0), (89, -5.4),
    (90, -4.9), (91, -4.4), (92, -3.9), (93, -3.5), (94, -3.0),
    (95, -2.5), (96, -2.0), (97, -1.5), (98, -1.0), (99, -0.5),
    (100, 0.0),
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
