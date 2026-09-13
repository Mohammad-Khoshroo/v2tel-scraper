# whoami.py - shows which account the session belongs to
import asyncio
from telethon import TelegramClient
from scraper import load_credentials, load_config, build_proxy

cfg = load_config()
api_id, api_hash = load_credentials()

client = TelegramClient(
    'v2tel_scraper',
    api_id,
    api_hash,
    proxy=build_proxy(cfg.get('proxy', {})),
)

async def main():
    me = await client.get_me()
    print(f"Name  : {me.first_name}")
    print(f"Phone : {me.phone}")
    print(f"User  : @{me.username}")

with client:
    client.loop.run_until_complete(main())