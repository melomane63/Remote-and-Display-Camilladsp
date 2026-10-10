#!/usr/bin/env bash
#
# Installs the LMS <-> CamillaDSP volume sync:
#   - dependencies (git, curl, python venv); stops if squeezelite is missing
#   - /opt/venv with pycamilladsp (only if missing)
#   - the sync script downloaded from GitHub into ~/scripts
#   - a /etc/hosts entry mapping lyrionserver.local to the LMS IP
#   - a systemd service, enabled and started
#
# Usage:
#   sudo bash install_cdsp_lms_volume_sync.sh [--old-service NAME] [--lms-ip IP] [--venv DIR]
#
# Safe to run again: it updates the script and the service in place.
# If we are running from a pipe (<(...)) and not root, force a re-download.
if [[ $EUID -ne 0 && ! -r "$0" || "$0" == /dev/fd/* ]]; then
    SELF_URL="https://raw.githubusercontent.com/melomane63/Remote-and-Display-Camilladsp/main/install_cdsp_lms_volume_sync.sh"
    TMP_SCRIPT="$(mktemp /tmp/install_cdsp_lms.XXXXXX)"
    curl -fsSL "$SELF_URL" -o "$TMP_SCRIPT"
    exec sudo bash "$TMP_SCRIPT" "$@"
fi
set -euo pipefail

# ---------------------------------------------------------------- settings
LMS_IP="192.168.1.81"                  # written to /etc/hosts for lyrionserver.local ("" = skip)
OLD_SERVICE=""                         # old sync service to stop and disable
VENV="/opt/venv"
SERVICE_NAME="cdsp-lms-volume-sync"
PYCAMILLADSP="git+https://github.com/HEnquist/pycamilladsp.git@15d9b7c434b8e795bcad25783b75d5354acdb840"
SYNC_URL="https://raw.githubusercontent.com/melomane63/Remote-and-Display-Camilladsp/main/cdsp_lms_volume_sync.py"
LMS_HOST="lyrionserver.local"          # fixed: the downloaded Python hard-codes this name

while [[ $# -gt 0 ]]; do
    case "$1" in
        --old-service) OLD_SERVICE="$2"; shift 2 ;;
        --lms-ip)      LMS_IP="$2";      shift 2 ;;
        --venv)        VENV="$2";        shift 2 ;;
        -h|--help)     sed -n '2,15p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

for v in VENV SERVICE_NAME PYCAMILLADSP SYNC_URL LMS_HOST; do
    if [[ -z "${!v}" ]]; then
        echo "ERROR: setting $v is empty: this copy of the script is incomplete. Download it again." >&2
        exit 1
    fi
done

# ---------------------------------------------------------------- checks
if [[ $EUID -ne 0 ]]; then
    # Not root: copy this script to a temp file and re-run it with sudo.
    if ! command -v sudo >/dev/null; then
        echo "sudo is not installed: run this script as root" >&2
        exit 1
    fi
    if [[ ! -r "$0" ]]; then
        echo "Cannot re-run with sudo from a pipe: use  curl ... | sudo bash" >&2
        exit 1
    fi
    TMP_SCRIPT="$(mktemp /tmp/install_cdsp_lms.XXXXXX)"
    cat "$0" > "$TMP_SCRIPT"
    echo "==> Root rights needed, re-running with sudo"
    exec sudo bash "$TMP_SCRIPT" "$@"
fi

case "$0" in /tmp/install_cdsp_lms.*) trap 'rm -f "$0"' EXIT ;; esac

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
SCRIPT_PATH="$SCRIPT_DIR/cdsp_lms_volume_sync.py"
SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME.service"

echo "==> User: $TARGET_USER"
echo "==> Script: $SCRIPT_PATH"
echo "==> LMS host: $LMS_HOST"
echo "==> Python environment: $VENV"

# ---------------------------------------------------------------- packages
echo "==> Installing packages"
export DEBIAN_FRONTEND=noninteractive
PKGS=()
command -v git >/dev/null || PKGS+=(git)
command -v curl >/dev/null || PKGS+=(curl)
python3 -c "import venv, ensurepip" 2>/dev/null || PKGS+=(python3-venv python3-pip)
if (( ${#PKGS[@]} )); then
    echo "    missing: ${PKGS[*]}"
    apt-get update -qq || echo "    WARNING: apt update reported errors (continuing with the old package index)"
    apt-get install -y -qq "${PKGS[@]}"
else
    echo "    nothing to install"
fi

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
else
    echo "==> Skipping /etc/hosts entry (LMS_IP is empty)"
fi

# ---------------------------------------------------------------- sync script
echo "==> Downloading sync script from GitHub"
install -d -o "$TARGET_USER" -g "$TARGET_USER" "$SCRIPT_DIR"

TMP_SYNC="$(mktemp /tmp/cdsp_lms_volume_sync.XXXXXX.py)"
trap 'rm -f "$TMP_SYNC"' EXIT

if ! curl -fsSL "$SYNC_URL" -o "$TMP_SYNC"; then
    echo "ERROR: could not download $SYNC_URL" >&2
    exit 1
fi

# Sanity checks on the downloaded file: make sure it's the expected script.
grep -q "def detect_player_mac" "$TMP_SYNC" \
    || { echo "ERROR: downloaded script has no detect_player_mac()" >&2; exit 1; }
grep -q "PLAYER_MANUAL" "$TMP_SYNC" \
    || { echo "ERROR: downloaded script has no PLAYER_MANUAL setting" >&2; exit 1; }
grep -q '^LMS_ADDR = ("lyrionserver.local", 9090)' "$TMP_SYNC" \
    || echo "    WARNING: LMS_ADDR in upstream script is not what was expected, check it"

install -o "$TARGET_USER" -g "$TARGET_USER" -m 644 "$TMP_SYNC" "$SCRIPT_PATH"
echo "    saved to $SCRIPT_PATH"

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
Description=LMS <-> CamillaDSP volume sync
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

    # Detect the MAC the same way the sync script does, to check LMS knows it.
    DETECTED_MAC="$(python3 - <<'PY'
import os
try:
    with os.popen("ip route show default") as r:
        iface = next((l.split()[4] for l in r if "default" in l), None)
    if iface:
        with open(f"/sys/class/net/{iface}/address") as f:
            print(f.read().strip().lower())
except Exception:
    pass
PY
)"
    if [[ -n "$DETECTED_MAC" ]]; then
        echo "    Detected player MAC: $DETECTED_MAC"
        PL_REPLY="$(timeout 5 bash -c 'exec 3<>/dev/tcp/'"$LMS_HOST"'/9090; printf "players 0 50\n" >&3; timeout 3 cat <&3' 2>/dev/null || true)"
        if grep -qi "$DETECTED_MAC" <<<"$PL_REPLY"; then
            echo "    Player known by LMS: OK"
        else
            echo "    WARNING: detected MAC not listed by LMS"
            echo "             (is squeezelite connected? right interface? or set PLAYER_MANUAL)"
        fi
    else
        echo "    WARNING: could not detect the default network interface MAC"
    fi
else
    echo "    WARNING: LMS CLI not reachable on $LMS_HOST:9090 (Settings > Advanced > Network)"
fi

echo
echo "Done."
echo "  Logs:    journalctl -u $SERVICE_NAME -f"
echo "  Edit:    nano $SCRIPT_PATH && sudo systemctl restart $SERVICE_NAME"
