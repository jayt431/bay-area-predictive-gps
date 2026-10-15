"""
Push notifications to your phone (Web Push).

The app is installable (manifest + service worker), and an installed app — or
a desktop browser — can receive notifications even when the page is closed.
That's what turns the overnight alert from an email into a buzz on your phone.

How it fits together:

- **VAPID keys** (`VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_SUBJECT`)
  identify this server to the push services. The public key goes to the
  browser; the private key signs every push and stays secret.
- When you turn notifications on, the browser's push service (Google's,
  Apple's, Mozilla's…) hands back a **subscription**: an address on that
  service plus encryption keys. It's stored here, behind the passcode, one per
  device.
- To notify, the server encrypts the message for each device and posts it to
  that address. A device that's gone (app removed, permission revoked) answers
  404 or 410 and is dropped.

**Only real push services are accepted as addresses.** The server makes a
request to whatever endpoint a subscription names, so an arbitrary URL would
let it be pointed anywhere. `_ALLOWED_HOSTS` is the list of push services the
major browsers use.

iPhones get Web Push only for sites added to the Home Screen (iOS 16.4+).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import store

SUBS_KEY = "push_subscriptions"
MAX_DEVICES = 20
TTL_S = 12 * 3600          # a night-before alert is stale by the next evening

# The push services behind Chrome/Android, Firefox, Safari/iOS and Edge.
_ALLOWED_HOSTS = ("fcm.googleapis.com", "android.googleapis.com",
                  "updates.push.services.mozilla.com", "push.services.mozilla.com",
                  "web.push.apple.com", "notify.windows.com")


def configured() -> bool:
    return bool(os.environ.get("VAPID_PUBLIC_KEY") and os.environ.get("VAPID_PRIVATE_KEY"))


def public_key() -> str | None:
    return os.environ.get("VAPID_PUBLIC_KEY")


def _valid(sub) -> dict | None:
    """A subscription as the browser's `subscription.toJSON()` gives it, or
    None if it isn't one — including any endpoint not on a known push service."""
    try:
        endpoint = str(sub["endpoint"])
        keys = sub["keys"]
        p256dh, auth = str(keys["p256dh"]), str(keys["auth"])
    except (KeyError, TypeError):
        return None
    url = urlparse(endpoint)
    host = (url.hostname or "").lower()
    if url.scheme != "https" or len(endpoint) > 1000 or not p256dh or not auth:
        return None
    if not any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS):
        return None
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh[:200], "auth": auth[:200]}}


def subscriptions(user_id: str = store.LOCAL_USER) -> list[dict]:
    try:
        return json.loads(store.get_setting(SUBS_KEY, user_id) or "[]")
    except ValueError:
        return []


def _save(subs: list[dict], user_id: str) -> None:
    store.set_setting(SUBS_KEY, json.dumps(subs[-MAX_DEVICES:]), user_id)


def add(sub: dict, label: str = "", user_id: str = store.LOCAL_USER) -> bool:
    clean = _valid(sub)
    if not clean:
        return False
    subs = [s for s in subscriptions(user_id) if s["endpoint"] != clean["endpoint"]]
    subs.append({**clean, "label": (label or "")[:80],
                 "added": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    _save(subs, user_id)
    return True


def remove(endpoint: str, user_id: str = store.LOCAL_USER) -> bool:
    subs = subscriptions(user_id)
    kept = [s for s in subs if s["endpoint"] != endpoint]
    _save(kept, user_id)
    return len(kept) != len(subs)


def send(payload: dict, user_id: str = store.LOCAL_USER) -> dict:
    """Notify every registered device. Returns counts and, for failures, the
    push service's status — never the endpoint, which identifies a device."""
    if not configured():
        return {"sent": 0, "failed": 0, "removed": 0, "reason": "VAPID keys not set"}
    subs = subscriptions(user_id)
    if not subs:
        return {"sent": 0, "failed": 0, "removed": 0, "reason": "no devices have notifications turned on"}

    from pywebpush import WebPushException, webpush       # imported late: only senders need it

    sent, failed, gone, reasons = 0, 0, [], []
    for sub in subs:
        try:
            webpush(subscription_info={"endpoint": sub["endpoint"], "keys": sub["keys"]},
                    data=json.dumps(payload),
                    vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                    vapid_claims={"sub": os.environ.get("VAPID_SUBJECT", "https://bay-area-predictive-gps.onrender.com")},
                    ttl=TTL_S, timeout=15)
            sent += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):                     # the device unsubscribed or is gone
                gone.append(sub["endpoint"])
            else:
                failed += 1
                reasons.append(f"{sub.get('label') or 'device'}: {status or exc.__class__.__name__}")
        except Exception as exc:                          # network trouble with one service
            failed += 1
            reasons.append(f"{sub.get('label') or 'device'}: {exc.__class__.__name__}")
    if gone:
        _save([s for s in subs if s["endpoint"] not in gone], user_id)
    result = {"sent": sent, "failed": failed, "removed": len(gone)}
    if reasons:
        result["reason"] = "; ".join(reasons)[:300]
    return result
