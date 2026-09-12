"""
Config & Proxy Extractor
========================
Reads the scraper output (output/public_messages.txt) and splits the
results into FIVE database files:

  database/proxy.log               -> Telegram proxy links (MTProto / SOCKS)
                                      tg://proxy?..., https://t.me/socks?...
                                      Paste into the Telegram app to connect.

  database/config.log              -> VPN configuration URIs
                                      vmess://, vless://, trojan://, ss://, ...
                                      Paste into v2rayN
                                      (Servers -> Import from clipboard).

  database/external_links.log      -> regular web links (possible subscription
                                      URLs). Manual review only.

  database/telegram_links.log      -> raw Telegram entity links (channel
                                      links, private invites, bots).

  database/discovered_channels.log -> public channel usernames found inside
                                      messages, normalized to "@username".
                                      Review them and copy the good ones
                                      into database/channels.json.

Existing database files are MERGED and deduplicated: every run accumulates
results instead of replacing them. Deduplication is exact-string based, so
configs that differ in ANY parameter (address, port, uuid, ...) are kept.

Paths are read from config.toml.

Usage:
    python extractor.py [input_file]
"""

import base64
import os
import re
import sys

# Python 3.11+ ships tomllib; older versions need the 'tomli' package
try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

CONFIG_FILE = 'config.toml'


def load_paths():
    """Load output/database paths from config.toml."""
    with open(CONFIG_FILE, 'rb') as f:
        cfg = tomllib.load(f)
    paths = cfg.get('paths', {})

    output_dir = paths.get('output_dir', 'output')
    db_dir = paths.get('database_dir', 'database')
    return {
        'input': os.path.join(output_dir,
                              paths.get('messages_file', 'public_messages.txt')),
        'proxies': os.path.join(db_dir,
                                paths.get('proxy_log', 'proxy.log')),
        'configs': os.path.join(db_dir,
                                paths.get('config_log', 'config.log')),
        'links': os.path.join(db_dir,
                              paths.get('external_links_log',
                                        'external_links.log')),
        'tg_links': os.path.join(db_dir,
                                 paths.get('telegram_links_log',
                                           'telegram_links.log')),
        'discovered': os.path.join(db_dir,
                                   paths.get('discovered_channels_log',
                                             'discovered_channels.log')),
    }


# ---- Telegram proxy links (MTProto / SOCKS) ----
# Matches: tg://proxy?..., tg://socks?...,
#          https://t.me/proxy?..., https://t.me/socks?...
PROXY_RE = re.compile(
    r'(?<![\w.-])('
    r'tg://(?:proxy|socks)'
    r'|https?://t\.me/(?:proxy|socks)'
    r'|https?://telegram\.me/(?:proxy|socks)'
    r')\?[^\s<>"\'`\\]+',
    re.IGNORECASE,
)

# ---- VPN config URIs ----
# Longer scheme names first so e.g. 'hysteria2' is not cut to 'hysteria'
# and 'ssr' is not cut to 'ss'.
CONFIG_RE = re.compile(
    r'(?<![\w.-])('
    r'vmess|vless|trojan|hysteria2|hy2|hysteria|ssr|ss|tuic|juicity|socks'
    r')://[^\s<>"\'`\\]+',
    re.IGNORECASE,
)

# ---- Telegram entity links (channels, invites, bots, ...) ----
TG_LINK_RE = re.compile(
    r'(?<![\w.-])('
    r'https?://t\.me/[^\s<>"\'`\\]+'
    r'|https?://telegram\.me/[^\s<>"\'`\\]+'
    r'|tg://resolve\?[^\s<>"\'`\\]+'
    r'|tg://join\?[^\s<>"\'`\\]+'
    r')',
    re.IGNORECASE,
)

# ---- Plain web links (possible subscription URLs etc.) ----
URL_RE = re.compile(r'https?://[^\s<>"\'`\\]+', re.IGNORECASE)

# Long base64-looking runs (channels often paste base64 config dumps)
B64_RE = re.compile(r'[A-Za-z0-9+/]{80,}={0,2}')

# Trailing punctuation the regexes may accidentally capture
TRIM = '.,;:)]}>\'"`*_'

# Domains that are never useful as external/subscription links
TELEGRAM_DOMAINS = (
    't.me', 'telegram.me', 'telegram.dog', 'telegram.org',
    'cdn-telegram.org', 'core.telegram.org',
)

# t.me/<reserved_path> prefixes that are NOT channel usernames
RESERVED_TG_PATHS = {
    'share', 'addstickers', 'addemoji', 'addlist', 'addtheme',
    'setlanguage', 'iv', 'proxy', 'socks', 'joinchat', 'contact',
    'premium', 'privacy', 'tos', 'features', 'bg', 'boost',
}

# t.me/<username> or t.me/<username>/123 -> capture the username part
TG_CHANNEL_RE = re.compile(
    r'https?://(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{2,64})',
    re.IGNORECASE,
)

# tg://resolve?domain=<username> -> capture the username part
TG_RESOLVE_RE = re.compile(
    r'tg://resolve\?domain=([A-Za-z][A-Za-z0-9_]{2,64})',
    re.IGNORECASE,
)

# Private invite forms
TG_INVITE_RE = re.compile(
    r'(/joinchat/|/\+|tg://join\?invite=)',
    re.IGNORECASE,
)


def clean(link: str) -> str:
    """Trim trailing punctuation/markdown leftovers from a link."""
    return link.strip().rstrip(TRIM)


def try_decode_base64(block: str):
    """Base64-decode a block; return the text if it contains links."""
    block = block.strip()
    padded = block + '=' * ((-len(block)) % 4)
    for candidate in (block, padded):
        try:
            decoded = base64.b64decode(candidate, validate=False)
            text = decoded.decode('utf-8', errors='ignore')
            if '://' in text:
                return text
        except Exception:
            continue
    return None


def load_existing(path: str) -> dict:
    """Read an existing database file into an ordered dict (dedup set)."""
    items = {}
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    items.setdefault(line)
    return items


def classify_tg_link(link: str):
    """
    Classify a Telegram entity link.

    Returns:
        ('channel', '@username')  for public channel/group links
        ('invite', link)          for private invite links
        None                      for proxy/socks (handled elsewhere)
                                  or reserved/uninteresting paths
    """
    low = link.lower()

    # Proxy links already handled by PROXY_RE
    if 'proxy?' in low or 'socks?' in low:
        return None

    # Private invite links: t.me/+hash, t.me/joinchat/hash, tg://join?invite=..
    if TG_INVITE_RE.search(link):
        return ('invite', link)

    # tg://resolve?domain=<username>
    m = TG_RESOLVE_RE.match(link)
    if m:
        return ('channel', '@' + m.group(1))

    # https://t.me/<username> or https://t.me/<username>/<msg_id>
    m = TG_CHANNEL_RE.match(link)
    if m:
        username = m.group(1)
        if username.lower() in RESERVED_TG_PATHS:
            return None
        return ('channel', '@' + username)

    return None


def main():
    paths = load_paths()

    input_file = sys.argv[1] if len(sys.argv) > 1 else paths['input']

    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Input file not found: {input_file}")
        print("Run the scraper first, then run this script.")
        sys.exit(1)

    # dicts keep insertion order and deduplicate exactly
    proxies = load_existing(paths['proxies'])
    configs = load_existing(paths['configs'])
    links = load_existing(paths['links'])
    tg_links = load_existing(paths['tg_links'])
    discovered = load_existing(paths['discovered'])

    old_counts = (len(proxies), len(configs), len(links),
                  len(tg_links), len(discovered))

    def harvest(text: str):
        """Pull all link types out of a text chunk."""
        # 1) Telegram proxy links (must run FIRST, otherwise
        #    https://t.me/proxy?... would be caught as a plain URL)
        for m in PROXY_RE.finditer(text):
            link = clean(m.group(0))
            if len(link) > 15:
                proxies.setdefault(link)

        # 2) VPN config URIs
        for m in CONFIG_RE.finditer(text):
            link = clean(m.group(0))
            if len(link) > 12:
                configs.setdefault(link)

        # 3) Telegram entity links (channels, invites)
        for m in TG_LINK_RE.finditer(text):
            link = clean(m.group(0))
            if len(link) < 8:
                continue

            result = classify_tg_link(link)
            if result is None:
                continue

            kind, value = result
            if kind == 'channel':
                discovered.setdefault(value)
            else:  # 'invite'
                tg_links.setdefault(link)

        # 4) Plain web links (exclude telegram domains entirely)
        for m in URL_RE.finditer(text):
            url = clean(m.group(0))
            low = url.lower()
            if any(d in low for d in TELEGRAM_DOMAINS):
                continue
            # Skip proxy links already captured above
            if re.search(r'/?(proxy|socks)\?', low):
                continue
            links.setdefault(url)

    # 1) Scan the raw log text
    harvest(content)

    # 2) Scan base64-encoded blobs and harvest from their decoded text
    b64_hits = 0
    for m in B64_RE.finditer(content):
        decoded = try_decode_base64(m.group(0))
        if decoded:
            b64_hits += 1
            harvest(decoded)

    # ---- Write merged results back to the database files ----
    for path in (paths['proxies'], paths['configs'], paths['links'],
                 paths['tg_links'], paths['discovered']):
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)

    with open(paths['proxies'], 'w', encoding='utf-8') as f:
        for link in proxies:
            f.write(link + '\n')

    with open(paths['configs'], 'w', encoding='utf-8') as f:
        for link in configs:
            f.write(link + '\n')

    with open(paths['links'], 'w', encoding='utf-8') as f:
        for url in links:
            f.write(url + '\n')

    with open(paths['tg_links'], 'w', encoding='utf-8') as f:
        for link in tg_links:
            f.write(link + '\n')

    with open(paths['discovered'], 'w', encoding='utf-8') as f:
        for username in discovered:
            f.write(username + '\n')

    print("=============== SUMMARY ===============")
    print(f"Input file          : {input_file}")
    print(f"Base64 blobs parsed : {b64_hits}")
    print(f"Telegram proxies    : {len(proxies)} "
          f"(+{len(proxies) - old_counts[0]} new)  -> {paths['proxies']}")
    print(f"VPN config links    : {len(configs)} "
          f"(+{len(configs) - old_counts[1]} new)  -> {paths['configs']}")
    print(f"External web links  : {len(links)} "
          f"(+{len(links) - old_counts[2]} new)  -> {paths['links']}")
    print(f"TG invite links     : {len(tg_links)} "
          f"(+{len(tg_links) - old_counts[3]} new)  -> {paths['tg_links']}")
    print(f"Discovered channels : {len(discovered)} "
          f"(+{len(discovered) - old_counts[4]} new)  -> {paths['discovered']}")
    print("=======================================")
    print("proxy.log : paste a line into Telegram to connect via proxy.")
    print("config.log: copy all -> v2rayN -> Servers -> Import from clipboard.")
    print("external_links.log: manual review only (subscription URLs).")
    print("discovered_channels.log: review and copy good ones into")
    print("                         database/channels.json.")


if __name__ == '__main__':
    main()