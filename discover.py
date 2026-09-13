"""
Channel Discovery Validator
===========================
After extractor.py has collected candidate channels in
database/discovered_channels.log, this script "auditions" each NEW
candidate: it fetches the last N messages of that channel and counts
how many proxy/config links those messages contain.

  - hits >= min_config_hits  -> the channel is appended to
                                database/channels.json, so every future
                                scraper run will scan it permanently.
  - otherwise                -> the channel is marked 'rejected' in
                                database/channel_status.json and is
                                never re-checked again.

The status file also stores the reason ('user', 'dead', 'private', ...)
and prevents re-checking accepted channels.

All settings come from config.toml ([discovery] and [paths]).

Usage:
    python discover.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telethon import TelegramClient, errors
from telethon.tl.types import Channel, User

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

# Reuse the regexes/cleaners from extractor and the link extractor
# from scraper so the detection logic stays identical everywhere.
from extractor import PROXY_RE, CONFIG_RE, B64_RE, clean, try_decode_base64
from scraper import extract_hidden_links, load_credentials

CONFIG_FILE = 'config.toml'


# ================= HELPERS =================
def load_config():
    with open(CONFIG_FILE, 'rb') as f:
        return tomllib.load(f)


def load_channels_list(path):
    """Load channels.json (a JSON array of '@username' strings)."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def save_channels_list(path, channels):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(channels, f, ensure_ascii=False, indent=2)


def load_status(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_status(path, status):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def load_lines(path):
    items = []
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    items.append(line)
    return items


# ================= HIT COUNTING =================
def count_hits(messages):
    """
    Count unique proxy/config links inside a batch of Telethon messages.

    Scans:
      - the plain message text
      - hidden hyperlinks / buttons / previews (via scraper.extract_hidden_links)
      - base64-encoded blobs pasted inside messages

    Returns the number of UNIQUE links found.
    """
    hits = set()

    def harvest(text):
        for m in PROXY_RE.finditer(text):
            link = clean(m.group(0))
            if len(link) > 15:
                hits.add(link)
        for m in CONFIG_RE.finditer(text):
            link = clean(m.group(0))
            if len(link) > 12:
                hits.add(link)

    for msg in messages:
        text = msg.message or ""
        harvest(text)

        # Hidden links (entities, buttons, preview)
        for _source, _label, url in extract_hidden_links(msg):
            harvest(url)

        # Base64 blobs inside the message body
        for m in B64_RE.finditer(text):
            decoded = try_decode_base64(m.group(0))
            if decoded:
                harvest(decoded)

    return len(hits)


# ================= MAIN =================
async def run():
    cfg = load_config()
    disc = cfg.get('discovery', {})
    paths = cfg.get('paths', {})
    tg = cfg.get('telegram', {})

    if not disc.get('enabled', True):
        print("Discovery is disabled in config.toml ([discovery] enabled).")
        return

    db_dir = paths.get('database_dir', 'database')
    channels_path = os.path.join(db_dir,
                                 paths.get('channels_file', 'channels.json'))
    status_path = os.path.join(db_dir,
                               paths.get('channel_status_file',
                                         'channel_status.json'))
    discovered_path = os.path.join(
        db_dir, paths.get('discovered_channels_log',
                          'discovered_channels.log'))

    messages_limit = int(disc.get('messages_per_channel', 100))
    min_hits = int(disc.get('min_config_hits', 1))
    max_per_run = int(disc.get('max_per_run', 20))
    request_delay = float(disc.get('request_delay', 1.5))

    # ---- Load state ----
    channels = load_channels_list(channels_path)
    known = {c.lower() for c in channels}
    status = load_status(status_path)
    candidates = load_lines(discovered_path)

    # A candidate is "pending" if it is not already a channel and has
    # never been checked before (no entry in the status file).
    pending = []
    for c in candidates:
        key = c.lower()
        if key in known:
            continue
        entry = status.get(key)
        if entry and entry.get('status') in ('accepted', 'rejected', 'dead'):
            continue
        pending.append(c)

    pending = pending[:max_per_run]

    print("================ DISCOVERY VALIDATOR ================")
    print(f"Candidates in log   : {len(candidates)}")
    print(f"Already known       : {len(known)} channels in channels.json")
    print(f"Pending this run    : {len(pending)} "
          f"(max_per_run = {max_per_run})")
    print("=====================================================\n")

    if not pending:
        print("Nothing to check. Done.")
        return

    # ---- Connect ----
    import socks
    pc = cfg.get('proxy', {})
    proxy = None
    if pc.get('enabled', False):
        ptype = socks.SOCKS5 if pc.get('type', 'socks5') == 'socks5' else socks.HTTP
        proxy = (ptype, pc.get('host', '127.0.0.1'), int(pc.get('port', 10808)))

    api_id, api_hash = load_credentials()
    client = TelegramClient(
        tg.get('session', 'v2tel_scraper'),
        api_id,
        api_hash,
        proxy=proxy,
    )
    
    accepted = 0
    rejected = 0

    async with client:
        for i, candidate in enumerate(pending, start=1):
            key = candidate.lower()
            print(f"[{i}/{len(pending)}] Auditioning {candidate} ...")

            # --- Resolve the entity first (is it even a channel?) ---
            try:
                entity = await client.get_entity(candidate)
            except errors.FloodWaitError as e:
                print(f"    [!] FloodWait {e.seconds}s - stopping this run, "
                      f"remaining candidates stay pending.")
                break
            except ValueError:
                print("    [x] Invalid/unresolvable username -> rejected")
                status[key] = {'status': 'rejected', 'reason': 'invalid',
                               'checked_at': datetime.now().isoformat()}
                save_status(status_path, status)
                rejected += 1
                continue
            except errors.RPCError as e:
                print(f"    [x] Telegram error: {str(e)[:60]} -> rejected")
                status[key] = {'status': 'rejected', 'reason': str(e)[:60],
                               'checked_at': datetime.now().isoformat()}
                save_status(status_path, status)
                rejected += 1
                continue

            # Users (personal accounts) are not interesting for us
            if isinstance(entity, User):
                print("    [x] This is a personal account, not a channel -> rejected")
                status[key] = {'status': 'rejected', 'reason': 'user',
                               'checked_at': datetime.now().isoformat()}
                save_status(status_path, status)
                rejected += 1
                continue

            # --- Fetch the last N messages and count config/proxy hits ---
            try:
                messages = await client.get_messages(entity,
                                                     limit=messages_limit)
            except errors.FloodWaitError as e:
                print(f"    [!] FloodWait {e.seconds}s - stopping this run.")
                break
            except (ValueError, errors.RPCError) as e:
                print(f"    [x] Cannot read messages: {str(e)[:60]} -> rejected")
                status[key] = {'status': 'rejected', 'reason': str(e)[:60],
                               'checked_at': datetime.now().isoformat()}
                save_status(status_path, status)
                rejected += 1
                continue

            hits = count_hits(messages)
            print(f"    > Read {len(messages)} messages, "
                  f"found {hits} unique proxy/config link(s)")

            if hits >= min_hits:
                channels.append(candidate)
                known.add(key)
                save_channels_list(channels_path, channels)
                status[key] = {'status': 'accepted', 'hits': hits,
                               'checked_at': datetime.now().isoformat()}
                print(f"    [+] ACCEPTED -> added to channels.json")
                accepted += 1
            else:
                status[key] = {'status': 'rejected', 'hits': hits,
                               'reason': 'not enough links',
                               'checked_at': datetime.now().isoformat()}
                print(f"    [-] Rejected (needs >= {min_hits} hits)")
                rejected += 1

            # Save status after every candidate (crash-safe)
            save_status(status_path, status)
            await asyncio.sleep(request_delay)

    print("\n==================== SUMMARY ====================")
    print(f"Checked   : {accepted + rejected}")
    print(f"Accepted  : {accepted}  -> database/channels.json")
    print(f"Rejected  : {rejected}  -> database/channel_status.json")
    print(f"Remaining : {len(candidates) - accepted - rejected} pending "
          f"for the next run")
    print("=================================================")


def main():
    asyncio.run(run())


if __name__ == '__main__':
    main()