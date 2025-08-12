import contextlib
import ctypes
import datetime
import json
import math
import os
import platform
import shlex
import code
import io
import traceback
import subprocess, signal
from pathlib import Path
import threading
from threading import Lock
import time
from typing import Any, Optional, Sequence
from typing import List, Dict, Tuple
import concurrent.futures

import Xlib
import lxml.etree
import pyautogui
try:
    import pyperclip
except ImportError:                      # library not installed
    pyperclip = None
import requests
import re
from PIL import Image, ImageGrab
from Xlib import display, X
from flask import Flask, request, jsonify, send_file, abort
from lxml.etree import _Element

from werkzeug.utils import secure_filename



platform_name: str = platform.system()

if platform_name == "Linux":
    import pyatspi
    from pyatspi import Accessible, StateType, STATE_SHOWING
    from pyatspi import Action as ATAction
    from pyatspi import Component  # , Document
    from pyatspi import Text as ATText
    from pyatspi import Value as ATValue

    BaseWrapper = Any

elif platform_name == "Windows":
    from pywinauto import Desktop
    from pywinauto.base_wrapper import BaseWrapper
    import pywinauto.application
    import win32ui, win32gui

    Accessible = Any

elif platform_name == "Darwin":
    import plistlib

    import AppKit
    import ApplicationServices
    import Foundation
    import Quartz
    import oa_atomacos

    Accessible = Any
    BaseWrapper = Any

else:
    # Platform not supported
    Accessible = None
    BaseWrapper = Any

from pyxcursor import Xcursor

# todo: need to reformat and organize this whole file

# ↓ new, optional third-party helpers (add to requirements.txt):
from pynput import keyboard, mouse          # cross-platform hooks

# trajectory-recording state
trajectory_lock      = threading.Lock()
trajectory_is_alive  = False
trajectory_events: list[dict] = []
trajectory_thread    = None
trajectory_file_path = None        # will hold "…/trajectory_YYYYmmdd_HHMMSS.json"
DOUBLE_CLICK_MS   = 350            # two left clicks ≤ this Δt → LeftDouble
DRAG_MIN_PX       = 15              # press→release disp ≥ this → Drag
TYPE_FLUSH_MS     = 400            # pause ≥ this → flush typing buffer

# mouse
last_down          = {}            # {button: (t, x, y)}
last_left_click    = (0, None)     # (t, (x,y))
move_since_down    = {}

# keyboard
pressed_now        = set()         # live modifiers / chars
combo_buffer       = set()         # what's been in the combo so far
type_buffer        = []            # accumulating printable chars
last_type_ts       = 0.0

WORD_DELIMS       = {" ", "\t", "\n", "\r"}
WORD_CHARS        = set("abcdefghijklmnopqrstuvwxyz"
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                        "0123456789-'")
word_buffer       = []          # accumulating characters of the current word
last_word_ts      = 0.0
# recognise space/enter/tab explicitly (pynput sends them as Key.xxx)
DELIM_KEYS        = [
    keyboard.Key.enter,
    keyboard.Key.tab,
    keyboard.Key.space,
]

# ───────── replay state ─────────
replay_is_alive   = False
replay_thread     = None
MOUSE_CTRL        = mouse.Controller()
KEY_CTRL          = keyboard.Controller()
KEY_MAP           = {                 # minimal map for special names we emit
    "ctrl":  keyboard.Key.ctrl,
    "alt":   keyboard.Key.alt,
    "shift": keyboard.Key.shift,
    "cmd":   keyboard.Key.cmd,
    "enter": keyboard.Key.enter,
    "tab":   keyboard.Key.tab,
    "esc":   keyboard.Key.esc,
    "space": keyboard.Key.space,
    "backspace": keyboard.Key.backspace,
}

# clipboard
clipboard_cache    = None
clipboard_enabled = pyperclip is not None

app = Flask(__name__)

pyautogui.PAUSE = 0
pyautogui.DARWIN_CATCH_UP_TIME = 0

logger = app.logger
recording_process = None  # fixme: this is a temporary solution for recording, need to be changed to support multiple-process
recording_path = "/tmp/recording.mp4"


console = code.InteractiveConsole(locals={})
console_lock = Lock()


def _safe_clipboard_paste() -> str | None:
    """Return clipboard text or None; never raises."""
    if pyperclip is None:
        return None
    with contextlib.suppress(Exception):     # PyperclipException etc.
        return pyperclip.paste()
    return None

def _flush_word_buffer(ts_end: float | None = None):
    """Emit a word-level Type() and clear the buffer."""
    global word_buffer
    if word_buffer:
        _append_event("Type", {"word": "".join(word_buffer)}, ts=ts_end)
        word_buffer.clear()

def _append_event(op: str, payload: dict, ts: float | None = None):
    with trajectory_lock:
        if trajectory_is_alive:
            event = {"ts": ts or time.time(), "op": op}
            event.update(payload)
            trajectory_events.append(event)

def _dist(p1, p2):
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])

def on_click(x, y, button, pressed):
    global last_left_click
    now = time.time()

    # ───── press ─────
    if pressed:
        last_down[button] = (now, x, y)
        move_since_down[button] = False
        return

    # ───── release ─────
    down_t, x0, y0 = last_down.pop(button, (now, x, y))
    moved          = move_since_down.pop(button, False)
    dt_ms          = (now - down_t) * 1000

    # Drag?
    if moved and _dist((x0, y0), (x, y)) >= DRAG_MIN_PX:
        _append_event("Drag", {"x1": x0, "y1": y0, "x2": x, "y2": y}, ts=now)
        return

    # Click variants
    if button.name == "left":
        # Double-click?
        last_t, last_xy = last_left_click
        if last_xy and _dist(last_xy, (x, y)) < DRAG_MIN_PX and (now - last_t) * 1000 <= DOUBLE_CLICK_MS:
            _append_event("LeftDouble", {"x": x, "y": y}, ts=now)
            last_left_click = (0, None)          # reset
        else:
            _append_event("Click", {"x": x, "y": y}, ts=now)
            last_left_click = (now, (x, y))
    elif button.name == "right":
        _append_event("RightSingle", {"x": x, "y": y}, ts=now)

def on_move(x, y):
    global move_since_down
    # mark drag candidate
    for btn in move_since_down:
        move_since_down[btn] = True

def on_scroll(x, y, dx, dy):
    direc = "down" if dy < 0 else "up"
    _append_event("Scroll", {"x": x, "y": y, "direction": direc})

def on_key_press(k):
    global last_word_ts
    now = time.time()
    key_str = str(k).strip("'")        # pynput → "'a'"  →  a

    pressed_now.add(key_str)
    combo_buffer.add(key_str)

    # ------------------------------------------------------------
    # 1)  word delimiters  (space / enter / tab)
    # ------------------------------------------------------------
    if k in DELIM_KEYS:
        _append_event("Hotkey", {"key": key_str}, ts=now)  # record delimiter
        _flush_word_buffer(ts_end=now)
        last_word_ts = now
        return

    # ------------------------------------------------------------
    # 2) printable, single-character keycode  (letters, digits, etc.)
    # ------------------------------------------------------------
    if len(key_str) == 1 and key_str.isprintable():
        if key_str in WORD_DELIMS:
            _flush_word_buffer(ts_end=now)
        elif key_str in WORD_CHARS:
            word_buffer.append(key_str)
            last_word_ts = now
        else:                          # punctuation "." "," "?"
            _flush_word_buffer(ts_end=now)
            _append_event("Type", {"word": key_str}, ts=now)
        return

def on_key_release(k):
    global last_word_ts
    now = time.time()
    key_str = str(k).strip("'")
    pressed_now.discard(key_str)

    # If all keys are up and the user paused, flush any unfinished word
    if not pressed_now and (now - last_word_ts) * 1000 > TYPE_FLUSH_MS:
        _flush_word_buffer(ts_end=now)

    # end of combo?
    if not pressed_now and combo_buffer:
        keys = sorted(combo_buffer)
        combo_buffer.clear()

        # single special ⇒ Hotkey
        if len(keys) == 1:
            _append_event("Hotkey", {"key": keys[0]}, ts=now)
        else:
            clip_delta = None
            if clipboard_enabled:
                clip_now = _safe_clipboard_paste()
                global clipboard_cache
                if clipboard_cache is None:
                    clipboard_cache = clip_now          # first run
                elif clip_now != clipboard_cache:
                    # something changed after the combo
                    clip_delta = clip_now if clip_now else "image"
                    clipboard_cache = clip_now

            payload = {"keys": keys}
            if clip_delta is not None:
                payload["clipboard"] = clip_delta

            _append_event("CombKey", payload, ts=now)


def _run_pynput_listener():
    """
    Background daemon that pumps keyboard + mouse events into the
    high-level logger.  Exits cleanly when `trajectory_is_alive` flips
    to False.
    """
    if keyboard is None or mouse is None:
        raise RuntimeError("Please `pip install pynput` inside the VM.")

    # ── build the two listeners ───────────────────────────────────────
    kb_listener = keyboard.Listener(
        on_press=on_key_press,
        on_release=on_key_release,
        suppress=False          # never block OS behaviour
    )
    ms_listener = mouse.Listener(
        on_click=on_click,
        on_move=on_move,
        on_scroll=on_scroll,    # ← you *must* wire this in too
        suppress=False
    )

    # ── fire them up ──────────────────────────────────────────────────
    kb_listener.start()
    ms_listener.start()

    # ── stay alive until `/end_trajectory` toggles the flag ───────────
    try:
        while trajectory_is_alive and (
              kb_listener.is_alive() and ms_listener.is_alive()):
            time.sleep(0.05)
    finally:
        kb_listener.stop()
        ms_listener.stop()
        kb_listener.join(1)
        ms_listener.join(1)


@app.route('/start_trajectory', methods=['POST'])
def start_trajectory():
    global trajectory_is_alive, trajectory_thread, trajectory_events, trajectory_file_path

    if trajectory_is_alive:
        return jsonify({"status": "error", "msg": "Trajectory recording already running"}), 400

    # reset state
    with trajectory_lock:
        trajectory_events.clear()
    trajectory_is_alive  = True
    trajectory_file_path = f"/tmp/trajectory_{datetime.datetime.now():%Y%m%d_%H%M%S}.json"

    # fire-and-forget background listener
    trajectory_thread = threading.Thread(target=_run_pynput_listener, daemon=True)
    trajectory_thread.start()

    return jsonify({"status": "success", "msg": "Trajectory recording started"})


@app.route('/end_trajectory', methods=['POST'])
def end_trajectory():
    global trajectory_is_alive, trajectory_thread, trajectory_file_path

    if not trajectory_is_alive:
        return jsonify({"status": "error", "msg": "No active trajectory recording"}), 400

    # flip the flag → listener thread exits its loop gracefully
    trajectory_is_alive = False
    trajectory_thread.join(timeout=2)
    _flush_word_buffer()

    return jsonify({"status": "success", "msg": "Trajectory recording started", "trajectories": trajectory_events})


def _clean_key(key_str: str) -> str:
    """
    Cleans pynput key names for pyautogui.
    e.g. "Key.ctrl" -> "ctrl"
    """
    return key_str.strip("'").replace('Key.', '')


def _replay_trajectory_events(events: list[dict]):
    """
    Takes a recorded trajectory and simulates the user inputs.
    Blocks until all events have been replayed.
    """
    logs = []
    if not events:
        return logs

    # sort by timestamp and prepare for timed replay
    events.sort(key=lambda e: e['ts'])
    start_time = time.time()
    start_ts = events[0]['ts']
    logs.append(f"Starting trajectory replay. Events: {len(events)}")

    for i, event in enumerate(events):
        # sync with wall-clock time
        target_time = start_time + (event['ts'] - start_ts)
        sleep_for = target_time - time.time()
        if sleep_for > 0:
            time.sleep(sleep_for)

        # execute action
        op = event['op']
        logs.append(f"Executing event {i+1}/{len(events)}: {op} with params {event}")
        if op == "Click":
            pyautogui.click(x=event['x'], y=event['y'])
        elif op == "LeftDouble":
            pyautogui.doubleClick(x=event['x'], y=event['y'])
        elif op == "RightSingle":
            pyautogui.rightClick(x=event['x'], y=event['y'])
        elif op == "Drag":
            pyautogui.moveTo(event['x1'], event['y1'], _pause=False)
            pyautogui.dragTo(event['x2'], event['y2'], duration=0.5, button='left', _pause=False)
        elif op == "Scroll":
            # scroll amount is in "clicks" - arbitrary unit
            scroll_dist = 120 if event['direction'] == 'up' else -120
            pyautogui.scroll(scroll_dist, x=event['x'], y=event['y'])
        # elif op == "Type":
        #     # interval between chars makes it feel more real
        #     pyautogui.write(event['word'], interval=0.01)
        elif op == "Hotkey":
            pyautogui.press(_clean_key(event['key']))
        elif op == "CombKey":
            clean_keys = [_clean_key(k) for k in event['keys']]
            pyautogui.hotkey(*clean_keys)
        elif op == "Command":
            result = subprocess.run(
                event["command"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                text=True,
                timeout=3,
                creationflags=0,
            )
            logs.append(f"Command executed. stdout: {result.stdout}, stderr: {result.stderr}")
    logs.append("Trajectory replay finished.")
    return logs


@app.route('/play_trajectory', methods=['POST'])
def play_trajectory():
    if 'file' not in request.files:
        return jsonify({"status": "error", "msg": "No trajectory file part"}), 400
    
    file = request.files['file']
    if not file or not file.filename:
        return jsonify({"status": "error", "msg": "No selected trajectory file"}), 400

    try:
        # content is bytes, decode to string, then parse
        content = file.read().decode('utf-8')
        events = json.loads(content)
        
        logs = _replay_trajectory_events(events) # This will block
        
        return jsonify({"status": "success", "msg": "Trajectory replayed", "log": logs})
    except Exception as e:
        logger.error(f"Error replaying trajectory: {e}")
        return jsonify({"status": "error", "msg": f"Error replaying trajectory: {e}"}), 500


@app.route('/stop_replay', methods=['POST'])
def stop_replay():
    global replay_is_alive
    replay_is_alive = False
    return jsonify({"status": "success", "msg": "replay stopped"})


@app.route('/setup/execute', methods=['POST'])
@app.route('/execute', methods=['POST'])
def execute_command():
    data = request.json
    # The 'command' key in the JSON request should contain the command to be executed.
    shell = data.get('shell', False)
    command = data.get('command', "" if shell else [])
    
    # Handle single command (preserve existing behavior)
    if isinstance(command, str) and not shell:
        command = shlex.split(command)

    # Expand user directory
    if isinstance(command, list):
        for i, arg in enumerate(command):
            if arg.startswith("~/"):
                command[i] = os.path.expanduser(arg)
    else:
        command = os.path.expanduser(command) if command.startswith("~/") else command

    # Execute the command without any safety checks.
    try:
        if platform_name == "Windows":
            flags = subprocess.CREATE_NO_WINDOW
        else:
            flags = 0
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=shell,
            text=True,
            timeout=3,
            creationflags=flags,
        )
        if "pyautogui" not in str(command):
            _append_event("Command", 
                          {"command": command, "output": result.stdout, "error": result.stderr, "returncode": result.returncode}, 
                          ts=time.time())

        return jsonify({
            'status': 'success',
            'output': result.stdout,
            'error': result.stderr,
            'returncode': result.returncode
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


def _get_machine_architecture() -> str:
    """ Get the machine architecture, e.g., x86_64, arm64, aarch64, i386, etc.
    """
    architecture = platform.machine().lower()
    if architecture in ['amd32', 'amd64', 'x86', 'x86_64', 'x86-64', 'x64', 'i386', 'i686']:
        return 'amd'
    elif architecture in ['arm64', 'aarch64', 'aarch32']:
        return 'arm'
    else:
        return 'unknown'


@app.route('/setup/launch', methods=["POST"])
def launch_app():
    data = request.json
    shell = data.get("shell", False)
    command: List[str] = data.get("command", "" if shell else [])

    if isinstance(command, str) and not shell:
        command = shlex.split(command)

    # Expand user directory
    for i, arg in enumerate(command):
        if arg.startswith("~/"):
            command[i] = os.path.expanduser(arg)

    try:
        if 'google-chrome' in command and _get_machine_architecture() == 'arm':
            index = command.index('google-chrome')
            command[index] = 'chromium'  # arm64 chrome is not available yet, can only use chromium
        subprocess.Popen(command, shell=shell)
        return "{:} launched successfully".format(command if shell else " ".join(command))
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/screenshot', methods=['GET'])
def capture_screen_with_cursor():
    # fixme: when running on virtual machines, the cursor is not captured, don't know why

    file_path = os.path.join(os.path.dirname(__file__), "screenshots", "screenshot.png")
    user_platform = platform.system()

    # Ensure the screenshots directory exists
    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    # fixme: This is a temporary fix for the cursor not being captured on Windows and Linux
    if user_platform == "Windows":
        def get_cursor():
            hcursor = win32gui.GetCursorInfo()[1]
            hdc = win32ui.CreateDCFromHandle(win32gui.GetDC(0))
            hbmp = win32ui.CreateBitmap()
            hbmp.CreateCompatibleBitmap(hdc, 36, 36)
            hdc = hdc.CreateCompatibleDC()
            hdc.SelectObject(hbmp)
            hdc.DrawIcon((0,0), hcursor)

            bmpinfo = hbmp.GetInfo()
            bmpstr = hbmp.GetBitmapBits(True)
            cursor = Image.frombuffer('RGB', (bmpinfo['bmWidth'], bmpinfo['bmHeight']), bmpstr, 'raw', 'BGRX', 0, 1).convert("RGBA")

            win32gui.DestroyIcon(hcursor)
            win32gui.DeleteObject(hbmp.GetHandle())
            hdc.DeleteDC()

            pixdata = cursor.load()

            width, height = cursor.size
            for y in range(height):
                for x in range(width):
                    if pixdata[x, y] == (0, 0, 0, 255):
                        pixdata[x, y] = (0, 0, 0, 0)

            hotspot = win32gui.GetIconInfo(hcursor)[1:3]

            return (cursor, hotspot)

        ratio = ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100

        img = ImageGrab.grab(bbox=None, include_layered_windows=True)

        try:
            cursor, (hotspotx, hotspoty) = get_cursor()

            pos_win = win32gui.GetCursorPos()
            pos = (round(pos_win[0]*ratio - hotspotx), round(pos_win[1]*ratio - hotspoty))

            img.paste(cursor, pos, cursor)
        except:
            pass

        img.save(file_path)
    elif user_platform == "Linux":
        cursor_obj = Xcursor()
        imgarray = cursor_obj.getCursorImageArrayFast()
        cursor_img = Image.fromarray(imgarray)
        screenshot = pyautogui.screenshot()
        cursor_x, cursor_y = pyautogui.position()
        screenshot.paste(cursor_img, (cursor_x, cursor_y), cursor_img)
        screenshot.save(file_path)
    elif user_platform == "Darwin":  # (Mac OS)
        # Use the screencapture utility to capture the screen with the cursor
        subprocess.run(["screencapture", "-C", file_path])
    else:
        logger.warning(f"The platform you're using ({user_platform}) is not currently supported")

    return send_file(file_path, mimetype='image/png')


def _has_active_terminal(desktop: Accessible) -> bool:
    """ A quick check whether the terminal window is open and active.
    """
    for app in desktop:
        if app.getRoleName() == "application" and app.name == "gnome-terminal-server":
            for frame in app:
                if frame.getRoleName() == "frame" and frame.getState().contains(pyatspi.STATE_ACTIVE):
                    return True
    return False


@app.route('/terminal', methods=['GET'])
def get_terminal_output():
    user_platform = platform.system()
    output: Optional[str] = None
    try:
        if user_platform == "Linux":
            desktop: Accessible = pyatspi.Registry.getDesktop(0)
            if _has_active_terminal(desktop):
                desktop_xml: _Element = _create_atspi_node(desktop)
                # 1. the terminal window (frame of application is st:active) is open and active
                # 2. the terminal tab (terminal status is st:focused) is focused
                xpath = '//application[@name="gnome-terminal-server"]/frame[@st:active="true"]//terminal[@st:focused="true"]'
                terminals: List[_Element] = desktop_xml.xpath(xpath, namespaces=_accessibility_ns_map_ubuntu)
                output = terminals[0].text.rstrip() if len(terminals) == 1 else None
        else:  # windows and macos platform is not implemented currently
            # raise NotImplementedError
            return "Currently not implemented for platform {:}.".format(platform.platform()), 500
        return jsonify({"output": output, "status": "success"})
    except Exception as e:
        logger.error("Failed to get terminal output. Error: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


_accessibility_ns_map = {
    "ubuntu": {
        "st": "https://accessibility.ubuntu.example.org/ns/state",
        "attr": "https://accessibility.ubuntu.example.org/ns/attributes",
        "cp": "https://accessibility.ubuntu.example.org/ns/component",
        "doc": "https://accessibility.ubuntu.example.org/ns/document",
        "docattr": "https://accessibility.ubuntu.example.org/ns/document/attributes",
        "txt": "https://accessibility.ubuntu.example.org/ns/text",
        "val": "https://accessibility.ubuntu.example.org/ns/value",
        "act": "https://accessibility.ubuntu.example.org/ns/action",
    },
    "windows": {
        "st": "https://accessibility.windows.example.org/ns/state",
        "attr": "https://accessibility.windows.example.org/ns/attributes",
        "cp": "https://accessibility.windows.example.org/ns/component",
        "doc": "https://accessibility.windows.example.org/ns/document",
        "docattr": "https://accessibility.windows.example.org/ns/document/attributes",
        "txt": "https://accessibility.windows.example.org/ns/text",
        "val": "https://accessibility.windows.example.org/ns/value",
        "act": "https://accessibility.windows.example.org/ns/action",
        "class": "https://accessibility.windows.example.org/ns/class"
    },
    "macos": {
        "st": "https://accessibility.macos.example.org/ns/state",
        "attr": "https://accessibility.macos.example.org/ns/attributes",
        "cp": "https://accessibility.macos.example.org/ns/component",
        "doc": "https://accessibility.macos.example.org/ns/document",
        "txt": "https://accessibility.macos.example.org/ns/text",
        "val": "https://accessibility.macos.example.org/ns/value",
        "act": "https://accessibility.macos.example.org/ns/action",
        "role": "https://accessibility.macos.example.org/ns/role",
    }

}

_accessibility_ns_map_ubuntu = _accessibility_ns_map['ubuntu']
_accessibility_ns_map_windows = _accessibility_ns_map['windows']
_accessibility_ns_map_macos = _accessibility_ns_map['macos']

# A11y tree getter for Ubuntu
libreoffice_version_tuple: Optional[Tuple[int, ...]] = None
MAX_DEPTH = 50
MAX_WIDTH = 1024
MAX_CALLS = 5000


def _get_libreoffice_version() -> Tuple[int, ...]:
    """Function to get the LibreOffice version as a tuple of integers."""
    result = subprocess.run("libreoffice --version", shell=True, text=True, stdout=subprocess.PIPE)
    version_str = result.stdout.split()[1]  # Assuming version is the second word in the command output
    return tuple(map(int, version_str.split(".")))


def _create_atspi_node(node: Accessible, depth: int = 0, flag: Optional[str] = None) -> _Element:
    node_name = node.name
    attribute_dict: Dict[str, Any] = {"name": node_name}

    #  States
    states: List[StateType] = node.getState().get_states()
    for st in states:
        state_name: str = StateType._enum_lookup[st]
        state_name: str = state_name.split("_", maxsplit=1)[1].lower()
        if len(state_name) == 0:
            continue
        attribute_dict["{{{:}}}{:}".format(_accessibility_ns_map_ubuntu["st"], state_name)] = "true"

    #  Attributes
    attributes: Dict[str, str] = node.get_attributes()
    for attribute_name, attribute_value in attributes.items():
        if len(attribute_name) == 0:
            continue
        attribute_dict["{{{:}}}{:}".format(_accessibility_ns_map_ubuntu["attr"], attribute_name)] = attribute_value

    #  Component
    if attribute_dict.get("{{{:}}}visible".format(_accessibility_ns_map_ubuntu["st"]), "false") == "true" \
            and attribute_dict.get("{{{:}}}showing".format(_accessibility_ns_map_ubuntu["st"]), "false") == "true":
        try:
            component: Component = node.queryComponent()
        except NotImplementedError:
            pass
        else:
            bbox: Sequence[int] = component.getExtents(pyatspi.XY_SCREEN)
            attribute_dict["{{{:}}}screencoord".format(_accessibility_ns_map_ubuntu["cp"])] = \
                str(tuple(bbox[0:2]))
            attribute_dict["{{{:}}}size".format(_accessibility_ns_map_ubuntu["cp"])] = str(tuple(bbox[2:]))

    text = ""
    #  Text
    try:
        text_obj: ATText = node.queryText()
        # only text shown on current screen is available
        # attribute_dict["txt:text"] = text_obj.getText(0, text_obj.characterCount)
        text: str = text_obj.getText(0, text_obj.characterCount)
        # if flag=="thunderbird":
        # appeared in thunderbird (uFFFC) (not only in thunderbird), "Object
        # Replacement Character" in Unicode, "used as placeholder in text for
        # an otherwise unspecified object; uFFFD is another "Replacement
        # Character", just in case
        text = text.replace("\ufffc", "").replace("\ufffd", "")
    except NotImplementedError:
        pass

    #  Image, Selection, Value, Action
    try:
        node.queryImage()
        attribute_dict["image"] = "true"
    except NotImplementedError:
        pass

    try:
        node.querySelection()
        attribute_dict["selection"] = "true"
    except NotImplementedError:
        pass

    try:
        value: ATValue = node.queryValue()
        value_key = f"{{{_accessibility_ns_map_ubuntu['val']}}}"

        for attr_name, attr_func in [
            ("value", lambda: value.currentValue),
            ("min", lambda: value.minimumValue),
            ("max", lambda: value.maximumValue),
            ("step", lambda: value.minimumIncrement)
        ]:
            try:
                attribute_dict[f"{value_key}{attr_name}"] = str(attr_func())
            except:
                pass
    except NotImplementedError:
        pass

    try:
        action: ATAction = node.queryAction()
        for i in range(action.nActions):
            action_name: str = action.getName(i).replace(" ", "-")
            attribute_dict[
                "{{{:}}}{:}_desc".format(_accessibility_ns_map_ubuntu["act"], action_name)] = action.getDescription(
                i)
            attribute_dict[
                "{{{:}}}{:}_kb".format(_accessibility_ns_map_ubuntu["act"], action_name)] = action.getKeyBinding(i)
    except NotImplementedError:
        pass

    # Add from here if we need more attributes in the future...

    raw_role_name: str = node.getRoleName().strip()
    node_role_name = (raw_role_name or "unknown").replace(" ", "-")

    if not flag:
        if raw_role_name == "document spreadsheet":
            flag = "calc"
        if raw_role_name == "application" and node.name == "Thunderbird":
            flag = "thunderbird"

    xml_node = lxml.etree.Element(
        node_role_name,
        attrib=attribute_dict,
        nsmap=_accessibility_ns_map_ubuntu
    )

    if len(text) > 0:
        xml_node.text = text

    if depth == MAX_DEPTH:
        logger.warning("Max depth reached")
        return xml_node

    if flag == "calc" and node_role_name == "table":
        # Maximum column: 1024 if ver<=7.3 else 16384
        # Maximum row: 104 8576
        # Maximun sheet: 1 0000

        global libreoffice_version_tuple
        MAXIMUN_COLUMN = 1024 if libreoffice_version_tuple < (7, 4) else 16384
        MAX_ROW = 104_8576

        index_base = 0
        first_showing = False
        column_base = None
        for r in range(MAX_ROW):
            for clm in range(column_base or 0, MAXIMUN_COLUMN):
                child_node: Accessible = node[index_base + clm]
                showing: bool = child_node.getState().contains(STATE_SHOWING)
                if showing:
                    child_node: _Element = _create_atspi_node(child_node, depth + 1, flag)
                    if not first_showing:
                        column_base = clm
                        first_showing = True
                    xml_node.append(child_node)
                elif first_showing and column_base is not None or clm >= 500:
                    break
            if first_showing and clm == column_base or not first_showing and r >= 500:
                break
            index_base += MAXIMUN_COLUMN
        return xml_node
    else:
        try:
            for i, ch in enumerate(node):
                if i == MAX_WIDTH:
                    logger.warning("Max width reached")
                    break
                xml_node.append(_create_atspi_node(ch, depth + 1, flag))
        except:
            logger.warning("Error occurred during children traversing. Has Ignored. Node: %s",
                           lxml.etree.tostring(xml_node, encoding="unicode"))
        return xml_node


# A11y tree getter for Windows
def _create_pywinauto_node(node, nodes, depth: int = 0, flag: Optional[str] = None) -> _Element:
    nodes = nodes or set()
    if node in nodes:
        return
    nodes.add(node)

    attribute_dict: Dict[str, Any] = {"name": node.element_info.name}

    base_properties = {}
    try:
        base_properties.update(
            node.get_properties())  # get all writable/not writable properties, but have bugs when landing on chrome and it's slower!
    except:
        logger.debug("Failed to call get_properties(), trying to get writable properites")
        try:
            _element_class = node.__class__

            class TempElement(node.__class__):
                writable_props = pywinauto.base_wrapper.BaseWrapper.writable_props

            # Instantiate the subclass
            node.__class__ = TempElement
            # Retrieve properties using get_properties()
            properties = node.get_properties()
            node.__class__ = _element_class

            base_properties.update(properties)  # only get all writable properties
            logger.debug("get writable properties")
        except Exception as e:
            logger.error(e)
            pass

    # Count-cnt
    for attr_name in ["control_count", "button_count", "item_count", "column_count"]:
        try:
            attribute_dict[f"{{{_accessibility_ns_map_windows['cnt']}}}{attr_name}"] = base_properties[
                attr_name].lower()
        except:
            pass

    # Columns-cols
    try:
        attribute_dict[f"{{{_accessibility_ns_map_windows['cols']}}}columns"] = base_properties["columns"].lower()
    except:
        pass

    # Id-id
    for attr_name in ["control_id", "automation_id", "window_id"]:
        try:
            attribute_dict[f"{{{_accessibility_ns_map_windows['id']}}}{attr_name}"] = base_properties[attr_name].lower()
        except:
            pass

    #  States
    # 19 sec out of 20
    for attr_name, attr_func in [
        ("enabled", lambda: node.is_enabled()),
        ("visible", lambda: node.is_visible()),
        # ("active", lambda: node.is_active()), # occupied most of the time: 20s out of 21s for slack, 51.5s out of 54s for WeChat # maybe use for cutting branches
        ("minimized", lambda: node.is_minimized()),
        ("maximized", lambda: node.is_maximized()),
        ("normal", lambda: node.is_normal()),
        ("unicode", lambda: node.is_unicode()),
        ("collapsed", lambda: node.is_collapsed()),
        ("checkable", lambda: node.is_checkable()),
        ("checked", lambda: node.is_checked()),
        ("focused", lambda: node.is_focused()),
        ("keyboard_focused", lambda: node.is_keyboard_focused()),
        ("selected", lambda: node.is_selected()),
        ("selection_required", lambda: node.is_selection_required()),
        ("pressable", lambda: node.is_pressable()),
        ("pressed", lambda: node.is_pressed()),
        ("expanded", lambda: node.is_expanded()),
        ("editable", lambda: node.is_editable()),
        ("has_keyboard_focus", lambda: node.has_keyboard_focus()),
        ("is_keyboard_focusable", lambda: node.is_keyboard_focusable()),
    ]:
        try:
            attribute_dict[f"{{{_accessibility_ns_map_windows['st']}}}{attr_name}"] = str(attr_func()).lower()
        except:
            pass

    #  Component
    try:
        rectangle = node.rectangle()
        attribute_dict["{{{:}}}screencoord".format(_accessibility_ns_map_windows["cp"])] = \
            "({:d}, {:d})".format(rectangle.left, rectangle.top)
        attribute_dict["{{{:}}}size".format(_accessibility_ns_map_windows["cp"])] = \
            "({:d}, {:d})".format(rectangle.width(), rectangle.height())

    except Exception as e:
        logger.error("Error accessing rectangle: ", e)

    #  Text
    text: str = node.window_text()
    if text == attribute_dict["name"]:
        text = ""

    #  Selection
    if hasattr(node, "select"):
        attribute_dict["selection"] = "true"

    # Value
    for attr_name, attr_funcs in [
        ("step", [lambda: node.get_step()]),
        ("value", [lambda: node.value(), lambda: node.get_value(), lambda: node.get_position()]),
        ("min", [lambda: node.min_value(), lambda: node.get_range_min()]),
        ("max", [lambda: node.max_value(), lambda: node.get_range_max()])
    ]:
        for attr_func in attr_funcs:
            if hasattr(node, attr_func.__name__):
                try:
                    attribute_dict[f"{{{_accessibility_ns_map_windows['val']}}}{attr_name}"] = str(attr_func())
                    break  # exit once the attribute is set successfully
                except:
                    pass

    attribute_dict["{{{:}}}class".format(_accessibility_ns_map_windows["class"])] = str(type(node))

    # class_name
    for attr_name in ["class_name", "friendly_class_name"]:
        try:
            attribute_dict[f"{{{_accessibility_ns_map_windows['class']}}}{attr_name}"] = base_properties[
                attr_name].lower()
        except:
            pass

    node_role_name: str = node.class_name().lower().replace(" ", "-")
    node_role_name = "".join(
        map(lambda _ch: _ch if _ch.isidentifier() or _ch in {"-"} or _ch.isalnum() else "-", node_role_name))

    if node_role_name.strip() == "":
        node_role_name = "unknown"
    if not node_role_name[0].isalpha():
        node_role_name = "tag" + node_role_name

    xml_node = lxml.etree.Element(
        node_role_name,
        attrib=attribute_dict,
        nsmap=_accessibility_ns_map_windows
    )

    if text is not None and len(text) > 0 and text != attribute_dict["name"]:
        xml_node.text = text

    if depth == MAX_DEPTH:
        logger.warning("Max depth reached")
        return xml_node

    # use multi thread to accelerate children fetching
    children = node.children()
    if children:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future_to_child = [executor.submit(_create_pywinauto_node, ch, nodes, depth + 1, flag) for ch in
                               children[:MAX_WIDTH]]
        try:
            xml_node.extend([future.result() for future in concurrent.futures.as_completed(future_to_child)])
        except Exception as e:
            logger.error(f"Exception occurred: {e}")
    return xml_node


# A11y tree getter for macOS

def _create_axui_node(node, nodes: set = None, depth: int = 0, bbox: tuple = None):
    nodes = nodes or set()
    if node in nodes:
        return
    nodes.add(node)

    reserved_keys = {
        "AXEnabled": "st",
        "AXFocused": "st",
        "AXFullScreen": "st",
        "AXTitle": "attr",
        "AXChildrenInNavigationOrder": "attr",
        "AXChildren": "attr",
        "AXFrame": "attr",
        "AXRole": "role",
        "AXHelp": "attr",
        "AXRoleDescription": "role",
        "AXSubrole": "role",
        "AXURL": "attr",
        "AXValue": "val",
        "AXDescription": "attr",
        "AXDOMIdentifier": "attr",
        "AXSelected": "st",
        "AXInvalid": "st",
        "AXRows": "attr",
        "AXColumns": "attr",
    }
    attribute_dict = {}

    if depth == 0:
        bbox = (
            node["kCGWindowBounds"]["X"],
            node["kCGWindowBounds"]["Y"],
            node["kCGWindowBounds"]["X"] + node["kCGWindowBounds"]["Width"],
            node["kCGWindowBounds"]["Y"] + node["kCGWindowBounds"]["Height"]
        )
        app_ref = ApplicationServices.AXUIElementCreateApplication(node["kCGWindowOwnerPID"])

        attribute_dict["name"] = node["kCGWindowOwnerName"]
        if attribute_dict["name"] != "Dock":
            error_code, app_wins_ref = ApplicationServices.AXUIElementCopyAttributeValue(
                app_ref, "AXWindows", None)
            if error_code:
                logger.error("MacOS parsing %s encountered Error code: %d", app_ref, error_code)
        else:
            app_wins_ref = [app_ref]
        node = app_wins_ref[0]

    error_code, attr_names = ApplicationServices.AXUIElementCopyAttributeNames(node, None)

    if error_code:
        # -25202: AXError.invalidUIElement
        #         The accessibility object received in this event is invalid.
        return

    value = None

    if "AXFrame" in attr_names:
        error_code, attr_val = ApplicationServices.AXUIElementCopyAttributeValue(node, "AXFrame", None)
        rep = repr(attr_val)
        x_value = re.search(r"x:(-?[\d.]+)", rep)
        y_value = re.search(r"y:(-?[\d.]+)", rep)
        w_value = re.search(r"w:(-?[\d.]+)", rep)
        h_value = re.search(r"h:(-?[\d.]+)", rep)
        type_value = re.search(r"type\s?=\s?(\w+)", rep)
        value = {
            "x": float(x_value.group(1)) if x_value else None,
            "y": float(y_value.group(1)) if y_value else None,
            "w": float(w_value.group(1)) if w_value else None,
            "h": float(h_value.group(1)) if h_value else None,
            "type": type_value.group(1) if type_value else None,
        }

        if not any(v is None for v in value.values()):
            x_min = max(bbox[0], value["x"])
            x_max = min(bbox[2], value["x"] + value["w"])
            y_min = max(bbox[1], value["y"])
            y_max = min(bbox[3], value["y"] + value["h"])

            if x_min > x_max or y_min > y_max:
                # No intersection
                return

    role = None
    text = None

    for attr_name, ns_key in reserved_keys.items():
        if attr_name not in attr_names:
            continue

        if value and attr_name == "AXFrame":
            bb = value
            if not any(v is None for v in bb.values()):
                attribute_dict["{{{:}}}screencoord".format(_accessibility_ns_map_macos["cp"])] = \
                    "({:d}, {:d})".format(int(bb["x"]), int(bb["y"]))
                attribute_dict["{{{:}}}size".format(_accessibility_ns_map_macos["cp"])] = \
                    "({:d}, {:d})".format(int(bb["w"]), int(bb["h"]))
            continue

        error_code, attr_val = ApplicationServices.AXUIElementCopyAttributeValue(node, attr_name, None)

        full_attr_name = f"{{{_accessibility_ns_map_macos[ns_key]}}}{attr_name}"

        if attr_name == "AXValue" and not text:
            text = str(attr_val)
            continue

        if attr_name == "AXRoleDescription":
            role = attr_val
            continue

        # Set the attribute_dict
        if not (isinstance(attr_val, ApplicationServices.AXUIElementRef)
                or isinstance(attr_val, (AppKit.NSArray, list))):
            if attr_val is not None:
                attribute_dict[full_attr_name] = str(attr_val)

    node_role_name = role.lower().replace(" ", "_") if role else "unknown_role"

    xml_node = lxml.etree.Element(
        node_role_name,
        attrib=attribute_dict,
        nsmap=_accessibility_ns_map_macos
    )

    if text is not None and len(text) > 0:
        xml_node.text = text

    if depth == MAX_DEPTH:
        logger.warning("Max depth reached")
        return xml_node

    future_to_child = []

    with concurrent.futures.ThreadPoolExecutor() as executor:
        for attr_name, ns_key in reserved_keys.items():
            if attr_name not in attr_names:
                continue

            error_code, attr_val = ApplicationServices.AXUIElementCopyAttributeValue(node, attr_name, None)
            if isinstance(attr_val, ApplicationServices.AXUIElementRef):
                future_to_child.append(executor.submit(_create_axui_node, attr_val, nodes, depth + 1, bbox))

            elif isinstance(attr_val, (AppKit.NSArray, list)):
                for child in attr_val:
                    future_to_child.append(executor.submit(_create_axui_node, child, nodes, depth + 1, bbox))

        try:
            for future in concurrent.futures.as_completed(future_to_child):
                result = future.result()
                if result is not None:
                    xml_node.append(result)
        except Exception as e:
            logger.error(f"Exception occurred: {e}")

    return xml_node


@app.route("/accessibility", methods=["GET"])
def get_accessibility_tree():
    os_name: str = platform.system()

    # AT-SPI works for KDE as well
    if os_name == "Linux":
        global libreoffice_version_tuple
        libreoffice_version_tuple = _get_libreoffice_version()

        desktop: Accessible = pyatspi.Registry.getDesktop(0)
        xml_node = lxml.etree.Element("desktop-frame", nsmap=_accessibility_ns_map_ubuntu)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(_create_atspi_node, app_node, 1) for app_node in desktop]
            for future in concurrent.futures.as_completed(futures):
                xml_tree = future.result()
                xml_node.append(xml_tree)
        return jsonify({"AT": lxml.etree.tostring(xml_node, encoding="unicode")})

    elif os_name == "Windows":
        # Attention: Windows a11y tree is implemented to be read through `pywinauto` module, however,
        # two different backends `win32` and `uia` are supported and different results may be returned
        desktop: Desktop = Desktop(backend="uia")
        xml_node = lxml.etree.Element("desktop", nsmap=_accessibility_ns_map_windows)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            futures = [executor.submit(_create_pywinauto_node, wnd, {}, 1) for wnd in desktop.windows()]
            for future in concurrent.futures.as_completed(futures):
                xml_tree = future.result()
                xml_node.append(xml_tree)
        return jsonify({"AT": lxml.etree.tostring(xml_node, encoding="unicode")})

    elif os_name == "Darwin":
        # TODO: Add Dock and MenuBar
        xml_node = lxml.etree.Element("desktop", nsmap=_accessibility_ns_map_macos)

        with concurrent.futures.ThreadPoolExecutor() as executor:
            foreground_windows = [
                win for win in Quartz.CGWindowListCopyWindowInfo(
                    (Quartz.kCGWindowListExcludeDesktopElements |
                     Quartz.kCGWindowListOptionOnScreenOnly),
                    Quartz.kCGNullWindowID
                ) if win["kCGWindowLayer"] == 0 and win["kCGWindowOwnerName"] != "Window Server"
            ]
            dock_info = [
                win for win in Quartz.CGWindowListCopyWindowInfo(
                    Quartz.kCGWindowListOptionAll,
                    Quartz.kCGNullWindowID
                ) if win.get("kCGWindowName", None) == "Dock"
            ]

            futures = [
                executor.submit(_create_axui_node, wnd, None, 0)
                for wnd in foreground_windows + dock_info
            ]

            for future in concurrent.futures.as_completed(futures):
                xml_tree = future.result()
                if xml_tree is not None:
                    xml_node.append(xml_tree)

        return jsonify({"AT": lxml.etree.tostring(xml_node, encoding="unicode")})

    else:
        return "Currently not implemented for platform {:}.".format(platform.platform()), 500


@app.route('/screen_size', methods=['POST'])
def get_screen_size():
    if platform_name == "Linux":
        d = display.Display()
        screen_width = d.screen().width_in_pixels
        screen_height = d.screen().height_in_pixels
    elif platform_name == "Windows":
        user32 = ctypes.windll.user32
        screen_width: int = user32.GetSystemMetrics(0)
        screen_height: int = user32.GetSystemMetrics(1)
    return jsonify(
        {
            "width": screen_width,
            "height": screen_height
        }
    )


@app.route('/window_size', methods=['POST'])
def get_window_size():
    if 'app_class_name' in request.form:
        app_class_name = request.form['app_class_name']
    else:
        return jsonify({"error": "app_class_name is required"}), 400

    d = display.Display()
    root = d.screen().root
    window_ids = root.get_full_property(d.intern_atom('_NET_CLIENT_LIST'), X.AnyPropertyType).value

    for window_id in window_ids:
        try:
            window = d.create_resource_object('window', window_id)
            wm_class = window.get_wm_class()

            if wm_class is None:
                continue

            if app_class_name.lower() in [name.lower() for name in wm_class]:
                geom = window.get_geometry()
                return jsonify(
                    {
                        "width": geom.width,
                        "height": geom.height
                    }
                )
        except Xlib.error.XError:  # Ignore windows that give an error
            continue
    return None


@app.route('/desktop_path', methods=['POST'])
def get_desktop_path():
    # Get the home directory in a platform-independent manner using pathlib
    home_directory = str(Path.home())

    # Determine the desktop path based on the operating system
    desktop_path = {
        "Windows": os.path.join(home_directory, "Desktop"),
        "Darwin": os.path.join(home_directory, "Desktop"),  # macOS
        "Linux": os.path.join(home_directory, "Desktop")
    }.get(platform.system(), None)

    # Check if the operating system is supported and the desktop path exists
    if desktop_path and os.path.exists(desktop_path):
        return jsonify(desktop_path=desktop_path)
    else:
        return jsonify(error="Unsupported operating system or desktop path not found"), 404


@app.route('/wallpaper', methods=['POST'])
def get_wallpaper():
    def get_wallpaper_windows():
        SPI_GETDESKWALLPAPER = 0x73
        MAX_PATH = 260
        buffer = ctypes.create_unicode_buffer(MAX_PATH)
        ctypes.windll.user32.SystemParametersInfoW(SPI_GETDESKWALLPAPER, MAX_PATH, buffer, 0)
        return buffer.value

    def get_wallpaper_macos():
        script = """
        tell application "System Events" to tell every desktop to get picture
        """
        process = subprocess.Popen(['osascript', '-e', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, error = process.communicate()
        if error:
            app.logger.error("Error: %s", error.decode('utf-8'))
            return None
        return output.strip().decode('utf-8')

    def get_wallpaper_linux():
        try:
            output = subprocess.check_output(
                ["gsettings", "get", "org.gnome.desktop.background", "picture-uri"],
                stderr=subprocess.PIPE
            )
            return output.decode('utf-8').strip().replace('file://', '').replace("'", "")
        except subprocess.CalledProcessError as e:
            app.logger.error("Error: %s", e)
            return None

    os_name = platform.system()
    wallpaper_path = None
    if os_name == 'Windows':
        wallpaper_path = get_wallpaper_windows()
    elif os_name == 'Darwin':
        wallpaper_path = get_wallpaper_macos()
    elif os_name == 'Linux':
        wallpaper_path = get_wallpaper_linux()
    else:
        app.logger.error(f"Unsupported OS: {os_name}")
        abort(400, description="Unsupported OS")

    if wallpaper_path:
        try:
            # Ensure the filename is secure
            return send_file(wallpaper_path, mimetype='image/png')
        except Exception as e:
            app.logger.error(f"An error occurred while serving the wallpaper file: {e}")
            abort(500, description="Unable to serve the wallpaper file")
    else:
        abort(404, description="Wallpaper file not found")


@app.route('/list_directory', methods=['POST'])
def get_directory_tree():
    def _list_dir_contents(directory):
        """
        List the contents of a directory recursively, building a tree structure.

        :param directory: The path of the directory to inspect.
        :return: A nested dictionary with the contents of the directory.
        """
        tree = {'type': 'directory', 'name': os.path.basename(directory), 'children': []}
        try:
            # List all files and directories in the current directory
            for entry in os.listdir(directory):
                full_path = os.path.join(directory, entry)
                # If entry is a directory, recurse into it
                if os.path.isdir(full_path):
                    tree['children'].append(_list_dir_contents(full_path))
                else:
                    tree['children'].append({'type': 'file', 'name': entry})
        except OSError as e:
            # If the directory cannot be accessed, return the exception message
            tree = {'error': str(e)}
        return tree

    # Extract the 'path' parameter from the JSON request
    data = request.get_json()
    if 'path' not in data:
        return jsonify(error="Missing 'path' parameter"), 400

    start_path = data['path']
    # Ensure the provided path is a directory
    if not os.path.isdir(start_path):
        return jsonify(error="The provided path is not a directory"), 400

    # Generate the directory tree starting from the provided path
    directory_tree = _list_dir_contents(start_path)
    return jsonify(directory_tree=directory_tree)


@app.route('/file', methods=['POST'])
def get_file():
    # Retrieve filename from the POST request
    if 'file_path' in request.form:
        file_path = os.path.expandvars(os.path.expanduser(request.form['file_path']))
    else:
        return jsonify({"error": "file_path is required"}), 400

    try:
        # Check if the file exists and send it to the user
        return send_file(file_path, as_attachment=True)
    except FileNotFoundError:
        # If the file is not found, return a 404 error
        return jsonify({"error": "File not found"}), 404


@app.route("/setup/upload", methods=["POST"])
def upload_file():
    # Retrieve filename from the POST request
    if 'file_path' in request.form and 'file_data' in request.files:
        file_path = os.path.expandvars(os.path.expanduser(request.form['file_path']))
        file = request.files["file_data"]
        file.save(file_path)
        return "File Uploaded"
    else:
        return jsonify({"error": "file_path and file_data are required"}), 400


@app.route('/platform', methods=['GET'])
def get_platform():
    return platform.system()


@app.route('/cursor_position', methods=['GET'])
def get_cursor_position():
    pos = pyautogui.position()
    return jsonify(pos.x, pos.y)

@app.route("/setup/change_wallpaper", methods=['POST'])
def change_wallpaper():
    data = request.json
    path = data.get('path', None)

    if not path:
        return "Path not supplied!", 400

    path = Path(os.path.expandvars(os.path.expanduser(path)))

    if not path.exists():
        return f"File not found: {path}", 404

    try:
        user_platform = platform.system()
        if user_platform == "Windows":
            import ctypes
            ctypes.windll.user32.SystemParametersInfoW(20, 0, str(path), 3)
        elif user_platform == "Linux":
            import subprocess
            subprocess.run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri", f"file://{path}"])
        elif user_platform == "Darwin":  # (Mac OS)
            import subprocess
            subprocess.run(
                ["osascript", "-e", f'tell application "Finder" to set desktop picture to POSIX file "{path}"'])
        return "Wallpaper changed successfully"
    except Exception as e:
        return f"Failed to change wallpaper. Error: {e}", 500


@app.route("/setup/download_file", methods=['POST'])
def download_file():
    data = request.json
    url = data.get('url', None)
    path = data.get('path', None)

    if not url or not path:
        return "Path or URL not supplied!", 400

    path = Path(os.path.expandvars(os.path.expanduser(path)))
    path.parent.mkdir(parents=True, exist_ok=True)

    max_retries = 3
    error: Optional[Exception] = None
    for i in range(max_retries):
        try:
            response = requests.get(url, stream=True)
            response.raise_for_status()

            with open(path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return "File downloaded successfully"

        except requests.RequestException as e:
            error = e
            logger.error(f"Failed to download {url}. Retrying... ({max_retries - i - 1} attempts left)")

    return f"Failed to download {url}. No retries left. Error: {error}", 500


@app.route("/setup/open_file", methods=['POST'])
def open_file():
    data = request.json
    path = data.get('path', None)

    if not path:
        return "Path not supplied!", 400

    path = Path(os.path.expandvars(os.path.expanduser(path)))

    if not path.exists():
        return f"File not found: {path}", 404

    try:
        if platform.system() == "Windows":
            os.startfile(path)
        else:
            open_cmd: str = "open" if platform.system() == "Darwin" else "xdg-open"
            subprocess.Popen([open_cmd, str(path)])
        return "File opened successfully"
    except Exception as e:
        return f"Failed to open {path}. Error: {e}", 500


@app.route("/setup/activate_window", methods=['POST'])
def activate_window():
    data = request.json
    window_name = data.get('window_name', None)
    if not window_name:
        return "window_name required", 400
    strict: bool = data.get("strict", False)  # compare case-sensitively and match the whole string
    by_class_name: bool = data.get("by_class", False)

    os_name = platform.system()

    if os_name == 'Windows':
        import pygetwindow as gw
        if by_class_name:
            return "Get window by class name is not supported on Windows currently.", 500
        windows: List[gw.Window] = gw.getWindowsWithTitle(window_name)

        window: Optional[gw.Window] = None
        if len(windows) == 0:
            return "Window {:} not found (empty results)".format(window_name), 404
        elif strict:
            for wnd in windows:
                if wnd.title == wnd:
                    window = wnd
            if window is None:
                return "Window {:} not found (strict mode).".format(window_name), 404
        else:
            window = windows[0]
        window.activate()

    elif os_name == 'Darwin':
        import pygetwindow as gw
        if by_class_name:
            return "Get window by class name is not supported on macOS currently.", 500
        # Find the VS Code window
        windows = gw.getWindowsWithTitle(window_name)

        window: Optional[gw.Window] = None
        if len(windows) == 0:
            return "Window {:} not found (empty results)".format(window_name), 404
        elif strict:
            for wnd in windows:
                if wnd.title == wnd:
                    window = wnd
            if window is None:
                return "Window {:} not found (strict mode).".format(window_name), 404
        else:
            window = windows[0]

        # Un-minimize the window and then bring it to the front
        window.unminimize()
        window.activate()

    elif os_name == 'Linux':
        # Attempt to activate VS Code window using wmctrl
        subprocess.run(["wmctrl"
                           , "-{:}{:}a".format("x" if by_class_name else ""
                                               , "F" if strict else ""
                                               )
                           , window_name
                        ]
                       )

    else:
        return f"Operating system {os_name} not supported.", 400

    return "Window activated successfully", 200


@app.route("/setup/close_window", methods=["POST"])
def close_window():
    data = request.json
    if "window_name" not in data:
        return "window_name required", 400
    window_name: str = data["window_name"]
    strict: bool = data.get("strict", False)  # compare case-sensitively and match the whole string
    by_class_name: bool = data.get("by_class", False)

    os_name: str = platform.system()
    if os_name == "Windows":
        import pygetwindow as gw

        if by_class_name:
            return "Get window by class name is not supported on Windows currently.", 500
        windows: List[gw.Window] = gw.getWindowsWithTitle(window_name)

        window: Optional[gw.Window] = None
        if len(windows) == 0:
            return "Window {:} not found (empty results)".format(window_name), 404
        elif strict:
            for wnd in windows:
                if wnd.title == wnd:
                    window = wnd
            if window is None:
                return "Window {:} not found (strict mode).".format(window_name), 404
        else:
            window = windows[0]
        window.close()
    elif os_name == "Linux":
        subprocess.run(["wmctrl"
                           , "-{:}{:}c".format("x" if by_class_name else ""
                                               , "F" if strict else ""
                                               )
                           , window_name
                        ]
                       )
    elif os_name == "Darwin":
        import pygetwindow as gw
        return "Currently not supported on macOS.", 500
    else:
        return "Not supported platform {:}".format(os_name), 500

    return "Window closed successfully.", 200


@app.route('/start_recording', methods=['POST'])
def start_recording():
    global recording_process
    if recording_process:
        return jsonify({'status': 'error', 'message': 'Recording is already in progress.'}), 400

    d = display.Display()
    screen_width = d.screen().width_in_pixels
    screen_height = d.screen().height_in_pixels

    start_command = f"ffmpeg -y -f x11grab -draw_mouse 1 -s {screen_width}x{screen_height} -i :0.0 -c:v libx264 -r 30 {recording_path}"

    recording_process = subprocess.Popen(shlex.split(start_command), stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)

    return jsonify({'status': 'success', 'message': 'Started recording.'})


@app.route('/end_recording', methods=['POST'])
def end_recording():
    global recording_process

    if not recording_process:
        return jsonify({'status': 'error', 'message': 'No recording in progress to stop.'}), 400

    recording_process.send_signal(signal.SIGINT)
    recording_process.wait()
    recording_process = None

    # return recording video file
    if os.path.exists(recording_path):
        return send_file(recording_path, as_attachment=True)
    else:
        return abort(404, description="Recording failed")


@app.route("/run_python", methods=['POST'])
def run_python():
    data = request.json
    code = data.get('code', None)

    if not code:
        return jsonify({'status': 'error', 'message': 'Code not supplied!'}), 400

    # Create a temporary file to save the Python code
    import tempfile
    import uuid
    
    # Generate unique filename
    temp_filename = f"/tmp/python_exec_{uuid.uuid4().hex}.py"
    
    try:
        # Write code to temporary file
        with open(temp_filename, 'w') as f:
            f.write(code)
        
        # Execute the file using subprocess to capture all output
        result = subprocess.run(
            ['/usr/bin/python3', temp_filename],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30  # 30 second timeout
        )
        
        # Clean up the temporary file
        try:
            os.remove(temp_filename)
        except:
            pass  # Ignore cleanup errors
        
        # Prepare response
        output = result.stdout
        error_output = result.stderr
        
        # Combine output and errors if both exist
        combined_message = output
        if error_output:
            combined_message += ('\n' + error_output) if output else error_output
        
        # Determine status based on return code and errors
        if result.returncode != 0:
            status = 'error'
            if not error_output:
                # If no stderr but non-zero return code, add a generic error message
                error_output = f"Process exited with code {result.returncode}"
                combined_message = combined_message + '\n' + error_output if combined_message else error_output
        else:
            status = 'success'
        
        return jsonify({
            'status': status,
            'message': combined_message,
            'need_more': False,      # Not applicable for file execution
            'output': output,        # stdout only
            'error': error_output,   # stderr only
            'return_code': result.returncode
        })
        
    except subprocess.TimeoutExpired:
        # Clean up the temporary file on timeout
        try:
            os.remove(temp_filename)
        except:
            pass
            
        return jsonify({
            'status': 'error',
            'message': 'Execution timeout: Code took too long to execute',
            'error': 'TimeoutExpired',
            'need_more': False,
            'output': None,
        }), 500
        
    except Exception as e:
        # Clean up the temporary file on error
        try:
            os.remove(temp_filename)
        except:
            pass
            
        # Capture the exception details
        return jsonify({
            'status': 'error',
            'message': f'Execution error: {str(e)}',
            'error': traceback.format_exc(),
            'need_more': False,
            'output': None,
        }), 500

@app.route("/reset_python_console", methods=['POST'])
def reset_python_console():
    global console
    
    with console_lock:
        # Create a new InteractiveConsole instance to reset the state
        console = code.InteractiveConsole(locals={})
    
    return jsonify({
        'status': 'success',
        'message': 'Python console has been reset'
    })

@app.route("/run_bash_script", methods=['POST'])
def run_bash_script():
    data = request.json
    script = data.get('script', None)
    timeout = data.get('timeout', 100)  # Default timeout of 30 seconds
    working_dir = data.get('working_dir', None)
    
    if not script:
        return jsonify({
            'status': 'error',
            'output': 'Script not supplied!',
            'error': "",  # Always empty as requested
            'returncode': -1
        }), 400
    
    # Expand user directory if provided
    if working_dir:
        working_dir = os.path.expanduser(working_dir)
        if not os.path.exists(working_dir):
            return jsonify({
                'status': 'error',
                'output': f'Working directory does not exist: {working_dir}',
                'error': "",  # Always empty as requested
                'returncode': -1
            }), 400
    
    # Create a temporary script file
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False) as tmp_file:
        if "#!/bin/bash" not in script:
            script = "#!/bin/bash\n\n" + script
        tmp_file.write(script)
        tmp_file_path = tmp_file.name
    
    try:
        # Make the script executable
        os.chmod(tmp_file_path, 0o755)
        
        # Execute the script
        if platform_name == "Windows":
            # On Windows, use Git Bash or WSL if available, otherwise cmd
            flags = subprocess.CREATE_NO_WINDOW
            # Try to use bash if available (Git Bash, WSL, etc.)
            result = subprocess.run(
                ['bash', tmp_file_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Merge stderr into stdout
                text=True,
                timeout=timeout,
                cwd=working_dir,
                creationflags=flags,
                shell=False
            )
        else:
            # On Unix-like systems, use bash directly
            flags = 0
            result = subprocess.run(
                ['/bin/bash', tmp_file_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Merge stderr into stdout
                text=True,
                timeout=timeout,
                cwd=working_dir,
                creationflags=flags,
                shell=False
            )
        
        # Log the command execution for trajectory recording
        _append_event("BashScript", 
                      {"script": script, "output": result.stdout, "error": "", "returncode": result.returncode}, 
                      ts=time.time())
        
        return jsonify({
            'status': 'success' if result.returncode == 0 else 'error',
            'output': result.stdout,  # Contains both stdout and stderr merged
            'error': "",  # Always empty as requested
            'returncode': result.returncode
        })
        
    except subprocess.TimeoutExpired:
        return jsonify({
            'status': 'error',
            'output': f'Script execution timed out after {timeout} seconds',
            'error': "",  # Always empty as requested
            'returncode': -1
        }), 500
    except FileNotFoundError:
        # Bash not found, try with sh
        try:
            result = subprocess.run(
                ['sh', tmp_file_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Merge stderr into stdout
                text=True,
                timeout=timeout,
                cwd=working_dir,
                shell=False
            )
            
            _append_event("BashScript", 
                          {"script": script, "output": result.stdout, "error": "", "returncode": result.returncode}, 
                          ts=time.time())
            
            return jsonify({
                'status': 'success' if result.returncode == 0 else 'error',
                'output': result.stdout,  # Contains both stdout and stderr merged
                'error': "",  # Always empty as requested
                'returncode': result.returncode,
            })
        except Exception as e:
            return jsonify({
                'status': 'error',
                'output': f'Failed to execute script: {str(e)}',
                'error': "",  # Always empty as requested
                'returncode': -1
            }), 500
    except Exception as e:
        return jsonify({
            'status': 'error',
            'output': f'Failed to execute script: {str(e)}',
            'error': "",  # Always empty as requested
            'returncode': -1
        }), 500
    finally:
        # Clean up the temporary file
        try:
            os.unlink(tmp_file_path)
        except:
            pass

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0")
