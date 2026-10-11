# Synchronize Lyrion Music Server (LMS) volume slider with CamillaDSP volume

This howto adds LMS volume sync on top of a working Linux squeezelite -> ALSA Loopback -> CamillaDSP -> DAC endpoint.

## Overview

The Python sync script (cdsp_lms_volume_sync.py) reads and writes the CamillaDSP volume over its websocket, and talks to LMS over its CLI (port 9090) to keep both volume sliders aligned. It also syncs muting status.

## Prerequisites

- A working LMS -> squeezelite -> Loopback -> CamillaDSP -> DAC audio chain.
- LMS >= 9.0.2 (for the use_volume_control player attribute).
- Material Skin >= 5.8.3 (to honour use_volume_control).
- CamillaDSP with websocket enabled on the Camilla host.
- Python 3 venv with pycamilladsp.

## Quick install

On the Camilla host, run:

    bash <(curl -sSL "https://raw.githubusercontent.com/melomane63/Remote-and-Display-Camilladsp/main/install_cdsp_lms_volume_sync.sh?t=$(date +%s)")

The installer will:

- Install missing packages (git, curl, python3-venv).
- Create /opt/venv if needed, install pycamilladsp.
- Add lyrionserver.local to /etc/hosts if not already present.
- Download cdsp_lms_volume_sync.py into ~/scripts/.
- Create, enable and start cdsp-lms-volume-sync.service.
- Run final checks: CamillaDSP websocket, LMS CLI, and player MAC.

## Step 1: LMS volume control configuration

In the LMS web UI:

- Settings -> Player -> [your player] -> Extra Settings -> Audio -> Volume Control
  Set to: "Output level is fixed at 100%".
- Settings -> Plugins -> Material Skin (server-side settings)
  Enable: "show and allow volume control for fixed-volume players".

Do NOT set volume control to "adjustable". That would re-enable LMS's own
digital attenuation and defeat the purpose of the fixed 100% output.

## Step 2: Confirm CamillaDSP websocket port

    ps aux | grep camilladsp | grep -v grep
    ss -tlnp | grep camilladsp

Default port: 1234. Note it for Step 3.

## Step 3: Install the Python sync script

    sudo apt install python3-venv
    sudo python3 -m venv /opt/venv
    sudo /opt/venv/bin/pip install \
        'git+https://github.com/HEnquist/pycamilladsp.git@15d9b7c434b8e795bcad25783b75d5354acdb840'

Verify:

    /opt/venv/bin/python3 -c "from camilladsp import CamillaClient; print('OK')"

Install the sync script:

    sudo mkdir -p /home/<user>/scripts
    sudo curl -fsSL \
        https://raw.githubusercontent.com/melomane63/Remote-and-Display-Camilladsp/main/cdsp_lms_volume_sync.py \
        -o /home/<user>/scripts/cdsp_lms_volume_sync.py
    sudo chown <user>:<user> /home/<user>/scripts/cdsp_lms_volume_sync.py

Configuration parameters (top of the script):

- LMS_ADDR       : LMS host and CLI port (default ("lyrionserver.local", 9090))
- CAMILLA_ADDR   : CamillaDSP websocket host and port (default ("127.0.0.1", 1234))
- PLAYER_MANUAL  : "" for auto-detect, or MAC string e.g. "dc:a6:32:3c:1c:21"
- CURVE_POINTS   : % -> dB mapping
- DEBUG          : True to log every LMS prefset line

### Manual test

    /opt/venv/bin/python3 /home/<user>/scripts/cdsp_lms_volume_sync.py

Expected output:

    Curve validated
    Connected to CamillaDSP
    Connected to LMS CLI
    Startup: Camilla -> LMS 22% muted=False

Test: move LMS slider -> Camilla follows. Change Camilla -> LMS follows.
Mute/unmute either side -> other side follows. Ctrl-C to stop.

## Step 4: systemd service

Create /etc/systemd/system/cdsp-lms-volume-sync.service:

    [Unit]
    Description=LMS <-> CamillaDSP volume sync
    After=network-online.target camilladsp.service
    Wants=network-online.target

    [Service]
    Type=simple
    User=<user>
    ExecStart=/opt/venv/bin/python3 /home/<user>/scripts/cdsp_lms_volume_sync.py
    Restart=always
    RestartSec=5

    [Install]
    WantedBy=multi-user.target

Then:

    sudo systemctl daemon-reload
    sudo systemctl enable --now cdsp-lms-volume-sync
    journalctl -u cdsp-lms-volume-sync -f

## Troubleshooting

- LMS slider does not move CamillaDSP.
  Check "Output level is fixed at 100%" AND Material Skin's fixed-volume option.

- LMS slider jumps (target, back, target).
  Feedback loop. Fix as above. Do NOT use "adjustable".

- "CamillaDSP not ready", retrying...
  Websocket unreachable. Check CAMILLA_ADDR and -p 1234.

- "LMS closed the connection" repeatedly.
  LMS CLI unreachable. Check LMS_ADDR and port 9090.

- Volume applied twice, slider oscillates.
  Increase CONFIRM_READS (default 2) or check nothing else pushes volume.
