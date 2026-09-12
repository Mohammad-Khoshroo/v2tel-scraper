"""
v2tel-scraper
==============================================================
Fetches the latest messages from public Telegram channels and saves
them along with any HIDDEN links (embedded hyperlinks, inline buttons,
web previews) to a local text file.

"""

import asyncio
import socks
import sys
from telethon import TelegramClient, errors
from telethon.tl.types import (
    MessageEntityTextUrl,
    MessageEntityMentionName,
)

# ================= CREDENTIALS =================
api_id = xxxxx
api_hash = 'xxxxxxxxxxxx'

# ================= SETTINGS =================
MESSAGE_LIMIT = 10

# ---- Proxy settings (see README section 3) ----
USE_PROXY = True
PROXY_TYPE = 'socks5'        # 'socks5' or 'http'
PROXY_HOST = '127.0.0.1'
PROXY_PORT = 10808

OUTPUT_FILE = 'public_messages.txt'
SESSION_NAME = 'scraper_session'
REQUEST_DELAY = 1


def build_proxy():
    """Build the proxy tuple for Telethon, or None if disabled."""
    if not USE_PROXY:
        return None
    ptype = socks.SOCKS5 if PROXY_TYPE == 'socks5' else socks.HTTP
    return (ptype, PROXY_HOST, PROXY_PORT)


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


target_chats = [
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
    '@TeleProxyTele', '@MTPROTO_PROXY01', '@Pruuxii', '@v2rayngfarse'
]

client = TelegramClient(
    SESSION_NAME,
    api_id,
    api_hash,
    proxy=build_proxy(),
)


async def main():
    print(f"Fetching last {MESSAGE_LIMIT} messages from {len(target_chats)} channels...")

    saved_channels = 0
    total_messages = 0
    total_links = 0

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(f"=== PUBLIC CHANNELS LOG (LAST {MESSAGE_LIMIT} MESSAGES EACH) ===\n\n")

        for index, chat in enumerate(target_chats, start=1):
            try:
                print(f"[{index}/{len(target_chats)}] Fetching {chat} ...")

                messages = await client.get_messages(chat, limit=MESSAGE_LIMIT)

                if not messages:
                    print(f"[{chat}] -> No messages found or inaccessible.")
                    continue

                f.write(f"--- CHAT: {chat} ---\n")

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

                await asyncio.sleep(REQUEST_DELAY)

            except errors.FloodWaitError as e:
                print(f"[{chat}] -> FloodWait! Sleeping {e.seconds} seconds...")
                await asyncio.sleep(e.seconds + 2)

            except (ValueError, TypeError) as e:
                print(f"[{chat}] -> Skipped: {e}")

            except Exception as e:
                print(f"[{chat}] -> Failed: {e}")

    print("\n==================== SUMMARY ====================")
    print(f"Channels saved : {saved_channels}/{len(target_chats)}")
    print(f"Messages saved : {total_messages}")
    print(f"Links found    : {total_links}")
    print(f"Output file    : {OUTPUT_FILE}")
    print("=================================================")
    
    import subprocess
    subprocess.run([sys.executable, 'extractor.py'], check=False)


with client:
    client.loop.run_until_complete(main())