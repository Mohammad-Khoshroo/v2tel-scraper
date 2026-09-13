"""
sync.py - Keep the Telegram folder "v2tel" and database/channels.json equal
===========================================================================

Policy:
  * The folder IS the scraping scope. scraper.py reads ONLY channels.json,
    so channels.json must mirror the folder - nothing more.
  * Folder does not exist   -> CREATED once, seeded with channels.json.
  * Folder exists           -> NEVER rebuilt, only diffed:
        in channels.json, never seen in folder      -> add to folder
        was in folder last sync, gone now           -> manually removed
                                                      -> remove from channels.json
        in folder, not in channels.json             -> manually added
                                                      -> add to channels.json
  * Personal chats and bots are ignored in BOTH directions.

The last known folder membership is journaled in database/folder_state.json.
That journal is what tells "manually removed" apart from "newly added" -
do not delete it manually.

Usage:
    python sync.py                 normal two-way sync
    python sync.py --from-folder   one-shot: rebuild channels.json from the folder
    python sync.py --dry-run       show the planned diff, change nothing
"""

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime

import socks
from telethon import TelegramClient, errors, functions, types, utils
from telethon.tl.types import DialogFilter, User

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

CONFIG_FILE = 'config.toml'
FOLDER_PEER_LIMIT = 100   # Telegram hard limit per folder


# ================= config / credentials =================
def load_config(path=CONFIG_FILE):
    if not os.path.exists(path):
        print(f"Config file not found: {path}")
        sys.exit(1)
    with open(path, 'rb') as f:
        return tomllib.load(f)


def load_credentials(path='APIK.lock'):
    from scraper import load_credentials as _load
    return _load(path)


def build_proxy(pc):
    if not pc.get('enabled', False):
        return None
    ptype = socks.SOCKS5 if pc.get('type', 'socks5') == 'socks5' else socks.HTTP
    return (ptype, pc.get('host', '127.0.0.1'), int(pc.get('port', 10808)))


# ================= channels.json =================
def normalize_entry(item):
    """Accept the legacy formats and return the canonical dict form."""
    if isinstance(item, str) and item.strip():
        u = item.strip()
        return {'peer': u, 'id': None, 'title': u, 'is_private': False}
    if isinstance(item, dict):
        pid = item.get('peer_id') or item.get('id')
        title = (item.get('title') or item.get('username')
                 or (str(pid) if pid else None))
        if not title:
            return None
        return {'peer': item.get('username'), 'id': pid, 'title': title,
                'is_private': bool(item.get('is_private')) or
                              (pid is not None and not item.get('username'))}
    return None


def load_entries(path):
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        print(f"[!] Could not parse {path} - treating it as empty.")
        return []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        e = normalize_entry(item)
        if e:
            out.append(e)
    return out


def save_entries(path, entries):
    if os.path.exists(path):
        shutil.copy2(path, path + '.bak')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


# ================= folder state journal =================
def load_state(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path, peer_ids):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'peer_ids': sorted(peer_ids),
                   'updated_at': datetime.now().isoformat()},
                  f, ensure_ascii=False, indent=2)


# ================= folder helpers =================
def _folder_title(f):
    t = getattr(f, 'title', None)
    if isinstance(t, str):
        return t
    return getattr(t, 'text', None)


async def get_filters(client):
    res = await client(functions.messages.GetDialogFiltersRequest())
    return getattr(res, 'filters', res)


def find_folder(filters, title):
    for f in filters or []:
        if isinstance(f, DialogFilter) and _folder_title(f) == title:
            return f
    return None


async def _update_filter(client, folder):
    try:
        await client(functions.messages.UpdateDialogFilterRequest(
            id=folder.id, filter=folder))
    except TypeError:
        # very new layers serialise the title as TextWithEntities
        folder.title = types.TextWithEntities(text=str(folder.title),
                                              entities=[])
        await client(functions.messages.UpdateDialogFilterRequest(
            id=folder.id, filter=folder))


async def create_folder(client, title, input_peers):
    filters = await get_filters(client)
    used = {f.id for f in filters if getattr(f, 'id', None) is not None}
    fid = next(i for i in range(2, 100) if i not in used)
    folder = types.DialogFilter(id=fid, title=title,
                                include_peers=list(input_peers),
                                exclude_peers=[], pinned_peers=[])
    await _update_filter(client, folder)
    return folder


async def folder_membership(client, folder):
    """
    Read the folder's current peers.
    Returns (members, ignored_users) where members maps
    marked_id -> {'input_peer', 'username', 'title', 'resolved'}.
    """
    members, ignored = {}, []
    for p in (folder.include_peers or []):
        mid = utils.get_peer_id(p)
        try:
            ent = await client.get_entity(p)
        except Exception:
            members[mid] = {'input_peer': p, 'username': None,
                            'title': None, 'resolved': False}
            continue
        if isinstance(ent, User):
            ignored.append(getattr(ent, 'username', None)
                           or getattr(ent, 'first_name', None) or str(mid))
            continue
        uname = getattr(ent, 'username', None)
        members[mid] = {'input_peer': p,
                        'username': ('@' + uname) if uname else None,
                        'title': (getattr(ent, 'title', None) or uname
                                  or str(mid)),
                        'resolved': True}
    return members, ignored


# ================= entry <-> folder matching =================
def match_existing(entries, members):
    """Match entries against the folder WITHOUT any network calls."""
    by_username = {m['username'].lower(): mid
                   for mid, m in members.items() if m['username']}
    matched, rest = {}, []
    for e in entries:
        mid = None
        if e['peer'] and e['peer'].lower() in by_username:
            mid = by_username[e['peer'].lower()]
        elif e['id'] is not None:
            try:
                pid = int(e['id'])
            except (TypeError, ValueError):
                pid = None
            if pid in members:
                mid = pid
            elif pid and pid > 0:
                # legacy raw positive ids -> marked channel/chat ids
                for cand in (-1000000000000 - pid, -pid):
                    if cand in members:
                        mid = cand
                        break
        if mid is None:
            rest.append(e)
        else:
            matched[mid] = e
    return matched, rest


async def resolve_entries(client, entries, delay=0.4):
    """Resolve entries the local match could not handle (API calls)."""
    resolved, unresolved, dropped_users = {}, [], 0
    for idx, e in enumerate(entries):
        ent = None
        try:
            if e['peer']:
                ent = await client.get_entity(e['peer'])
            elif e['id'] is not None:
                ent = await client.get_entity(int(e['id']))
        except errors.FloodWaitError as fw:
            print(f"    [!] FloodWait {fw.seconds}s - keeping the remaining "
                  f"{len(entries) - idx} entries untouched for now.")
            unresolved.extend(entries[idx:])
            break
        except Exception as ex:
            print(f"    [!] Cannot resolve {e['title']}: {str(ex)[:60]}")
            unresolved.append(e)
        if ent is None:
            continue
        if isinstance(ent, User):
            print(f"    [-] {e['title']} is a personal chat - it will be "
                  f"dropped from channels.json.")
            dropped_users += 1
        else:
            resolved[utils.get_peer_id(ent)] = {
                'entry': e,
                'input_peer': utils.get_input_peer(ent),
            }
            await asyncio.sleep(delay)
    return resolved, unresolved, dropped_users


async def resolve_one(client, entry):
    """Resolve a single entry to an InputPeer (None if impossible/user)."""
    e = normalize_entry(entry)
    if not e:
        return None
    try:
        if e['peer']:
            ent = await client.get_entity(e['peer'])
        elif e['id'] is not None:
            ent = await client.get_entity(int(e['id']))
        else:
            return None
    except Exception as ex:
        print(f"    [!] Cannot resolve {e['title']}: {str(ex)[:60]}")
        return None
    if isinstance(ent, User):
        return None
    return utils.get_input_peer(ent)


def build_final_entries(members, keep_unresolved=()):
    """channels.json = exactly the folder (+ entries we could not verify)."""
    entries = []
    usernames = {m['username'].lower() for m in members.values()
                 if m.get('username')}
    for e in keep_unresolved:
        peer = (e.get('peer') or '').lower()
        eid = None
        if e.get('id') is not None:
            try:
                eid = int(e['id'])
            except (TypeError, ValueError):
                eid = None
        if peer and peer in usernames:
            continue
        if eid is not None and eid in members:
            continue
        entries.append(e)
    for mid, m in members.items():
        if m.get('username'):
            entries.append({'peer': m['username'], 'id': None,
                            'title': m.get('title') or m['username'],
                            'is_private': False})
        else:
            entries.append({'peer': None, 'id': mid,
                            'title': m.get('title') or str(mid),
                            'is_private': True})
    return entries


def _plan(removed, added, hand, unchanged):
    if added:
        print(f"\n  + ADD to Telegram folder ({len(added)}):")
        for t in added:
            print(f"      + {t}")
    if removed:
        print(f"\n  - REMOVE from channels.json ({len(removed)}) "
              f"[deleted from the folder manually]:")
        for t in removed:
            print(f"      - {t}")
    if hand:
        print(f"\n  + ADD to channels.json ({len(hand)}) "
              f"[added to the folder manually]:")
        for t in hand:
            print(f"      + {t}")
    if not (added or removed or hand):
        print("\n  Everything is already in sync.")


# ================= public API (used by discover.py) =================
async def add_entry_to_folder(client, folder_title, entry, folder_state_path):
    """Add one entry to the folder. Returns True if the folder changed."""
    e = normalize_entry(entry)
    if not e:
        return False
    filters = await get_filters(client)
    folder = find_folder(filters, folder_title)
    if folder is None:
        ip = await resolve_one(client, e)
        if ip is None:
            return False
        await create_folder(client, folder_title, [ip])
        members, _ = await folder_membership(client, folder)
        save_state(folder_state_path, members.keys())
        return True

    members, _ = await folder_membership(client, folder)
    matched, _rest = match_existing([e], members)
    if matched:
        return False          # already in the folder
    if len(folder.include_peers) >= FOLDER_PEER_LIMIT:
        print(f"    [!] Folder is full ({FOLDER_PEER_LIMIT} peers) - "
              f"skipping {e['title']}")
        return False
    ip = await resolve_one(client, e)
    if ip is None:
        return False
    folder.include_peers.append(ip)
    await _update_filter(client, folder)
    state = load_state(folder_state_path)
    save_state(folder_state_path,
               set(state.get('peer_ids', [])) | {utils.get_peer_id(ip)})
    return True


# ================= main =================
async def run(argv):
    cfg = load_config()
    sync_cfg = cfg.get('sync', {})
    paths = cfg.get('paths', {})
    tg = cfg.get('telegram', {})
    folder_title = sync_cfg.get('folder_title', 'v2tel')

    if sync_cfg.get('enabled', True) is False:
        print("Sync is disabled in config.toml ([sync] enabled = false).")
        return

    db_dir = paths.get('database_dir', 'database')
    os.makedirs(db_dir, exist_ok=True)
    channels_path = os.path.join(db_dir,
                                 paths.get('channels_file', 'channels.json'))
    state_path = os.path.join(
        db_dir, paths.get('folder_state_file', 'folder_state.json'))

    from_folder = '--from-folder' in argv
    dry = '--dry-run' in argv

    entries = load_entries(channels_path)
    state = load_state(state_path)
    prev = set(state.get('peer_ids', []))

    client = TelegramClient(tg.get('session', 'v2tel_scraper'),
                            *load_credentials(),
                            proxy=build_proxy(cfg.get('proxy', {})))

    async with client:
        filters = await get_filters(client)
        folder = find_folder(filters, folder_title)

        mode = 'REBUILD channels.json from folder' if from_folder \
            else 'two-way sync'
        print("=" * 56)
        print(f"  Folder sync: '{folder_title}'   (mode: {mode})")
        print("=" * 56)

        # ---------- Case 1: folder missing -> create + seed ----------
        if folder is None:
            print(f"\nFolder '{folder_title}' does not exist yet.")
            if not entries:
                print("channels.json is empty as well - nothing to do.")
                return
            print(f"Creating it with {len(entries)} entry/entries "
                  f"from channels.json ...")
            resolved, unresolved, _d = await resolve_entries(client, entries)
            ips = [info['input_peer'] for info in resolved.values()]
            if not ips:
                print("[!] Nothing resolvable - folder was not created.")
                return
            if dry:
                print(f"[dry-run] Would create the folder with {len(ips)} peers.")
                return
            await create_folder(client, folder_title, ips)
            members, _ignored = await folder_membership(client, folder)
            save_state(state_path, members.keys())
            save_entries(channels_path,
                         build_final_entries(members, unresolved))
            print(f"[+] Folder '{folder_title}' created and seeded.")
            return

        # ---------- Case 2: folder exists -> diff, never rebuild ----------
        members, ignored = await folder_membership(client, folder)
        if ignored:
            print(f"\n  Ignoring {len(ignored)} personal chat(s) in the "
                  f"folder (they are never scraped).")
        curr = set(members)

        if from_folder:
            if dry:
                print(f"[dry-run] Would rewrite channels.json with "
                      f"{len(members)} peers.")
                return
            save_entries(channels_path, build_final_entries(members))
            save_state(state_path, curr)
            print(f"[+] channels.json rewritten from the folder "
                  f"({len(members)} peers).")
            return

        matched, rest = match_existing(entries, members)
        resolved, unresolved, dropped = await resolve_entries(client, rest)
        db_map = {mid: {'entry': e, 'input_peer': members[mid]['input_peer']}
                  for mid, e in matched.items()}
        db_map.update(resolved)
        db = set(db_map)

        removed_ids = (prev & db) - curr      # was in folder, now gone
        add_ids = db - curr - prev            # new in channels.json
        hand_ids = curr - db                  # manually added to the folder

        _plan(
            removed=[db_map[i]['entry']['title'] for i in sorted(removed_ids)],
            added=[db_map[i]['entry']['title'] for i in sorted(add_ids)],
            hand=[members[i]['title'] for i in sorted(hand_ids)],
            unchanged=len(prev & curr & db),
        )

        if dry:
            print("\n[dry-run] No changes were written.")
            return

        # apply folder additions
        if add_ids:
            for mid in add_ids:
                if len(folder.include_peers) >= FOLDER_PEER_LIMIT:
                    print(f"    [!] Folder is full "
                          f"({FOLDER_PEER_LIMIT} peers) - skipping the rest.")
                    break
                folder.include_peers.append(db_map[mid]['input_peer'])
            await _update_filter(client, folder)
            print(f"[+] Folder updated: +{len(add_ids)} peer(s).")

        # apply database changes
        final_members = dict(members)
        for mid in add_ids:
            e = db_map[mid]['entry']
            final_members[mid] = {'username': e['peer'], 'title': e['title'],
                                  'resolved': True}

        if removed_ids or hand_ids or add_ids or dropped:
            save_entries(channels_path,
                         build_final_entries(final_members, unresolved))
            print("[+] channels.json updated to mirror the folder.")

        if (curr | add_ids) != prev:
            save_state(state_path, curr | add_ids)

        print("\nDone.")


def main():
    asyncio.run(run(sys.argv[1:]))


if __name__ == '__main__':
    main()