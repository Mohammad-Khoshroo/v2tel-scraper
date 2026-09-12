"""
Config & Proxy Extractor
==============================
Reads the scraper output (public_messages.txt) and splits the results into
TWO separate files:

  1. proxies.txt  -> Telegram proxy links (MTProto / SOCKS)
                     Format: tg://proxy?..., https://t.me/proxy?...,
                             tg://socks?..., https://t.me/socks?...
                     Paste into the Telegram app to connect.

  2. configs.txt  -> VPN configuration URIs
                     Format: vmess://, vless://, trojan://, ss://, ssr://,
                             hysteria2://, tuic://, ...
                     Paste into v2rayN (Servers -> Import from clipboard).

A third file, external_links.txt, collects regular web links (possible
subscription URLs, sites) for manual review only - do NOT paste it
anywhere blindly.

Usage:
    python3 extract_configs.py [input_file]

Default input file: public_messages.txt
"""

import base64
import re
import sys

INPUT_FILE = sys.argv[1] if len(sys.argv) > 1 else 'public_messages.txt'

PROXIES_FILE = 'proxies.txt'         # Telegram proxy links
CONFIGS_FILE = 'configs.txt'         # VPN config URIs (v2rayN etc.)
LINKS_FILE = 'external_links.txt'    # web links, manual review only

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

# ---- Plain web links (possible subscription URLs etc.) ----
URL_RE = re.compile(r'https?://[^\s<>"\'`\\]+', re.IGNORECASE)

# Long base64-looking runs (channels often paste base64 config dumps)
B64_RE = re.compile(r'[A-Za-z0-9+/]{80,}={0,2}')

# Trailing punctuation the regexes may accidentally capture
TRIM = '.,;:)]}>\'"`*_'

# Domains that are never useful as configs or subscriptions
TELEGRAM_DOMAINS = (
    't.me', 'telegram.me', 'telegram.dog', 'telegram.org',
    'cdn-telegram.org', 'core.telegram.org',
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


def main():
    try:
        with open(INPUT_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Input file not found: {INPUT_FILE}")
        print("Run the scraper first, then run this script.")
        sys.exit(1)

    proxies = {}  # dicts keep insertion order + deduplicate exactly
    configs = {}
    links = {}

    def harvest(text: str):
        """Pull proxy links, config URIs and web links out of a chunk."""
        # 1) Telegram proxy links (must run BEFORE URL_RE, otherwise
        #    https://t.me/proxy?... would be treated as a plain URL)
        for m in PROXY_RE.finditer(text):
            link = clean(m.group(0))
            if len(link) > 15:
                proxies.setdefault(link)

        # 2) VPN config URIs
        for m in CONFIG_RE.finditer(text):
            link = clean(m.group(0))
            if len(link) > 12:
                configs.setdefault(link)

        # 3) Plain web links (exclude telegram/proxy domains)
        for m in URL_RE.finditer(text):
            url = clean(m.group(0))
            low = url.lower()
            if any(d in low for d in TELEGRAM_DOMAINS):
                continue
            # Skip the proxy links already captured above
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

    # ---- Write outputs ----
    with open(PROXIES_FILE, 'w', encoding='utf-8') as f:
        for link in proxies:
            f.write(link + '\n')

    with open(CONFIGS_FILE, 'w', encoding='utf-8') as f:
        for link in configs:
            f.write(link + '\n')

    with open(LINKS_FILE, 'w', encoding='utf-8') as f:
        for url in links:
            f.write(url + '\n')

    print("=============== SUMMARY ===============")
    print(f"Input file          : {INPUT_FILE}")
    print(f"Base64 blobs parsed : {b64_hits}")
    print(f"Telegram proxies    : {len(proxies)}  -> {PROXIES_FILE}")
    print(f"VPN config links    : {len(configs)}  -> {CONFIGS_FILE}")
    print(f"External web links  : {len(links)}  -> {LINKS_FILE}")
    print("=======================================")
    print(f"'{PROXIES_FILE}': paste a line into Telegram to connect via proxy.")
    print(f"'{CONFIGS_FILE}' : copy all -> v2rayN -> Servers -> Import from clipboard.")
    print(f"'{LINKS_FILE}'   : manual review only (possible subscription URLs).")


if __name__ == '__main__':
    main()