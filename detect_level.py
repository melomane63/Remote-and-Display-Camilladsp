#!/usr/bin/env python3
"""TOSLINK detection, active mode.

Same structure as monitor_tv_gpio in the main remote script:
    - a `last_source` variable holds the previous state
    - the action is triggered only when the state changes
    - the state is updated after the action

Two phases:
    - SQUEEZELITE: the Aureon device is free, we read it directly.
      Source = "TV" if level >= TV_ON_THRESHOLD_DB, else "SQUEEZELITE".
    - TV: the Aureon is held by CamillaDSP.
      Source = "SQUEEZELITE" if STALLED or silent for SILENCE_DELAY_SECONDS,
      else "TV".

Manual override:
    If the loaded config title changes on its own, the script stops acting
    until the title matches the TV reference title again.

Script-initiated switch tracking:
    After a script-initiated switch, the expected title is memorized in
    `expected_title`. When the title watcher sees this title, it recognizes
    it as the script's own change and does NOT trigger a manual override.

Transient state filtering:
    While CamillaDSP is restarting (state == "STARTING") or reports an empty
    title, the iteration is skipped.
"""

import alsaaudio
import glob
import json
import math
import os
import struct
import time

from camilladsp import CamillaClient


# --- Configuration ----------------------------------------------------------

CAPTURE_DEVICE = "hw:CARD=Aureon51MkII,DEV=0"
RATE = 48000
CHANNELS = 2
FORMAT = alsaaudio.PCM_FORMAT_S16_LE
PERIOD_SIZE = 4096
CAPTURE_SECONDS = 0.5

TV_ON_THRESHOLD_DB = -60.0
SILENCE_DB = -100.0
SILENCE_DELAY_SECONDS = 120

LOOP_INTERVAL = 1.0

CAMILLA_HOST = "127.0.0.1"
CAMILLA_PORT = 1234

CONFIG_DIR = os.path.expanduser("~") + "/camilladsp/configs/"
SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "settings.json"
)


# --- Audio helpers ----------------------------------------------------------

def amplitude_to_db(amplitude):
    return 20.0 * math.log10(amplitude) if amplitude > 0 else -1000.0


def peak_amplitude(data):
    if not data:
        return 0.0
    samples = struct.unpack(f"<{len(data)//2}h", data)
    return max((abs(s) for s in samples), default=0) / 32768.0


def open_aureon():
    try:
        pcm = alsaaudio.PCM(
            type=alsaaudio.PCM_CAPTURE,
            mode=alsaaudio.PCM_NONBLOCK,
            device=CAPTURE_DEVICE,
            channels=CHANNELS,
            rate=RATE,
            format=FORMAT,
            periodsize=PERIOD_SIZE,
        )
        return pcm, None
    except Exception as e:
        return None, e


def read_aureon_amplitude(pcm):
    peak = 0.0
    deadline = time.time() + CAPTURE_SECONDS
    while time.time() < deadline:
        length, data = pcm.read()
        if length > 0:
            peak = max(peak, peak_amplitude(data))
        else:
            time.sleep(0.01)
    return peak


# --- CamillaDSP helpers -----------------------------------------------------

def read_rms(cdsp):
    try:
        return cdsp.levels.capture_rms()
    except Exception as e:
        print(f"[ERROR] capture_rms failed: {e}")
        return None


def read_state(cdsp):
    try:
        return cdsp.general.state().name
    except Exception as e:
        return f"<error: {e}>"


def read_title(cdsp):
    try:
        title = cdsp.config.title()
        return title if title else None
    except Exception:
        return None


def read_title_from_file(cdsp, pattern):
    """Read the title field of the first config file matching the pattern."""
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    try:
        data = cdsp.config.read_and_parse_file(files[0])
        if isinstance(data, dict):
            title = data.get("title")
            return title if title else None
    except Exception as e:
        print(f"[ERROR] Cannot read {files[0]}: {e}")
    return None


def levels_to_list(levels):
    if levels is None:
        return []
    if isinstance(levels, (int, float)):
        return [float(levels)]
    values = list(levels.values()) if isinstance(levels, dict) else list(levels)
    return [float(v) for v in values]


def all_silent(levels, threshold=SILENCE_DB):
    values = levels_to_list(levels)
    return bool(values) and all(v <= threshold for v in values)


def format_levels(levels):
    if levels is None:
        return "None"
    if isinstance(levels, (int, float)):
        return f"{levels:+.2f} dB"
    values = list(levels.values()) if isinstance(levels, dict) else list(levels)
    return "  ".join(f"{float(v):+7.2f}" for v in values)


# --- Settings ---------------------------------------------------------------

def config_key(path):
    return os.path.splitext(os.path.basename(path))[0]


def load_last_config():
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        last = data.get("last_config")
        return last if isinstance(last, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_last_config(last_config):
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["last_config"] = last_config
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp, SETTINGS_PATH)


# --- Config switching -------------------------------------------------------

def change_config(cdsp, config_pattern, last_config, current_title):
    """Load the last-used config of the group (or the first one).

    Does nothing if the target's title is already the loaded title.
    Returns the target title if a switch was performed, None otherwise.
    """
    group = os.path.basename(config_pattern)[:1]
    config_files = sorted(glob.glob(config_pattern))
    if not config_files:
        print(f"[CONFIG] No file matching {config_pattern}")
        return None

    last_key = last_config.get(group)
    target = next(
        (p for p in config_files if config_key(p) == last_key),
        config_files[0],
    )

    target_data = cdsp.config.read_and_parse_file(target)
    if not isinstance(target_data, dict):
        print(f"[CONFIG] Invalid config: {target}")
        return None
    target_title = target_data.get("title")

    if target_title is not None and current_title is not None and target_title == current_title:
        return None   # already loaded

    print(f"[CONFIG] Switch: -> {target} (title={target_title!r})")

    config_dir = os.path.dirname(os.path.abspath(target))
    for f in (target_data.get("filters") or {}).values():
        filename = f.get("parameters", {}).get("filename")
        if isinstance(filename, str) and filename and not os.path.isabs(filename):
            f["parameters"]["filename"] = os.path.normpath(
                os.path.join(config_dir, filename)
            )

    try:
        cdsp.config.set_active(target_data)
        cdsp.config.set_file_path(target)
    except Exception as e:
        print(f"[CONFIG] Failed to apply {target}: {e}")
        return None

    last_config[group] = config_key(target)
    save_last_config(last_config)
    return target_title


def pattern_for_source(source):
    """Return the glob pattern matching the given source."""
    return CONFIG_DIR + ("|*" if source == "TV" else "_*")


# --- Main -------------------------------------------------------------------

def main():
    cdsp = CamillaClient(CAMILLA_HOST, CAMILLA_PORT)
    cdsp.connect()

    last_config = load_last_config()

    tv_title = read_title_from_file(cdsp, CONFIG_DIR + "|*")
    print(f"TV reference title: {tv_title!r}")

    last_title = read_title(cdsp)
    print(f"Loaded title: {last_title!r}")
    manual_override = False

    # Title expected after a script-initiated switch. When the watcher sees
    # this title, it recognizes the script's own change instead of a manual one.
    expected_title = None

    last_source = None
    last_phase = None
    silence_counter = 0

    print("TOSLINK detection started (ACTIVE)")
    print(f"Device: {CAPTURE_DEVICE}")
    print(f"TV ON threshold: {TV_ON_THRESHOLD_DB} dB")
    print(f"Silence threshold: {SILENCE_DB} dB, delay {SILENCE_DELAY_SECONDS} s")
    print(f"Config dir: {CONFIG_DIR}")
    print()

    while True:
        # --- 0. Ignore iterations while CamillaDSP is restarting ---
        state = read_state(cdsp)
        if state == "STARTING":
            time.sleep(LOOP_INTERVAL)
            continue

        # --- 1. Watch the title for external changes ---
        current_title = read_title(cdsp)
        if current_title is None:
            time.sleep(LOOP_INTERVAL)
            continue

        if current_title != last_title:
            if expected_title is not None and current_title == expected_title:
                # Our own switch, confirmed
                print(f"[TITLE] Script switch confirmed: {current_title!r}")
                last_title = current_title
                expected_title = None
            else:
                # External change
                print(f"[MANUAL] External config change: {last_title!r} -> {current_title!r}")
                manual_override = True
                last_title = current_title

        # --- 2. Resume when the loaded title matches the TV reference ---
        if manual_override and tv_title is not None and current_title == tv_title:
            print(f"[MANUAL] Return to TV config ({tv_title!r}), resuming")
            manual_override = False
            last_source = "TV"

        # --- 3. Manual override: do nothing ---
        if manual_override:
            time.sleep(LOOP_INTERVAL)
            continue

        # --- 4. Detect source (like reading the TV GPIO) ---
        pcm, err = open_aureon()

        if pcm is not None:
            # --- SQUEEZELITE phase ---
            if last_phase != "SQUEEZELITE":
                last_phase = "SQUEEZELITE"
                silence_counter = 0
                print("--- phase: SQUEEZELITE (Aureon available) ---")

            amp = read_aureon_amplitude(pcm)
            pcm.close()
            db = amplitude_to_db(amp)

            if db >= TV_ON_THRESHOLD_DB:
                current_source = "TV"
                verdict = "TV ON"
            else:
                current_source = "SQUEEZELITE"
                verdict = "TV OFF"

            print(f"[SQUEEZELITE] amp={amp:.6f} ({db:+.2f} dB) -> {verdict}")

            if current_source != last_source:
                pattern = pattern_for_source(current_source)
                new_title = change_config(cdsp, pattern, last_config, last_title)
                if new_title is not None:
                    expected_title = new_title
                last_source = current_source

        else:
            # --- TV phase ---
            if last_phase != "TV":
                last_phase = "TV"
                silence_counter = 0
                print(f"--- phase: TV (Aureon busy: {err}) ---")

            rms = read_rms(cdsp)
            state_name = read_state(cdsp)
            silent = all_silent(rms)
            stalled = (state_name == "STALLED")

            if stalled:
                current_source = "SQUEEZELITE"
                flag = "  <-- STALLED"
                silence_counter = 0
            elif silent:
                silence_counter += 1
                if silence_counter >= SILENCE_DELAY_SECONDS:
                    current_source = "SQUEEZELITE"
                    flag = f"  <-- SILENCE {silence_counter}s"
                else:
                    current_source = "TV"
                    flag = f"  (silent {silence_counter}/{SILENCE_DELAY_SECONDS}s)"
            else:
                silence_counter = 0
                current_source = "TV"
                flag = ""

            print(f"[TV] state={state_name:10s} silent={silent} stalled={stalled}{flag}")
            print(f"     RMS : {format_levels(rms)}")

            if current_source != last_source:
                pattern = pattern_for_source(current_source)
                new_title = change_config(cdsp, pattern, last_config, last_title)
                if new_title is not None:
                    expected_title = new_title
                last_source = current_source

        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
