#!/usr/bin/env python3
"""Desktop tester for the TV box: acts like pressing the NeoKey keys, using the same
shows.json and the same Roku commands as code.py. No hardware needed.

Usage:
  python colby_roku_test.py shows.json                 # interactive menu
  python colby_roku_test.py shows.json --ip 192.168.1.50
  python colby_roku_test.py shows.json --press 2       # press key 2 once
  python colby_roku_test.py shows.json --dry-run       # print requests, send nothing

Interactive commands: a key number, "apps" (list installed Roku apps and ids),
"list" (show the key and channel tables), "q" (quit).
Needs only the Python standard library. Your computer must be on the same
network as the Roku, with "Control by mobile apps" enabled on the Roku.

Note: on the real box, channel up/down waits a moment after the LAST press before
playing, so quick presses just move through the list. Here each press plays at once.

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
import json
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

APP_IDS = {
    "youtube_tv": 195316,
    "netflix": 12,
    "spotify": 22297
}
channel_index = -1  # -1 = nothing played yet, same as the box


class Roku:
    def __init__(self, ip, dry_run):
        self.ip = ip
        self.dry_run = dry_run

    def post(self, path):
        url = "http://%s:8060%s" % (self.ip, path)
        if self.dry_run:
            print("  [dry-run] POST", url)
            return True
        try:
            req = urllib.request.Request(url, data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=6) as r:
                ok = r.status in (200, 202, 204)
            print("  POST %s -> %s" % (path, "ok" if ok else "failed"))
            return ok
        except (urllib.error.URLError, OSError) as e:
            print("  POST %s -> error: %s" % (path, e))
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


def pause(seconds, dry_run):
    if dry_run:
        print("  [dry-run] wait %ss" % seconds)
    else:
        print("  waiting %ss ..." % seconds)
        time.sleep(seconds)


def launch_item(item, config, roku):
    app_id = item.get("app_id") or APP_IDS.get(item.get("app", "netflix"))
    if not app_id:
        print("  unknown app %r; add it to APP_IDS or set app_id" % item.get("app"))
        return
    content_id = item.get("content_id", "")
    if item.get("app") == "youtube_tv":
        path = "/launch/%s" % app_id
        if content_id:
            path += "?contentId=%s&mediaType=%s" % (content_id, item.get("type", "series"))
        if not roku.post(path):
            print("  launch failed (key would flash red)")
            return
        pause(item.get("select_delay", 5), roku.dry_run)
        try:
            downs = int(content_id)
        except (ValueError, TypeError):
            print("  youtube_tv needs content_id to be a number (key would flash red)")
            return
        gap = item.get("nav_delay", 0.25)
        roku.post("/keypress/Right")
        pause(item.get("guide_load_delay", 5), roku.dry_run)
        for _ in range(downs):
            roku.post("/keypress/Down")
            pause(gap, roku.dry_run)
        roku.post("/keypress/Select")
        pause(gap, roku.dry_run)
    else:
        # Normal deep link. Put any needed URL encoding (like %3A) in content_id itself.
        path = "/launch/%s" % app_id
        if content_id:
            path += "?contentId=%s&mediaType=%s" % (content_id, item.get("type", "series"))
        if not roku.post(path):
            print("  launch failed (key would flash red)")
            return


def change_channel(step, config, roku):
    global channel_index
    chans = config.get("channels", [])
    if not chans:
        print("  no channels in the file (key would flash red)")
        return
    n = len(chans)
    if channel_index < 0:
        channel_index = 0 if step > 0 else n - 1
    else:
        channel_index = (channel_index + step) % n
    item = chans[channel_index]
    print("  channel %d of %d: %s" % (channel_index + 1, n, item.get("content_name", "")))
    launch_item(item, config, roku)


def do_key(i, config, roku):
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
        change_channel(1 if action == "channel_up" else -1, config, roku)
    elif action in ("launch", "netflix"):
        launch_item(k, config, roku)
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
    while True:
        try:
            cmd = input("key number / apps / list / q > ").strip().lower()
        except EOFError:
            break
        if cmd in ("q", "quit", "exit"):
            break
        elif cmd == "apps":
            roku.apps()
        elif cmd == "list":
            list_keys(config)
        elif cmd.isdigit():
            do_key(int(cmd), config, roku)
        elif cmd:
            print("  not a command")


if __name__ == "__main__":
    main()