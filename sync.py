"""
sync.py - Mirror the Telegram folder "v2tel" <-> database/channels.json

Resolution strategy (flood-safe):
  1. ONE messages.GetDialogs pass builds a local entity index
     (username -> InputPeer, id -> InputPeer, title -> InputPeer).
     This NEVER calls contacts.ResolveUsername, so it cannot trigger
     the flood that per-entity get_entity() hits.
  2. channels.json entries are matched against that index locally.
  3. Only stragglers (max 10 per run) use get_entity as a fallback.

Policy (unchanged):
  folder missing          -> created once, seeded from channels.json
  folder exists           -> diffed, NEVER rebuilt
  manual folder removal   -> removed from channels.json (via journal)
  manual folder addition  -> added to channels.json
  personal chats / bots   -> ignored in both directions

channels.json entry format (what this file WRITES):
  {"username": "@channel", "id": null, "title": ..., "is_private": false}
  {"username": null, "id": -100..., "title": ..., "is_private": true}
Reading also accepts "peer" as an alias for "username" (older format)
and plain "@channel" strings.

Usage:
    python sync.py                 two-way sync
    python sync.py --from-folder   rewrite channels.json from the folder
    python sync.py --dry-run       show the plan, change nothing
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
import tomllib

# FIX 1: floodguard only defines record_flood(). The old name (record)
# crashed the import - and discover.py too, since it imports from here.
from floodguard import check_and_exit, record_flood

CONFIG_FILE = 'config.toml'
FOLDER_PEER_LIMIT = 100


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
    """
    Any accepted input -> internal dict:
      {'peer': '@username'|None, 'id': int|None,
       'title': str, 'is_private': bool}

    Accepts plain "@channel" strings and dicts with EITHER 'username'
    or 'peer' as the key (older files / discover.py use 'peer').
    """
    if isinstance(item, str) and item.strip():
        u = item.strip()
        return {'peer': u, 'id': None, 'title': u, 'is_private': False}
    if isinstance(item, dict):
        u = item.get('username') or item.get('peer')
        pid = item.get('peer_id') or item.get('id')
        title = item.get('title') or u or (str(pid) if pid else None)
        if not title:
            return None
        return {'peer': u, 'id': pid, 'title': title,
                'is_private': bool(item.get('is_private')) or
                              (pid is not None and not u)}
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


def entry_to_json(e):
    """
    Internal dict -> dict written to channels.json.

    FIX 3: the file key for usernames is 'username' — that is what
    scraper.load_channels() and discover.channel_keys() read. The old
    version wrote 'peer', which those scripts could not read (public
    channels came back as peer=None and were skipped everywhere).
    """
    return {'username': e.get('peer'), 'id': e.get('id'),
            'title': e.get('title'),
            'is_private': bool(e.get('is_private'))}


# ================= folder state journal =================
def load_state(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path, members, extra=None):
    """members: {marked_id: {'username':..., 'title':...}}"""
    data = {
        'peer_ids': sorted(members),
        'usernames': sorted({m['username'] for m in members.values()
                             if m.get('username')}),
        'titles': sorted({m['title'] for m in members.values()
                          if m.get('title')}),
        'updated_at': datetime.now().isoformat(),
    }
    prev = load_state(path)
    if 'join_failed' in prev:            # keep across runs
        data['join_failed'] = prev['join_failed']
    if extra:
        data.update(extra)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ================= folder helpers =================
def _folder_title(f):
    t = getattr(f, 'title', None)
    return t if isinstance(t, str) else getattr(t, 'text', None)


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
        folder.title = types.TextWithEntities(text=str(folder.title),
                                              entities=[])
        await client(functions.messages.UpdateDialogFilterRequest(
            id=folder.id, filter=folder))


async def create_folder(client, title, input_peers):
    filters = await get_filters(client)
    used = {f.id for f in filters if getattr(f, 'id', None) is not None}
    # FIX 4: folder IDs 2-9 are reserved by Telegram for system folders;
    # custom folders must use 10+.
    fid = next((i for i in range(10, 100) if i not in used), None)
    if fid is None:
        raise RuntimeError('No free folder id (10-99) - '
                           'delete an old folder first')
    folder = types.DialogFilter(id=fid, title=title,
                                include_peers=list(input_peers),
                                exclude_peers=[], pinned_peers=[])
    await _update_filter(client, folder)
    return folder


async def folder_membership(client, folder):
    members, ignored = {}, []
    for p in (folder.include_peers or []):
        mid = utils.get_peer_id(p)
        try:
            ent = await client.get_entity(p)   # folder peers are cached
        except Exception:
            members[mid] = {'input_peer': p, 'username': None, 'title': None}
            continue
        if isinstance(ent, User):
            ignored.append(getattr(ent, 'username', None)
                           or getattr(ent, 'first_name', None) or str(mid))
            continue
        uname = getattr(ent, 'username', None)
        members[mid] = {
            'input_peer': p,
            'username': ('@' + uname) if uname else None,
            'title': getattr(ent, 'title', None) or uname or str(mid),
        }
    return members, ignored


# ================= flood-safe entity index =================
async def build_dialog_index(client):
    """
    ONE GetDialogs pass -> {username: InputPeer}, {marked_id: InputPeer},
    {title: InputPeer}. No contacts.ResolveUsername involved.
    """
    by_username, by_id, by_title = {}, {}, {}
    async for d in client.iter_dialogs():
        ent = d.entity
        if ent is None or isinstance(ent, User):
            continue
        try:
            ip = d.input_entity
        except Exception:
            try:
                ip = utils.get_input_peer(ent)
            except Exception:
                continue
        uname = getattr(ent, 'username', None)
        if uname:
            by_username[uname.lower()] = ip
        title = getattr(ent, 'title', None)
        if title:
            by_title[title.lower()] = ip
        by_id[utils.get_peer_id(ent)] = ip
    return by_username, by_id, by_title


def _id_candidates(pid):
    yield pid
    yield -pid
    if pid > 0:
        yield -1000000000000 - pid     # legacy raw -> marked channel id


def match_with_index(entries, by_username, by_id, by_title):
    """Pure-local matching. Returns (matched, rest)."""
    matched, rest = {}, []
    for e in entries:
        ip = None
        if e['peer']:
            ip = by_username.get(e['peer'].lstrip('@').lower())
        if ip is None and e['id'] is not None:
            try:
                pid = int(e['id'])
            except (TypeError, ValueError):
                pid = None
            if pid is not None:
                for cand in _id_candidates(pid):
                    if cand in by_id:
                        ip = by_id[cand]
                        break
        if ip is None and e['title']:
            ip = by_title.get(e['title'].lower())   # legacy fallback
        if ip is None:
            rest.append(e)
        else:
            matched[utils.get_peer_id(ip)] = {'entry': e, 'input_peer': ip}
    return matched, rest


async def join_pending(client, entries, cap, sleep_s=2.5):
    """
    Join public channels that are missing from the dialog list and
    return them as {marked_id: {'entry', 'input_peer'}}.

    Telegram prunes non-dialog peers from chat folders on the next
    sync, so resolving a channel into the folder is temporary - only
    JOINING it makes the membership stick. Joins are rate-limited hard,
    so keep cap small and sleep between requests.
    Returns (resolved, failed_usernames).
    """
    resolved, failed = {}, []
    for i, e in enumerate(entries[:cap]):
        uname = e['peer']
        try:
            await client(functions.channels.JoinChannelRequest(uname))
            print(f"    [+] Joined {uname}")
        except errors.FloodWaitError as fw:
            record_flood(fw.seconds, 'sync/join')
            print(f"    [!] FloodWait {fw.seconds}s - joining stops, "
                  f"{len(entries) - i} entries stay pending.")
            break
        except errors.UserAlreadyParticipantError:
            pass    # already joined - fall through and resolve it
        except (errors.RPCError, TypeError, ValueError) as ex:
            # TypeError = username resolves to a user, not a channel
            print(f"    [!] Cannot join {uname}: {type(ex).__name__}")
            failed.append(uname.lstrip('@').lower())
            continue
        try:
            ent = await client.get_entity(uname)   # cached by join reply
        except errors.FloodWaitError as fw:
            record_flood(fw.seconds, 'sync/join-resolve')
            print(f"    [!] FloodWait {fw.seconds}s - resolving stops.")
            break
        except Exception as ex:
            print(f"    [!] Joined but cannot resolve {uname}: "
                  f"{str(ex)[:50]}")
            continue
        if isinstance(ent, User):
            print(f"    [-] {uname} is a personal chat - dropped.")
            failed.append(uname.lstrip('@').lower())
            continue
        resolved[utils.get_peer_id(ent)] = {
            'entry': e, 'input_peer': utils.get_input_peer(ent)}
        await asyncio.sleep(sleep_s)
    return resolved, failed


# ================= final entries =================
def _is_int(v):
    try:
        int(v)
        return True
    except (TypeError, ValueError):
        return False


def build_final_entries(members, keep_unresolved=()):
    """channels.json = exactly the folder (+ not-yet-verified entries)."""
    entries = []
    usernames = {(m.get('username') or '').lstrip('@').lower()
                 for m in members.values() if m.get('username')}
    member_ids = set(members)
    for e in keep_unresolved:
        peer = (e.get('peer') or '').lstrip('@').lower()
        if peer and peer in usernames:
            continue
        if e.get('id') is not None and _is_int(e['id']):
            if any(c in member_ids for c in _id_candidates(int(e['id']))):
                continue
        entries.append(entry_to_json(e))     # FIX 3: 'username' key
    for mid, m in members.items():
        if m.get('username'):
            u = m['username']
            entries.append({'username': u if u.startswith('@') else '@' + u,
                            'id': None,
                            'title': m.get('title') or u,
                            'is_private': False})
        else:
            entries.append({'username': None, 'id': mid,
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
              f"[manually removed from the folder]:")
        for t in removed:
            print(f"      - {t}")
    if hand:
        print(f"\n  + ADD to channels.json ({len(hand)}) "
              f"[manually added to the folder]:")
        for t in hand:
            print(f"      + {t}")
    if not (added or removed or hand):
        print(f"\n  Nothing to change ({unchanged} already in sync).")


# ================= public API (used by discover.py) =================
async def add_entry_to_folder(client, folder_title, entry, folder_state_path):
    """Add one entry to the folder. Returns True if the folder changed."""
    e = normalize_entry(entry)
    if not e:
        return False
    try:
        filters = await get_filters(client)
        folder = find_folder(filters, folder_title)
        if folder is None:
            try:
                ent = await client.get_entity(
                    e['peer'] if e['peer'] else int(e['id']))
            except errors.FloodWaitError as fw:
                record_flood(fw.seconds, 'sync/add_entry')
                return False
            except Exception:
                return False
            if isinstance(ent, User):
                return False
            # FIX 2: keep the returned folder object (was None before ->
            # AttributeError in folder_membership below).
            folder = await create_folder(client, folder_title,
                                         [utils.get_input_peer(ent)])
            members, _ = await folder_membership(client, folder)
            save_state(folder_state_path, members)
            return True

        members, _ = await folder_membership(client, folder)
        by_username = {(m.get('username') or '').lstrip('@').lower()
                       for m in members.values() if m.get('username')}
        peer = (e['peer'] or '').lstrip('@').lower()
        if (peer and peer in by_username) or (
                e['id'] is not None and _is_int(e['id']) and any(
                    c in members for c in _id_candidates(int(e['id'])))):
            return False                     # already in the folder
        if len(folder.include_peers) >= FOLDER_PEER_LIMIT:
            print(f"    [!] Folder is full ({FOLDER_PEER_LIMIT} peers).")
            return False
        try:
            ent = await client.get_entity(
                e['peer'] if e['peer'] else int(e['id']))
        except errors.FloodWaitError as fw:
            record_flood(fw.seconds, 'sync/add_entry')
            return False
        except Exception:
            return False
        if isinstance(ent, User):
            return False
        folder.include_peers.append(utils.get_input_peer(ent))
        await _update_filter(client, folder)
        members, _ = await folder_membership(client, folder)
        save_state(folder_state_path, members)
        return True
    except errors.FloodWaitError as fw:
        record_flood(fw.seconds, 'sync/add_entry')
        return False


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
    if check_and_exit('sync'):
        return

    db_dir = paths.get('database_dir', 'database')
    os.makedirs(db_dir, exist_ok=True)
    channels_path = os.path.join(
        db_dir, paths.get('channels_file', 'channels.json'))
    state_path = os.path.join(
        db_dir, paths.get('folder_state_file', 'folder_state.json'))

    from_folder = '--from-folder' in argv
    dry = '--dry-run' in argv

    entries = load_entries(channels_path)

    client = TelegramClient(tg.get('session', 'v2tel_scraper'),
                            *load_credentials(),
                            proxy=build_proxy(cfg.get('proxy', {})))

    async with client:
        try:
            filters = await get_filters(client)
        except errors.FloodWaitError as fw:
            record_flood(fw.seconds, 'sync/get_filters')
            print("[!] Try again after the flood expires.")
            return
        folder = find_folder(filters, folder_title)

        mode = ('REBUILD channels.json from folder' if from_folder
                else 'two-way sync')
        print("=" * 56)
        print(f"  Folder sync: '{folder_title}'   (mode: {mode})")
        print("=" * 56)

        # ---------- Case 1: folder missing ----------
        if folder is None:
            print(f"\nFolder '{folder_title}' does not exist yet.")
            if not entries:
                print("channels.json is empty as well - nothing to do.")
                return
            try:
                idx = await build_dialog_index(client)
            except errors.FloodWaitError as fw:
                record_flood(fw.seconds, 'sync/dialogs')
                return
            matched, rest = match_with_index(entries, *idx)
            join_failed = set(load_state(state_path).get('join_failed', []))
            joinable = [e for e in rest
                        if e['peer'] and not e.get('is_private')
                        and e['peer'].lstrip('@').lower() not in join_failed]
            per_run = int(sync_cfg.get('join_per_run', 10))
            joined, failed = {}, []
            if per_run > 0 and joinable and not dry:
                print(f"\n  {len(joinable)} channel(s) not joined - "
                      f"joining up to {per_run} this run.")
                joined, failed = await join_pending(client, joinable, per_run)
                join_failed |= set(failed)
            joined_keys = {id(v['entry']) for v in joined.values()}
            unresolved = [e for e in rest if id(e) not in joined_keys]
            db_map = {**matched, **joined}
            ips = [i['input_peer'] for i in db_map.values()]
            if not ips:
                print("[!] Nothing resolvable - folder was not created.")
                return
            if dry:
                print(f"[dry-run] Would create the folder with {len(ips)} peers.")
                return
            print(f"Creating it with {len(ips)} peer(s) ...")
            # FIX 2: keep the returned folder object (was None before).
            folder = await create_folder(client, folder_title, ips)
            members, _ign = await folder_membership(client, folder)
            save_state(state_path, members,
                       {'join_failed': sorted(join_failed)})
            save_entries(channels_path,
                         build_final_entries(members, unresolved))
            print(f"[+] Folder '{folder_title}' created with {len(ips)} peers.")
            return

        # ---------- Case 2: folder exists -> diff, never rebuild ----------
        members, ignored = await folder_membership(client, folder)
        if ignored:
            print(f"\n  Ignoring {len(ignored)} personal chat(s) in the folder.")
        curr = set(members)

        if from_folder:
            if dry:
                print(f"[dry-run] Would rewrite channels.json with "
                      f"{len(members)} peers.")
                return
            save_entries(channels_path, build_final_entries(members))
            save_state(state_path, members)
            print(f"[+] channels.json rewritten from the folder.")
            return

        try:
            idx = await build_dialog_index(client)
        except errors.FloodWaitError as fw:
            record_flood(fw.seconds, 'sync/dialogs')
            return
        matched, rest = match_with_index(entries, *idx)

        # Not-in-dialogs channels: JOIN them (Telegram deletes unjoined
        # peers from folders, so joining is what makes them stick).
        join_failed = set(load_state(state_path).get('join_failed', []))
        joinable = [e for e in rest
                    if e['peer'] and not e.get('is_private')
                    and e['peer'].lstrip('@').lower() not in join_failed]
        per_run = int(sync_cfg.get('join_per_run', 10))
        joined, failed = {}, []
        if per_run > 0 and joinable and not dry:
            print(f"\n  {len(joinable)} channel(s) not joined - "
                  f"joining up to {per_run} this run.")
            joined, failed = await join_pending(client, joinable, per_run)
            join_failed |= set(failed)
        # entries are dicts (unhashable) - track them by identity;
        # join_pending stores the very same objects it received
        joined_keys = {id(v['entry']) for v in joined.values()}
        unresolved = [e for e in rest if id(e) not in joined_keys]
        db_map = {**matched, **joined}
        db = set(db_map)

        prev_ids = set(load_state(state_path).get('peer_ids', []))
        removed_ids = (prev_ids & db) - curr      # manual removal in app
        # freshly joined peers count as ADD even if they were in a
        # previous folder state (Telegram pruned them because they
        # were not joined - that is not a manual removal)
        add_ids = (db - curr - prev_ids) | (set(joined) - curr)
        hand_ids = curr - db                      # manual addition in app

        _plan(
            removed=[db_map[i]['entry']['title'] for i in sorted(removed_ids)],
            added=[db_map[i]['entry']['title'] for i in sorted(add_ids)],
            hand=[members[i]['title'] for i in sorted(hand_ids)],
            unchanged=len(prev_ids & curr & db),
        )
        if dry:
            print("\n[dry-run] No changes were written.")
            return

        if add_ids:
            for mid in add_ids:
                if len(folder.include_peers) >= FOLDER_PEER_LIMIT:
                    print(f"    [!] Folder is full ({FOLDER_PEER_LIMIT}).")
                    break
                folder.include_peers.append(db_map[mid]['input_peer'])
            try:
                await _update_filter(client, folder)
                print(f"[+] Folder updated: +{len(add_ids)} peer(s) "
                      f"(in ONE request).")
            except errors.FloodWaitError as fw:
                record_flood(fw.seconds, 'sync/update_filter')
                print("[!] Folder unchanged - run again later.")
                return

        final_members = dict(members)
        for mid in add_ids:
            e = db_map[mid]['entry']
            final_members[mid] = {'username': e['peer'], 'title': e['title']}

        if removed_ids or hand_ids or add_ids:
            save_entries(channels_path,
                         build_final_entries(final_members, unresolved))
            print("[+] channels.json updated to mirror the folder.")

        save_state(state_path, final_members,
                   {'join_failed': sorted(join_failed)})
        print("\nDone.")


def main():
    asyncio.run(run(sys.argv[1:]))


if __name__ == '__main__':
    main()