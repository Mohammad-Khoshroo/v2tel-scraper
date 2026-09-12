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
import subprocess
import sys
import tomllib

from telethon import TelegramClient, errors
from telethon.tl.types import (
    MessageEntityTextUrl,
    MessageEntityMentionName,
)


CONFIG_FILE = 'config.toml'

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

    tg = cfg.get('telegram', {})
    if not tg.get('api_id') or not tg.get('api_hash'):
        print("Please set 'api_id' and 'api_hash' in config.toml.")
        sys.exit(1)
    if str(tg.get('api_hash', '')).startswith('PASTE_'):
        print("Please replace the placeholder api_hash in config.toml.")
        sys.exit(1)

    return cfg


def build_proxy(pc):
    """Build the proxy tuple for Telethon, or None if disabled."""
    if not pc.get('enabled', False):
        return None
    ptype = socks.SOCKS5 if pc.get('type', 'socks5') == 'socks5' else socks.HTTP
    return (ptype, pc.get('host', '127.0.0.1'), int(pc.get('port', 10808)))


# ================= CHANNELS =================
def load_channels(db_dir, channels_file):
    """
    Load the channel list from database/channels.json.
    If the file does not exist, seed it with DEFAULT_CHANNELS.
    """
    path = os.path.join(db_dir, channels_file)

    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                channels = json.load(f)
        except json.JSONDecodeError:
            print(f"Invalid JSON in {path} - fix or delete the file.")
            sys.exit(1)
        if not isinstance(channels, list) or not channels:
            print(f"No channels in {path}. Add usernames like \"@channel\".")
            sys.exit(1)
        return channels

    # First run: seed the file with the default channel list
    os.makedirs(db_dir, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(DEFAULT_CHANNELS, f, ensure_ascii=False, indent=2)
    print(f"Seeded channel list: {path} ({len(DEFAULT_CHANNELS)} channels)")
    return list(DEFAULT_CHANNELS)


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

        for index, chat in enumerate(channels, start=1):
            try:
                print(f"[{index}/{len(channels)}] Fetching {chat} ...")

                # Fetch the latest messages (no need to join the channel)
                messages = await client.get_messages(chat, limit=message_limit)

                if not messages:
                    print(f"[{chat}] -> No messages found or inaccessible.")
                    continue

                f.write(f"--- CHAT: {chat} ---\n")

                # Write in chronological order (oldest first)
                for msg in reversed(messages):
                    has_text = bool(msg.message)
                    links = extract_hidden_links(msg)

                    if not has_text and not links:
                        continue  # media-only message with no text/links

                    date_str = msg.date.strftime("%Y-%m-%d %H:%M")
                    f.write(f"[{date_str}]\n")

                    if has_text:
                        f.write(f"{msg.message}\n")

                    # Write hidden links right after the message text
                    for source, label, url in links:
                        f.write(f"  >> LINK ({source}): \"{label}\" -> {url}\n")

                    total_links += len(links)
                    f.write("-" * 20 + "\n")

                f.write("\n\n")
                saved_channels += 1
                total_messages += len(messages)
                print(f"[{chat}] -> Saved {len(messages)} messages.")

                # Small delay to avoid Telegram rate limits
                await asyncio.sleep(request_delay)

            except errors.FloodWaitError as e:
                # Telegram asked us to slow down; wait and continue
                print(f"[{chat}] -> FloodWait! Sleeping {e.seconds} seconds...")
                await asyncio.sleep(e.seconds + 2)

            except (ValueError, TypeError) as e:
                # Usually: channel does not exist anymore or username changed
                print(f"[{chat}] -> Skipped: {e}")

            except Exception as e:
                print(f"[{chat}] -> Failed: {e}")

    print("\n==================== SUMMARY ====================")
    print(f"Channels saved : {saved_channels}/{len(channels)}")
    print(f"Messages saved : {total_messages}")
    print(f"Links found    : {total_links}")
    print(f"Output file    : {output_path}")
    print("=================================================")

    # Optionally run the extractor right after scraping
    if sc.get('auto_extract', True):
        print("\nRunning extractor...")
        subprocess.run([sys.executable, 'extractor.py'], check=False)
    
    # Optionally audition newly discovered channels
    if sc.get('auto_discover', True):
        print("\nRunning discovery validator...")
        subprocess.run([sys.executable, 'discover.py'], check=False)


# ================= ENTRY POINT =================
def main():
    cfg = load_config()

    paths = cfg.get('paths', {})
    db_dir = paths.get('database_dir', 'database')
    channels = load_channels(db_dir, paths.get('channels_file', 'channels.json'))

    global client
    tg = cfg.get('telegram', {})
    client = TelegramClient(
        tg.get('session', 'v2tel_scraper'),
        int(tg['api_id']),
        tg['api_hash'],
        proxy=build_proxy(cfg.get('proxy', {})),
    )

    with client:
        client.loop.run_until_complete(run_scraper(cfg, channels))


client = None  # created in main() after loading the config

if __name__ == '__main__':
    main()