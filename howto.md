# Synchronize Lyrion Music Server (LMS) volume slider with CamillaDSP volume

This howto adds **LMS volume sync** on top of a working Linux **squeezelite → ALSA Loopback → CamillaDSP → DAC** endpoint.

## Overview: What you are building

On the Camilla host, the squeezelite player feeds the CamillaDSP instance. LMS volume for that player no longer uses squeezelite's built-in software gain; instead, volume is applied in CamillaDSP's **Volume** filter.

The Python sync script (`cdsp_lms_volume_sync.py`) reads and writes the CamillaDSP volume directly over its **websocket**, and talks to LMS over its **CLI** (port 9090) to keep both volume sliders aligned. It also syncs muting status across LMS and CamillaDSP.

### Audio path (PCM data)

The PCM data stays at 100% volume level through squeezelite and the loopback. The volume is controlled in CamillaDSP.

```
               LMS-stream
                    |
                    v
             squeezelite-PCM
                    |
                    v
            Loopback PCM dev 0
                    |
                    v
               CamillaDSP
                    |
                    v
                   DAC
```

### Volume-control path

Synchronisation of volume levels and muting status is **bidirectional**:

* Change in LMS GUI (volume slider or mute button) → CamillaDSP volume or muting follows
* CamillaDSP volume or muting change → LMS GUI follows

**Volume changes** are not pushed both ways at the same time to avoid feedback loops or race conditions. The script talks to:

- **CamillaDSP** directly over its websocket (`main_volume()` / `set_main_volume()`, `main_mute()` / `set_main_mute()`), polled every 250 ms.
- **LMS** via its CLI (port 9090), using `listen 1` to receive `prefset server volume` and `prefset server mute` notifications in real time.

The player is identified by its **MAC address**, either auto-detected from the default network interface (via `ip route show default` and `/sys/class/net/<iface>/address`) or set manually in `PLAYER_MANUAL`. The MAC is URL-encoded (`:` → `%3A`) when sent to LMS.

At startup, **CamillaDSP wins**: its current volume and mute state are pushed to LMS. From then on, both sides are kept in sync:

- **LMS → Camilla**: when LMS sends a `prefset` notification, the volume is converted % → dB using a **psychoacoustic curve** (21 points, from −99 dB at 0% to 0 dB at 100%) and applied to CamillaDSP.
- **Camilla → LMS**: the CamillaDSP volume is polled every 250 ms; a change is confirmed after **2 consecutive identical readings**, then pushed to LMS as a percentage.

**Anti-loop protection**: when the script pushes a value to LMS, it records it as `expected_echo` and ignores the notification LMS sends back. Similarly, after an unmute, it waits `RESTORE_TIMEOUT_S` (0.5 s) before resuming normal sync.

**Mute** is handled inline (no separate thread): a change on either side is propagated to the other, and the volume sync is paused while muted.

### LMS → Camilla and Camilla → LMS volume paths

```
           LMS-slider                   LMS-slider
                |                            ^
                v                            |
        (LMS prefset events)          (LMS CLI 9090,
                |                       mixer volume)
                v                            |
      cdsp_lms_volume_sync.py      cdsp_lms_volume_sync.py
                |                            ^
                |                            |
                v                            |
        CamillaDSP-volume            CamillaDSP-volume
              (websocket)                 (websocket)
```

### Components

| Piece | Role |
|-------|------|
| **LMS (Lyrion)** | Music server and GUI (often on another host than squeezelite and CamillaDSP) |
| **squeezelite** | Audio player, receives audio data from LMS and forwards to ALSA |
| **ALSA Loopback** (card) | Virtual ALSA PCM card; device 0 = squeezelite out, PCM device 1 = CamillaDSP capture |
| **ALSA Dummy** | Virtual ALSA mixer card (module `snd-dummy`); used only to neutralize squeezelite's software volume, **not read** by the sync script |
| **CamillaDSP** | DSP and volume filter |
| **`cdsp_lms_volume_sync.py`** | Python code to sync volume and muting between LMS and CamillaDSP |

## Quick install (recommended)

An installer script automates all the steps below. On the Camilla host, run:

```bash
bash <(curl -sSL "https://raw.githubusercontent.com/melomane63/Remote-and-Display-Camilladsp/main/install_cdsp_lms_volume_sync.sh?t=$(date +%s)")
```

The installer will:

1. Install missing packages (`git`, `curl`, `python3-venv`).
2. Create `/opt/venv` if needed, install `pycamilladsp`.
3. Load `snd-dummy` if not already present.
4. Patch `/etc/default/squeezelite` to add `-O hw:Dummy -V Master` if missing, and restart squeezelite.
5. Add `lyrionserver.local` to `/etc/hosts` if not already present.
6. Download `cdsp_lms_volume_sync.py` from GitHub into `~/scripts/`.
7. Create, enable and start `cdsp-lms-volume-sync.service`.
8. Run final checks: CamillaDSP websocket, LMS CLI, and player MAC known by LMS.

Options:

| Option | Description |
|--------|-------------|
| `--old-service NAME` | Disable and stop an older service (e.g. `camilla-lms-volume`) before installing |
| `--lms-ip IP` | IP written to `/etc/hosts` for `lyrionserver.local` (default `192.168.1.81`; set to `""` to skip) |
| `--venv DIR` | Python venv path (default `/opt/venv`) |

If you prefer to understand each step, or to do it manually, follow the sections below.

## Starting point (verify before you begin)

This How-To assumes you have a working LMS → squeezelite → Loopback card → CamillaDSP → DAC setup. Confirm your setup aligns with the below description.

### Camilla host

**squeezelite** — Debian package, autostart via generated `squeezelite.service` and `/etc/default/squeezelite`.

```bash
pgrep -a squeezelite
# expect: ... -n <name> -o hw:Loopback,0 -s <lms-host>
sudo ss -tnp | grep squeezelite
# expect: ESTAB ... :3483 and :9000 to LMS while playing
```

Example `/etc/default/squeezelite`:

```bash
SL_NAME="Living Room"
SB_SERVER_IP="lyrionserver.local"
SL_SOUNDCARD="hw:Loopback,0"
```

Use your LMS host as **`hostname.local`** (mDNS) or a fixed LAN IP for `SB_SERVER_IP` (required if discovery fails at boot).

**CamillaDSP** — running, audio OK, **named** ALSA cards/PCM devices in the active config:

```bash
systemctl status camilladsp
grep -E 'device:|hw:' ~/camilladsp/configs/<your-config>.yml
```

Typical PCM devices (CamillaDSP 3 syntax; card names from `aplay -l`):

```yaml
capture:
  device: hw:Loopback,1,0
playback:
  device: hw:DAC8PRO,0,0
```

Use **card names** from `aplay -l`, not numeric `hw:3,0` (card numbers can change across reboots).

**LMS** — on another host (e.g. lyrionserver.local). From the Camilla host, confirm the **CLI** (TCP port **9090**) is reachable—the sync script uses this both for pushing volumes and for receiving `prefset` notifications.

```bash
printf "version ?\n" | nc -w 3 lyrionserver.local 9090
```

Expect a line like `version 9.1.0`. If nothing comes back, check the LMS server is running and reachable; on the **LMS server** web UI see **Settings → Security** (Material: **Settings → Server → Security**) if remote access is blocked.

### ALSA layout

You should see something like:

| Card / target | Role |
|---------------|------|
| `Loopback`, PCM device 0 | squeezelite `-o` / `SL_SOUNDCARD` |
| `Loopback`, PCM device 1 | CamillaDSP capture |
| `DAC8PRO` (example), PCM device 0 | CamillaDSP playback |
| `Dummy`, Master mixer | squeezelite's volume target (`-O hw:Dummy -V Master`); **not read** by the sync script |

The **Dummy** card is needed to neutralize squeezelite's own volume attenuation: with `-O hw:Dummy -V Master`, squeezelite sends LMS volume commands to the Dummy card (which affects nothing) and passes the PCM signal through at full scale.

## Step 1: Set up the Dummy card and squeezelite volume flags

Create the **Dummy card** as a **`snd-dummy` module** with a **Master** mixer control, and load `snd-dummy` at boot:

```bash
echo snd-dummy | sudo tee /etc/modules-load.d/snd-dummy.conf
echo 'options snd-dummy fake_buffer=0 pcm_substreams=1' | sudo tee /etc/modprobe.d/snd_dummy.conf
```

`fake_buffer=0` keeps the card control-only (no fake PCM). `pcm_substreams=1` is required—do **not** use `pcm_substreams=0` (can hang on reboot).

Then reboot to activate the Dummy card:

```bash
sudo reboot
```

After reboot, confirm:

```bash
aplay -l                    # expect the new Dummy card
amixer -c Dummy scontrols   # expect Master
```

> Note: **Card indices may shift** when Dummy loads. Keep **`hw:Loopback,...`** and **`hw:DAC8PRO,...`** (names) in CamillaDSP YAML. Re-check audio after reboot before Step 2.

Edit `/etc/default/squeezelite`:

```bash
sudo nano /etc/default/squeezelite
```

If there is no **`SB_EXTRA_ARGS`**, add:

```
SB_EXTRA_ARGS="-O hw:Dummy -V Master"
```

Otherwise add the flags to the existing line (e.g. `SB_EXTRA_ARGS="-e alac -O hw:Dummy -V Master"`). If `-O` or `-V` are already present, set them to `hw:Dummy` and `Master`.

| Flag | Purpose |
|------|---------|
| `-O hw:Dummy` | Volume card (not PCM): squeezelite sends LMS volume commands to the Dummy card, not to the audio signal |
| `-V Master` | LMS volume drives Dummy **Master** |

With these flags, the PCM signal that squeezelite sends to the Loopback stays at **full scale**, and squeezelite's own attenuation is neutralized. The sync script does **not** read the Dummy card: it relies on LMS's `prefset` notifications instead.

Restart squeezelite:

```bash
sudo systemctl restart squeezelite
```

## Step 2: Enable LMS volume control for this player

In the LMS web UI, go to **Settings → Player → Extra settings → Audio → Volume control** and set it to **adjustable**. The sync script relies on LMS pushing `prefset server volume` and `prefset server mute` notifications over its CLI; with a fixed or disabled volume control, LMS sends none, and the sync script has nothing to react to.

## Step 3: Confirm CamillaDSP websocket port

The Python sync script reads and sets CamillaDSP volume over a **websocket** on this host (default port **1234**).

Check if CamillaDSP is started with websocket enabled:

```bash
ps aux | grep camilladsp | grep -v grep
```

If the command already includes **`-w`** or **`-p`** (e.g. `-p 1234`), websocket is on. Otherwise, reconfigure CamillaDSP to use websocket (e.g. add `-p 1234` to your `camilladsp` systemd unit; see CamillaDSP documentation).

Determine the websocket port:

```bash
ss -tlnp | grep camilladsp
```

Example line: `127.0.0.1:1234` — the number after the colon (**1234**) is the websocket port. Note the port number for Step 4.

## Step 4: Install the Python sync script

Install the Python dependencies. The sync script only needs **`pycamilladsp`** (no `pyalsaaudio`, no `amixer`).

If you already have a venv for Camilla tools (e.g. `~/camilla-venv`), you can reuse it, or use the standard `/opt/venv` used by this installer:

```bash
sudo apt install python3-venv
sudo python3 -m venv /opt/venv
sudo /opt/venv/bin/pip install \
    'git+https://github.com/HEnquist/pycamilladsp.git@15d9b7c434b8e795bcad25783b75d5354acdb840'
```

Verify:

```bash
/opt/venv/bin/python3 -c "from camilladsp import CamillaClient; print('OK')"
```

Install the sync script:

```bash
sudo mkdir -p /home/<user>/scripts
sudo curl -fsSL \
    https://raw.githubusercontent.com/melomane63/Remote-and-Display-Camilladsp/main/cdsp_lms_volume_sync.py \
    -o /home/<user>/scripts/cdsp_lms_volume_sync.py
sudo chown <user>:<user> /home/<user>/scripts/cdsp_lms_volume_sync.py
```

Then review the configuration block at the top of the script:

```bash
sudo nano /home/<user>/scripts/cdsp_lms_volume_sync.py
```

| Parameter | Description |
|---------|-------------|
| `LMS_ADDR` | LMS host and CLI port (default `("lyrionserver.local", 9090)`). The host name must resolve, either via mDNS or via `/etc/hosts`. |
| `CAMILLA_ADDR` | CamillaDSP websocket host and port (default `("127.0.0.1", 1234)`) |
| `PLAYER_MANUAL` | Set to `""` for auto-detection of the player MAC (from the default network interface), or set it manually to the MAC as shown in the LMS web UI (**Settings → Player → Extra settings → Basic**), e.g. `PLAYER_MANUAL = "dc:a6:32:3c:1c:21"` |
| `CURVE_POINTS` | Mapping between LMS 0–100 % and CamillaDSP dB. Edit to taste. |
| `DEBUG` | Set to `True` to log every LMS `prefset` line received (useful for troubleshooting). |

The MAC can also be checked from the LMS CLI:

```bash
printf "players 0 50\n" | nc -w 3 lyrionserver.local 9090
```

### Manual test of the sync script

> **WARNING:** Keep amplifiers low or switched off until the volume controls are confirmed to work correctly.

Test-run the script in a terminal so messages print there and you can stop with `Ctrl-C`:

```bash
/opt/venv/bin/python3 /home/<user>/scripts/cdsp_lms_volume_sync.py
```

You should see something like:

```
Curve validated
Connected to CamillaDSP
Connected to LMS CLI
Startup: Camilla -> LMS 22% muted=False
```

Test the sync script:

- Move the LMS volume slider → CamillaDSP volume follows; the log shows `LMS … % -> Camilla … dB` (and, with `DEBUG = True`, the `<< … prefset server volume …` line).
- Change volume in CamillaDSP → LMS slider follows; the log shows `Camilla … % -> LMS`.
- Press Camilla Mute / Unmute → LMS GUI mute follows; the log shows `Camilla mute=… -> LMS`.
- Press LMS GUI Mute / Unmute → Camilla mute follows; the log shows `LMS mute=… -> Camilla`.
- Press `Ctrl-C` to stop the test run.

## Step 5: Install and run the sync script as a `systemd` service

Use the installer (see **Quick install** at the top), or create the service manually. To do it manually, create `/etc/systemd/system/cdsp-lms-volume-sync.service`:

```ini
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
```

Adjust `User`, `ExecStart` (venv path if not `/opt/venv`), and paths as needed. Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cdsp-lms-volume-sync
journalctl -u cdsp-lms-volume-sync -f
```

The volume and mute settings of the LMS GUI and CamillaDSP should now stay aligned, and the service should start automatically after a reboot.

## Troubleshooting

**The LMS slider does not move CamillaDSP.**
Check that the player's volume control is set to *adjustable* in LMS (Step 2). With *Fixed* or *Disabled*, LMS sends no `prefset` notifications, and the sync script stays idle.

**CamillaDSP slider does not move the LMS slider.**
Set `DEBUG = True` in the script and restart it. If you see `<< … prefset server volume …` lines but no `LMS … % -> Camilla … dB` lines, the detected MAC does not match the one LMS uses. Check the MAC that LMS reports with `players 0 50` on the CLI, and set `PLAYER_MANUAL` accordingly.

**The service restarts in a loop.**
Check the journal: `journalctl -u cdsp-lms-volume-sync -n 50`. Common causes: CamillaDSP websocket not enabled (Step 3), LMS CLI not reachable, or `/etc/hosts` not populated with `lyrionserver.local`.

**Squeezelite applies its own volume attenuation (double attenuation).**
Make sure `/etc/default/squeezelite` has `-O hw:Dummy -V Master` in `SB_EXTRA_ARGS`, and that the Dummy card is loaded. Without these, squeezelite attenuates the PCM signal before it reaches CamillaDSP.