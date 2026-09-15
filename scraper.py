"""
v2tel-scraper
=============
Fetches the latest messages from public Telegram channels and saves them
- together with any HIDDEN links (embedded hyperlinks, inline buttons,
web previews) - to a local log file.

Settings are read from config.toml.
Channels are read from database/channels.json (seeded with a default
list on the very first run).

Usage:
    python scraper.py
"""

import asyncio
import json
import os
import socks
import sys
import tomllib

from telethon import TelegramClient, errors
from telethon.tl.types import (
    MessageEntityTextUrl,
    MessageEntityMentionName,
)


CONFIG_FILE = 'config.toml'
CREDENTIALS_FILE = 'APIK.lock'

# Channels written to database/channels.json on the very first run.
# Edit database/channels.json afterwards to add/remove channels.
DEFAULT_CHANNELS = [
    '@freedom_iran_npv', '@projectXhttp', '@Pruuxi', '@v2rayNGt', '@VpnQavi', '@v2rayngfars',
    '@Freenetirano', '@share_config', '@NetAccount', '@BugFreeNet_Chat',
    '@saministamm', '@persianvpnhub', '@V2rayTun0', '@iiproxyIran', '@V2Ray_Tz',
    '@configmax', '@canfmelishkn', '@vpn_Click', '@YamYamProxy', '@v2ra_config',
    '@PeopleProxy', '@ConfigX2ray', '@ProxyMTProto', '@NT_Safe', '@F0X_CONFIG',
    '@VPN_SOLVE', '@BugFreeNet', '@Eag1e_YT', '@VPNCUSTOMIZE', '@Network_442', '@NetZone_ir', '@iRoProxy',
    '@Proxymelimon', '@V2rayEnglish', '@Net_3rf', '@MiniFoxTeam', '@AstroVPN_official', '@Proxxiy',
    '@DirectVPN', '@PrivateVPNs', '@Azadnet_Npv', '@PewezaTech', '@Config_magazine',
    '@MelliNet_NpV', '@vpnfredom', '@TapsiVpn', '@EsTunnel', '@v2rayng_fars', '@vmessorg', '@nufilter',
    '@iP_CF', '@ZagfaVPN', '@BegzarVPN', '@ircfspace', '@vpn_v2rayNG_fast', '@Maznet',
    '@SafeNet_Server', '@FAR30TM', '@free_netplus', '@eafciran7', '@V2Good', '@vpn_proxy400', '@canfigm',
    '@AzadNet', '@ShadowProxy66', '@vpn_proxy66', '@Begoo_VPN', '@Free_Nettm', '@Whitee_vpn',
    '@NormanV2ray', '@white_configs', '@wbnet', '@SudoFlux', '@arash_vpn', '@AstroVPNConfigBot',
    '@TeleProxyTele', '@MTPROTO_PROXY01', '@Pruuxii', '@v2rayngfarse',
]


# ================= CONFIG =================
def load_config(path=CONFIG_FILE):
    """Load config.toml and validate the essential fields."""
    if not os.path.exists(path):
        print(f"Config file not found: {path}")
        print("Create a config.toml next to scraper.py (see README.md).")
        sys.exit(1)

    with open(path, 'rb') as f:
        cfg = tomllib.load(f)
        
    return cfg

def load_credentials(path=CREDENTIALS_FILE):
    """
    Read api_id / api_hash from the personal APIK.lock file.

    Format: simple 'key = value' lines; '#' starts a comment.
    Keeps credentials out of config.toml so the project can be shared
    without leaking them.
    """
    if not os.path.exists(path):
        print(f"Credentials file not found: {path}")
        print("Create it next to scraper.py with these two lines:")
        print("    api_id = <your api_id>")
        print("    api_hash = <your api_hash>")
        print("Get them from https://my.telegram.org -> API development tools.")
        sys.exit(1)

    creds = {}
    with open(path, 'r', encoding='utf-8') as f:
        for raw in f:
            line = raw.split('#', 1)[0].strip()  # strip comments
            if not line or '=' not in line:
                continue
            key, _, value = line.partition('=')
            creds[key.strip().lower()] = value.strip().strip('"\'')

    api_id = creds.get('api_id', '')
    api_hash = creds.get('api_hash', '')

    if not api_id.isdigit():
        print(f"Invalid or missing 'api_id' in {path}.")
        sys.exit(1)
    if not api_hash or api_hash.startswith('PASTE_'):
        print(f"Invalid or missing 'api_hash' in {path}.")
        sys.exit(1)

    return int(api_id), api_hash

def build_proxy(pc):
    """Build the proxy tuple for Telethon, or None if disabled."""
    if not pc.get('enabled', False):
        return None
    ptype = socks.SOCKS5 if pc.get('type', 'socks5') == 'socks5' else socks.HTTP
    return (ptype, pc.get('host', '127.0.0.1'), int(pc.get('port', 10808)))


# ================= CHANNELS =================
def load_channels(db_dir, channels_file):
    """
    Load and normalize the channel list from database/channels.json.

    Accepted entry formats:
      "@username"                                        -> public channel
      {"title":..., "peer_id":..., "is_private": true}   -> private channel/group

    Returns normalized dicts:
      {'peer': '@username'|None, 'id': int|None, 'title': str, 'is_private': bool}
    """
    path = os.path.join(db_dir, channels_file)

    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except json.JSONDecodeError:
            print(f"Invalid JSON in {path} - fix or delete the file.")
            sys.exit(1)
        if not isinstance(raw, list) or not raw:
            print(f"No channels in {path}. Add usernames like \"@channel\".")
            sys.exit(1)
    else:
        os.makedirs(db_dir, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(DEFAULT_CHANNELS, f, ensure_ascii=False, indent=2)
        print(f"Seeded channel list: {path} ({len(DEFAULT_CHANNELS)} channels)")
        raw = list(DEFAULT_CHANNELS)

    entries = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            u = item.strip()
            entries.append({'peer': u, 'id': None, 'title': u,
                            'is_private': False})
        elif isinstance(item, dict):
            pid = item.get('peer_id') or item.get('id')
            title = (item.get('title') or item.get('username')
                     or (str(pid) if pid else None))
            if not title:
                continue
            entries.append({
                'peer': item.get('username') or item.get('peer'),
                'id': pid,
                'title': title,
                'is_private': bool(item.get('is_private')) or
                              (pid is not None and not item.get('username')),
            })
    return entries


# ================= LINK EXTRACTION =================
def extract_hidden_links(msg):
    """
    Extract all hidden/embedded links from a message.

    Returns a list of (source, label, url) tuples:
      - source: where the link was found ('hyperlink', 'button', 'preview')
      - label : the visible text associated with the link
      - url   : the actual destination URL
    """
    links = []
    text = msg.message or ""

    # 1) Embedded hyperlinks (text with a URL hidden underneath)
    for ent in (msg.entities or []):
        if isinstance(ent, MessageEntityTextUrl):
            label = text[ent.offset:ent.offset + ent.length]
            links.append(('hyperlink', label, ent.url))
        elif isinstance(ent, MessageEntityMentionName):
            label = text[ent.offset:ent.offset + ent.length]
            links.append(('user', label, f"tg://user?id={ent.user_id}"))

    # 2) Web preview (the link card Telegram shows under the message)
    preview = getattr(msg, 'web_preview', None)
    if preview is not None:
        url = getattr(preview, 'url', None)
        if url:
            links.append(('preview', 'web_preview', url))

    # 3) Inline buttons under the message
    try:
        if msg.buttons:
            for row in msg.buttons:
                for btn in row:
                    if getattr(btn, 'url', None):
                        links.append(('button', btn.text, btn.url))
    except Exception:
        pass  # buttons not available for this message

    return links

def filter_folder_only(entries, db_dir, paths):
    """
    Keep only entries confirmed to be inside the Telegram folder
    (database/folder_state.json, written by sync.py).
    """
    state_path = os.path.join(
        db_dir, paths.get('folder_state_file', 'folder_state.json'))
    if not os.path.exists(state_path):
        print("[!] folder_state.json not found (run sync.py first) - "
              "scraping the full channels.json list.")
        return entries
    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return entries

    users = {u.lower().lstrip('@') for u in state.get('usernames', [])}
    ids = set()
    for i in state.get('peer_ids', []):
        try:
            ids.add(int(i))
        except (TypeError, ValueError):
            pass

    kept, skipped = [], []
    for e in entries:
        peer = (e.get('peer') or '').lower().lstrip('@')
        eid = e.get('id')
        eid = int(eid) if _is_int(eid) else None
        ok = (peer and peer in users) or (eid is not None and (
            eid in ids or -eid in ids
            or (eid > 0 and -1000000000000 - eid in ids)))
        (kept if ok else skipped).append(e)

    if skipped:
        print(f"folder_only: {len(kept)} in folder, {len(skipped)} skipped:")
        for e in skipped[:10]:
            print(f"    - {e['title']}")
        if len(skipped) > 10:
            print(f"    ... and {len(skipped) - 10} more")
    return kept


def _is_int(v):
    try:
        int(v)
        return True
    except (TypeError, ValueError):
        return False
    
# ================= MAIN LOOP =================
async def run_scraper(cfg, channels):
    sc = cfg.get('scraper', {})
    paths = cfg.get('paths', {})

    message_limit = int(sc.get('message_limit', 10))
    request_delay = int(sc.get('request_delay', 1))

    output_dir = paths.get('output_dir', 'output')
    messages_file = paths.get('messages_file', 'public_messages.txt')
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, messages_file)

    saved_channels = 0
    total_messages = 0
    total_links = 0

    print(f"Fetching last {message_limit} messages "
          f"from {len(channels)} channels...")

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(f"=== PUBLIC CHANNELS LOG "
                f"(LAST {message_limit} MESSAGES EACH) ===\n\n")

        for index, ch in enumerate(channels, start=1):
            target = ch['id'] if ch['is_private'] else ch['peer']
            label = ch['title']

            try:
                print(f"[{index}/{len(channels)}] Fetching {label} ...")

                messages = await client.get_messages(target,
                                                     limit=message_limit)

                if not messages:
                    print(f"[{label}] -> No messages found or inaccessible.")
                    continue

                f.write(f"--- CHAT: {label} ---\n")

                for msg in reversed(messages):
                    has_text = bool(msg.message)
                    links = extract_hidden_links(msg)

                    if not has_text and not links:
                        continue

                    date_str = msg.date.strftime("%Y-%m-%d %H:%M")
                    f.write(f"[{date_str}]\n")

                    if has_text:
                        f.write(f"{msg.message}\n")

                    for source, lnk_label, url in links:
                        f.write(f"  >> LINK ({source}): "
                                f"\"{lnk_label}\" -> {url}\n")

                    total_links += len(links)
                    f.write("-" * 20 + "\n")

                f.write("\n\n")
                saved_channels += 1
                total_messages += len(messages)
                print(f"[{label}] -> Saved {len(messages)} messages.")

                await asyncio.sleep(request_delay)

            except errors.FloodWaitError as e:
                print(f"[{label}] -> FloodWait! Sleeping {e.seconds} seconds...")
                await asyncio.sleep(e.seconds + 2)

            except (ValueError, TypeError) as e:
                print(f"[{label}] -> Skipped: {e}")

            except Exception as e:
                print(f"[{label}] -> Failed: {e}")

    print("\n==================== SUMMARY ====================")
    print(f"Channels saved : {saved_channels}/{len(channels)}")
    print(f"Messages saved : {total_messages}")
    print(f"Links found    : {total_links}")
    print(f"Output file    : {output_path}")
    print("=================================================")
    
# ================= ENTRY POINT =================
def main():
    cfg = load_config()

    paths = cfg.get('paths', {})
    db_dir = paths.get('database_dir', 'database')
    channels = load_channels(db_dir, paths.get('channels_file', 'channels.json'))
    if cfg.get('scraper', {}).get('folder_only', False):
        p = cfg.get('paths', {})
        channels = filter_folder_only(
            channels, p.get('database_dir', 'database'), p)
        
    api_id, api_hash = load_credentials()

    global client
    client = TelegramClient(
        'v2tel_scraper',
        api_id,
        api_hash,
        proxy=build_proxy(cfg.get('proxy', {})),
    )

    with client:
        client.loop.run_until_complete(run_scraper(cfg, channels))
        
client = None  # created in main() after loading the config

if __name__ == '__main__':
    main()