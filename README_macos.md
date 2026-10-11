# LMS <-> CamillaDSP volume sync on macOS

The Python script runs unchanged on macOS. Only the installation differs from Linux:
the installer script (`apt`, `systemd`) is not used, and a `launchd` agent replaces the systemd service.

> Not tested on a Mac by the author: adapt if something differs on your system.

## Prerequisites

- **Python 3.8 or newer**: check with `python3 --version`.
  If missing, run `xcode-select --install` (also provides `git`) or install Python with Homebrew.
- **CamillaDSP** running with its websocket enabled (`-p 1234`).
- **LMS CLI** enabled on port 9090 (LMS: Settings > Advanced > Network).
- Squeezelite connected to LMS, with these LMS settings:
  - Settings > Player > *your player* > Extra Settings > Audio > Volume Control:
    set to "Output level is fixed at 100%".
  - Settings > Plugins > Material Skin (server-side settings):
    enable "show and allow volume control for fixed-volume players".

## Installation

**1. Create the folder and the Python environment**

```
mkdir -p ~/cdsp-lms-sync && cd ~/cdsp-lms-sync
python3 -m venv venv
venv/bin/pip install "git+https://github.com/HEnquist/pycamilladsp.git@15d9b7c434b8e795bcad25783b75d5354acdb840"
```

**2. Download the script**

```
curl -fsSL https://raw.githubusercontent.com/melomane63/Remote-and-Display-Camilladsp/main/cdsp_lms_volume_sync.py -o cdsp_lms_volume_sync.py
```

**3. Set the player MAC address**

Automatic detection only works on Linux, so edit the script (`nano cdsp_lms_volume_sync.py`) and set
`PLAYER_MANUAL` to the MAC of your Squeezelite player, in lowercase:

```
PLAYER_MANUAL = "aa:bb:cc:dd:ee:ff"
```

It must be the MAC address LMS shows for your player. If it is the Mac's own network interface, you can read it with:

```
ifconfig en0 | awk '/ether/{print $2}'
```

If `lyrionserver.local` does not resolve, replace it with the LMS IP address in `LMS_ADDR`
(for example `("192.168.1.81", 9090)`).

**4. Test it by hand**

```
venv/bin/python3 cdsp_lms_volume_sync.py
```

You should see `Connected to CamillaDSP` and `Connected to LMS CLI`. Stop it with Ctrl-C.

**5. Install the launchd agent**

Put `local.cdsp-lms-volume-sync.plist` in the current folder, then:

```
sed "s|__HOME__|$HOME|g" local.cdsp-lms-volume-sync.plist > ~/Library/LaunchAgents/local.cdsp-lms-volume-sync.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.cdsp-lms-volume-sync.plist
```

The agent starts at login and restarts if it stops. If CamillaDSP or LMS is not ready yet, the script retries every 5 seconds.

## Daily use

| Task | Command |
|------|---------|
| Follow the logs | `tail -f ~/Library/Logs/cdsp-lms-volume-sync.log` |
| Restart after editing the script | `launchctl kickstart -k gui/$(id -u)/local.cdsp-lms-volume-sync` |
| Stop and remove | `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/local.cdsp-lms-volume-sync.plist` then delete the plist |

The log file is not rotated by launchd. It grows slowly in normal use, and you can delete it at any time.
