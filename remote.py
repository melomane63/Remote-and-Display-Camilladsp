import asyncio
import glob
import json
import logging
import os
import signal
import socket
import sys
import time

import evdev
import lgpio
import serial
from camilladsp import CamillaClient
from tm1637_lgpio import TM1637


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("remote")


# ========================= CONSTANTS =========================

REMOTE_NAME = "HID Remote01 Keyboard"   # Found with `python3 -m evdev.evtest`
POWER_OFF_DELAY = 2                     # Minutes of silence before the power relay is switched off
HALT_DELAY = 48                         # Hours of silence (or without remote) before the Raspberry Pi shuts down
POWER_GPIO = 4                          # GPIO controlling the power relay
TV_GPIO = 12                            # GPIO reading the TV state (0 = TV on)
CLK, DIO = 23, 24                       # TM1637 display pins

# Remote control keys
KEY_BINDINGS = {
    'VOLUMEDOWN': 'KEY_VOLUMEDOWN',     # Decrease volume
    'VOLUMEUP': 'KEY_VOLUMEUP',         # Increase volume
    'MUTE': 'KEY_MUTE',                 # Mute/unmute audio
    'PLAYPAUSE': 'KEY_PLAYPAUSE',       # Pause (short press) / stop (long press) - LMS
    'PREVIOUSSONG': 'KEY_PREVIOUSSONG', # Previous song (LMS)
    'NEXTSONG': 'KEY_NEXTSONG',         # Next song (LMS)
    'UP': 'KEY_UP',                     # Presence gain + / treble +
    'DOWN': 'KEY_DOWN',                 # Presence gain - / treble -
    'LEFT': 'KEY_LEFT',                 # Tilt gain - / bass -
    'RIGHT': 'KEY_RIGHT',               # Tilt gain + / bass +
    'POWER': 'KEY_POWER',               # Long press: toggle auto power; very long press: shutdown
    'ENTER': 'KEY_ENTER',               # Short: bass/treble/tilt/presence all 0 or restored (loudness screen: loudness on-off); long: switch tone/tilt screen
    'BACK': 'KEY_BACK',                 # Next DSP configuration (files prefixed with "_")
    'HOMEPAGE': 'KEY_HOMEPAGE',         # Next DSP configuration (files prefixed with "|")
}

CONFIG_DIR = os.path.expanduser("~") + "/camilladsp/configs/"
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

SERIAL_PORT = "/dev/serial0"
BAUD_RATE = 115200

# Ambient light sensor (ADC) -> display brightness
ADC_LEVELS = [80, 1000, 2500, 3500]
BRIGHTNESS_LEVELS = [0, 1, 2, 3, 7]
HYSTERESIS_RATIO = 0.03  # 3 % hysteresis between two brightness levels

# Key hold durations, in key-repeat events
HOLD_TOGGLE_POWER = 2
HOLD_STOP = 10
HOLD_SWITCH_TONE_TILT = 15
HOLD_SHUTDOWN = 400

SCREEN_RETURN_DELAY = 30    # Seconds without key press before returning to the volume screen

CONFIG_GROUPS = ("_", "|")  # Config file prefixes: "_" = stereo (BACK key), "|" = TV (HOMEPAGE key)

DISPLAY_BLANK_DELAY = POWER_OFF_DELAY * 60 + 5  # Seconds of silence before the display is blanked

TILT_DB_STEP = 2            # Real dB applied per displayed tilt step
DEFAULT_LOUDNESS_REF = -30

# settings.json key -> (CamillaDSP filter, parameter)
TONE_PARAMS = {
    "bass_gain":     ("Bass", "gain"),
    "treble_gain":   ("Treble", "gain"),
    "tilt_gain":     ("Tilt", "gain"),
    "loudness_ref":  ("Loudness", "reference_level"),
    "presence_gain": ("Presence", "gain"),
}


# ====================== GLOBAL VARIABLES ======================

key_hold_counter = 0            # Key-repeat events of the key being held (remote_events only)
screen_idle_ticks = 0           # Seconds since the last key event (display_manager_loop)
auto_power_enabled = True
is_waiting_for_sound = False
is_key_held = False
is_volume_key_held = False
last_displayed = None
blank_volume_when_mute = False
enter_display_at_press = None   # Screen shown when ENTER was pressed (to tell short/long press apart)

bass_gain_prev = treble_gain_prev = tilt_gain_prev = presence_gain_prev = 0
loudness_gain_prev = DEFAULT_LOUDNESS_REF
current_config_key = None       # Settings key of the active config file

adc_buffer = b""                # Incomplete serial data waiting for its newline

display_refresh_event = asyncio.Event()


# ====================== HELPERS ======================

def clamp(value, low, high):
    return max(low, min(high, value))


def swap(segs):
    """Reorder TM1637 segments to match the physical wiring of the display."""
    length = len(segs)
    if length == 4 or length == 5:
        segs.extend(bytearray([0] * (6 - length)))
    segs[0], segs[2] = segs[2], segs[0]
    if length >= 4:
        segs[3], segs[5] = segs[5], segs[3]
    return segs


def show(text):
    """Write a 6-character text on the display."""
    tm.write(swap(tm.encode_string(text)))


def clear_display():
    tm.write(tm.encode_string(" " * 6))


def is_silent():
    return "-1000.0" in str(cdsp.levels.capture_rms())


def shutdown_system():
    """Cut the power relay and halt the Raspberry Pi."""
    lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
    show(" HALT ")
    os.system("sudo shutdown -h now")


def adc_to_brightness(adc_value):
    """Convert an ADC reading to a display brightness, with hysteresis."""
    global last_level

    for i, threshold in enumerate(ADC_LEVELS):
        if adc_value < threshold:
            target_level = i
            break
    else:
        target_level = len(BRIGHTNESS_LEVELS) - 1

    if target_level > last_level:
        if adc_value > ADC_LEVELS[last_level] * (1 + HYSTERESIS_RATIO):
            last_level = target_level
    elif target_level < last_level:
        if adc_value < ADC_LEVELS[target_level] * (1 - HYSTERESIS_RATIO):
            last_level = target_level

    return BRIGHTNESS_LEVELS[last_level]


# ====================== INITIALISATION ======================

# Ambient light sensor: initial brightness level
ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
try:
    adc_value_init = int(ser.readline().decode().strip())
except ValueError:
    adc_value_init = 1500   # fallback if the sensor is absent
ser.timeout = 0   # non-blocking reads from here on

last_level = next(
    (i for i, t in enumerate(ADC_LEVELS) if adc_value_init < t),
    len(BRIGHTNESS_LEVELS) - 1,
)


def find_remote():
    """Return the remote input device, or None if it is not present."""
    for path in evdev.list_devices():
        device = evdev.InputDevice(path)
        if device.name == REMOTE_NAME:
            log.info("'%s' found at %s.", REMOTE_NAME, path)
            device.grab()
            return device
    return None


remote = None
for attempt in range(HALT_DELAY * 3600):
    remote = find_remote()
    if remote:
        break
    log.info("'%s' not found. Retrying...", REMOTE_NAME)
    time.sleep(1)
if remote is None:
    log.info("'%s' not found after %s hours. Shutting down...", REMOTE_NAME, HALT_DELAY)
    os.system("sudo shutdown -h now")
    sys.exit(0)

# CamillaDSP
cdsp = CamillaClient("127.0.0.1", 1234)
cdsp.connect()
config_active = cdsp.config.active()

# GPIO
h = lgpio.gpiochip_open(0)
lgpio.gpio_claim_output(h, POWER_GPIO)
lgpio.gpio_write(h, POWER_GPIO, 1)
lgpio.gpio_claim_input(h, TV_GPIO, lgpio.SET_PULL_UP)

# TM1637 display
tm = TM1637(clk=CLK, dio=DIO)
clear_display()
tm.brightness(BRIGHTNESS_LEVELS[last_level])


# ====================== FILTER / SETTINGS ACCESS ======================

def apply_filter_param(filters, filt, param, value):
    """Set a CamillaDSP filter parameter, silently ignoring filters that are
    missing from the active config (configs need not define every filter)."""
    f = filters.get(filt)
    if not isinstance(f, dict):
        return
    params = f.get("parameters")
    if not isinstance(params, dict):
        return
    params[param] = value


def get_filter_param(config, filt, param):
    """Return a filter parameter, or None if the filter/parameter is missing."""
    f = (config.get("filters") or {}).get(filt)
    params = f.get("parameters") if isinstance(f, dict) else None
    return params.get(param) if isinstance(params, dict) else None


def get_filter_params(config, filt):
    """Return the 'parameters' dict of a filter, or None if the filter is missing."""
    f = (config.get("filters") or {}).get(filt)
    params = f.get("parameters") if isinstance(f, dict) else None
    return params if isinstance(params, dict) else None


def get_bass_treble(config, mode="gain"):
    """Return (bass, treble, tilt, loudness) gains or filter parameter dicts."""
    try:
        filters = config.get('filters', {})
        bass = filters['Bass']['parameters']
        treble = filters['Treble']['parameters']
        tilt = filters['Tilt']['parameters']
        loudness = filters['Loudness']['parameters']

        if mode == "gain":
            return (bass.get('gain', 0), treble.get('gain', 0),
                    tilt.get('gain', 0), loudness.get('reference_level', 0))
        elif mode == "parameters":
            return bass, treble, tilt, loudness
        raise ValueError("Invalid mode: use 'gain' or 'parameters'")

    except (KeyError, TypeError):
        return (0, 0, 0, 0) if mode == "gain" else (None, None, None, None)


def get_presence_tilt(config, mode="gain"):
    """Return the (Presence, Tilt) gains or filter parameter dicts."""
    try:
        filters = config.get('filters', {})
        presence = filters['Presence']['parameters']
        tilt = filters['Tilt']['parameters']

        if mode == "gain":
            return presence.get('gain', 0), tilt.get('gain', 0)
        elif mode == "parameters":
            return presence, tilt
        raise ValueError("Invalid mode: use 'gain' or 'parameters'")

    except (KeyError, TypeError):
        return (0, 0) if mode == "gain" else (None, None)


def config_key(path):
    """Settings key of a config file: its name without directory or extension."""
    return os.path.splitext(os.path.basename(path))[0]


def get_tone(config):
    """Current tone values of a config (only for filters that really exist)."""
    tone = {}
    for key, (filt, param) in TONE_PARAMS.items():
        value = get_filter_param(config, filt, param)
        if value is not None:
            tone[key] = value
    return tone


def apply_tone(filters, tone):
    """Apply a saved tone dict to the filters of a config."""
    for key, (filt, param) in TONE_PARAMS.items():
        if key in tone:
            apply_filter_param(filters, filt, param, tone[key])


def load_settings():
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    configs = data.get("configs")
    last_config = data.get("last_config")
    return {
        "configs": configs if isinstance(configs, dict) else {},
        "last_config": last_config if isinstance(last_config, dict) else {},
        "last_tone_tilt": data.get("last_tone_tilt", "tone"),
    }


def write_settings():
    """Write settings.json atomically (no half-written file on power loss)."""
    tmp_path = SETTINGS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(settings, f, indent=4, ensure_ascii=False)
    os.replace(tmp_path, SETTINGS_PATH)


def save_audio_settings(config):
    """Store the tone of the active config, then write settings.json."""
    if current_config_key:
        entry = get_tone(config)
        entry["prev"] = {
            "bass_gain": bass_gain_prev,
            "treble_gain": treble_gain_prev,
            "tilt_gain": tilt_gain_prev,
            "loudness_ref": loudness_gain_prev,
            "presence_gain": presence_gain_prev,
        }
        settings["configs"][current_config_key] = entry
    settings["last_tone_tilt"] = last_tone_tilt
    write_settings()


def reset_prev_values():
    """Forget the ENTER-key restore values (they belong to the previous config)."""
    global bass_gain_prev, treble_gain_prev, tilt_gain_prev
    global loudness_gain_prev, presence_gain_prev
    bass_gain_prev = treble_gain_prev = tilt_gain_prev = presence_gain_prev = 0
    loudness_gain_prev = DEFAULT_LOUDNESS_REF


def load_prev_values(saved):
    """Load the ENTER-key restore values saved for a config (defaults if none)."""
    global bass_gain_prev, treble_gain_prev, tilt_gain_prev
    global loudness_gain_prev, presence_gain_prev
    reset_prev_values()
    prev = saved.get("prev") if saved else None
    if isinstance(prev, dict):
        bass_gain_prev = prev.get("bass_gain", 0)
        treble_gain_prev = prev.get("treble_gain", 0)
        tilt_gain_prev = prev.get("tilt_gain", 0)
        loudness_gain_prev = prev.get("loudness_ref", DEFAULT_LOUDNESS_REF)
        presence_gain_prev = prev.get("presence_gain", 0)


def init_tone():
    """Apply the saved tone of the config CamillaDSP started with."""
    global current_config_key
    path = cdsp.config.file_path()
    if not path:
        return
    current_config_key = config_key(path)
    saved = settings["configs"].get(current_config_key)
    load_prev_values(saved)
    if saved:
        filters = config_active.get("filters")
        apply_tone(filters if isinstance(filters, dict) else {}, saved)
        cdsp.config.set_active(config_active)


settings = load_settings()
last_tone_tilt = settings["last_tone_tilt"]
reset_prev_values()
init_tone()


# ====================== DISPLAY ======================

def display_volume_info(current_volume=None, is_muted=None):
    global blank_volume_when_mute, last_displayed

    if current_volume is None:
        current_volume = cdsp.volume.main_volume()
    if is_muted is None:
        is_muted = cdsp.volume.main_mute()

    config_path = (cdsp.config.file_path() or "").replace(CONFIG_DIR, '')
    config_path = config_path.split('.')[0].replace("_", "").replace("|", "")
    config_path = config_path[:3].ljust(3)

    display_vol = max(-99, round(current_volume))
    blanks = '  -' if display_vol == 0 else (' --', '---')[min(len(str(abs(display_vol))), 2) - 1]

    if is_muted and not is_volume_key_held:
        blank_volume_when_mute, volume_str = True, f"{config_path}{blanks}"
    else:
        blank_volume_when_mute, volume_str = False, f"{config_path}{display_vol:3}"

    segs = tm.encode_string(volume_str)

    bass_gain, treble_gain, tilt_gain, _ = get_bass_treble(config_active, mode="gain")
    presence_gain, _ = get_presence_tilt(config_active, mode="gain")
    if bass_gain or treble_gain or tilt_gain or presence_gain:
        segs[-4] |= 0x80   # decimal point on the 3rd digit (before swap): tone settings active

    tm.write(swap(segs))
    last_displayed = "volume"


def display_tone_info():
    global last_displayed
    bass_gain, treble_gain, _, _ = get_bass_treble(config_active, mode="gain")
    show(f"{round(bass_gain):2}B{round(treble_gain):2}T")
    last_displayed = "tone"


def display_loudness_info():
    global last_displayed
    _, _, _, loudness_ref = get_bass_treble(config_active, mode="gain")
    loudness_ref = round(loudness_ref)
    value_str = "---" if loudness_ref == -99 else f"{loudness_ref}"
    show(f"{value_str} Ld")
    last_displayed = "loudness"


def display_tilt_info():
    global last_displayed
    presence_gain, tilt_gain = get_presence_tilt(config_active, mode="gain")
    tilt_step = round(tilt_gain / TILT_DB_STEP)  # displayed -3..+3, actual gain = step * TILT_DB_STEP dB
    show(f"{tilt_step:2}T{round(presence_gain):2}P")
    last_displayed = "tilt"


# ====================== REMOTE KEY ACTIONS ======================

def change_volume_from_key(key, current_volume, volume_step=1):
    volume_change = -volume_step if key == KEY_BINDINGS['VOLUMEDOWN'] else volume_step
    new_volume = clamp(current_volume + volume_change, -99, 0)

    if new_volume != current_volume:
        cdsp.volume.set_main_volume(new_volume)
        display_volume_info(new_volume)


def get_repeat_speed(volume, direction, exponent=2.0, pivot=-50):
    """Delay between two volume steps while a key is held."""
    min_delay = 0.01
    max_delay = 0.5

    if direction == "down":
        return min_delay
    if direction != "up":
        raise ValueError("direction must be 'up' or 'down'")

    volume = clamp(volume, -99, 0)
    t = (volume - pivot) / (0 - pivot) if volume >= pivot else 0
    return min_delay + (t ** exponent) * (max_delay - min_delay)


def handle_arrow_keys(key):
    """Adjust the parameters of the screen currently displayed."""
    up, down = KEY_BINDINGS['UP'], KEY_BINDINGS['DOWN']
    left, right = KEY_BINDINGS['LEFT'], KEY_BINDINGS['RIGHT']

    if last_displayed == "loudness":
        _, _, _, loudness_params = get_bass_treble(config_active, mode="parameters")
        if loudness_params is not None:
            ref_level = loudness_params.get('reference_level', 0)
            if key in (up, right):
                ref_level += 5
            elif key in (down, left):
                ref_level -= 5
            loudness_params['reference_level'] = clamp(ref_level, -50, -10)
            cdsp.config.set_active(config_active)
            display_loudness_info()

    elif last_displayed == "tilt":
        presence_params, tilt_params = get_presence_tilt(config_active, mode="parameters")
        if presence_params is not None and tilt_params is not None:
            presence_gain = presence_params.get('gain', 0)
            tilt_gain = tilt_params.get('gain', 0)
            if key == up:
                presence_gain += 1
            elif key == down:
                presence_gain -= 1
            elif key == right:
                tilt_gain += TILT_DB_STEP
            elif key == left:
                tilt_gain -= TILT_DB_STEP
            presence_params['gain'] = clamp(presence_gain, -3, 3)
            tilt_params['gain'] = clamp(tilt_gain, -3 * TILT_DB_STEP, 3 * TILT_DB_STEP)
            cdsp.config.set_active(config_active)
            display_tilt_info()

    elif last_displayed == "tone":
        bass_params, treble_params, _, _ = get_bass_treble(config_active, mode="parameters")
        if bass_params is not None and treble_params is not None:
            bass_gain = bass_params.get('gain', 0)
            treble_gain = treble_params.get('gain', 0)
            if key == up:
                treble_gain += 1
            elif key == down:
                treble_gain -= 1
            elif key == right:
                bass_gain += 1
            elif key == left:
                bass_gain -= 1
            bass_params['gain'] = clamp(bass_gain, -9, 9)
            treble_params['gain'] = clamp(treble_gain, -9, 9)
            cdsp.config.set_active(config_active)
            display_tone_info()

    else:
        if last_tone_tilt == "tilt":
            display_tilt_info()
        else:
            display_tone_info()


def handle_enter_press(screen):
    """Short ENTER press.
    - tone / tilt screen: bass, treble, tilt and presence are ALL set to 0,
      or ALL restored to their previous values if they are already all at 0.
    - loudness screen: loudness is switched off (-99) or restored."""
    global bass_gain_prev, treble_gain_prev, tilt_gain_prev, loudness_gain_prev, presence_gain_prev

    if screen in ("tone", "tilt"):
        bass = get_filter_params(config_active, "Bass")
        treble = get_filter_params(config_active, "Treble")
        tilt = get_filter_params(config_active, "Tilt")
        presence = get_filter_params(config_active, "Presence")

        existing = [p for p in (bass, treble, tilt, presence) if p is not None]
        if not existing:
            return

        if all(p.get('gain', 0) == 0 for p in existing):
            # Everything is at 0: restore the previous values
            if bass:
                bass['gain'] = bass_gain_prev
            if treble:
                treble['gain'] = treble_gain_prev
            if tilt:
                tilt['gain'] = tilt_gain_prev
            if presence:
                presence['gain'] = presence_gain_prev
            bass_gain_prev = treble_gain_prev = tilt_gain_prev = presence_gain_prev = 0
        else:
            # Remember the current values, then set everything to 0
            bass_gain_prev = bass.get('gain', 0) if bass else 0
            treble_gain_prev = treble.get('gain', 0) if treble else 0
            tilt_gain_prev = tilt.get('gain', 0) if tilt else 0
            presence_gain_prev = presence.get('gain', 0) if presence else 0
            for p in existing:
                p['gain'] = 0

        cdsp.config.set_active(config_active)
        if screen == "tone":
            display_tone_info()
        else:
            display_tilt_info()

    elif screen == "loudness":
        loudness_params = get_filter_params(config_active, "Loudness")
        if loudness_params is not None:
            if loudness_params.get('reference_level') == -99:
                loudness_params['reference_level'] = loudness_gain_prev
                loudness_gain_prev = -99
            else:
                loudness_gain_prev = loudness_params.get('reference_level', DEFAULT_LOUDNESS_REF)
                loudness_params['reference_level'] = -99
            cdsp.config.set_active(config_active)
            display_loudness_info()


def send_lms_command(command):
    """Send a command to Logitech Media Server, identifying the player by the
    MAC address of the default network interface."""
    try:
        with os.popen("ip route show default") as route_info:
            default_iface = next((line.split()[4] for line in route_info if "default" in line), None)
        if not default_iface:
            raise ValueError("No default network interface found")

        with open(f"/sys/class/net/{default_iface}/address") as f:
            mac = f.read().strip().replace(":", "%3A").lower()

        with socket.create_connection((socket.gethostname(), 9090), timeout=5) as sock:
            sock.sendall(f"{mac} {command}\r\nexit\r\n".encode("utf-8"))
            return sock.recv(4096).decode("utf-8").strip()

    except Exception as e:
        log.error("LMS command failed: %s", e)
        return None


# ====================== DSP CONFIGURATION ======================

async def change_config(cdsp, config_pattern, cycle=True):
    """Switch config within the group of files matching the pattern.
    cycle=True : go to the next file of the group if the active config belongs to it.
    Otherwise (or if cycle=False): go to the last config used in this group.
    Each config keeps its own tone settings (bass, treble, tilt, loudness, presence)."""
    global config_active, current_config_key

    group = os.path.basename(config_pattern)[:1]
    config_files = sorted(glob.glob(config_pattern))
    if not config_files:
        return
    current_config = cdsp.config.file_path()
    if cycle and current_config in config_files:
        index = config_files.index(current_config)
        new_config_path = config_files[(index + 1) % len(config_files)]
    else:
        last_key = settings["last_config"].get(group)
        new_config_path = next(
            (p for p in config_files if config_key(p) == last_key),
            config_files[0],
        )
    if new_config_path == current_config:
        return

    # Remember the tone of the config we are leaving (and write it to disk)
    save_audio_settings(config_active)

    new_config_data = cdsp.config.read_and_parse_file(new_config_path)
    filters = new_config_data.get('filters')
    if not isinstance(filters, dict):
        filters = {}
        new_config_data['filters'] = filters

    # Saved tone for this config; if none, the values from the .yml are kept
    new_key = config_key(new_config_path)
    saved = settings["configs"].get(new_key)
    if saved:
        apply_tone(filters, saved)

    # Make relative filter file names absolute
    config_dir = os.path.dirname(os.path.abspath(new_config_path))
    for f in filters.values():
        filename = f.get("parameters", {}).get("filename")
        if isinstance(filename, str) and filename and not os.path.isabs(filename):
            f["parameters"]["filename"] = os.path.normpath(os.path.join(config_dir, filename))

    cdsp.config.set_active(new_config_data)
    cdsp.config.set_file_path(new_config_path)
    config_active = new_config_data
    current_config_key = new_key
    load_prev_values(saved)

    # Remember this config as the last used of its group (survives TV on/off and restarts)
    new_group = os.path.basename(new_config_path)[:1]
    if new_group in CONFIG_GROUPS:
        settings["last_config"][new_group] = new_key
        write_settings()

    display_volume_info()


# ====================== POWER ======================

def toggle_power():
    global auto_power_enabled, is_waiting_for_sound, last_displayed

    if not auto_power_enabled:
        # Relay was forced off: switch it on and re-enable auto power-off
        auto_power_enabled = True
        lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)
        show("PW ON")
        is_waiting_for_sound = False

    elif is_waiting_for_sound:
        # Relay was switched off by auto power-off: switch it back on
        lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)
        show("PW ON")
        is_waiting_for_sound = False

    else:
        # Force the relay off and disable auto power
        auto_power_enabled = False
        lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
        show("PW OFF")

    last_displayed = "power"


# ====================== BACKGROUND TASKS ======================

async def adc_reader_loop():
    """Read the ambient light sensor without ever blocking the event loop.
    Only the most recent complete line received since the last pass is used."""
    global adc_buffer
    last_brightness = -1

    while True:
        if ser.in_waiting:
            adc_buffer += ser.read(ser.in_waiting)
            # Everything before the last \n is complete; the rest waits for more data
            *lines, adc_buffer = adc_buffer.split(b"\n")
            line = next(
                (l.decode('utf-8', errors='ignore').strip()
                 for l in reversed(lines) if l.strip()),
                None,
            )
            if line:
                try:
                    adc_value = int(line)
                    brightness = adc_to_brightness(adc_value)
                    if brightness != last_brightness:
                        tm.brightness(brightness)
                        last_brightness = brightness
                        log.info("ADC = %s -> Brightness = %s", adc_value, brightness)
                except ValueError:
                    log.warning("Non-numeric input received: %s", line)

        await asyncio.sleep(0.1)


async def display_manager_loop():
    global screen_idle_ticks, is_key_held, is_volume_key_held
    idle_display_counter = 0

    while True:
        if is_volume_key_held:
            is_volume_key_held = False
            await asyncio.sleep(0.5)
            if last_displayed == "volume":
                display_volume_info()

        # Return to the volume screen after SCREEN_RETURN_DELAY seconds without key event
        if last_displayed != "volume":
            screen_idle_ticks += 1
            if screen_idle_ticks >= SCREEN_RETURN_DELAY:
                screen_idle_ticks = 0
                display_volume_info()
        else:
            screen_idle_ticks = 0

        if is_key_held:
            idle_display_counter = 0
            is_key_held = False

        # Blank the display after a long silence
        if is_silent():
            idle_display_counter += 1
            if idle_display_counter == DISPLAY_BLANK_DELAY:
                idle_display_counter = 0
                clear_display()
        else:
            idle_display_counter = 0

        try:
            await asyncio.wait_for(display_refresh_event.wait(), timeout=1.0)
            display_refresh_event.clear()
        except asyncio.TimeoutError:
            pass


async def auto_poweroff():
    """Switch the power relay off after POWER_OFF_DELAY minutes of silence, back on
    when sound returns, and halt the Raspberry Pi after HALT_DELAY hours of silence."""
    global is_waiting_for_sound
    silence_counter = 0
    last_power_relay_state = lgpio.gpio_read(h, POWER_GPIO)

    while True:
        await asyncio.sleep(1)

        if is_silent():
            silence_counter += 1
            if silence_counter == POWER_OFF_DELAY * 60 and not is_waiting_for_sound:
                lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
                show("PW OFF")
                last_power_relay_state = lgpio.gpio_read(h, POWER_GPIO)
                is_waiting_for_sound = True  # Wait for sound or POWER key to switch back on

            if silence_counter >= HALT_DELAY * 3600:
                shutdown_system()
                break

        else:
            silence_counter = 0
            if auto_power_enabled and last_power_relay_state == 0:
                if is_waiting_for_sound:
                    lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)
                    display_volume_info()
                    last_power_relay_state = lgpio.gpio_read(h, POWER_GPIO)
                    is_waiting_for_sound = False
            elif not auto_power_enabled and last_power_relay_state == 1:
                lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
                last_power_relay_state = lgpio.gpio_read(h, POWER_GPIO)


async def monitor_tv_gpio():
    """Watch the TV GPIO; a state change is validated once it stays stable for 1 second."""
    last_state = lgpio.gpio_read(h, TV_GPIO)

    while True:
        current_state = lgpio.gpio_read(h, TV_GPIO)

        if current_state != last_state:
            await asyncio.sleep(1.0)
            if lgpio.gpio_read(h, TV_GPIO) == current_state:
                pattern = '|*' if current_state == 0 else '_*'  # 0 = TV on
                await change_config(cdsp, CONFIG_DIR + pattern, cycle=False)
                last_state = current_state

        await asyncio.sleep(0.05)


async def remote_events(device):
    global last_displayed, key_hold_counter, screen_idle_ticks, is_key_held, is_volume_key_held
    global last_tone_tilt, enter_display_at_press

    last_repeat_time = 0
    while True:
        try:
            async for event in device.async_read_loop():
                if event.type != evdev.ecodes.EV_KEY:
                    continue

                screen_idle_ticks = 0  # Any key event restarts the return-to-volume timer

                attrib = evdev.categorize(event)
                key = attrib.keycode
                volume_keys = (KEY_BINDINGS['VOLUMEDOWN'], KEY_BINDINGS['VOLUMEUP'])

                if attrib.keystate == 1:  # Key pressed
                    is_key_held = True
                    key_hold_counter = 0

                    if key in volume_keys:
                        is_volume_key_held = True
                        current_volume = cdsp.volume.main_volume()
                        is_muted = cdsp.volume.main_mute()

                        if last_displayed != "volume" or blank_volume_when_mute:
                            display_volume_info(current_volume, is_muted)
                        else:
                            change_volume_from_key(key, current_volume)

                    # `in`, not `==`: evdev returns a list when a code has several names
                    elif KEY_BINDINGS['MUTE'] in key:
                        is_muted = not cdsp.volume.main_mute()
                        cdsp.volume.set_main_mute(is_muted)
                        display_volume_info(cdsp.volume.main_volume(), is_muted)

                    elif key == KEY_BINDINGS['PREVIOUSSONG']:
                        send_lms_command("playlist index -1")

                    elif key == KEY_BINDINGS['NEXTSONG']:
                        send_lms_command("playlist index +1")

                    elif key in (KEY_BINDINGS['UP'], KEY_BINDINGS['DOWN'],
                                 KEY_BINDINGS['RIGHT'], KEY_BINDINGS['LEFT']):
                        handle_arrow_keys(key)

                    elif key == KEY_BINDINGS['BACK']:
                        await change_config(cdsp, CONFIG_DIR + '_*')

                    elif key == KEY_BINDINGS['HOMEPAGE']:
                        await change_config(cdsp, CONFIG_DIR + '|*')

                    elif key == KEY_BINDINGS['ENTER']:
                        # Remember the screen shown BEFORE any change, for the action on release
                        enter_display_at_press = last_displayed
                        if last_displayed == "volume":
                            display_loudness_info()

                elif attrib.keystate == 2:  # Key held down

                    if key in volume_keys:
                        is_volume_key_held = True
                        current_time = time.time()
                        current_volume = cdsp.volume.main_volume()

                        if key == KEY_BINDINGS['VOLUMEUP']:
                            direction, pivot = "up", -60
                        else:
                            direction, pivot = "down", -40

                        repeat_speed = get_repeat_speed(current_volume, direction, exponent=2.0, pivot=pivot)
                        if current_time - last_repeat_time >= repeat_speed:
                            change_volume_from_key(key, current_volume)
                            last_repeat_time = current_time

                    elif key == KEY_BINDINGS['PLAYPAUSE']:
                        key_hold_counter += 1
                        if key_hold_counter == HOLD_STOP:
                            send_lms_command("stop")

                    elif key == KEY_BINDINGS['POWER']:
                        key_hold_counter += 1
                        if key_hold_counter == HOLD_TOGGLE_POWER:
                            toggle_power()
                        elif key_hold_counter == HOLD_SHUTDOWN:
                            save_audio_settings(config_active)
                            shutdown_system()

                    elif key == KEY_BINDINGS['ENTER']:
                        key_hold_counter += 1
                        if key_hold_counter == HOLD_SWITCH_TONE_TILT and last_displayed in ("tone", "tilt"):
                            last_tone_tilt = "tilt" if last_displayed == "tone" else "tone"
                            (display_tilt_info if last_tone_tilt == "tilt" else display_tone_info)()

                elif attrib.keystate == 0:  # Key released

                    if key == KEY_BINDINGS['ENTER'] and key_hold_counter < HOLD_SWITCH_TONE_TILT:
                        handle_enter_press(enter_display_at_press)

                    elif key == KEY_BINDINGS['PLAYPAUSE'] and key_hold_counter < HOLD_STOP:
                        send_lms_command("pause")

                    elif key in volume_keys:
                        display_refresh_event.set()

                    key_hold_counter = 0
                    last_repeat_time = 0

        except OSError as e:
            log.warning("Remote disconnected: %s. Attempting to reconnect...", e)
            device = None
            while device is None:
                device = find_remote()
                if device is None:
                    await asyncio.sleep(1)
            log.info("Remote reconnected.")


# ====================== SHUTDOWN / MAIN ======================

def exit_gracefully(signum, frame):
    """Save the settings, cut the relay and release the GPIO on SIGINT/SIGTERM."""
    try:
        save_audio_settings(config_active)
        lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
        time.sleep(0.5)
        lgpio.gpiochip_close(h)
        log.info("Gracefully shutting down the service...")
        sys.exit(0)
    except Exception as e:
        log.error("Error during cleanup: %s", e)
        sys.exit(1)


signal.signal(signal.SIGINT, exit_gracefully)
signal.signal(signal.SIGTERM, exit_gracefully)


async def main():
    # Load the config matching the TV state at startup
    if lgpio.gpio_read(h, TV_GPIO) == 0:
        log.info("TV detected ON at startup: loading ON config")
        await change_config(cdsp, CONFIG_DIR + '|*', cycle=False)
    else:
        await change_config(cdsp, CONFIG_DIR + '_*', cycle=False)
    display_volume_info()  # change_config returns early when the config is already active

    tasks = [
        asyncio.create_task(auto_poweroff()),
        asyncio.create_task(display_manager_loop()),
        asyncio.create_task(adc_reader_loop()),
        asyncio.create_task(monitor_tv_gpio()),
        asyncio.create_task(remote_events(remote)),
    ]
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
