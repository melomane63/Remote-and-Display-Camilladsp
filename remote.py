import asyncio
import glob
import os
import json
import signal
import socket
import sys
import time
import math
import evdev
import lgpio
from tm1637_lgpio import TM1637
from camilladsp import CamillaClient
import serial


# ========================= CONSTANTS =========================


REMOTE_NAME = "HID Remote01 Keyboard"   # Remote name found using `python3 -m evdev.evtest`
POWER_OFF_DELAY = 2                     # Delay (in minutes) before trigger power-off when no sound is detected
HALT_DELAY = 48                         # Delay (in hours) before HALT raspberry when no sound is detected
POWER_GPIO = 4                          # GPIO pin controlling the power relay
CLK, DIO = 23, 24                       # Clock and Data pins for the TM1637 display
TV_GPIO = 12

# Mapping of remote control keys to their functions
KEY_BINDINGS = {
    'VOLUMEDOWN': 'KEY_VOLUMEDOWN',     # Decrease volume
    'VOLUMEUP': 'KEY_VOLUMEUP',         # Increase volume
    'MUTE': 'KEY_MUTE',                 # Mute/unmute audio
    'PLAYPAUSE': 'KEY_PLAYPAUSE',       # Play/pause music (LMS command)
    'PREVIOUSSONG': 'KEY_PREVIOUSSONG', # Play previous song (LMS command)
    'NEXTSONG': 'KEY_NEXTSONG',         # Play next song (LMS command)
    'UP': 'KEY_UP',                     # Increase presence gain
    'DOWN': 'KEY_DOWN',                 # Decrease presence gain
    'LEFT': 'KEY_LEFT',                 # Decrease tilt gain
    'RIGHT': 'KEY_RIGHT',               # Increase tilt gain
    'POWER': 'KEY_POWER',               # Display brightness,toggle Auto power, Shutdown
    'ENTER': 'KEY_ENTER',               # Toggle tone gain settings / Loudness display on long press
    'BACK': 'KEY_BACK',                 # Switch to the next DSP configuration (prefixed with "_")
    'HOMEPAGE': 'KEY_HOMEPAGE',         # Switch to the next DSP configuration (prefixed with "|")
}

CONFIG_DIR = os.path.expanduser("~") + "/camilladsp/configs/"  # Path to DSP configuration files

SERIAL_PORT = "/dev/serial0"
BAUD_RATE   = 115200

ADC_LEVELS = [80, 1000, 2500, 3500]
BRIGHTNESS_LEVELS = [0, 1, 2, 3, 7]
NUM_LEVELS = len(ADC_LEVELS)
HYSTERESIS_PCT    = 0.03  # %

ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
line = ser.readline().decode().strip()
try:
    adc_value_init = int(line)
except ValueError:
    adc_value_init = 0

last_level = next(
    (i for i, t in enumerate(ADC_LEVELS) if adc_value_init < t),
    len(BRIGHTNESS_LEVELS) - 1
)

# ====================== GLOBAL VARIABLES ======================


key_hold_counter      = 0
auto_power_enabled    = True
is_waiting_for_sound  = False
is_key_held           = False
is_volume_key_held    = False
last_displayed = None
blank_volume_when_mute = False
enter_display_at_press = None  # écran affiché au moment de l'appui sur ENTER (pour distinguer appui court/long sans effet de bord)
#blank_volume_when_mute = False

# ====================== CONFIGURATION ======================

# Remote detection
counter = 0
while True:
    for path in evdev.list_devices():
        device = evdev.InputDevice(path)
        if device.name == REMOTE_NAME:
            print(f"'{REMOTE_NAME}' found at {path}.")
            remote = device
            remote.grab()
            break
    else:
        counter += 1
        if counter >= HALT_DELAY*3600:
            print(f"'{REMOTE_NAME}' not found after {HALT_DELAY} hours. Shutting down...")
            os.system("sudo shutdown -h now")
            break
        print(f"'{REMOTE_NAME}' not found. Retrying...")
        time.sleep(1)
        continue
    break

# Connect to CamillaDSP
cdsp = CamillaClient("127.0.0.1", 1234)
cdsp.connect()
config_active = cdsp.config.active()

# Load saved values
DEFAULT_SETTINGS = {
    "bass_gain": 0,
    "treble_gain": 0,
    "tilt_gain": 0,
    "loudness_ref": -30,
    "presence_gain": 0,
    "last_tone_tilt": "tone",
}

try:
    with open("settings.json") as f: settings = json.load(f) if os.path.getsize("settings.json") > 0 else DEFAULT_SETTINGS
except (FileNotFoundError, json.JSONDecodeError):
    settings = DEFAULT_SETTINGS
    with open("settings.json", "w") as f: json.dump(settings, f)

# Ensure any missing keys (e.g. upgrading from an older settings.json) fall back to defaults
for k, v in DEFAULT_SETTINGS.items():
    settings.setdefault(k, v)

for key, (filt, param) in {
    "bass_gain": ("Bass", "gain"),
    "treble_gain": ("Treble", "gain"),
    "tilt_gain": ("Tilt", "gain"),
    "loudness_ref": ("Loudness", "reference_level"),
    "presence_gain": ("Presence", "gain"),
}.items():
    filters = config_active.setdefault("filters", {})
    filters.get(filt, {}).get("parameters", {}).update({param: settings[key]})

cdsp.config.set_active(config_active)
bass_gain_prev = settings["bass_gain"]
treble_gain_prev = settings["treble_gain"]
tilt_gain_prev = settings["tilt_gain"]
loudness_gain_prev = settings["loudness_ref"]
presence_gain_prev = settings["presence_gain"]
last_tone_tilt = settings["last_tone_tilt"]

# GPIO setup
h = lgpio.gpiochip_open(0)  # Open the GPIO chip
lgpio.gpio_claim_output(h, POWER_GPIO)  # Configure GPIO as output
lgpio.gpio_write(h, POWER_GPIO, 1)  # Set initial state to HIGH
# Configurer GPIO 12 comme entrée avec pull-up interne
lgpio.gpio_claim_input(h, TV_GPIO)

# TM1637 Display Configuration
tm = TM1637(clk=CLK, dio=DIO)
tm.write(tm.encode_string(" " *6))
tm.brightness(last_level)

# ====================== FONCTIONS   =============================

def adc_to_brightness(adc_value):
    global last_level

    for i, threshold in enumerate(ADC_LEVELS):
        if adc_value < threshold:
            target_level = i
            break
    else:
        target_level = len(BRIGHTNESS_LEVELS) - 1

    if target_level != last_level:
        if target_level > last_level:
            lower_bound = ADC_LEVELS[last_level] * (1 + HYSTERESIS_PCT)
            if adc_value > lower_bound:
                last_level = target_level
        else:
            upper_bound = ADC_LEVELS[target_level] * (1 - HYSTERESIS_PCT)
            if adc_value < upper_bound:
                last_level = target_level

    return BRIGHTNESS_LEVELS[last_level]


def save_audio_settings(config):
    bass_gain, treble_gain, tilt_gain, loudness_ref = get_bass_treble(config, mode="gain")
    presence_gain, _ = get_presence_tilt(config, mode="gain")

    data = {
        "bass_gain": bass_gain,
        "treble_gain": treble_gain,
        "tilt_gain": tilt_gain,
        "loudness_ref": loudness_ref,
        "presence_gain": presence_gain,
        "last_tone_tilt": last_tone_tilt,
    }

    with open("settings.json", "w") as f:
        json.dump(data, f, indent=4)


def swap(segs):
    length = len(segs)
    if length == 4 or length == 5:
        segs.extend(bytearray([0] * (6 - length)))
    segs[0], segs[2] = segs[2], segs[0]
    if length >= 4:
        segs[3], segs[5] = segs[5], segs[3]
    return segs


def change_volume_from_key(cdsp, key, current_volume, is_muted=None, volume_step=1):
    volume_change = -volume_step if key == KEY_BINDINGS['VOLUMEDOWN'] else volume_step
    new_volume = max(-99, min(0, current_volume + volume_change))

    if new_volume != current_volume:
        cdsp.volume.set_main_volume(new_volume)
        display_volume_info(new_volume)


def get_bass_treble(config, mode="gain"):

    try:
        filters = config.get('filters', {})
        bass = filters['Bass']['parameters']
        treble = filters['Treble']['parameters']
        tilt = filters['Tilt']['parameters']
        loudness = filters['Loudness']['parameters']

        if mode == "gain":
            bass_gain = bass.get('gain', 0)
            treble_gain = treble.get('gain', 0)
            tilt_gain = tilt.get('gain', 0)
            loudness_ref = loudness.get('reference_level', 0)
            return bass_gain, treble_gain, tilt_gain, loudness_ref

        elif mode == "parameters":
            return bass, treble, tilt, loudness

        else:
            raise ValueError("Invalid mode : use 'gain' or 'parameters'")

    except (KeyError, TypeError):
        if mode == "gain":
            return 0, 0, 0, 0
        else:
            return None, None, None, None


def get_presence_tilt(config, mode="gain"):
    """Retrieve Presence (medium) and Tilt filter gain or parameters."""
    try:
        filters = config.get('filters', {})
        presence = filters['Presence']['parameters']
        tilt = filters['Tilt']['parameters']

        if mode == "gain":
            return presence.get('gain', 0), tilt.get('gain', 0)

        elif mode == "parameters":
            return presence, tilt

        else:
            raise ValueError("Invalid mode : use 'gain' or 'parameters'")

    except (KeyError, TypeError):
        if mode == "gain":
            return 0, 0
        else:
            return None, None


def display_volume_info(current_volume=None, is_muted=None):
    global blank_volume_when_mute, last_displayed

    if current_volume is None:
        current_volume = cdsp.volume.main_volume()
    if is_muted is None:
        is_muted = cdsp.volume.main_mute()

    config_path = cdsp.config.file_path().replace(CONFIG_DIR, '')
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
        segs[-4] |= 0x80   # allume le point décimal du dernier digit

    tm.write(swap(segs))
    last_displayed = "volume"


def display_tone_info():
    global last_displayed
    bass_gain, treble_gain, _, _ = get_bass_treble(config_active, mode="gain")

    bass_gain = round(bass_gain)
    treble_gain = round(treble_gain)
    display_str = f"{bass_gain:2}B{treble_gain:2}T"
    tm.write(swap(tm.encode_string(display_str)))
    last_displayed = "tone"


def display_loudness_info():
    global last_displayed
    _, _, _, loudness_ref = get_bass_treble(config_active, mode="gain")

    loudness_ref = round(loudness_ref)
    value_str = "---" if loudness_ref == -99 else f"{loudness_ref}"
    display_str = f"{value_str} Ld"

    tm.write(swap(tm.encode_string(display_str)))
    last_displayed = "loudness"


TILT_DB_STEP = 2  # pas réel appliqué au filtre CamillaDSP (dB) par incrément affiché

def display_tilt_info():
    global last_displayed
    presence_gain, tilt_gain = get_presence_tilt(config_active, mode="gain")

    presence_gain = round(presence_gain)
    tilt_step = round(tilt_gain / TILT_DB_STEP)  # valeur affichée (-3..+3), gain réel = tilt_step * 2 dB
    display_str = f"{tilt_step:2}T{presence_gain:2}P"

    tm.write(swap(tm.encode_string(display_str)))
    last_displayed = "tilt"


def handle_arrow_keys(key, config_active, cdsp):

    if last_displayed == "loudness":
        _, _, _, loudness_params = get_bass_treble(config_active, mode="parameters")
        if loudness_params is not None:
            ref_level = loudness_params.get('reference_level', 0)
            if key in (KEY_BINDINGS['UP'], KEY_BINDINGS['RIGHT']):
                ref_level += 5
            elif key in (KEY_BINDINGS['DOWN'], KEY_BINDINGS['LEFT']):
                ref_level -= 5
            ref_level = max(-50, min(-10, ref_level))
            loudness_params['reference_level'] = ref_level
            cdsp.config.set_active(config_active)
            display_loudness_info()

    elif last_displayed == "tilt":
        presence_params, tilt_params = get_presence_tilt(config_active, mode="parameters")
        if presence_params is not None and tilt_params is not None:
            presence_gain = presence_params.get('gain', 0)
            tilt_gain = tilt_params.get('gain', 0)
            if key == KEY_BINDINGS['UP']:
                presence_gain += 1
            elif key == KEY_BINDINGS['DOWN']:
                presence_gain -= 1
            elif key == KEY_BINDINGS['RIGHT']:
                tilt_gain += TILT_DB_STEP
            elif key == KEY_BINDINGS['LEFT']:
                tilt_gain -= TILT_DB_STEP
            presence_gain = max(-3, min(+3, presence_gain))
            tilt_gain = max(-3 * TILT_DB_STEP, min(+3 * TILT_DB_STEP, tilt_gain))  # affiché -3..+3, pas réel 2 dB
            presence_params['gain'] = presence_gain
            tilt_params['gain'] = tilt_gain
            cdsp.config.set_active(config_active)
            display_tilt_info()

    elif last_displayed == "tone":
        bass_params, treble_params, _, _ = get_bass_treble(config_active, mode="parameters")
        if bass_params is not None and treble_params is not None:
            bass_gain = bass_params.get('gain', 0)
            treble_gain = treble_params.get('gain', 0)
            if key == KEY_BINDINGS['UP']:
                treble_gain += 1
            elif key == KEY_BINDINGS['DOWN']:
                treble_gain -= 1
            elif key == KEY_BINDINGS['RIGHT']:
                bass_gain += 1
            elif key == KEY_BINDINGS['LEFT']:
                bass_gain -= 1
            bass_gain = max(-9, min(+9, bass_gain))
            treble_gain = max(-9, min(+9, treble_gain))
            bass_params['gain'] = bass_gain
            treble_params['gain'] = treble_gain
            cdsp.config.set_active(config_active)
            display_tone_info()

    else:
        if last_tone_tilt == "tilt":
            display_tilt_info()
        else:
            display_tone_info()


def send_lms_command(command):
    """Retrieve the MAC address of the default network interface, format it,
    and send a command to LMS using the MAC address as an identifier."""
    try:
        # Get the default network interface
        with os.popen("ip route show default") as route_info:
            # Extract the interface name from the routing table entry
            default_iface = next((line.split()[4] for line in route_info if "default" in line), None)

        # Raise an exception if no default network interface is found
        if not default_iface:
            raise ValueError("No default network interface found")

        # Read the MAC address of the interface from the system file
        mac_path = f"/sys/class/net/{default_iface}/address"
        with open(mac_path) as f:
            mac = f.read().strip()

        # Format the MAC address to be URL-encoded (%3A instead of ':')
        mac = mac.replace(":", "%3A").lower()

        # Get the hostname of the device
        host = socket.gethostname()
        port = 9090

        # Create a socket connection to LMS and send the command
        with socket.create_connection((host, port)) as sock:
            sock.sendall(f"{mac} {command}\r\nexit\r\n".encode("utf-8"))
            response = sock.recv(4096).decode("utf-8")  # Read the response
            return response.strip()

    except Exception as e:
        # Print error if any exception occurs
        print(f"Error: {e}")
        return None


def get_repeat_speed(volume, direction, exponent=2.0, pivot=-50):

    volume = max(-99, min(0, volume))

    min_delay = 0.01
    max_delay = 0.5

    if direction == "up":

        if volume >= pivot:
            t = (volume - pivot) / (0 - pivot)
        else:
            t = 0
        factor = math.pow(t, exponent)
        return min_delay + factor * (max_delay - min_delay)

    elif direction == "down":
        return min_delay

def handle_enter_press(last_displayed):
    bass_params, treble_params, tilt_params, loudness_params = get_bass_treble(config_active, mode="parameters")
    presence_params, _ = get_presence_tilt(config_active, mode="parameters")

    global bass_gain_prev, treble_gain_prev, tilt_gain_prev, loudness_gain_prev, presence_gain_prev

    if last_displayed == "tone" and bass_params is not None and treble_params is not None:
        if bass_params['gain'] == treble_params['gain'] == 0:
            bass_params['gain'], treble_params['gain'] = bass_gain_prev, treble_gain_prev
            bass_gain_prev, treble_gain_prev = 0, 0
        else:
            bass_gain_prev, treble_gain_prev = bass_params['gain'], treble_params['gain']
            bass_params['gain'] = treble_params['gain'] = 0
        cdsp.config.set_active(config_active)
        display_tone_info()

    elif last_displayed == "tilt" and presence_params is not None and tilt_params is not None:
        if presence_params['gain'] == tilt_params['gain'] == 0:
            presence_params['gain'], tilt_params['gain'] = presence_gain_prev, tilt_gain_prev
            presence_gain_prev, tilt_gain_prev = 0, 0
        else:
            presence_gain_prev, tilt_gain_prev = presence_params['gain'], tilt_params['gain']
            presence_params['gain'] = tilt_params['gain'] = 0
        cdsp.config.set_active(config_active)
        display_tilt_info()

    elif last_displayed == "loudness" and loudness_params is not None:
        if loudness_params['reference_level'] == -99:
            loudness_params['reference_level'] = loudness_gain_prev
            loudness_gain_prev = -99
        else:
            loudness_gain_prev = loudness_params['reference_level']
            loudness_params['reference_level'] = -99
        cdsp.config.set_active(config_active)
        display_loudness_info()

async def adc_reader_loop():
    global ser, last_level
    last_brightness = -1

    while True:
        line = ser.readline().decode('utf-8', errors='ignore').strip()
        if line:
            try:
                adc_value = int(line)
                brightness = adc_to_brightness(adc_value)

                if brightness != last_brightness:
                    tm.brightness(brightness)
                    last_brightness = brightness
                    print(f"ADC = {adc_value} -> Brightness = {brightness}")

            except ValueError:
                print(f"Non-numeric input received: {line}")

        await asyncio.sleep(0.1)


async def change_config(cdsp, config_pattern):
    global config_active
    bass_gain, treble_gain, tilt_gain, loudness_ref = get_bass_treble(config_active, mode="gain")
    presence_gain, _ = get_presence_tilt(config_active, mode="gain")
    config_files = glob.glob(config_pattern)
    current_config = cdsp.config.file_path()
    if not config_files:
        return
    try: index = config_files.index(current_config); new_config_path = config_files[(index+1)%len(config_files)]
    except ValueError: new_config_path = config_files[0]
    if new_config_path == current_config: return

    new_config_data = cdsp.config.read_and_parse_file(new_config_path)
    filters = new_config_data.get('filters', {})
    for value, (filt, param) in {bass_gain:("Bass","gain"), treble_gain:("Treble","gain"),
                                  tilt_gain:("Tilt","gain"), loudness_ref:("Loudness","reference_level"),
                                  presence_gain:("Presence","gain")}.items():
        if value is not None: filters.get(filt, {}).get("parameters", {}).update({param: value})

    config_dir = os.path.dirname(os.path.abspath(new_config_path))
    for f in filters.values():
        filename = f.get("parameters", {}).get("filename")
        if isinstance(filename, str) and filename and not os.path.isabs(filename):
            f["parameters"]["filename"] = os.path.normpath(os.path.join(config_dir, filename))

    cdsp.config.set_active(new_config_data)
    cdsp.config.set_file_path(new_config_path)
    config_active = new_config_data
    display_volume_info()


display_refresh_event = asyncio.Event()
async def display_manager_loop():
    global key_hold_counter, is_key_held, is_volume_key_held
    idleDisplayCounter = 0

    while True:

        if is_volume_key_held:
            is_volume_key_held = False
            await asyncio.sleep(0.5)
            if last_displayed == "volume" :
                display_volume_info()

        if last_displayed != "volume" :
            key_hold_counter += 1
            if key_hold_counter == 30:
                key_hold_counter = 0
                display_volume_info()

        if is_key_held:
            idleDisplayCounter = 0
            is_key_held = False

        if "-1000.0" in str(cdsp.levels.capture_rms()):
            idleDisplayCounter +=1
            if idleDisplayCounter == ( POWER_OFF_DELAY*60 ) + 5 :
                idleDisplayCounter = 0
                tm.write(tm.encode_string(" " *6))  # Clear the display

        try:
            await asyncio.wait_for(display_refresh_event.wait(), timeout=1.0)
            display_refresh_event.clear()
        except asyncio.TimeoutError:
            pass


async def toggle_power():
    global auto_power_enabled, is_waiting_for_sound, last_displayed
    if not auto_power_enabled:
        # If the relay is off, activate it and re-enable auto shutdown
        auto_power_enabled = True
        lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)  # Activate the relay
        tm.write(swap(tm.encode_string("PW ON")))  # Display "POWER ON"
        is_waiting_for_sound = False  # No need to wait for sound anymore

    else:
        # If auto_power_enabled, check if the relay was turned off by auto shutdown
        if is_waiting_for_sound:
            # If waiting for sound to turn back on, force relay activation
            lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)  # Activate the relay
            tm.write(swap(tm.encode_string("PW ON")))  # Display "POWER ON"
            is_waiting_for_sound = False  # Reset sound waiting state

        else:
            # Otherwise, deactivate the relay normally
            auto_power_enabled = False
            lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)  # Deactivate the relay
            tm.write(swap(tm.encode_string("PW OFF")))  # Display "POWER OFF"

    last_displayed = "power"

async def auto_poweroff():
    global auto_power_enabled, is_waiting_for_sound
    silenceCounter = 0
    last_power_relay_state = lgpio.gpio_read(h, POWER_GPIO)

    while True:
        await asyncio.sleep(1)

        if "-1000.0" in str(cdsp.levels.capture_rms()):  # No sound detected

            silenceCounter += 1
            if silenceCounter == POWER_OFF_DELAY*60:
                if not is_waiting_for_sound:
                    # Auto power-off: deactivate the relay after the delay without sound
                    lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
                    tm.write(swap(tm.encode_string("PW OFF")))
                    last_power_relay_state = lgpio.gpio_read(h, POWER_GPIO)  # Read the actual state
                    is_waiting_for_sound = True  # Wait for sound or Power button press to reactivate

            if silenceCounter >= HALT_DELAY*3600:
                tm.write(swap(tm.encode_string(" HALT ")))
                lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
                os.system("sudo shutdown -h now")
                break

        else:  # Sound detected
            silenceCounter = 0
            if auto_power_enabled and last_power_relay_state == 0:
                # If auto_power_enabled and sound is detected, reactivate the relay
                if is_waiting_for_sound:
                    lgpio.gpio_write(h, POWER_GPIO, lgpio.HIGH)
                    display_volume_info()
                    last_power_relay_state = lgpio.gpio_read(h, POWER_GPIO)
                    is_waiting_for_sound = False  # Reset the wait-for-sound flag
            elif not auto_power_enabled and last_power_relay_state == 1:
                # Always turn off the relay if not auto_power_enabled
                lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
                last_power_relay_state = lgpio.gpio_read(h, POWER_GPIO)


async def monitor_tv_gpio():
    """
    Monitors the TV GPIO and validates a change of state
    only after it has remained stable for 1 second.
    """
    last_state = lgpio.gpio_read(h, TV_GPIO)

    while True:
        current_state = lgpio.gpio_read(h, TV_GPIO)

        if current_state != last_state:
            # Begin confirmation period
            await asyncio.sleep(1.0)

            # Check if the state remained stable after 1 second
            confirmed_state = lgpio.gpio_read(h, TV_GPIO)
            if confirmed_state == current_state:
                # Valid change detected
                if current_state == 0:
                    await tv_on_action()
                else:
                    await tv_off_action()

                last_state = current_state

        await asyncio.sleep(0.05)

async def tv_on_action():
    await change_config(cdsp, CONFIG_DIR + '|*')

async def tv_off_action():
    await change_config(cdsp, CONFIG_DIR + '_*')


async def remote_events(device):
    global last_displayed, key_hold_counter, is_key_held, is_volume_key_held, loudness_gain_prev, last_tone_tilt, enter_display_at_press

    bass_gain_prev = treble_gain_prev  = br_direction = last_repeat_time = 0

    while True:
        try:
            # Process events asynchronously from the device
            async for event in device.async_read_loop():
                if event.type == evdev.ecodes.EV_KEY:
                    attrib = evdev.categorize(event)
                    key = attrib.keycode

                    if attrib.keystate == 1:  # Key pressed
                        is_key_held = True


                        if key in (KEY_BINDINGS['VOLUMEDOWN'], KEY_BINDINGS['VOLUMEUP']):
                            is_volume_key_held = True
                            current_volume = cdsp.volume.main_volume()
                            is_muted = cdsp.volume.main_mute()

                            if last_displayed != "volume" or blank_volume_when_mute :
                                display_volume_info(current_volume, is_muted)
                            else:
                                change_volume_from_key(cdsp, key, current_volume, is_muted)


                        elif KEY_BINDINGS['MUTE'] in key:
                            # Toggle mute status
                            is_muted = not cdsp.volume.main_mute()
                            cdsp.volume.set_main_mute(is_muted)
                            current_volume = cdsp.volume.main_volume()
                            display_volume_info(current_volume, is_muted)


                        elif key == KEY_BINDINGS['PREVIOUSSONG']:
                            # Command to play the previous song
                            send_lms_command("playlist index -1")


                        elif key == KEY_BINDINGS['NEXTSONG']:
                            # Command to play the next song
                            send_lms_command("playlist index +1")


                        elif key in (
                            KEY_BINDINGS['UP'],
                            KEY_BINDINGS['DOWN'],
                            KEY_BINDINGS['RIGHT'],
                            KEY_BINDINGS['LEFT']
                        ):
                            handle_arrow_keys(key, config_active, cdsp)


                        elif key == KEY_BINDINGS['BACK']:
                            await change_config(cdsp, CONFIG_DIR + '_*')


                        elif key == KEY_BINDINGS['HOMEPAGE']:
                            await change_config(cdsp, CONFIG_DIR + '|*')


                        elif key == KEY_BINDINGS['ENTER']:
                            # Mémorise l'écran affiché AVANT tout changement, pour l'action au relâchement (reset/restore)
                            enter_display_at_press = last_displayed
                            if last_displayed == "volume":
                                display_loudness_info()  # accès immédiat, sans délai


                    if attrib.keystate == 2:  # Key held down


                        if key in (KEY_BINDINGS['VOLUMEDOWN'], KEY_BINDINGS['VOLUMEUP']):

                                is_volume_key_held = True
                                current_time = time.time()
                                current_volume = cdsp.volume.main_volume()
                                is_muted = cdsp.volume.main_mute()

                                if key == KEY_BINDINGS['VOLUMEUP']:
                                    direction = "up"
                                    exponent = 2.0
                                    pivot = -60
                                else:
                                    direction = "down"
                                    exponent = 2.0
                                    pivot = -40

                                repeat_speed = get_repeat_speed(current_volume, direction, exponent=exponent, pivot=pivot)

                                if current_time - last_repeat_time >= repeat_speed:
                                    step = 1
                                    change_volume_from_key(cdsp, key, current_volume, is_muted, volume_step=step)
                                    last_repeat_time = current_time


                        elif key == KEY_BINDINGS['PLAYPAUSE']:
                            # Stop LMS
                            key_hold_counter += 1
                            if key_hold_counter == 10:
                                send_lms_command("stop")


                        elif key == KEY_BINDINGS['POWER']:
                            key_hold_counter += 1
                            if key_hold_counter == 2:
                                await toggle_power()
                            elif key_hold_counter == 400:
                                lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
                                tm.write(swap(tm.encode_string(" HALT ")))
                                os.system("sudo shutdown -h now")


                        elif key == KEY_BINDINGS['ENTER']:
                            key_hold_counter += 1
                            if key_hold_counter == 15:

                                if last_displayed in ("tone", "tilt"):
                                    last_tone_tilt = "tilt" if last_displayed == "tone" else "tone"
                                    (display_tilt_info if last_tone_tilt == "tilt" else display_tone_info)()



                    if attrib.keystate == 0:  # Key released


                        if key == KEY_BINDINGS['ENTER'] and key_hold_counter < 15:
                            handle_enter_press(enter_display_at_press)


                        elif key == KEY_BINDINGS['PLAYPAUSE'] and key_hold_counter < 10:
                            send_lms_command("pause")


                        elif key in (KEY_BINDINGS['VOLUMEDOWN'], KEY_BINDINGS['VOLUMEUP']):
                            display_refresh_event.set()

                        key_hold_counter = 0
                        last_repeat_time = 0

        except OSError as e:
                print(f"⚠️ Remote disconnected: {e}. Attempting to reconnect...")
                device = None

                # Reconnection loop
                while device is None:
                        for path in evdev.list_devices():
                                dev = evdev.InputDevice(path)
                                if dev.name == REMOTE_NAME:
                                        print(f"📶 Remote reconnected at {path}")
                                        device = dev
                                        device.grab()
                                        break
                        if device is None:
                                await asyncio.sleep(1)  # avoid 100% CPU usage


def exit_gracefully(signal, frame):
    """Gracefully shuts down the service, including cleanup of GPIO resources."""
    try:
        # Save audio settings
        save_audio_settings(config_active)

        lgpio.gpio_write(h, POWER_GPIO, lgpio.LOW)
        time.sleep(0.5)
        # Properly close GPIO resources
        lgpio.gpiochip_close(h)

        # Exit with a success code
        print("Gracefully shutting down the service...")
        sys.exit(0)
    except Exception as e:
        # If there's an error during cleanup, print the error and exit with a failure code
        print(f"Error during cleanup: {e}")
        sys.exit(1)

# Register the signal handler for clean shutdown
signal.signal(signal.SIGINT, exit_gracefully)  # Handle Ctrl+C (SIGINT)
signal.signal(signal.SIGTERM, exit_gracefully)  # Handle termination signal (SIGTERM)


if lgpio.gpio_read(h, TV_GPIO) == 0:
    print("TV detected ON at startup → loading ON config")
    asyncio.run(change_config(cdsp, CONFIG_DIR + '|*'))
else:
    asyncio.run(change_config(cdsp, CONFIG_DIR + '_*'))

async def main():
    asyncio.create_task(auto_poweroff())
    asyncio.create_task(display_manager_loop())
    asyncio.create_task(adc_reader_loop())
    asyncio.create_task(monitor_tv_gpio())

    for device in (remote, ):
        asyncio.create_task(remote_events(device))

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())

#EOF
