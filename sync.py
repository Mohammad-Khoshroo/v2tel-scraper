"""
Channel Sync
============
Keeps Telegram and the local channel list in sync:

  1. Reads current dialogs (what this account is a member of).
  2. Sync-back: channels the user manually added to the Telegram folder
     ([sync] folder_title, default "v2tel") on their phone are detected
     and appended to database/channels.json.
  3. Membership scan: every channel/group this account is a member of
     (including PRIVATE ones) is added to channels.json, so the scraper
     can read them. Controlled by [sync] include_private.
  4. Joins every PUBLIC channel from channels.json not joined yet.
  5. Rebuilds the folder so it contains exactly the channel list.

channels.json format (mixed, backward compatible):
    "@public_channel"                                     -> public
    {"title": "...", "peer_id": -100..., "is_private": true} -> private

Settings: [sync] in config.toml. Credentials: APIK.lock.

Usage:
    python sync.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime

from telethon import TelegramClient, errors, functions, types
from telethon import utils as tl_utils

from scraper import load_credentials, load_config, SESSION_NAME, load_channels

# Telegram free accounts allow max peers per folder
FOLDER_PEER_LIMIT = 100


# ---------- channels.json (mixed format) helpers ----------

def entry_key(entry):
    """Stable identity key for a normalized channel entry."""
    if entry.get('peer'):
        return entry['peer'].lower()
    return f"id:{entry['id']}"


def entries_to_raw(entries):
    """Convert normalized entries back to the channels.json mixed format."""
    out = []
    for e in entries:
        if e['is_private']:
            out.append({
                'title': e['title'],
                'peer_id': e['id'],
                'username': None,
                'is_private': True,
            })
        else:
            out.append(e['peer'])
    return out


def save_channels(path, entries):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(entries_to_raw(entries), f, ensure_ascii=False, indent=2)


# ---------- dialog filter (folder) helpers ----------

def filter_title_text(f):
    """Return folder title as plain text (handles str and TextWithEntities)."""
    t = getattr(f, 'title', None)
    if t is None:
        return None
    return getattr(t, 'text', t)


async def upsert_folder(client, entries, title):
    """Create or replace the folder so it contains exactly our entries."""
    result = await client(functions.messages.GetDialogFiltersRequest())

    existing = None
    existing_ids = []
    for f in result:
        if isinstance(f, (types.DialogFilterDefault, types.DialogFilterChatlist)):
            continue
        existing_ids.append(getattr(f, 'id', 0) or 0)
        if filter_title_text(f) == title:
            existing = f

    # Reuse the existing folder id, or pick a fresh unused one
    fid = existing.id if existing else (max(existing_ids, default=0) + 1)

    include_peers = []
    unresolved = 0
    for e in entries:
        try:
            if e['is_private']:
                peer = await client.get_input_entity(e['id'])
            else:
                peer = await client.get_input_entity(e['peer'])
            include_peers.append(peer)
        except Exception:
            unresolved += 1

    if unresolved:
        print(f"    [!] {unresolved} entry(ies) could not be resolved - "
              f"not added to the folder (kept in channels.json)")

    capped = False
    if len(include_peers) > FOLDER_PEER_LIMIT:
        include_peers = include_peers[:FOLDER_PEER_LIMIT]
        capped = True

    # Match the title type the current Telethon layer expects:
    # newer layers use TextWithEntities, older ones plain str.
    sample = filter_title_text(existing) if existing else None
    if sample is not None and not isinstance(sample, str):
        first = types.TextWithEntities(text=title, entities=[])
        second = title
    else:
        first = title
        second = types.TextWithEntities(text=title, entities=[])

    def make(t):
        return types.DialogFilter(
            id=fid, title=t,
            include_peers=include_peers,
            pinned_peers=[], exclude_peers=[],
            contacts=False, non_contacts=False, groups=False,
            broadcasts=False, bots=False,
            exclude_muted=False, exclude_read=False, exclude_new=False,
        )

    try:
        await client(functions.messages.UpdateDialogFilterRequest(
            id=fid, filter=make(first)))
    except Exception:
        # Retry with the other title representation (layer differences)
        await client(functions.messages.UpdateDialogFilterRequest(
            id=fid, filter=make(second)))

    if capped:
        print(f"    [!] Folder capped at {FOLDER_PEER_LIMIT} chats "
              f"(Telegram free-account limit). The scraper still scans "
              f"ALL entries from channels.json.")
    return len(include_peers)


# ---------- main ----------

async def run():
    cfg = load_config()
    sync_cfg = cfg.get('sync', {})
    if not sync_cfg.get('enabled', True):
        print("Sync is disabled in config.toml ([sync] enabled = false).")
        return

    folder_title = sync_cfg.get('folder_title', 'v2tel')
    include_private = bool(sync_cfg.get('include_private', True))
    join_delay = float(sync_cfg.get('join_delay', 1.0))

    paths = cfg.get('paths', {})
    db_dir = paths.get('database_dir', 'database')
    channels_path = os.path.join(db_dir,
                                 paths.get('channels_file', 'channels.json'))

    entries = load_channels(db_dir, paths.get('channels_file', 'channels.json'))
    known = {entry_key(e) for e in entries}
    print(f"channels.json: {len(entries)} entries")

    api_id, api_hash = load_credentials()
    pc = cfg.get('proxy', {})
    proxy = None
    if pc.get('enabled', False):
        import socks
        ptype = (socks.SOCKS5 if pc.get('type', 'socks5') == 'socks5'
                 else socks.HTTP)
        proxy = (ptype, pc.get('host', '127.0.0.1'), int(pc.get('port', 10808)))

    client = TelegramClient(SESSION_NAME, api_id, api_hash, proxy=proxy)

    added_from_folder = 0
    added_memberships = 0
    joined_count = 0

    async with client:
        # ---- 1) Read current dialogs ----
        print("\n[1/4] Reading your dialogs...")
        joined_usernames = set()
        memberships = []  # (marked_id, entity, title)
        async for d in client.iter_dialogs():
            ent = d.entity
            if isinstance(ent, types.User):
                continue  # personal chats / bots are not tracked
            uname = getattr(ent, 'username', None)
            if uname:
                joined_usernames.add(uname.lower())
            memberships.append((tl_utils.get_peer_id(ent), ent, d.name or ''))

        # ---- 2) Sync-back from the folder ----
        print(f"[2/4] Checking folder '{folder_title}' for manual additions...")
        try:
            filters_list = await client(
                functions.messages.GetDialogFiltersRequest())
        except Exception as e:
            filters_list = []
            print(f"    [!] Could not read folders: {e}")

        folder = None
        for f in filters_list:
            if isinstance(f, (types.DialogFilterDefault,
                              types.DialogFilterChatlist)):
                continue
            if filter_title_text(f) == folder_title:
                folder = f
                break

        if folder is not None:
            for peer in folder.include_peers:
                try:
                    ent = await client.get_entity(peer)
                except Exception:
                    continue
                if isinstance(ent, types.User):
                    continue
                uname = getattr(ent, 'username', None)
                username = f"@{uname}" if uname else None
                marked = tl_utils.get_peer_id(ent)
                key = username.lower() if username else f"id:{marked}"
                if key in known:
                    continue
                title = (getattr(ent, 'title', None) or username or str(marked))
                is_broadcast = bool(getattr(ent, 'broadcast', False))
                entries.append({
                    'peer': username if (username and is_broadcast) else None,
                    'id': marked,
                    'title': title,
                    'is_private': not (username and is_broadcast),
                })
                known.add(key)
                added_from_folder += 1
                print(f"    [+] From folder: {title} ({username or marked})")
        else:
            print("    (folder does not exist yet - it will be created)")

        # ---- 3) Membership scan (private channels & groups) ----
        if include_private:
            print("[3/4] Scanning your channel/group memberships...")
            for marked, ent, title in memberships:
                uname = getattr(ent, 'username', None)
                username = f"@{uname}" if uname else None
                key = username.lower() if username else f"id:{marked}"
                if key in known:
                    continue
                title = title or username or str(marked)
                is_broadcast = bool(getattr(ent, 'broadcast', False))
                entries.append({
                    'peer': username if (username and is_broadcast) else None,
                    'id': marked,
                    'title': title,
                    'is_private': not (username and is_broadcast),
                })
                known.add(key)
                added_memberships += 1
                kind = 'channel' if is_broadcast else 'group'
                print(f"    [+] Membership: {title} [{kind}]")
        else:
            print("[3/4] Membership scan disabled "
                  "([sync] include_private = false)")

        # ---- 4) Join missing public channels ----
        print("[4/4] Joining missing public channels...")
        for e in entries:
            if e['is_private'] or not e['peer']:
                continue
            uname = e['peer'].lstrip('@').lower()
            if uname in joined_usernames:
                continue
            try:
                await client(functions.channels.JoinChannelRequest(e['peer']))
                joined_usernames.add(uname)
                joined_count += 1
                print(f"    [+] Joined: {e['peer']}")
            except errors.UserAlreadyParticipantError:
                joined_usernames.add(uname)
            except errors.FloodWaitError as ex:
                print(f"    [!] FloodWait {ex.seconds}s - "
                      f"stopping joins for this run.")
                break
            except errors.ChannelPrivateError:
                print(f"    [!] {e['peer']}: invite-only, cannot join "
                      f"(kept in list).")
            except Exception as ex:
                print(f"    [!] {e['peer']}: {str(ex)[:60]}")
            await asyncio.sleep(join_delay)

        # ---- Save channels.json ----
        save_channels(channels_path, entries)
        print(f"\nchannels.json updated: {len(entries)} entries "
              f"(+{added_from_folder} from folder, "
              f"+{added_memberships} memberships, "
              f"{joined_count} newly joined)")

        # ---- Build/refresh the folder ----
        print(f"Updating folder '{folder_title}' ...")
        try:
            n = await upsert_folder(client, entries, folder_title)
            print(f"    [OK] Folder now contains {n} chats.")
        except Exception as e:
            print(f"    [!] Folder update failed: {e}")


def main():
    asyncio.run(run())


if __name__ == '__main__':
    main()