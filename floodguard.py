"""
floodguard.py - Remember active Telegram FloodWaits across runs.

A FloodWait applies to the whole ACCOUNT, not one script. Retrying
before it expires can extend it, so every stage checks this file first.
"""

import json
import os
from datetime import datetime, timedelta

_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'database', 'flood_state.json')


def until():
    try:
        with open(_STATE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        dt = datetime.fromisoformat(data['flood_wait_until'])
        return dt if dt > datetime.now() else None
    except Exception:
        return None


def record_flood(seconds, source=''):
    """Store an active FloodWait (+60s safety margin)."""
    dt = datetime.now() + timedelta(seconds=int(seconds) + 60)
    os.makedirs(os.path.dirname(_STATE), exist_ok=True)
    with open(_STATE, 'w', encoding='utf-8') as f:
        json.dump({'flood_wait_until': dt.isoformat(),
                   'seconds': int(seconds), 'source': source,
                   'recorded_at': datetime.now().isoformat()},
                  f, ensure_ascii=False, indent=2)
    print(f"[floodguard] FloodWait recorded: until {dt:%Y-%m-%d %H:%M} "
          f"({int(seconds) / 3600:.1f}h, source: {source})")


def clear():
    if os.path.exists(_STATE):
        os.remove(_STATE)
        print("[floodguard] FloodWait expired - guard cleared.")


def check_and_exit(name='script'):
    dt = until()
    if dt is None:
        clear()
        return False
    left = (dt - datetime.now()).total_seconds()
    print(f"[!] Telegram FloodWait active until {dt:%Y-%m-%d %H:%M} "
          f"({left / 3600:.1f}h left). {name} skips this run.")
    return True