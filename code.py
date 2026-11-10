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
    "live_tv_guide": 683311,
    "netflix": 12,
    "prime_video": 13,
    "hbo_max": 61322,
    "hulu": 2285,
    "disney_plus": 291097,
    "roku_channel": 151908,
    "kids_and_family": 606242,
    "peacock": 593099,
    "paramount_plus": 31440,
    "frndly_tv": 298229,
    "backdrops": 773622,
    "spotify": 22297,
    "youtube_tv": 195316,
    "apple_tv": 551012,
    "youtube": 837
}
DEFAULT_REFRESH_SECONDS = 60
DEFAULT_SETTLE_SECONDS = 0.0 # wait this long after the last channel press, then play

# ---------- hardware ----------
try:
    i2c = board.STEMMA_I2C()
except AttributeError:
    i2c = board.I2C()

# One NeoKey 1x4 plugged straight into the board (no multiplexer needed).
pad = NeoKey1x4(i2c, addr=0x30)
pad.pixels.brightness = 0.5
NUM_KEYS = 4


def is_pressed(i):
    return pad[i]


def parse_color(value):
    try:
        v = value.lstrip("#")
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except Exception:
        return (255, 255, 255)


def scale(rgb, f):
    return (int(rgb[0] * f), int(rgb[1] * f), int(rgb[2] * f))


def set_key(i, rgb):
    pad.pixels[i] = rgb


# ---------- network ----------
# Each profile = one Wi-Fi network plus the config URL to use on it.
PROFILES = []
for _n in (1, 2, 3):
    _ssid = os.getenv("WIFI_SSID_%d" % _n)
    if _ssid:
        PROFILES.append(
            {
                "ssid": _ssid,
                "password": os.getenv("WIFI_PASSWORD_%d" % _n, ""),
                "url": os.getenv("CONFIG_URL_%d" % _n),
                "name": os.getenv("PROFILE_NAME_%d" % _n, ""),
            }
        )
active = None  # the profile we are currently connected with


def roku_ip_for(data):
    """Roku IP for the active profile: "<name>_roku_ip" if present, else "roku_ip"."""
    prefix = active.get("name") if active else ""
    if prefix and data.get(prefix + "_roku_ip"):
        return data[prefix + "_roku_ip"]
    return data.get("roku_ip", "")


def connect_wifi():
    """Join the first listed network that is in range. Returns True when connected."""
    global active
    if wifi.radio.connected:
        if active is None:
            # Already joined (CircuitPython can auto-join at boot): find the matching profile.
            try:
                current = wifi.radio.ap_info.ssid
            except Exception:
                current = None
            for p in PROFILES:
                if p["ssid"] == current:
                    active = p
            if active is None and PROFILES:
                active = PROFILES[0]
        return True
    seen = set()
    try:
        for net in wifi.radio.start_scanning_networks():
            seen.add(net.ssid)
    except Exception as e:
        print("wifi scan error:", e)
    finally:
        try:
            wifi.radio.stop_scanning_networks()
        except Exception:
            pass
    for p in PROFILES:
        if p["ssid"] in seen:
            try:
                wifi.radio.connect(p["ssid"], p["password"])
                active = p
                return True
            except Exception as e:
                print("wifi error:", e)
    return False


print("profiles:", [p["ssid"] for p in PROFILES])
print("wifi connected:", connect_wifi(), "profile:", active.get("name") if active else None)
pool = socketpool.SocketPool(wifi.radio)
http = adafruit_requests.Session(pool, ssl.create_default_context())


def fetch_config():
    """Return a config dict from the web, or None on any failure."""
    if not PROFILES:
        print("no WIFI_SSID_1 found in settings.toml")
        return None
    if not connect_wifi() or not active:
        print("not connected to any listed Wi-Fi network")
        return None
    url = active.get("url")
    if not url:
        print("no CONFIG_URL for", active.get("ssid"))
        return None
    try:
        r = http.get(url, timeout=10)
        if r.status_code != 200:
            print("config fetch: HTTP", r.status_code, "for", url)
            r.close()
            return None
        data = r.json()
        r.close()
        if roku_ip_for(data) and "keys" in data:
            try:
                with open(CACHE_FILE, "w") as f:
                    json.dump(data, f)
            except OSError:
                pass  # filesystem is read-only while plugged into a computer
            return data
        print("config rejected: needs a 'keys' list and a roku ip for profile:", active.get("name"))
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
    ip = roku_ip_for(config)
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
    path = "/launch/%s" % app_id
    if content_id:
        path += "?contentId=%s&mediaType=%s" % (content_id, item.get("type", "series"))
    set_key(i, color)
    if item.get("app") == "youtube_tv":
        if not roku_post(path):
            fail(i)
            return
        blink(i, color, item.get("youtube_tv_boot_delay", 11))
        try:
            downs = int(content_id)
        except (ValueError, TypeError):
            fail(i)
            return
        gap = item.get("nav_delay", 0.5)
        roku_post("/keypress/Right")
        blink(i, color, item.get("youtube_tv_guide_load_delay", 4))
        for _ in range(downs):
            roku_post("/keypress/Down")
            time.sleep(gap)
        roku_post("/keypress/Select")
        time.sleep(gap)
    else:
        if not roku_post(path):
            fail(i)
            return
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
        time.sleep(0.2)
        apply_idle_colors()
    elif action == "playpause":
        if not roku_post("/keypress/Play"):
            fail(i)
        time.sleep(0.2)
        apply_idle_colors()
    elif action == "power":
        state["pending_at"] = None  # cancel any channel waiting to start
        if not roku_post("/keypress/Power"):  # toggles Roku/TV power via CEC
            fail(i)
        time.sleep(0.2)
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