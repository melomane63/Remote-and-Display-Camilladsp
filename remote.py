import asyncio
import functools
import glob
import json
import logging
import math
import os
import re
import select
import signal
import socket
import struct
import sys
import threading
import time
from urllib.parse import quote, unquote
from camilladsp import CamillaClient


# ========================= FEATURES =========================
# Each block can be switched off. A disabled block is never started, and the
# hardware libraries it needs are neither imported nor required.

ENABLE_LMS_SYNC      = True   # LMS <-> CamillaDSP volume bridge (own thread)
ENABLE_AUTO_POWEROFF = True   # Power relay: off after silence, on with sound,
                              # halt after HALT_DELAY, relay toggle on POWER long press
ENABLE_REMOTE        = True   # Remote control (all keys: volume, mute, LMS, BACK,
                              # HOMEPAGE, POWER). If False, no key works at all.
ENABLE_DISPLAY       = True   # TM1637 display + ambient light sensor
                              # (the tone / tilt / loudness keys need it)
ENABLE_CONFIG_SWITCH = "gpio" # Auto config switching: False | "gpio" | "toslink"
                              #   False     : disabled
                              #   "gpio"    : TV GPIO (original logic)
                              #   "toslink" : audio detection on the TOSLINK input
                              # (BACK / HOMEPAGE keys do NOT depend on this)

if ENABLE_CONFIG_SWITCH not in (False, "gpio", "toslink"):
    raise ValueError(f"Invalid ENABLE_CONFIG_SWITCH: {ENABLE_CONFIG_SWITCH!r}")

# Derived from the switches above (do not edit)
SWITCH_GPIO    = ENABLE_CONFIG_SWITCH == "gpio"
SWITCH_TOSLINK = ENABLE_CONFIG_SWITCH == "toslink"
NEED_CDSP    = ENABLE_DISPLAY or ENABLE_REMOTE or ENABLE_AUTO_POWEROFF or bool(ENABLE_CONFIG_SWITCH)
TONE_ACTIVE  = ENABLE_REMOTE or bool(ENABLE_CONFIG_SWITCH)   # tone is saved / restored per config
USE_GPIO     = ENABLE_AUTO_POWEROFF or SWITCH_GPIO           # TOSLINK detection needs no GPIO


if ENABLE_REMOTE:
    import evdev
if ENABLE_DISPLAY:
    import serial
    from tm1637_lgpio import TM1637
if USE_GPIO:
    import lgpio
if SWITCH_TOSLINK:
    import alsaaudio


logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("remote")


# ========================= CONSTANTS =========================

REMOTE_NAME = "HID Remote01 Keyboard"   # Found with `python3 -m evdev.evtest`
POWER_OFF_DELAY = 2                     # Minutes of silence before the power relay is switched off
HALT_DELAY = 48                         # Hours of silence (or without remote) before the Raspberry Pi shuts down
POWER_GPIO = 4                          # GPIO controlling the power relay
TV_GPIO = 12                            # GPIO reading the TV state (0 = TV on)
CLK, DIO = 23, 24                       # TM1637 display pins

# Remote control keys (all require ENABLE_REMOTE = True)
KEY_BINDINGS = {
    'VOLUMEDOWN':    'KEY_VOLUMEDOWN',   # Decrease volume
    'VOLUMEUP':      'KEY_VOLUMEUP',     # Increase volume
    'MUTE':          'KEY_MUTE',         # Mute / unmute audio
    'PLAYPAUSE':     'KEY_PLAYPAUSE',    # Pause (short press) / stop (long press) - LMS
    'PREVIOUSSONG':  'KEY_PREVIOUSSONG', # Previous song (short press) / rewind (long press) - LMS
    'NEXTSONG':      'KEY_NEXTSONG',     # Next song (short press) / fast-forward (long press) - LMS
    'UP':            'KEY_UP',           # Presence gain + / treble + (needs ENABLE_DISPLAY)
    'DOWN':          'KEY_DOWN',         # Presence gain - / treble -        "
    'LEFT':          'KEY_LEFT',         # Tilt gain - / bass -              "
    'RIGHT':         'KEY_RIGHT',        # Tilt gain + / bass +              "
    'POWER':         'KEY_POWER',        # Long press: toggle relay (needs ENABLE_AUTO_POWEROFF)
                                         # Very long press: shutdown (always)
    'ENTER':         'KEY_ENTER',        # Short: bass/treble/tilt/presence all 0 or restored
                                         #        (loudness screen: loudness on/off)
                                         # Long:  switch tone / tilt screen (needs ENABLE_DISPLAY)
    'BACK':          'KEY_BACK',         # Next DSP config (files prefixed with "_")
    'HOMEPAGE':      'KEY_HOMEPAGE',     # Next DSP config (files prefixed with "|")
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
HOLD_SEEK = 10              # Long press on NEXT/PREVIOUS: key-repeat events before seeking starts
SEEK_STEP_S = 10            # Seconds jumped at each seek
SEEK_INTERVAL_S = 0.5       # Seconds between two seeks while the key is held (10 s / 0.5 s = 20x speed)
HOLD_SWITCH_TONE_TILT = 15
HOLD_SHUTDOWN = 300

SCREEN_RETURN_DELAY = 30    # Seconds without key press before returning to the volume screen

CONFIG_GROUPS = ("_", "|")  # Config file prefixes: "_" = stereo (BACK key), "|" = TV (HOMEPAGE key)

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


# TOSLINK detection (ENABLE_CONFIG_SWITCH = "toslink")
# NOTE: device / rate / channels are read at startup from the first "|*" YAML.
TOSLINK_TV_ON_DB = -60.0         # TOSLINK free: level >= threshold -> TV
TOSLINK_SILENCE_DELAY_S = 120    # Seconds of silence before going back to SQUEEZELITE


# LMS <-> CamillaDSP volume sync
CAMILLA_ADDR = ("127.0.0.1", 1234)
LMS_ADDR = ("127.0.0.1", 9090)           # LMS CLI (the server runs on this Pi)
LMS_PLAYER_MANUAL = ""                   # "" = auto (MAC of the default interface); or e.g. "dc:a6:32:3c:1c:21" (raw, lowercase)
LMS_SYNC_DEBUG = False                   # Log every volume/mute change and the raw LMS notifications


# ====================== VOLUME CURVE ======================
# Volume curve: level 0..100 (LMS scale) <-> dB (CamillaDSP).
#
# Base: gain sent by LMS to Squeezelite (dB = 20 * log10(gain / 65536)),
# rounded to a 0.5 dB grid:
#     2 ..  25 : ~1.5 dB / level  (-71 .. -37.5 dB)
#    25 .. 100 :  0.5 dB / level  (-37.5 .. 0 dB), exactly
# 0 equals -100 dB (the real mute is handled separately).
# The curve is strictly increasing, which makes the inverse conversion exact.
# Deviation from the original measurement: < 0.5 dB (0.7 dB worst case, level 44).

CURVE_POINTS = [
    (0, -100.0), (1, -75.0), (2, -71.0), (3, -69.5), (4, -68.0),
    (5, -66.5), (6, -65.0), (7, -63.5), (8, -62.0), (9, -60.5),
    (10, -59.0), (11, -57.5), (12, -56.0), (13, -55.0), (14, -53.5),
    (15, -52.0), (16, -50.5), (17, -49.0), (18, -47.5), (19, -46.0),
    (20, -44.5), (21, -43.0), (22, -41.5), (23, -40.0), (24, -38.5),
    (25, -37.5), (26, -37.0), (27, -36.5), (28, -36.0), (29, -35.5),
    (30, -35.0), (31, -34.5), (32, -34.0), (33, -33.5), (34, -33.0),
    (35, -32.5), (36, -32.0), (37, -31.5), (38, -31.0), (39, -30.5),
    (40, -30.0), (41, -29.5), (42, -29.0), (43, -28.5), (44, -28.0),
    (45, -27.5), (46, -27.0), (47, -26.5), (48, -26.0), (49, -25.5),
    (50, -25.0), (51, -24.5), (52, -24.0), (53, -23.5), (54, -23.0),
    (55, -22.5), (56, -22.0), (57, -21.5), (58, -21.0), (59, -20.5),
    (60, -20.0), (61, -19.5), (62, -19.0), (63, -18.5), (64, -18.0),
    (65, -17.5), (66, -17.0), (67, -16.5), (68, -16.0), (69, -15.5),
    (70, -15.0), (71, -14.5), (72, -14.0), (73, -13.5), (74, -13.0),
    (75, -12.5), (76, -12.0), (77, -11.5), (78, -11.0), (79, -10.5),
    (80, -10.0), (81, -9.5), (82, -9.0), (83, -8.5), (84, -8.0),
    (85, -7.5), (86, -7.0), (87, -6.5), (88, -6.0), (89, -5.5),
    (90, -5.0), (91, -4.5), (92, -4.0), (93, -3.5), (94, -3.0),
    (95, -2.5), (96, -2.0), (97, -1.5), (98, -1.0), (99, -0.5),
    (100, 0.0),
]


def _interp(x, pts):
    if x <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x <= x1:
            return y0 + (x - x0) / (x1 - x0) * (y1 - y0)
    return pts[-1][1]


_INV = [(d, p) for p, d in CURVE_POINTS]


def pct_to_db(p):
    """Niveau 0..100 -> dB."""
    return _interp(p, CURVE_POINTS)


def db_to_pct(d):
    """dB -> niveau 0..100 (entier le plus proche)."""
    return round(_interp(d, _INV))


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

silent_now = False              # True when CamillaDSP reports silence (updated once per second by silence_monitor)

display_is_off = False          # True when the display has been blanked

display_refresh_event = asyncio.Event()


# ====================== HELPERS ======================

def clamp(value, low, high):
    return max(low, min(high, value))


class NullDisplay:
    """Stand-in for the TM1637 when ENABLE_DISPLAY is False: accepts every call
    and shows nothing, so the rest of the code does not have to care."""
    def encode_string(self, text):
        return bytearray(len(text))

    def write(self, segs):
        pass

    def brightness(self, level):
        pass


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


def blank_display():
    """Blank the display and remember it, so that a key or sound can wake it."""
    global display_is_off
    if not ENABLE_DISPLAY:
        return
    clear_display()
    display_is_off = True


def is_silent():
    levels = cdsp.levels.capture_rms()
    values = list(levels.values()) if isinstance(levels, dict) else list(levels)
    return bool(values) and all(float(v) <= -100.0 for v in values)

def shutdown_system():
    """Cut the power relay (if managed) and halt the Raspberry Pi."""
    if ENABLE_AUTO_POWEROFF:
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


@functools.lru_cache(maxsize=None)   # read once; a failure (exception) is not cached
def detect_player_mac():
    """MAC of the default network interface, raw format (dc:a6:32:3c:1c:21)."""
    with os.popen("ip route show default") as route_info:
        default_iface = next((line.split()[4] for line in route_info if "default" in line), None)
    if not default_iface:
        raise RuntimeError("No default network interface found")
    with open(f"/sys/class/net/{default_iface}/address") as f:
        return f.read().strip().lower()


def get_config_display_label():
    """Return the 3-character label shown on the display for the active config.
    Prefer the config `title` field; fall back to the config file name.
    Config file prefixes ("_" and "|") are stripped in both cases."""
    title = cdsp.config.title()
    if title:
        label = title.strip().replace("_", "").replace("|", "")
    else:
        label = "---"
    return label[:3].ljust(3)


# ====================== INITIALISATION ======================

# Ambient light sensor: initial brightness level
ser = None
adc_value_init = 1500   # fallback if the sensor is absent (or the display is disabled)
if ENABLE_DISPLAY:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
    try:
        adc_value_init = int(ser.readline().decode().strip())
    except ValueError:
        pass
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
if ENABLE_REMOTE:
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

# CamillaDSP (the LMS sync thread has its own connection)
cdsp = None
config_active = {}
if NEED_CDSP:
    cdsp = CamillaClient(*CAMILLA_ADDR)
    cdsp.connect()
    # Protection 1: never let config_active be None at startup
    config_active = cdsp.config.active() or {}

# GPIO
h = None
if USE_GPIO:
    h = lgpio.gpiochip_open(0)
    if ENABLE_AUTO_POWEROFF:
        lgpio.gpio_claim_output(h, POWER_GPIO, 0)   # Initial level LOW: no LOW -> HIGH glitch on the relay
        time.sleep(1)                   # Keep the relay off so the A70 can re-arm
        lgpio.gpio_write(h, POWER_GPIO, 1)
    if SWITCH_GPIO:
        lgpio.gpio_claim_input(h, TV_GPIO, lgpio.SET_PULL_UP)

# TM1637 display
tm = TM1637(clk=CLK, dio=DIO) if ENABLE_DISPLAY else NullDisplay()
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

    except (KeyError, TypeError, AttributeError):
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

    except (KeyError, TypeError, AttributeError):
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
    if not TONE_ACTIVE:
        return
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
if TONE_ACTIVE:
    init_tone()


# ====================== DISPLAY ======================

def display_volume_info(current_volume=None, is_muted=None):
    """Show the config label and the volume on the 0..100 scale.
    `current_volume` is the CamillaDSP main volume in dB."""
    global blank_volume_when_mute, last_displayed, display_is_off

    if current_volume is None:
        current_volume = cdsp.volume.main_volume()
    if is_muted is None:
        is_muted = cdsp.volume.main_mute()

    config_path = get_config_display_label()

    pct = db_to_pct(current_volume)                 # 0..100 (LMS / Squeezelite curve)

    if is_muted and not is_volume_key_held:
        blank_volume_when_mute, volume_str = True, f"{config_path} --"
    else:
        shown = "HI" if pct >= 100 else f"{pct:02}"
        blank_volume_when_mute, volume_str = False, f"{config_path} {shown}"

    segs = tm.encode_string(volume_str)

    bass_gain, treble_gain, tilt_gain, _ = get_bass_treble(config_active, mode="gain")
    presence_gain, _ = get_presence_tilt(config_active, mode="gain")
    if bass_gain or treble_gain or tilt_gain or presence_gain:
        segs[3] |= 0x01    # top horizontal segment (A) on the 4th character: tone settings active

    if ENABLE_AUTO_POWEROFF and auto_power_enabled:
        segs[2] |= 0x80    # decimal point on the 3rd character: auto power-off active

    if ENABLE_AUTO_POWEROFF and lgpio.gpio_read(h, POWER_GPIO) == 1:
        segs[3] |= 0x80    # decimal point on the 4th character: power relay really ON

    tm.write(swap(segs))
    last_displayed = "volume"
    display_is_off = False


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
    """Move one step on the 0..100 scale (LMS / Squeezelite curve).
    `current_volume` is the CamillaDSP main volume in dB."""
    pct = db_to_pct(current_volume)
    delta = -volume_step if key == KEY_BINDINGS['VOLUMEDOWN'] else volume_step
    new_pct = clamp(pct + delta, 0, 100)

    if new_pct != pct:
        new_volume = pct_to_db(new_pct)
        cdsp.volume.set_main_volume(new_volume)
        display_volume_info(new_volume)


def get_repeat_speed(volume, direction, exponent=2.0, pivot=-50):
    """Delay between two volume steps while a key is held (volume in dB)."""
    min_delay = 0.01
    max_delay = 0.5

    if direction == "down":
        return min_delay
    if direction != "up":
        raise ValueError("direction must be 'up' or 'down'")

    volume = clamp(volume, -99, 0)
    t = (volume - pivot) / (0 - pivot) if volume >= pivot else 0
    delay = min_delay + (t ** exponent) * (max_delay - min_delay)

    # The delays above were tuned for 1 dB per step. A step is now ~0.5 dB in the
    # upper range, so scale the delay by the real dB size of the next step to keep
    # the same speed in dB per second.
    pct = db_to_pct(volume)
    step_db = pct_to_db(min(pct + 1, 100)) - pct_to_db(pct)
    return max(min_delay, delay * step_db)


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
    MAC address of the default network interface.
    Uses its own socket (no shared state), so it is safe to run in a thread."""
    try:
        mac = quote(detect_player_mac(), safe="")

        with socket.create_connection(LMS_ADDR, timeout=5) as sock:
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
    Each config keeps its own tone settings (bass, treble, tilt, loudness, presence).
    Returns the title of the new config if a switch was performed, None otherwise."""
    global config_active, current_config_key

    group = os.path.basename(config_pattern)[:1]
    config_files = sorted(glob.glob(config_pattern))
    if not config_files:
        return None
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
        return None

    # Remember the tone of the config we are leaving (and write it to disk)
    save_audio_settings(config_active)

    # Protection 3: abort if the new config does not parse to a dict
    new_config_data = cdsp.config.read_and_parse_file(new_config_path)
    if not isinstance(new_config_data, dict):
        log.error("Config %s is invalid, aborting switch", new_config_path)
        return None

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

    # Protection 4: only update the local state if CamillaDSP accepted the config
    try:
        cdsp.config.set_active(new_config_data)
        cdsp.config.set_file_path(new_config_path)
    except Exception as e:
        log.error("Failed to apply config %s: %s", new_config_path, e)
        return None

    config_active = new_config_data
    current_config_key = new_key
    load_prev_values(saved)

    # Remember this config as the last used of its group (survives TV on/off and restarts)
    new_group = os.path.basename(new_config_path)[:1]
    if new_group in CONFIG_GROUPS:
        settings["last_config"][new_group] = new_key
        write_settings()

    display_volume_info()
    return new_config_data.get("title")


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


POWER_MSG_DURATION = 1.5    # Seconds PW ON / PW OFF stays on the display


async def return_to_volume_later(delay=POWER_MSG_DURATION):
    """Go back to the volume screen shortly after PW ON / PW OFF, unless another
    screen has been shown in the meantime (a key was pressed)."""
    await asyncio.sleep(delay)
    if last_displayed == "power":
        display_volume_info()


# ====================== BACKGROUND TASKS ======================

async def silence_monitor():
    """Single reader of the CamillaDSP levels: refreshes `silent_now` once per
    second. auto_poweroff, display_manager_loop and monitor_tv_toslink read the
    variable instead of each querying CamillaDSP themselves.
    On a read error the previous value is kept (no power cut because of a glitch)."""
    global silent_now
    while True:
        try:
            silent_now = is_silent()
        except Exception as e:
            log.warning("Silence monitor: cannot read levels (%s)", e)
        await asyncio.sleep(1)


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


async def volume_watcher_loop():
    """Watch for external changes: main volume, mute, and active config title.
    On a config change, resync the local state (config_active, current_config_key,
    prev values) so that the script does not later overwrite the external config."""
    global config_active, current_config_key

    last_volume = cdsp.volume.main_volume()
    last_mute = cdsp.volume.main_mute()
    last_title = cdsp.config.title()
    last_warned_title = None


    while True:
        await asyncio.sleep(0.5)
        try:
            current_volume = cdsp.volume.main_volume()
            current_mute = cdsp.volume.main_mute()
            current_title = cdsp.config.title()
        except Exception as e:
            log.warning("Volume watcher: CamillaDSP unreachable (%s)", e)
            continue

        # Ignore empty titles (CamillaDSP can briefly report "" during a reload)
        title_changed = current_title != last_title
        if current_title:
            last_title = current_title

        if title_changed:
            # Protection 2: never let config_active become None
            try:
                new_active = cdsp.config.active() or {}
            except Exception as e:
                log.warning("Volume watcher: could not fetch active config (%s)", e)
                new_active = None

            if isinstance(new_active, dict) and new_active:
                config_active = new_active
                path = cdsp.config.file_path()
                current_config_key = config_key(path) if path else None
                saved = settings["configs"].get(current_config_key) if current_config_key else None
                load_prev_values(saved)
                last_warned_title = None
                log.debug("Config title changed to '%s'", current_title)

            else:
                if current_title != last_warned_title:
                    log.warning("Config changed but active config is unusable; keeping previous (title=%r)", current_title)
                    last_warned_title = current_title


        if current_volume != last_volume or current_mute != last_mute or title_changed:
            last_volume = current_volume
            last_mute = current_mute
            # Redraw only if the volume (or PW ON/OFF) screen is shown and no volume key is held
            if last_displayed in ("volume", "power") and not is_volume_key_held:
                display_volume_info(current_volume, current_mute)


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

        # Blank the display after a long silence, wake it when sound returns
        if silent_now:
            if not display_is_off:
                idle_display_counter += 1
                if idle_display_counter >= POWER_OFF_DELAY * 60 + 5:  # +5 s: margin after the relay cut, so the 1.5 s PW OFF message stays visible
                    idle_display_counter = 0
                    blank_display()
        else:
            idle_display_counter = 0
            if display_is_off:
                display_volume_info()

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

    while True:
        await asyncio.sleep(1)

        if silent_now:
            silence_counter += 1
            if silence_counter == POWER_OFF_DELAY * 60 and not is_waiting_for_sound:
                if auto_power_enabled:
                    lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
                    show("PW OFF")
                    is_waiting_for_sound = True  # Wait for sound or POWER key to switch back on
                    await asyncio.sleep(1.5)     # how long PW OFF stays visible
                    if silent_now and is_waiting_for_sound:
                        blank_display()
                else:
                    blank_display()              # relay already forced off: just blank the display

            if silence_counter >= HALT_DELAY * 3600:
                shutdown_system()
                break

        else:
            silence_counter = 0
            if auto_power_enabled and lgpio.gpio_read(h, POWER_GPIO) == 0:
                if is_waiting_for_sound:
                    lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)
                    display_volume_info()
                    is_waiting_for_sound = False
            elif not auto_power_enabled and lgpio.gpio_read(h, POWER_GPIO) == 1:
                lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)


# ---------------------- Config switching: GPIO mode ----------------------

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


# --------------------- Config switching: TOSLINK mode ---------------------
# Same structure as monitor_tv_gpio: a `last_source` variable holds the previous
# state and the action is triggered only when the state changes.
#
# Two phases:
#   - SQUEEZELITE: the toslink device is free, it is read directly.
#     Source = "TV" if level >= TOSLINK_TV_ON_DB, else "SQUEEZELITE".
#   - TV: the TOSLINK is held by CamillaDSP.
#     Source = "SQUEEZELITE" if STALLED or silent for TOSLINK_SILENCE_DELAY_S,
#     else "TV".
#
# Manual override: if the config title changes on its own, the detection stops
# acting until the title matches the TV reference title again.
#
# Script-initiated switch: the title of the config we load is memorized in
# `expected_title`, so the watcher does not mistake our own change for a manual one.

def read_toslink_capture_params():
    """Read device / rate / channels from the first '|*' YAML config.
    All '|*' configs share the same capture input, so the first one is enough.
    Returns a dict {'device', 'rate', 'channels'} or None on failure."""
    files = sorted(glob.glob(CONFIG_DIR + "|*"))
    if not files:
        log.error("[TOSLINK] No '|*' config file found in %s", CONFIG_DIR)
        return None
    try:
        data = cdsp.config.read_and_parse_file(files[0])
        if not isinstance(data, dict):
            return None
        dev = data.get("devices") or {}
        cap = dev.get("capture") or {}
        device = cap.get("device")
        if not isinstance(device, str) or not device:
            log.error("[TOSLINK] No capture device in %s", files[0])
            return None
        return {
            "device":   device,
            "rate":     int(dev.get("samplerate") or 48000),
            "channels": int(cap.get("channels") or 2),
        }
    except Exception as e:
        log.error("[TOSLINK] Cannot read %s: %s", files[0], e)
        return None


def _toslink_probe(params):
    """Runs in a thread (blocks ~0.5 s).
    `params` is the dict returned by read_toslink_capture_params().
    Returns (db, None) if the TOSLINK is free, (None, error) if it is busy."""
    try:
        pcm = alsaaudio.PCM(
            type=alsaaudio.PCM_CAPTURE, mode=alsaaudio.PCM_NONBLOCK,
            device=params["device"], channels=params["channels"], rate=params["rate"],
            format=alsaaudio.PCM_FORMAT_S16_LE, periodsize=4096,
        )
    except Exception as e:
        return None, e

    peak = 0.0
    try:
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            length, data = pcm.read()
            if length > 0:
                samples = struct.unpack(f"<{len(data) // 2}h", data)
                peak = max(peak, max((abs(s) for s in samples), default=0) / 32768.0)
            else:
                time.sleep(0.01)
    finally:
        pcm.close()
    return (20.0 * math.log10(peak) if peak > 0 else -1000.0), None


def toslink_read_title_from_file(pattern):
    """Title of the first config file matching the pattern."""
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    try:
        data = cdsp.config.read_and_parse_file(files[0])
        return (data.get("title") or None) if isinstance(data, dict) else None
    except Exception as e:
        log.error("[TOSLINK] Cannot read %s: %s", files[0], e)
        return None


async def monitor_tv_toslink():
    """Detect the source (TV / SQUEEZELITE) from the audio and switch config."""
    loop = asyncio.get_running_loop()

    # Capture parameters are read once from the first '|*' YAML config
    capture_params = read_toslink_capture_params()
    if capture_params is None:
        log.error("[TOSLINK] Cannot determine capture parameters, disabling detection")
        return
    log.info("[TOSLINK] Capture: %s @ %d Hz, %d ch",
             capture_params["device"], capture_params["rate"], capture_params["channels"])

    tv_title = toslink_read_title_from_file(CONFIG_DIR + "|*")
    log.info("[TOSLINK] TV reference title: %r", tv_title)

    last_title = cdsp.config.title() or None
    expected_title = None       # Title expected after OUR switch
    manual_override = False
    last_source = None
    last_phase = None
    silence_counter = 0

    while True:
        await asyncio.sleep(1.0)
        try:
            # --- 0. Ignore transient states ---
            state = cdsp.general.state().name
            current_title = cdsp.config.title()
            if state == "STARTING" or not current_title:
                continue

            # --- 1. Watch the title for external changes ---
            if current_title != last_title:
                if expected_title is not None and current_title == expected_title:
                    log.info("[TOSLINK] Script switch confirmed: %r", current_title)
                    expected_title = None
                else:
                    log.info("[TOSLINK] Manual change: %r -> %r", last_title, current_title)
                    manual_override = True
                last_title = current_title

            # --- 2. Resume when the title is back to the TV reference ---
            if manual_override and tv_title is not None and current_title == tv_title:
                log.info("[TOSLINK] Back on TV config, resuming")
                manual_override = False
                last_source = "TV"

            # --- 3. Manual override: do nothing ---
            if manual_override:
                continue

            # --- 4. Detect the source ---
            db, err = await loop.run_in_executor(None, _toslink_probe, capture_params)

            if err is None:
                # SQUEEZELITE phase: TOSLINK free, direct reading
                if last_phase != "SQUEEZELITE":
                    last_phase, silence_counter = "SQUEEZELITE", 0
                    log.info("[TOSLINK] phase: SQUEEZELITE (input available)")
                current_source = "TV" if db >= TOSLINK_TV_ON_DB else "SQUEEZELITE"
            else:
                # TV phase: TOSLINK held by CamillaDSP
                if last_phase != "TV":
                    last_phase, silence_counter = "TV", 0
                    log.info("[TOSLINK] phase: TV (input busy: %s)", err)

                silent = silent_now   # shared value, no extra CamillaDSP request

                if state == "STALLED":
                    silence_counter = 0
                    current_source = "SQUEEZELITE"
                elif silent:
                    silence_counter += 1
                    current_source = ("SQUEEZELITE"
                                      if silence_counter >= TOSLINK_SILENCE_DELAY_S else "TV")
                else:
                    silence_counter = 0
                    current_source = "TV"

            # --- 5. Act only when the source changes ---
            if current_source != last_source:
                pattern = '|*' if current_source == "TV" else '_*'
                new_title = await change_config(cdsp, CONFIG_DIR + pattern, cycle=False)
                if new_title is not None:
                    expected_title = new_title
                last_source = current_source

        except Exception as e:
            log.warning("[TOSLINK] iteration failed: %s", e)


async def remote_events(device):
    global last_displayed, key_hold_counter, screen_idle_ticks, is_key_held, is_volume_key_held
    global last_tone_tilt, enter_display_at_press

    last_repeat_time = 0
    last_seek_time = 0
    swallowed_key = None            # key that only woke the display: ignored until released
    while True:
        try:
            async for event in device.async_read_loop():
                if event.type != evdev.ecodes.EV_KEY:
                    continue

                screen_idle_ticks = 0  # Any key event restarts the return-to-volume timer

                attrib = evdev.categorize(event)
                key = attrib.keycode
                volume_keys = (KEY_BINDINGS['VOLUMEDOWN'], KEY_BINDINGS['VOLUMEUP'])

                # Display off: the first key only wakes it up, and its repeat / release are ignored
                if attrib.keystate == 1 and display_is_off:
                    is_key_held = True
                    display_volume_info()
                    swallowed_key = key
                    continue
                if swallowed_key is not None and key == swallowed_key:
                    if attrib.keystate == 0:
                        swallowed_key = None
                    continue

                if attrib.keystate == 1:  # Key pressed
                    is_key_held = True
                    key_hold_counter = 0

                    if key in volume_keys:
                        is_volume_key_held = True
                        current_volume = cdsp.volume.main_volume()
                        is_muted = cdsp.volume.main_mute()

                        if ENABLE_DISPLAY and (last_displayed != "volume" or blank_volume_when_mute):
                            display_volume_info(current_volume, is_muted)
                        else:
                            change_volume_from_key(key, current_volume)

                    # `in`, not `==`: evdev returns a list when a code has several names
                    elif KEY_BINDINGS['MUTE'] in key:
                        is_muted = not cdsp.volume.main_mute()
                        cdsp.volume.set_main_mute(is_muted)
                        display_volume_info(cdsp.volume.main_volume(), is_muted)

                    elif ENABLE_DISPLAY and key in (KEY_BINDINGS['UP'], KEY_BINDINGS['DOWN'],
                                                    KEY_BINDINGS['RIGHT'], KEY_BINDINGS['LEFT']):
                        handle_arrow_keys(key)

                    elif key == KEY_BINDINGS['BACK']:
                        await change_config(cdsp, CONFIG_DIR + '_*')

                    elif key == KEY_BINDINGS['HOMEPAGE']:
                        await change_config(cdsp, CONFIG_DIR + '|*')

                    elif ENABLE_DISPLAY and key == KEY_BINDINGS['ENTER']:
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

                    elif key in (KEY_BINDINGS['PREVIOUSSONG'], KEY_BINDINGS['NEXTSONG']):
                        key_hold_counter += 1
                        current_time = time.time()
                        if key_hold_counter >= HOLD_SEEK and current_time - last_seek_time >= SEEK_INTERVAL_S:
                            sign = "+" if key == KEY_BINDINGS['NEXTSONG'] else "-"
                            await asyncio.to_thread(send_lms_command, f"time {sign}{SEEK_STEP_S}")
                            last_seek_time = current_time

                    elif key == KEY_BINDINGS['PLAYPAUSE']:
                        key_hold_counter += 1
                        if key_hold_counter == HOLD_STOP:
                            await asyncio.to_thread(send_lms_command, "stop")

                    elif key == KEY_BINDINGS['POWER']:
                        key_hold_counter += 1
                        if ENABLE_AUTO_POWEROFF and key_hold_counter == HOLD_TOGGLE_POWER:
                            toggle_power()
                            asyncio.create_task(return_to_volume_later())
                        elif key_hold_counter == HOLD_SHUTDOWN:
                            save_audio_settings(config_active)
                            shutdown_system()

                    elif key == KEY_BINDINGS['ENTER']:
                        key_hold_counter += 1
                        if key_hold_counter == HOLD_SWITCH_TONE_TILT and last_displayed in ("tone", "tilt"):
                            last_tone_tilt = "tilt" if last_displayed == "tone" else "tone"
                            (display_tilt_info if last_tone_tilt == "tilt" else display_tone_info)()

                elif attrib.keystate == 0:  # Key released

                    if ENABLE_DISPLAY and key == KEY_BINDINGS['ENTER'] and key_hold_counter < HOLD_SWITCH_TONE_TILT:
                        handle_enter_press(enter_display_at_press)

                    elif key == KEY_BINDINGS['PLAYPAUSE'] and key_hold_counter < HOLD_STOP:
                        await asyncio.to_thread(send_lms_command, "pause")

                    elif key in (KEY_BINDINGS['PREVIOUSSONG'], KEY_BINDINGS['NEXTSONG']) \
                            and key_hold_counter < HOLD_SEEK:
                        await asyncio.to_thread(
                            send_lms_command,
                            "playlist index -1" if key == KEY_BINDINGS['PREVIOUSSONG']
                            else "playlist index +1")

                    elif key in volume_keys:
                        display_refresh_event.set()

                    key_hold_counter = 0
                    last_repeat_time = 0
                    last_seek_time = 0

        except OSError as e:
            log.warning("Remote disconnected: %s. Attempting to reconnect...", e)
            device = None
            while device is None:
                device = find_remote()
                if device is None:
                    await asyncio.sleep(1)
            log.info("Remote reconnected.")


# ====================== LMS <-> CAMILLADSP VOLUME SYNC ======================
# Based on the original work by mbrennwa:
#     https://github.com/mbrennwa/camilla_LMS_volume_sync
# Runs in its own thread (blocking socket + select) with its own CamillaDSP
# connection, so it never blocks the asyncio loop of the remote.

def blog(message):
    log.info("[LMS sync] %s", message)


def run_lms_sync():
    """One bridge session. Only ever leaves by raising (connection lost...)."""
    player = LMS_PLAYER_MANUAL or detect_player_mac()
    pq = quote(player, safe="")
    player_re = re.escape(player)
    re_vol = re.compile(rf"{player_re} prefset server volume (-?\d+(?:\.\d+)?)$", re.I)
    re_mute = re.compile(rf"{player_re} prefset server mute ([01])$", re.I)

    sync_cdsp = CamillaClient(*CAMILLA_ADDR)
    sync_cdsp.connect()
    blog("Connected to CamillaDSP")
    sock = None
    try:
        sock = socket.create_connection(LMS_ADDR, timeout=5)
        sock.settimeout(None)
        blog("Connected to LMS CLI")
        send = lambda cmd: sock.sendall((cmd + "\n").encode())

        # --- Bridge state ---
        vol = sync_cdsp.volume
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
            if LMS_SYNC_DEBUG:
                blog(f"{origin} mute={new}")
            apply(new)
            restore_until = 0.0 if new else time.monotonic() + 0.5   # 0.5 s: after an unmute, LMS may report a wrong volume for this long

        def on_lms_volume(p):
            nonlocal pct, restore_until
            if time.monotonic() < restore_until:
                if p != pct:
                    blog(f"LMS unmute: restore LMS to {pct}%")
                    send(f"{pq} mixer volume {pct}")
                else:
                    restore_until = 0.0
                return
            if p != pct:   # real user action (an echo of our own push has p == pct)
                pct = p
                if LMS_SYNC_DEBUG:
                    blog(f"LMS {p}% -> Camilla {pct_to_db(p):.1f} dB")
                vol.set_main_volume(pct_to_db(p))

        # --- Startup: Camilla wins ---
        send("listen 1")
        send(f"{pq} mixer volume {pct}")
        send(f"{pq} mixer muting {int(muted)}")
        blog(f"Startup: Camilla -> LMS {pct}% muted={muted}")

        buf = b""
        while True:
            # --- LMS -> Camilla ---
            # 0.25 s: max wait for an LMS message, and so the polling interval of CamillaDSP
            if select.select([sock], [], [], 0.25)[0]:
                data = sock.recv(4096)
                if not data:
                    raise ConnectionError("LMS closed the connection")
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = unquote(line.decode(errors="replace")).strip()
                    if LMS_SYNC_DEBUG and "prefset server" in line:
                        blog(f"<< {line}")
                    if (m := re_mute.match(line)):
                        if (lms_muted := m[1] == "1") != muted:
                            set_mute(lms_muted, "LMS -> Camilla", vol.set_main_mute)
                        continue
                    if (m := re_vol.match(line)) and not muted:
                        on_lms_volume(round(abs(float(m[1]))))

            # --- Camilla -> LMS ---
            if (cam_muted := bool(vol.main_mute())) != muted:
                set_mute(cam_muted, "Camilla -> LMS",
                         lambda m: send(f"{pq} mixer muting {int(m)}"))

            if muted or time.monotonic() < restore_until:
                prev = None
                continue

            p = db_to_pct(vol.main_volume())
            if p != pct and p == prev:   # confirmed by two consecutive reads
                pct, prev = p, None
                if LMS_SYNC_DEBUG:
                    blog(f"Camilla {p}% -> LMS")
                send(f"{pq} mixer volume {p}")
            else:
                prev = p
    finally:
        if sock is not None:
            sock.close()
        try:
            sync_cdsp.disconnect()
        except Exception:
            pass


def lms_sync_loop():
    """Thread body: run the bridge and restart it 5 s after any error."""
    while True:
        try:
            run_lms_sync()
        except Exception as e:
            blog(f"Bridge error: {e}, restart in 5s")
            time.sleep(5)


# ====================== SHUTDOWN / MAIN ======================

def exit_gracefully(signum, frame):
    """Save the settings, cut the relay and release the GPIO on SIGINT/SIGTERM."""
    try:
        save_audio_settings(config_active)
        if ENABLE_AUTO_POWEROFF:
            lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
            time.sleep(0.5)
        if h is not None:
            lgpio.gpiochip_close(h)
        log.info("Gracefully shutting down the service...")
        sys.exit(0)
    except Exception as e:
        log.error("Error during cleanup: %s", e)
        sys.exit(1)


signal.signal(signal.SIGINT, exit_gracefully)
signal.signal(signal.SIGTERM, exit_gracefully)


async def main():
    if SWITCH_GPIO:
        # Load the config matching the TV state at startup
        if lgpio.gpio_read(h, TV_GPIO) == 0:
            log.info("TV detected ON at startup: loading ON config")
            await change_config(cdsp, CONFIG_DIR + '|*', cycle=False)
        else:
            await change_config(cdsp, CONFIG_DIR + '_*', cycle=False)
    # TOSLINK mode: nothing to do at startup, last_source = None forces a
    # first switch on the first iteration of monitor_tv_toslink.
    if ENABLE_DISPLAY:
        display_volume_info()  # change_config returns early when the config is already active

    if ENABLE_LMS_SYNC:
        # Daemon thread: it dies with the process (SIGTERM handler -> sys.exit)
        threading.Thread(target=lms_sync_loop, name="lms-sync", daemon=True).start()

    tasks = []
    if ENABLE_AUTO_POWEROFF or ENABLE_DISPLAY or SWITCH_TOSLINK:
        # Single reader of the CamillaDSP levels, shared through `silent_now`
        tasks.append(asyncio.create_task(silence_monitor()))
    if ENABLE_AUTO_POWEROFF:
        tasks.append(asyncio.create_task(auto_poweroff()))
    if ENABLE_DISPLAY:
        tasks.append(asyncio.create_task(display_manager_loop()))
        tasks.append(asyncio.create_task(adc_reader_loop()))
    if ENABLE_DISPLAY or ENABLE_REMOTE or ENABLE_CONFIG_SWITCH:
        # Redraws the display and resyncs config_active after an external change
        tasks.append(asyncio.create_task(volume_watcher_loop()))
    if SWITCH_GPIO:
        tasks.append(asyncio.create_task(monitor_tv_gpio()))
    elif SWITCH_TOSLINK:
        tasks.append(asyncio.create_task(monitor_tv_toslink()))
    if ENABLE_REMOTE:
        tasks.append(asyncio.create_task(remote_events(remote)))

    if tasks:
        await asyncio.gather(*tasks)
    else:
        await asyncio.Event().wait()   # only the LMS sync thread is running


if __name__ == "__main__":
    asyncio.run(main())
