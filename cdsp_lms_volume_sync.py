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
        default_iface = next(
            (line.split()[4] for line in route_info if "default" in line), None
        )
    if not default_iface:
        raise RuntimeError("No default network interface found")
    with open(f"/sys/class/net/{default_iface}/address") as f:
        return f.read().strip().lower()


PLAYER = PLAYER_MANUAL or detect_player_mac()

# --- Logging ---
_logger = logging.getLogger("camilla_lms")


def setup_logging():
    """Log to stdout (goes to journalctl under systemd)."""
    _logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                                      datefmt="%Y-%m-%d %H:%M:%S"))
    _logger.addHandler(ch)


def log(msg):
    _logger.info(msg)


# --- Curve ---
CURVE_POINTS = [
    (0, -99.0), (5, -80.0), (10, -65.0), (15, -50.0), (20, -44.1),
    (25, -36.9), (30, -34.5), (35, -31.8), (40, -30.0), (45, -27.3),
    (50, -24.6), (55, -21.9), (60, -19.8), (65, -17.1), (70, -14.7),
    (75, -12.3), (80, -9.6), (85, -7.2), (90, -4.8), (95, -2.4), (100, 0.0),
]


def validate_curve(pts):
    """Ensure the curve is strictly increasing in both % and dB (inverse must exist)."""
    if len(pts) < 2:
        raise ValueError("CURVE_POINTS must have at least 2 points")
    if pts[0][0] != 0 or pts[-1][0] != 100:
        raise ValueError(f"CURVE_POINTS must span 0..100 %, got {pts[0][0]}..{pts[-1][0]}")
    for (p0, d0), (p1, d1) in zip(pts, pts[1:]):
        if p1 <= p0:
            raise ValueError(f"Curve not strictly increasing in %: {p0} -> {p1}")
        if d1 <= d0:
            raise ValueError(f"Curve not strictly increasing in dB: {d0} -> {d1}")


# --- Interpolation ---
def _interp(x, pts):
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (x - x0) / (x1 - x0) * (y1 - y0)


_INV = [(d, p) for p, d in CURVE_POINTS]
pct_to_db = lambda p: _interp(p, CURVE_POINTS)
db_to_pct = lambda d: round(_interp(d, _INV))


# --- Timing / LMS regex ---
POLL_S = 0.25
RESTORE_TIMEOUT_S = 0.5   # after an unmute: wait this long before pushing Camilla's volume
ECHO_TIMEOUT_S = 0.5      # max wait for LMS to echo a volume we pushed
CONFIRM_READS = 2         # Camilla volume must be read this many times in a row

PQ = quote(PLAYER, safe="")
_P = re.escape(PLAYER)
RE_VOL = re.compile(rf"{_P} prefset server volume (-?\d+(?:\.\d+)?)$", re.I)
RE_MUTE = re.compile(rf"{_P} prefset server mute ([01])$", re.I)


def connect_camilla():
    while True:
        try:
            c = CamillaClient(*CAMILLA_ADDR)
            c.connect()
            log("Connected to CamillaDSP")
            return c
        except Exception as e:
            log(f"CamillaDSP not ready ({e}), retrying...")
            time.sleep(2)


def run():
    cdsp = connect_camilla()
    sock = None
    try:
        vol = cdsp.volume
        sock = socket.create_connection(LMS_ADDR, timeout=5)
        sock.settimeout(None)
        log("Connected to LMS CLI")
        send = lambda cmd: sock.sendall((cmd + "\n").encode())

        # --- Bridge state ---
        st = {"awaiting_restore": False, "expected_echo": None, "state_deadline": 0.0}

        pct = db_to_pct(vol.main_volume())
        muted = bool(vol.main_mute())
        cand, cand_n = None, 0

        def to_idle():
            st["awaiting_restore"] = False
            st["expected_echo"] = None

        def arm_restore():
            # After an unmute LMS may report a wrong volume (+/- keys restart from 0/5 %),
            # or nothing at all (icon click). Either way, push nothing to LMS until the
            # deadline, and correct any wrong volume it reports meanwhile.
            st["awaiting_restore"] = True
            st["expected_echo"] = None
            st["state_deadline"] = time.monotonic() + RESTORE_TIMEOUT_S

        def push_volume(p):
            st["awaiting_restore"] = False
            st["expected_echo"] = p
            st["state_deadline"] = time.monotonic() + ECHO_TIMEOUT_S
            send(f"{PQ} mixer volume {p}")

        def on_lms_volume(p):
            nonlocal pct
            # 1. Waiting for LMS to report its restored volume after unmute.
            if st["awaiting_restore"]:
                if p != pct:
                    log(f"LMS unmute: restore LMS to {pct}%")
                    push_volume(pct)
                else:
                    to_idle()
                return
            # 2. Echo of our own push.
            if st["expected_echo"] is not None and p == st["expected_echo"]:
                to_idle()
                return
            # 3. Real user action.
            to_idle()
            if p != pct:
                pct = p
                log(f"LMS {p}% -> Camilla {pct_to_db(p):.1f} dB")
                vol.set_main_volume(pct_to_db(p))

        def tick(now):
            if (st["awaiting_restore"] or st["expected_echo"] is not None) \
                    and now >= st["state_deadline"]:
                if st["expected_echo"] is not None:
                    log("State timeout, resuming")
                to_idle()

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
                        lms_muted = m[1] == "1"
                        if lms_muted != muted:
                            muted = lms_muted
                            log(f"LMS mute={muted} -> Camilla")
                            vol.set_main_mute(muted)
                            (to_idle if muted else arm_restore)()
                        continue
                    if (m := RE_VOL.match(line)) and not muted:
                        on_lms_volume(round(abs(float(m[1]))))

            now = time.monotonic()
            tick(now)

            # --- Camilla -> LMS ---
            cam_muted = bool(vol.main_mute())
            if cam_muted != muted:
                muted = cam_muted
                log(f"Camilla mute={muted} -> LMS")
                send(f"{PQ} mixer muting {int(muted)}")
                (to_idle if muted else arm_restore)()

            if muted or st["awaiting_restore"] or st["expected_echo"] is not None:
                cand, cand_n = None, 0
                continue

            p = db_to_pct(vol.main_volume())
            if p == pct:
                cand, cand_n = None, 0
            elif p != cand:
                cand, cand_n = p, 1
            else:
                cand_n += 1
                if cand_n >= CONFIRM_READS:
                    pct, cand, cand_n = p, None, 0
                    log(f"Camilla {p}% -> LMS")
                    push_volume(p)
    finally:
        if sock is not None:
            sock.close()
        try:
            cdsp.disconnect()
        except Exception:
            pass


# --- Signal handling ---
def cleanup(signum, frame):
    log(f"Stopping bridge (signal {signum})")
    sys.exit(0)   # the finally block in run() closes the connections


for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(_sig, cleanup)


def main():
    setup_logging()
    validate_curve(CURVE_POINTS)
    log("Curve validated")

    while True:
        try:
            run()
        except Exception as e:
            log(f"Bridge error: {e}, restart in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()
