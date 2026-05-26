"""
Pushover notification helper.

Requires environment variables:
  PUSHOVER_USER_KEY  — your Pushover user key
  PUSHOVER_APP_TOKEN — app token for poly-potato app

Priority levels:
  -1 = quiet (no sound)
   0 = normal
   1 = high (bypasses quiet hours)
   2 = emergency (repeats every retry_secs until acknowledged)
"""

import json
import os
import urllib.request
import urllib.parse

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def send(title: str, message: str, priority: int = 1, url: str = "", url_title: str = "") -> bool:
    user_key  = os.environ.get("PUSHOVER_USER_KEY", "")
    app_token = os.environ.get("PUSHOVER_APP_TOKEN", "")

    if not user_key or not app_token:
        print(f"[alert] PUSHOVER_USER_KEY or PUSHOVER_APP_TOKEN not set — skipping push")
        print(f"[alert] {title}: {message}")
        return False

    payload: dict = {
        "token":   app_token,
        "user":    user_key,
        "title":   title,
        "message": message,
        "priority": priority,
    }
    if url:
        payload["url"]       = url
        payload["url_title"] = url_title or url
    if priority == 2:
        payload["retry"]  = 60   # retry every 60 seconds
        payload["expire"] = 3600 # give up after 1 hour

    try:
        data = urllib.parse.urlencode(payload).encode()
        req  = urllib.request.Request(PUSHOVER_URL, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            if result.get("status") == 1:
                print(f"[alert] pushed: {title}")
                return True
            print(f"[alert] pushover error: {result}")
            return False
    except Exception as e:
        print(f"[alert] failed to send pushover: {e}")
        return False
