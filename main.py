"""
v2tel-scraper - Main Orchestrator
=================================
Runs the three pipeline stages in order:

  1. scraper.py    -> fetch latest messages from Telegram
  2. extractor.py  -> parse links into the database files
  3. discover.py   -> audition newly discovered channels

Each stage runs as a separate process. IMPORTANT: stages never overlap,
because they all share the same Telegram session file (SQLite) - running
two clients at once would lock the session.

All settings come from config.toml. Credentials come from APIK.lock.

Usage:
    python main.py                     # full pipeline
    python main.py --skip-scraper      # extractor + discover only (no Telegram connection)
    python main.py --only-extract      # extractor only
    python main.py --only-discover     # discover only
    python main.py --status            # show database summary and exit
"""

import argparse
import json
import os
import subprocess
import sys
import tomllib

CONFIG_FILE = 'config.toml'

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


# ================= CONFIG =================
def load_config(path=CONFIG_FILE):
    """Load config.toml; fail fast with a clear message."""
    full = os.path.join(SCRIPTS_DIR, path)
    if not os.path.exists(full):
        print(f"Config file not found: {full}")
        print("Create a config.toml next to main.py (see README.md).")
        sys.exit(1)

    with open(full, 'rb') as f:
        cfg = tomllib.load(f)
    return cfg


# ================= STATUS =================
def count_lines(path):
    if not os.path.exists(path):
        return 0
    with open(path, 'r', encoding='utf-8') as f:
        return sum(1 for line in f if line.strip())


def show_status(cfg):
    """Print a summary of the current database files."""
    paths = cfg.get('paths', {})
    db_dir = paths.get('database_dir', 'database')
    output_dir = paths.get('output_dir', 'output')

    def db(name):
        return os.path.join(db_dir, name)

    rows = [
        ('Telegram proxies', db(paths.get('proxy_log', 'proxy.log'))),
        ('VPN config links', db(paths.get('config_log', 'config.log'))),
        ('External web links', db(paths.get('external_links_log',
                                            'external_links.log'))),
        ('TG invite links', db(paths.get('telegram_links_log',
                                         'telegram_links.log'))),
        ('Discovered channels', db(paths.get('discovered_channels_log',
                                             'discovered_channels.log'))),
    ]

    print("\n================ DATABASE STATUS ================")
    for label, path in rows:
        print(f"{label:<20}: {count_lines(path):>6}  "
              f"({os.path.relpath(path, SCRIPTS_DIR)})")

    # channels.json
    channels_path = db(paths.get('channels_file', 'channels.json'))
    try:
        with open(channels_path, 'r', encoding='utf-8') as f:
            n_channels = len(json.load(f))
    except Exception:
        n_channels = 0
    print(f"{'Active channels':<20}: {n_channels:>6}  "
          f"({os.path.relpath(channels_path, SCRIPTS_DIR)})")

    # channel_status.json breakdown
    status_path = db(paths.get('channel_status_file', 'channel_status.json'))
    try:
        with open(status_path, 'r', encoding='utf-8') as f:
            status = json.load(f)
        counts = {}
        for entry in status.values():
            s = entry.get('status', '?')
            counts[s] = counts.get(s, 0) + 1
        parts = ', '.join(f"{k}: {v}" for k, v in sorted(counts.items()))
        print(f"{'Auditioned (status)':<20}: {len(status):>6}  ({parts})")
    except Exception:
        print(f"{'Auditioned (status)':<20}: {0:>6}")

    # raw output file size
    raw = os.path.join(output_dir, paths.get('messages_file',
                                             'public_messages.txt'))
    if os.path.exists(raw):
        size_kb = os.path.getsize(raw) / 1024
        print(f"{'Raw messages file':<20}: {size_kb:>6.1f} KB  "
              f"({os.path.relpath(raw, SCRIPTS_DIR)})")
    print("=================================================")


# ================= STAGE RUNNER =================
def run_stage(script, description):
    """Run one pipeline stage as a separate process."""
    script_path = os.path.join(SCRIPTS_DIR, script)
    if not os.path.exists(script_path):
        print(f"[!] {script} not found next to main.py - skipping {description}.")
        return False

    print(f"\n{'=' * 60}")
    print(f"  STAGE: {description}  ({script})")
    print('=' * 60)

    result = subprocess.run([sys.executable, script_path])

    if result.returncode != 0:
        print(f"\n[!] Stage '{description}' exited with code "
              f"{result.returncode}.")
        print("    Continuing anyway (databases are cumulative).")
        return False
    return True


# ================= MAIN =================
def main():
    parser = argparse.ArgumentParser(
        description='v2tel-scraper orchestrator')
    parser.add_argument('--skip-scraper', action='store_true',
                        help='skip fetching (extract + discover only)')
    parser.add_argument('--only-extract', action='store_true',
                        help='run extractor only')
    parser.add_argument('--only-discover', action='store_true',
                        help='run discover only')
    parser.add_argument('--status', action='store_true',
                        help='show database summary and exit')
    args = parser.parse_args()

    cfg = load_config()

    if args.status:
        show_status(cfg)
        return

    print("===============================================")
    print("           v2tel-scraper pipeline")
    print("===============================================")

    # --- Stage 1: scrape ---
    if not (args.skip_scraper or args.only_extract or args.only_discover):
        run_stage('scraper.py', 'Fetch messages from Telegram')

    # --- Stage 2: extract ---
    if not args.only_discover:
        run_stage('extractor.py', 'Parse links into database')

    # --- Stage 3: discover ---
    if not args.only_extract:
        run_stage('discover.py', 'Audition discovered channels')

    # --- Final summary ---
    print("\n")
    show_status(cfg)


if __name__ == '__main__':
    main()