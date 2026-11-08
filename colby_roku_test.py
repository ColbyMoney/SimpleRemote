#!/usr/bin/env python3
"""Desktop tester for the TV box: acts like pressing the NeoKey keys, using the same
shows.json and the same Roku commands as code.py. No hardware needed.

Usage:
  python colby_roku_test.py shows.json                 # interactive menu
  python colby_roku_test.py shows.json --ip 192.168.1.50
  python colby_roku_test.py shows.json --press 2       # press key 2 once
  python colby_roku_test.py shows.json --dry-run       # print requests, send nothing

Interactive mode works like the real remote: press a key number (0-9) and it fires
at once, no Enter needed. Also: a = list installed Roku apps and ids,
l = show the key and channel tables, q = quit.
Needs only the Python standard library. Your computer must be on the same
network as the Roku, with "Control by mobile apps" enabled on the Roku.

Note: on the real box, channel up/down waits a moment after the LAST press before
playing, so quick presses just move through the list. Here each press plays at once.
Only ONE Roku request is ever in flight at a time. A channel press that arrives while
a request is still going out is IGNORED (never queued). A channel press that arrives
between requests (for example while waiting for YouTube TV to open) is accepted and
CANCELS the older channel change, so the two never mix their button presses.
Other keys (volume, OK, power) wait their turn for the request slot.

If a launch request gets no answer (timeout or connection error), the same content is
launched again, up to 2 more times ("launch_retries" in the file changes that), for
every app. A reply with an error status (like 404) is not retried: asking again
cannot fix a wrong app id or link.

Keys:
   0  power         Power
   1  playpause     News
   2  channel_up    Channel Up
   3  channel_down  Channel Down
   4  key           OK (Yes, I'm still watching)
   5  key           Game Show
   6  key           Volume Up
   7  key           Volume Down
"""
import argparse
from contextlib import contextmanager
import json
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

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
channel_index = -1  # -1 = nothing played yet, same as the box
CHANNEL_ACTIONS = ("channel_up", "channel_down", "launch", "netflix")
http_lock = threading.Lock()   # held while a Roku request is in flight: one at a time
state_lock = threading.Lock()  # protects generation and channel_index
other_lock = threading.Lock()  # non-channel keys run one at a time, in the order pressed
generation = 0                 # bumped by every accepted channel press; older ones cancel
workers = []


class Cancelled(Exception):
    """A newer channel press replaced this channel change."""


def stale(gen):
    return gen is not None and gen != generation


class Roku:
    TIMEOUT = 5  # seconds to wait for the Roku to answer one request

    def __init__(self, ip, dry_run):
        self.ip = ip
        self.dry_run = dry_run

    def _send(self, path, gen):
        """One request. Returns "ok", "failed" (the Roku answered with an error),
        or "noreply" (timeout or connection error, worth trying again)."""
        url = "http://%s:8060%s" % (self.ip, path)
        with http_lock:  # never two requests at once
            if stale(gen):
                raise Cancelled()
            if self.dry_run:
                print("  [dry-run] POST", url)
                return "ok"
            try:
                req = urllib.request.Request(url, data=b"", method="POST")
                with urllib.request.urlopen(req, timeout=self.TIMEOUT) as r:
                    ok = r.status in (200, 202, 204)
                print("  POST %s -> %s" % (path, "ok" if ok else "failed"))
                return "ok" if ok else "failed"
            except urllib.error.HTTPError as e:  # the Roku replied, with an error
                print("  POST %s -> error: %s" % (path, e))
                return "failed"
            except (urllib.error.URLError, OSError) as e:  # timeout, unreachable, reset
                print("  POST %s -> no reply: %s" % (path, e))
                return "noreply"

    def post(self, path, gen=None):
        return self._send(path, gen) == "ok"

    def launch(self, path, config, retries, gen=None):
        """Launch content. If the Roku does not answer, send the same launch again, up
        to `retries` more times. The request slot is free between tries, so a newer
        channel press can still take over."""
        for attempt in range(retries + 1):
            result = self._send(path, gen)
            if result == "ok":
                return True
            if result == "failed":
                return False  # an error reply will not fix itself
            if attempt < retries:
                print("  no reply; launching the same content again (try %d of %d)"
                      % (attempt + 2, retries + 1))
                pause(5, self.dry_run, gen)
        return False

    def apps(self):
        url = "http://%s:8060/query/apps" % self.ip
        if self.dry_run:
            print("  [dry-run] GET", url)
            return
        try:
            with urllib.request.urlopen(url, timeout=6) as r:
                root = ET.fromstring(r.read())
            for app in root.findall("app"):
                print("  %8s  %s" % (app.get("id"), app.text))
        except (urllib.error.URLError, OSError, ET.ParseError) as e:
            print("  could not read app list:", e)


def pause(seconds, dry_run, gen=None):
    if stale(gen):
        raise Cancelled()
    if dry_run:
        print("  [dry-run] wait %ss" % seconds)
        return
    print("  waiting %ss ..." % seconds)
    end = time.time() + seconds
    while time.time() < end:  # wake often so a newer press can cancel us quickly
        if stale(gen):
            raise Cancelled()
        time.sleep(min(0.05, max(0.0, end - time.time())))


def launch_item(item, config, roku, gen=None):
    app_id = item.get("app_id") or APP_IDS.get(item.get("app", "netflix"))
    if not app_id:
        print("  unknown app %r; add it to APP_IDS or set app_id" % item.get("app"))
        return
    content_id = item.get("content_id", "")
    retries = int(config.get("launch_retries", 2))  # extra tries if the Roku does not answer
    if item.get("app") == "youtube_tv":
        path = "/launch/%s" % app_id
        if content_id:
            path += "?contentId=%s&mediaType=%s" % (content_id, item.get("type", "series"))
        if not roku.launch(path, retries, gen):
            print("  launch failed (key would flash red)")
            return
        pause(config.get("youtube_tv_boot_delay", 10), roku.dry_run, gen)
        try:
            downs = int(content_id)
        except (ValueError, TypeError):
            print("  youtube_tv needs content_id to be a number (key would flash red)")
            return
        gap = config.get("nav_delay", 0.25)
        roku.post("/keypress/Right", gen)
        pause(config.get("youtube_tv_guide_load_delay", 5), roku.dry_run, gen)
        for _ in range(downs):
            roku.post("/keypress/Down", gen)
            pause(gap, roku.dry_run, gen)
        roku.post("/keypress/Select", gen)
        pause(gap, roku.dry_run, gen)
    else:
        # Normal deep link. Put any needed URL encoding (like %3A) in content_id itself.
        path = "/launch/%s" % app_id
        if content_id:
            path += "?contentId=%s&mediaType=%s" % (content_id, item.get("type", "series"))
        if not roku.launch(path, retries, gen):
            print("  launch failed (key would flash red)")
            return


def change_channel(step, config, roku, gen=None):
    global channel_index
    chans = config.get("channels", [])
    if not chans:
        print("  no channels in the file (key would flash red)")
        return
    n = len(chans)
    with state_lock:
        if channel_index < 0:
            channel_index = 0 if step > 0 else n - 1
        else:
            channel_index = (channel_index + step) % n
        idx = channel_index
    item = chans[idx]
    print("  channel %d of %d: %s" % (idx + 1, n, item.get("content_name", "")))
    launch_item(item, config, roku, gen)


def do_key(i, config, roku, gen=None):
    keys = config.get("keys", [])
    if i < 0 or i >= len(keys):
        print("  no entry for key %d (that key would stay dark)" % i)
        return
    k = keys[i]
    action = k.get("action")
    name = k.get("content_name") or k.get("label") or ""
    print("Key %d: %s (%s)" % (i, name, action))
    if action == "none":
        print("  empty slot, does nothing")
    elif action in ("channel_up", "channel_down"):
        change_channel(1 if action == "channel_up" else -1, config, roku, gen)
    elif action in ("launch", "youtube_tv"):
        launch_item(k, config, roku, gen)
    elif action == "key":
        if not k.get("key_name") or not roku.post("/keypress/" + k["key_name"]):
            print("  failed (key would flash red)")
    elif action == "playpause":
        roku.post("/keypress/Play")
    elif action == "power":
        # One press toggles power. Nothing reads the TV's state: the Roku's own
        # "Power" key does the toggling. Set "key_name" on the Power key to change it.
        roku.post("/keypress/" + k.get("key_name", "Power"))
    elif action == "tv_key":
        # Sends a key straight to the Samsung TV (needs samsung_tv.py beside this file).
        key = k.get("samsung_key", "KEY_VOLDOWN")
        if roku.dry_run:
            print("  [dry-run] Samsung %s -> %s" % (key, config.get("colby_samsung_tv_ip")))
        else:
            import os
            import samsung_tv
            port = int(config.get("colby_samsung_tv_port", 8002))
            token_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "colby_samsung_tv_token.txt")
            token = config.get("colby_samsung_tv_token")
            if not token and port == 8002 and os.path.exists(token_file):
                token = open(token_file).read().strip() or None
            try:
                new_token = samsung_tv.send_key(config["colby_samsung_tv_ip"], key, token=token, port=port)
                if port == 8002 and new_token and new_token != token:
                    open(token_file, "w").write(new_token)
                print("  Samsung %s -> ok" % key)
            except (OSError, ValueError, KeyError) as e:
                print("  Samsung %s -> error: %s (key would flash red)" % (key, e))
    else:
        print("  unknown action %r (key would flash red)" % action)


@contextmanager
def keyboard_mode():
    """Mac/Linux: read one key at a time for the whole session (Windows needs nothing)."""
    if not sys.stdin.isatty():
        yield
        return
    try:
        import termios
        import tty
    except ImportError:
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)  # Ctrl-C still works
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def read_key():
    """Wait for one keypress and return it, without needing Enter.
    Returns None when input is closed (end of piped input)."""
    if not sys.stdin.isatty():
        while True:  # piped input: take the next non-blank character
            ch = sys.stdin.read(1)
            if not ch:
                return None
            if not ch.isspace():
                return ch
    try:
        import msvcrt  # Windows
    except ImportError:
        return sys.stdin.read(1)
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):  # arrow / function key: swallow its second code
        msvcrt.getwch()
        return ""
    return ch


def start_key(i, config, roku):
    """Run a key press in the background so the keyboard stays responsive."""
    global generation
    keys = config.get("keys", [])
    action = keys[i].get("action") if 0 <= i < len(keys) else None
    gen = None
    if action in CHANNEL_ACTIONS:
        if http_lock.locked():  # a request is still going out: drop this press
            print("Key %d ignored: a request is still in flight" % i)
            return
        with state_lock:
            generation += 1  # cancels any older channel change still waiting between requests
            gen = generation
        lock = None
    else:
        lock = other_lock
        lock.acquire()  # wait our turn; these are quick

    def work():
        try:
            do_key(i, config, roku, gen)
        except Cancelled:
            print("  channel change replaced by a newer press")
        except Exception as e:
            print("  key %d crashed: %s" % (i, e))
        finally:
            if lock:
                lock.release()

    t = threading.Thread(target=work, daemon=True)
    workers.append(t)
    t.start()


def list_keys(config):
    print("Keys:")
    for i, k in enumerate(config.get("keys", [])):
        label = k.get("content_name") or k.get("label") or ""
        print("  %2d  %-13s %s" % (i, k.get("action", ""), label))
    chans = config.get("channels", [])
    if chans:
        print("Channels (in order):")
        for n, c in enumerate(chans, 1):
            print("  %2d  %-8s %s" % (n, c.get("app", ""), c.get("content_name", "")))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("config", help="path to shows.json")
    p.add_argument("--ip", help="Roku IP (overrides colby_roku_ip in the file)")
    p.add_argument("--press", type=int, help="press this key number once, then exit")
    p.add_argument("--dry-run", action="store_true", help="print requests only")
    a = p.parse_args()

    with open(a.config) as f:
        config = json.load(f)
    ip = a.ip or config.get("colby_roku_ip")
    if not ip:
        sys.exit("No Roku IP: set colby_roku_ip in the file or pass --ip")
    roku = Roku(ip, a.dry_run)

    if a.press is not None:
        do_key(a.press, config, roku)
        return

    print("Roku at %s%s" % (ip, " (dry run)" if a.dry_run else ""))
    list_keys(config)
    print("Press a key number (0-9). a = apps, l = list, q = quit. No Enter needed.")
    with keyboard_mode():
        while True:
            try:
                ch = read_key()
            except (KeyboardInterrupt, EOFError):
                break
            if ch is None or ch.lower() == "q" or ch in ("\x03", "\x04"):
                break
            ch = ch.lower()
            if ch == "a":
                roku.apps()
            elif ch == "l":
                list_keys(config)
            elif ch.isdigit():
                start_key(int(ch), config, roku)
            elif ch.strip():
                print("  not a command: %r" % ch)
    if any(t.is_alive() for t in workers):
        print("Finishing the key that is still running (Ctrl-C to stop now) ...")
        try:
            for t in workers:
                t.join()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()