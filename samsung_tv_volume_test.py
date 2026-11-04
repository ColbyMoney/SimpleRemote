"""Try Samsung TV volume over Wi-Fi from your computer.

  python samsung_volume_test.py 192.168.1.50 up
  python samsung_volume_test.py 192.168.1.50 down
  python samsung_volume_test.py 192.168.1.50 mute
  python samsung_volume_test.py 192.168.1.50 up --port 8001   (older 2016 to 2018 sets)

The first run on port 8002 shows an "Allow?" box on the TV. Pick Allow with the
TV's own remote. The token is saved to colby_samsung_tv_token.txt next to this script.
"""
import argparse
import os
import sys

import samsung_tv

KEYS = {"up": "KEY_VOLUP", "down": "KEY_VOLDOWN", "mute": "KEY_MUTE", "power": "KEY_POWER"}
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "colby_samsung_tv_token.txt")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("ip", help="the Samsung TV's IP address")
    p.add_argument("key", help="up, down, mute, or a raw key like KEY_CHUP")
    p.add_argument("--port", type=int, default=8002, choices=[8001, 8002])
    p.add_argument("--times", type=int, default=1, help="how many presses")
    a = p.parse_args()

    key = KEYS.get(a.key, a.key)
    token = None
    if a.port == 8002 and os.path.exists(TOKEN_FILE):
        token = open(TOKEN_FILE).read().strip() or None
    if a.port == 8002 and not token:
        print("No saved token. Watch the TV and press Allow when asked.")

    try:
        for n in range(a.times):
            token = samsung_tv.send_key(a.ip, key, token=token, port=a.port)
            if a.port == 8002 and token:
                with open(TOKEN_FILE, "w") as f:
                    f.write(token)
            print("sent %s (%d of %d)" % (key, n + 1, a.times))
    except (OSError, ValueError) as e:
        sys.exit("FAILED: %s" % e)


if __name__ == "__main__":
    main()