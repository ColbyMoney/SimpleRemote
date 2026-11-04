# Grandma's TV box: Wi-Fi CircuitPython board + Adafruit NeoKey 1x4 pads (STEMMA QT)
# Channel up/down flips through a list of shows; everything is set in a small JSON
# file fetched over Wi-Fi, so you can change it without touching the device.
#
# settings.toml on the CIRCUITPY drive:
#   CIRCUITPY_WIFI_SSID = "wifi-name"
#   CIRCUITPY_WIFI_PASSWORD = "wifi-password"
#   CONFIG_URL = "https://raw.githubusercontent.com/<you>/<repo>/main/shows.json"
#   NEOKEY_COUNT = 2        # 1 = one NeoKey 1x4; 2+ = several, via an I2C multiplexer
#
# Libraries (Adafruit bundle) in /lib:
#   adafruit_neokey, adafruit_seesaw, adafruit_requests,
#   adafruit_connection_manager, adafruit_bus_device, neopixel,
#   adafruit_tca9548a (only needed when NEOKEY_COUNT is 2 or more)

import os
import time
import json
import ssl
import wifi
import socketpool
import board
import adafruit_requests
from adafruit_neokey.neokey1x4 import NeoKey1x4

CACHE_FILE = "/config_cache.json"
STATE_FILE = "/state.json"
# Roku app (channel) ids. Anything not listed can be set per item with "app_id".
# Check your own Roku with: http://<roku-ip>:8060/query/apps
APP_IDS = {
    "youtube_tv": 195316,
    "netflix": 12,
    "spotify": 22297
}
DEFAULT_REFRESH_SECONDS = 120
DEFAULT_SETTLE_SECONDS = 2.5  # wait this long after the last channel press, then play

# ---------- hardware ----------
try:
    i2c = board.STEMMA_I2C()
except AttributeError:
    i2c = board.I2C()

# 1 = a single NeoKey 1x4 plugged straight into the board.
# 2+ = several NeoKey 1x4s, each on its own port of an I2C multiplexer (TCA9548A).
# Keys 0-3 are the first pad, keys 4-7 the second, and so on.
NEOKEY_COUNT = int(os.getenv("NEOKEY_COUNT", "1"))

if NEOKEY_COUNT == 1:
    pads = [NeoKey1x4(i2c, addr=0x30)]
else:
    import adafruit_tca9548a

    mux = adafruit_tca9548a.TCA9548A(i2c)
    pads = [NeoKey1x4(mux[n], addr=0x30) for n in range(NEOKEY_COUNT)]

for pad in pads:
    pad.pixels.brightness = 0.5
NUM_KEYS = 4 * NEOKEY_COUNT


def is_pressed(i):
    return pads[i // 4][i % 4]


def parse_color(value):
    try:
        v = value.lstrip("#")
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except Exception:
        return (255, 255, 255)


def scale(rgb, f):
    return (int(rgb[0] * f), int(rgb[1] * f), int(rgb[2] * f))


def set_key(i, rgb):
    pads[i // 4].pixels[i % 4] = rgb


# ---------- network ----------
def connect_wifi():
    if wifi.radio.connected:
        return True
    try:
        wifi.radio.connect(
            os.getenv("CIRCUITPY_WIFI_SSID"), os.getenv("CIRCUITPY_WIFI_PASSWORD")
        )
        return True
    except Exception as e:
        print("wifi error:", e)
        return False


connect_wifi()
pool = socketpool.SocketPool(wifi.radio)
http = adafruit_requests.Session(pool, ssl.create_default_context())


def fetch_config():
    """Return a config dict from the web, or None on any failure."""
    url = os.getenv("CONFIG_URL")
    if not url or not connect_wifi():
        return None
    try:
        r = http.get(url, timeout=10)
        if r.status_code != 200:
            r.close()
            return None
        data = r.json()
        r.close()
        if "roku_ip" in data and "keys" in data:
            try:
                with open(CACHE_FILE, "w") as f:
                    json.dump(data, f)
            except OSError:
                pass  # filesystem is read-only while plugged into a computer
            return data
    except Exception as e:
        print("config fetch error:", e)
    return None


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


config = fetch_config() or load_cache() or {"roku_ip": "", "keys": [], "channels": []}

# ---------- channel state ----------
# channel: index into config["channels"], or -1 if nothing has been played yet.
# pending_at: when to start playing the channel she flipped to (None = nothing waiting).
state = {"channel": -1, "pending_at": None, "pending_key": 0}


def load_state():
    try:
        with open(STATE_FILE) as f:
            state["channel"] = int(json.load(f).get("channel", -1))
    except Exception:
        pass


def save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"channel": state["channel"]}, f)
    except OSError:
        pass  # read-only while plugged into a computer


def channels():
    return config.get("channels", [])


def fix_channel_index():
    n = len(channels())
    if n == 0 or state["channel"] >= n:
        state["channel"] = -1


load_state()
fix_channel_index()


def apply_idle_colors():
    keys = config.get("keys", [])
    for i in range(NUM_KEYS):
        if i < len(keys) and keys[i].get("action") != "none":
            set_key(i, scale(parse_color(keys[i].get("color", "#FFFFFF")), 0.25))
        else:
            set_key(i, (0, 0, 0))


# ---------- Roku ----------
def roku_post(path):
    ip = config.get("roku_ip")
    if not ip:
        return False
    try:
        r = http.post("http://%s:8060%s" % (ip, path), data="", timeout=6)
        ok = r.status_code in (200, 202, 204)
        r.close()
        return ok
    except Exception as e:
        print("roku error:", e)
        return False


# ---------- feedback ----------
def blink(i, rgb, seconds):
    end = time.monotonic() + seconds
    on = True
    while time.monotonic() < end:
        set_key(i, rgb if on else scale(rgb, 0.1))
        on = not on
        time.sleep(0.35)


def fail(i):
    for _ in range(3):
        set_key(i, (255, 0, 0))
        time.sleep(0.25)
        set_key(i, (0, 0, 0))
        time.sleep(0.25)
    apply_idle_colors()


# ---------- actions ----------
def launch_item(i, item, keep_lit=False):
    """Wake the Roku, open an app (and a specific show/movie), then press Select."""
    color = parse_color(item.get("color", "#FFFFFF"))
    app_id = item.get("app_id") or APP_IDS.get(item.get("app", "netflix"))
    if not app_id:
        fail(i)
        return
    content_id = item.get("content_id", "")
    set_key(i, color)
    # Wake the Roku (and the TV, if HDMI-CEC / Anynet+ is on), then launch.
    roku_post("/keypress/PowerOn")
    blink(i, color, config.get("boot_seconds", 6))
    path = "/launch/%s" % app_id
    if content_id:
        path += "?contentId=%s&mediaType=%s" % (content_id, item.get("type", "series"))
    if not roku_post(path):
        fail(i)
        return
    blink(i, color, item.get("select_delay", 8))
    # Usually one Select presses the pre-focused Play / Resume. Some apps need more
    # (profile picker, "Resume or Start Over"), so each item can override.
    for key_name in item.get("post_keys", ["Select"]):
        roku_post("/keypress/" + key_name)
        time.sleep(1.5)
    apply_idle_colors()
    if keep_lit:
        set_key(i, color)


def change_channel(i, step):
    """Flip to the next/previous channel. Playing starts after a short pause, so
    several quick presses just move through the list."""
    chans = channels()
    if not chans:
        fail(i)
        return
    n = len(chans)
    if state["channel"] < 0:
        state["channel"] = 0 if step > 0 else n - 1
    else:
        state["channel"] = (state["channel"] + step) % n
    save_state()
    set_key(i, parse_color(chans[state["channel"]].get("color", "#FFFFFF")))
    state["pending_at"] = time.monotonic() + config.get(
        "channel_settle_seconds", DEFAULT_SETTLE_SECONDS
    )
    state["pending_key"] = i


def do_key(i):
    keys = config.get("keys", [])
    if i >= len(keys):
        return
    k = keys[i]
    action = k.get("action")
    if action == "none":
        return  # empty slot: stays dark and does nothing
    color = parse_color(k.get("color", "#FFFFFF"))

    if action in ("channel_up", "channel_down"):
        change_channel(i, 1 if action == "channel_up" else -1)
        return

    set_key(i, color)  # instant "I heard you"
    if action in ("launch", "netflix"):
        # A key can still launch one fixed show. "netflix" is kept as an alias.
        launch_item(i, k, keep_lit=True)
    elif action == "key":
        # Press any Roku remote key: Select, Home, Back, InstantReplay, VolumeUp...
        key_name = k.get("key_name", "")
        if not key_name or not roku_post("/keypress/" + key_name):
            fail(i)
        time.sleep(0.4)
        apply_idle_colors()
    elif action == "playpause":
        if not roku_post("/keypress/Play"):
            fail(i)
        time.sleep(0.4)
        apply_idle_colors()
    elif action == "power":
        state["pending_at"] = None  # cancel any channel waiting to start
        roku_post("/keypress/Home")
        time.sleep(1)
        if not roku_post("/keypress/PowerOff"):  # sleeps the Roku; CEC turns the TV off
            fail(i)
        time.sleep(0.4)
        apply_idle_colors()
    else:
        fail(i)


# ---------- main loop ----------
apply_idle_colors()
last_refresh = time.monotonic()
if not config.get("keys"):
    fail(0)  # no usable config yet

while True:
    for i in range(NUM_KEYS):
        if is_pressed(i):
            do_key(i)
            while is_pressed(i):  # wait for release
                time.sleep(0.02)
            time.sleep(0.15)

    # She stopped flipping channels: start the one she landed on.
    if state["pending_at"] is not None and time.monotonic() >= state["pending_at"]:
        state["pending_at"] = None
        chans = channels()
        if 0 <= state["channel"] < len(chans):
            launch_item(state["pending_key"], chans[state["channel"]])

    refresh = config.get("refresh_seconds", DEFAULT_REFRESH_SECONDS)
    if time.monotonic() - last_refresh > refresh and state["pending_at"] is None:
        last_refresh = time.monotonic()
        new = fetch_config()
        if new and new != config:
            config = new
            fix_channel_index()
            apply_idle_colors()
    time.sleep(0.02)