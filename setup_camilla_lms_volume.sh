#!/usr/bin/env bash
#
# Installs the LMS <-> CamillaDSP volume bridge:
#   - dependencies (git, python venv); stops if squeezelite is missing
#   - /opt/venv with pycamilladsp (only if missing)
#   - the bridge script in ~/scripts
#   - a systemd service, enabled and started
#
# Usage:
#   sudo bash install_camilla_lms_volume.sh [--old-service NAME] [--lms-host HOST] [--lms-ip IP] [--player MAC] [--venv DIR]
#
# Safe to run again: it updates the script and the service in place.

set -euo pipefail

# ---------------------------------------------------------------- settings
LMS_HOST="lyrionserver.local"          # name used by the bridge
LMS_IP="192.168.1.81"                  # written to /etc/hosts for LMS_HOST ("" = skip)
PLAYER="dc:a6:32:3c:1c:21"             # MAC of the squeezelite player
OLD_SERVICE=""                         # old bridge service to stop and disable
VENV="/opt/venv"
SERVICE_NAME="camilla-lms-volume"
PYCAMILLADSP="git+https://github.com/HEnquist/pycamilladsp.git@15d9b7c434b8e795bcad25783b75d5354acdb840"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --old-service) OLD_SERVICE="$2"; shift 2 ;;
        --lms-host)    LMS_HOST="$2";    shift 2 ;;
        --lms-ip)      LMS_IP="$2";      shift 2 ;;
        --player)      PLAYER="$2";      shift 2 ;;
        --venv)        VENV="$2";        shift 2 ;;
        -h|--help)     sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

# ---------------------------------------------------------------- checks
if [[ $EUID -ne 0 ]]; then
    # Not root: copy this script to a temp file and re-run it with sudo.
    # (A temp copy is needed because "bash <(curl ...)" gives a pipe that sudo cannot reuse.)
    if ! command -v sudo >/dev/null; then
        echo "sudo is not installed: run this script as root" >&2
        exit 1
    fi
    if [[ ! -r "$0" ]]; then
        echo "Cannot re-run with sudo from a pipe: use  curl ... | sudo bash" >&2
        exit 1
    fi
    TMP_SCRIPT="$(mktemp /tmp/install_camilla_lms.XXXXXX)"
    cat "$0" > "$TMP_SCRIPT"
    echo "==> Root rights needed, re-running with sudo"
    exec sudo bash "$TMP_SCRIPT" "$@"
fi

# Remove the temporary copy when this run was started by the sudo re-exec above.
case "$0" in /tmp/install_camilla_lms.*) trap 'rm -f "$0"' EXIT ;; esac

# Preflight: squeezelite must already be installed (this script does not install it)
if ! command -v squeezelite >/dev/null; then
    echo "ERROR: squeezelite is not installed. Install it first, then run this script again." >&2
    exit 1
fi
SQ_DEFAULT="/etc/default/squeezelite"
if [[ ! -f "$SQ_DEFAULT" ]]; then
    echo "ERROR: $SQ_DEFAULT not found (squeezelite settings file expected there)." >&2
    exit 1
fi

TARGET_USER="${SUDO_USER:-pi}"
if ! id "$TARGET_USER" &>/dev/null; then
    echo "User '$TARGET_USER' does not exist" >&2
    exit 1
fi
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
SCRIPT_DIR="$TARGET_HOME/scripts"
SCRIPT_PATH="$SCRIPT_DIR/camilla_lms_volume.py"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

echo "==> User: $TARGET_USER"
echo "==> Script: $SCRIPT_PATH"
echo "==> LMS: $LMS_HOST  Player: $PLAYER"
echo "==> Python environment: $VENV"

# ---------------------------------------------------------------- packages
echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip

# ---------------------------------------------------------------- python venv
if [[ ! -x "$VENV/bin/python3" ]]; then
    echo "==> Creating $VENV"
    python3 -m venv "$VENV"
fi

if ! "$VENV/bin/python3" -c "from camilladsp import CamillaClient" &>/dev/null; then
    echo "==> Installing pycamilladsp"
    "$VENV/bin/pip" install --quiet "$PYCAMILLADSP"
else
    echo "==> pycamilladsp already installed"
fi
"$VENV/bin/python3" -c "from camilladsp import CamillaClient; print('    pycamilladsp OK')"

# ---------------------------------------------------------------- dummy sound card
# squeezelite needs a mixer (-V Master) so it leaves the signal at full scale.
if ! grep -qi dummy /proc/asound/cards 2>/dev/null; then
    echo "==> Loading snd-dummy"
    echo snd-dummy > /etc/modules-load.d/snd-dummy.conf
    modprobe snd-dummy || true
    if ! grep -qi dummy /proc/asound/cards 2>/dev/null; then
        echo "ERROR: the Dummy sound card could not be loaded (modprobe snd-dummy failed)." >&2
        exit 1
    fi
else
    echo "==> Dummy sound card already present"
fi

# ---------------------------------------------------------------- squeezelite options
# Append "-O hw:Dummy -V Master" to SB_EXTRA_ARGS in /etc/default/squeezelite.
# Existing options are kept; only the missing ones are added.
echo "==> Checking SB_EXTRA_ARGS in $SQ_DEFAULT"
[[ -f "$SQ_DEFAULT.bak" ]] || cp -p "$SQ_DEFAULT" "$SQ_DEFAULT.bak"

SQ_RESULT="$(python3 - "$SQ_DEFAULT" <<'PY'
import re, sys

path = sys.argv[1]
lines = open(path).read().split("\n")
pat = re.compile(r'^\s*SB_EXTRA_ARGS=(["\']?)(.*)\1\s*$')

idx, quote, value = None, '"', ""
for i, line in enumerate(lines):
    stripped = line.lstrip()
    if stripped.startswith("#") or not stripped.startswith("SB_EXTRA_ARGS"):
        continue
    m = pat.match(line)
    if not m:
        sys.exit("cannot parse this line, edit it by hand: " + line)
    idx, quote, value = i, (m.group(1) or '"'), m.group(2)
    break


def has(opt, text):
    return re.search(r'(^|\s)' + opt + r'(\s|$)', text) is not None


if idx is None:
    new_line = 'SB_EXTRA_ARGS="-O hw:Dummy -V Master"'
    if lines and lines[-1] == "":
        lines.insert(len(lines) - 1, new_line)
    else:
        lines.append(new_line)
else:
    add = []
    if not has("-O", value):
        add.append("-O hw:Dummy")
    if not has("-V", value):
        add.append("-V Master")
    if not add:
        print("unchanged")
        sys.exit(0)
    new_value = (value.strip() + " " + " ".join(add)).strip()
    lines[idx] = "SB_EXTRA_ARGS=" + quote + new_value + quote

open(path, "w").write("\n".join(lines))
print("changed")
PY
)" || { echo "ERROR: could not update $SQ_DEFAULT (backup: $SQ_DEFAULT.bak)" >&2; exit 1; }

if [[ "$SQ_RESULT" == "changed" ]]; then
    echo "    updated: $(grep -E '^[[:space:]]*SB_EXTRA_ARGS' "$SQ_DEFAULT")"
    echo "    backup:  $SQ_DEFAULT.bak"
    SQ_UNIT="$(systemctl list-unit-files --no-legend 'squeezelite*.service' 2>/dev/null \
        | awk '{print $1}' | head -n1 || true)"
    if [[ -n "$SQ_UNIT" ]]; then
        echo "==> Restarting $SQ_UNIT"
        systemctl restart "$SQ_UNIT"
    else
        echo "    WARNING: no squeezelite service found, restart squeezelite yourself"
    fi
else
    echo "    already contains -O and -V (not modified)"
fi

# ---------------------------------------------------------------- /etc/hosts
# Makes the LMS name resolve without relying on mDNS at boot.
if [[ -n "$LMS_IP" ]]; then
    if grep -qE "^[^#]*[[:space:]]${LMS_HOST//./\\.}([[:space:]]|$)" /etc/hosts; then
        echo "==> /etc/hosts already has an entry for $LMS_HOST:"
        grep -E "^[^#]*[[:space:]]${LMS_HOST//./\\.}([[:space:]]|$)" /etc/hosts | sed 's/^/    /'
    else
        echo "==> Adding $LMS_IP $LMS_HOST to /etc/hosts"
        echo "$LMS_IP $LMS_HOST" >> /etc/hosts
    fi
fi

# ---------------------------------------------------------------- bridge script
echo "==> Writing $SCRIPT_PATH"
install -d -o "$TARGET_USER" -g "$TARGET_USER" "$SCRIPT_DIR"

cat > "$SCRIPT_PATH" <<'PYEOF'
"""
Lyrion Music Server (LMS) <-> CamillaDSP volume bridge.
LMS -> Camilla : events pushed by the CLI (listen 1).
Camilla -> LMS : polling.
"""

import logging
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
LMS_ADDR = ("@LMS_HOST@", 9090)
CAMILLA_ADDR = ("127.0.0.1", 1234)
PLAYER = "@PLAYER@"

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
PYEOF

sed -i "s|@LMS_HOST@|$LMS_HOST|; s|@PLAYER@|$PLAYER|" "$SCRIPT_PATH"
chown "$TARGET_USER:$TARGET_USER" "$SCRIPT_PATH"
chmod 644 "$SCRIPT_PATH"

# Syntax check before touching the service
"$VENV/bin/python3" -m py_compile "$SCRIPT_PATH"
rm -rf "$SCRIPT_DIR/__pycache__"
echo "    syntax OK"

# ---------------------------------------------------------------- old service
if [[ -n "$OLD_SERVICE" ]]; then
    echo "==> Disabling old service: $OLD_SERVICE"
    systemctl disable --now "$OLD_SERVICE" 2>/dev/null || echo "    (not found or already stopped)"
fi

# ---------------------------------------------------------------- systemd service
# Order after the CamillaDSP service if one is found.
CAMILLA_UNIT="$(systemctl list-unit-files --no-legend 'camilla*.service' 2>/dev/null \
    | awk '{print $1}' | grep -v "^$SERVICE_NAME" | head -n1 || true)"
AFTER="network-online.target"
[[ -n "$CAMILLA_UNIT" ]] && AFTER="$AFTER $CAMILLA_UNIT"
echo "==> Writing $SERVICE_FILE (After=$AFTER)"

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=LMS <-> CamillaDSP volume bridge
After=$AFTER
Wants=network-online.target

[Service]
ExecStart=$VENV/bin/python3 $SCRIPT_PATH
Restart=always
RestartSec=5
User=$TARGET_USER

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
systemctl restart "$SERVICE_NAME"
sleep 2

# ---------------------------------------------------------------- result
echo
systemctl --no-pager --lines=8 status "$SERVICE_NAME" || true
echo
echo "==> Final checks"
tcp_ok() { timeout 3 bash -c "exec 3<>/dev/tcp/$1/$2" 2>/dev/null; }

if tcp_ok 127.0.0.1 1234; then
    echo "    CamillaDSP websocket (127.0.0.1:1234): OK"
else
    echo "    WARNING: CamillaDSP websocket not reachable (start camilladsp with -p 1234)"
fi

if tcp_ok "$LMS_HOST" 9090; then
    echo "    LMS CLI ($LMS_HOST:9090): OK"
    PL_REPLY="$(timeout 5 bash -c 'exec 3<>/dev/tcp/'"$LMS_HOST"'/9090; printf "players 0 50\n" >&3; read -t 3 -r l <&3; echo "$l"' 2>/dev/null || true)"
    if grep -qi "${PLAYER//:/%3A}" <<<"$PL_REPLY"; then
        echo "    Player $PLAYER known by LMS: OK"
    else
        echo "    WARNING: player $PLAYER not listed by LMS (is squeezelite connected? right MAC?)"
    fi
else
    echo "    WARNING: LMS CLI not reachable on $LMS_HOST:9090 (Settings > Advanced > Network)"
fi

echo
echo "Done."
echo "  Logs:    journalctl -u $SERVICE_NAME -f"
echo "  Edit:    nano $SCRIPT_PATH && sudo systemctl restart $SERVICE_NAME"
