import os
import re
import json
import logging
import random
import base64
import asyncio
from datetime import datetime, timedelta
from telethon.sync import TelegramClient
from telethon.tl.types import Message, MessageEntityTextUrl, MessageEntityUrl
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.sessions import StringSession
from telethon.errors import ChannelInvalidError, PeerIdInvalidError
from collections import defaultdict

SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING", None)
API_ID = os.getenv("TELEGRAM_API_ID", None)
API_HASH = os.getenv("TELEGRAM_API_HASH", None)
CHANNELS_FILE = "telegram_channels.json"
LOG_DIR = "Logs"
OUTPUT_DIR = "Config"
INVALID_CHANNELS_FILE = os.path.join(LOG_DIR, "invalid_channels.txt")
STATS_FILE = os.path.join(LOG_DIR, "channel_stats.json")
CONFIG_PATTERNS = {
    "vless": r"vless://[^\s\n]+",
    "vmess": r"vmess://[^\s\n]+",
    "shadowsocks": r"ss://[^\s\n]+",
    "trojan": r"trojan://[^\s\n]+",
    "socks": r"(?:https?:\/\/t\.me\/socks\?|tg:\/\/socks\?|socks[45]?:\/\/)[^\s\n]+",
    "http": r"(?:https?:\/\/)(?:[a-zA-Z0-9_\-\.]+:[a-zA-Z0-9_\-\.]+@)?[a-zA-Z0-9_\-\.]+:\d{2,5}[^\s\n]*",
    "mtproto": r"(?:https?:\/\/t\.me\/proxy\?|tg:\/\/proxy\?)[^\s\n]+"
}
PROXY_PATTERN = r"(?:https?:\/\/t\.me\/proxy\?|tg:\/\/proxy\?|https?:\/\/t\.me\/socks\?|tg:\/\/socks\?)[^\s\n]+"

OPERATORS = {
    "همراه اول": "HamrahAval",
    "#همراه_اول": "HamrahAval",
    "ایرانسل": "Irancell",
    "#ایرانسل": "Irancell",
    "مخابرات": "Makhaberat",
    "#مخابرات": "Makhaberat",
    "سامانتل": "Samantel",
    "#سامانتل": "Samantel",
    "سامان تل": "Samantel",
    "#سامان_تل": "Samantel",
    "شاتل": "Shatel",
    "#شاتل": "Shatel",
}

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.handlers = []
file_handler = logging.FileHandler(os.path.join(LOG_DIR, "collector.log"), mode='w', encoding='utf-8')
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
file_handler.setLevel(logging.DEBUG)
logger.addHandler(file_handler)

def load_channels():
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        channels = json.load(f)
    logger.info(f"Loaded {len(channels)} channels from {CHANNELS_FILE}")
    return channels

def update_channels(channels):
    with open(CHANNELS_FILE, "w", encoding="utf-8") as f:
        json.dump(channels, f, ensure_ascii=False, indent=4)
    logger.info(f"Updated {CHANNELS_FILE} with {len(channels)} channels")

if not os.path.exists(OUTPUT_DIR):
    logger.info(f"Creating directory: {OUTPUT_DIR}")
    os.makedirs(OUTPUT_DIR)

def extract_server_address(config, protocol):
    try:
        if protocol == "vmess":
            config_data = config.split("vmess://")[1]
            decoded = base64.b64decode(config_data).decode("utf-8")
            config_json = json.loads(decoded)
            return config_json.get("add", "")
        else:
            match = re.search(r"@([^\s:]+):", config)
            if match:
                return match.group(1)
            match = re.search(r"{}://[^\s@]+?([^\s:]+):".format(protocol), config)
            if match:
                return match.group(1)
        return None
    except Exception as e:
        logger.error(f"Failed to extract server address from {config}: {str(e)}")
        return None

def extract_proxies_from_message(message):
    proxies = []
    text = message.message or ""
    full_text = text
    if hasattr(message, 'entities') and message.entities:
        for entity in message.entities:
            if isinstance(entity, (MessageEntityTextUrl, MessageEntityUrl)):
                if hasattr(entity, 'url') and entity.url:
                    full_text += "\n" + entity.url
                else:
                    offset = entity.offset
                    length = entity.length
                    full_text += "\n" + text[offset:offset+length]

    proxies += re.findall(PROXY_PATTERN, full_text)
    return proxies

def detect_operator(text):
    text_lower = text.lower()
    for keyword, op in OPERATORS.items():
        if keyword.lower() in text_lower:
            return op
    return None

async def fetch_configs_and_proxies_from_channel(client, channel):
    configs = {k: [] for k in CONFIG_PATTERNS.keys()}
    config_timeline = []
    operator_configs = defaultdict(list)
    proxies = []
    proxy_timeline = []
    try:
        channel_entity = await resolve_channel_target(client, channel)
    except (ChannelInvalidError, PeerIdInvalidError, ValueError) as e:
        logger.error(f"Channel {channel} does not exist or is inaccessible: {str(e)}")
        return configs, config_timeline, operator_configs, proxies, proxy_timeline, False
    except Exception as e:
        logger.error(f"Channel {channel} could not be resolved: {str(e)}")
        return configs, config_timeline, operator_configs, proxies, proxy_timeline, False

    try:
        message_count = 0
        configs_found_count = 0
        today = datetime.now().date()
        min_proxy_date = today - timedelta(days=7)

        async for message in client.iter_messages(channel_entity, limit=150):
            message_count += 1
            if message.date:
                message_date = message.date.date()
            else:
                continue

            if isinstance(message, Message) and message.message:
                text = message.message

                full_text = text
                if hasattr(message, 'entities') and message.entities:
                    for entity in message.entities:
                        if isinstance(entity, (MessageEntityTextUrl, MessageEntityUrl)):
                            if hasattr(entity, 'url') and entity.url:
                                full_text += "\n" + entity.url
                            else:
                                offset = entity.offset
                                length = entity.length
                                full_text += "\n" + text[offset:offset+length]

                operator = detect_operator(full_text)

                for protocol, pattern in CONFIG_PATTERNS.items():
                    matches = list(set(re.findall(pattern, full_text)))
                    if matches:
                        logger.info(f"[{channel}] Found {len(matches)} {protocol} configs in message {message.id}")
                        print(f"✅ [{channel}] Found {len(matches)} {protocol} configs")
                        configs[protocol].extend(matches)
                        for config in matches:
                            config_timeline.append({
                                "protocol": protocol.capitalize(),
                                "config": config,
                                "publisher": str(channel),
                                "source": str(channel),
                                "time": message.date.isoformat() if message.date else datetime.now().isoformat()
                            })
                        configs_found_count += len(matches)
                        if operator:
                            for config in matches:
                                operator_configs[operator].append(config)

                if message_date >= min_proxy_date:
                    proxy_links = extract_proxies_from_message(message)
                    if proxy_links:
                        logger.info(f"[{channel}] Found {len(proxy_links)} proxies in message {message.id}")
                        print(f"✅ [{channel}] Found {len(proxy_links)} proxies")
                        proxies.extend(proxy_links)
                        for proxy in proxy_links:
                            proxy_timeline.append({
                                "proxy": proxy,
                                "source": str(channel)
                            })
        
        summary = f"[{channel}] ✔️ Processed {message_count} messages → Found {configs_found_count} configs + {len(proxies)} proxies"
        logger.info(summary)
        print(summary)
        return configs, config_timeline, operator_configs, proxies, proxy_timeline, True
    except Exception as e:
        logger.error(f"Failed to fetch from {channel}: {str(e)}")
        print(f"❌ [{channel}] Error: {str(e)}")
        return configs, config_timeline, operator_configs, proxies, proxy_timeline, False

def save_configs(configs, protocol):
    output_file = os.path.join(OUTPUT_DIR, f"{protocol}.txt")
    logger.info(f"Saving configs to {output_file}")
    with open(output_file, "w", encoding="utf-8") as f:
        if configs:
            for config in configs:
                f.write(config + "\n")
            logger.info(f"Saved {len(configs)} {protocol} configs to {output_file}")
        else:
            f.write("No configs found for this protocol.\n")
            logger.info(f"No {protocol} configs found, wrote placeholder to {output_file}")

def save_operator_configs(operator_configs):
    for op, configs in operator_configs.items():
        output_file = os.path.join(OUTPUT_DIR, f"{op}.txt")
        logger.info(f"Saving operator configs to {output_file}")
        with open(output_file, "w", encoding="utf-8") as f:
            if configs:
                for config in configs:
                    f.write(config + "\n")
                logger.info(f"Saved {len(configs)} configs for {op} to {output_file}")
            else:
                f.write(f"No configs found for {op}.\n")
                logger.info(f"No configs found for {op}, wrote placeholder to {output_file}")

def save_proxies(proxies):
    output_file = os.path.join(OUTPUT_DIR, f"proxies.txt")
    logger.info(f"Saving proxies to {output_file}")
    with open(output_file, "w", encoding="utf-8") as f:
        if proxies:
            for proxy in proxies:
                f.write(f"{proxy}\n")
            logger.info(f"Saved {len(proxies)} proxies to {output_file}")
        else:
            f.write("No proxies found.\n")
            logger.info("No proxies found, wrote placeholder to proxies.txt")

def save_invalid_channels(invalid_channels):
    logger.info(f"Saving invalid channels to {INVALID_CHANNELS_FILE}")
    with open(INVALID_CHANNELS_FILE, "w", encoding="utf-8") as f:
        if invalid_channels:
            for channel in invalid_channels:
                f.write(f"{channel}\n")
            logger.info(f"Saved {len(invalid_channels)} invalid channels to {INVALID_CHANNELS_FILE}")
        else:
            f.write("No invalid channels found.\n")
            logger.info(f"No invalid channels found, wrote placeholder to {INVALID_CHANNELS_FILE}")

def save_channel_stats(stats):
    logger.info(f"Saving channel stats to {STATS_FILE}")
    stats_list = [{"channel": channel, **data} for channel, data in stats.items()]
    sorted_stats = sorted(stats_list, key=lambda x: x["score"], reverse=True)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted_stats, f, ensure_ascii=False, indent=4)
    logger.info(f"Saved channel stats to {STATS_FILE}")



def parse_channel_identifier(channel_str):
    if not isinstance(channel_str, str):
        return channel_str

    channel_str = channel_str.strip()

    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if channel_str.startswith(prefix):
            channel_str = channel_str[len(prefix):]
            break

    if "/" in channel_str and not channel_str.startswith(("c/", "joinchat/")):
        channel_str = channel_str.split("/", 1)[0]

    if channel_str.startswith("+") or channel_str.startswith("joinchat/"):
        return channel_str
    
    if channel_str.startswith('-100'):
        return int(channel_str)
    
    if channel_str.startswith('/c/') or channel_str.startswith('c/'):
        try:
            channel_id = int(channel_str.replace('/c/', '').replace('c/', ''))
            return -100 * (10**9) + channel_id
        except ValueError:
            return channel_str
    
    if channel_str.isdigit():
        return int(channel_str)

    if channel_str and not channel_str.startswith('@'):
        return f"@{channel_str}"
    
    return channel_str

def extract_invite_hash(channel):
    if not isinstance(channel, str):
        return None

    value = channel.strip()

    if value.startswith("https://t.me/+"):
        return value.split("https://t.me/+", 1)[1].split("/", 1)[0]
    if value.startswith("http://t.me/+"):
        return value.split("http://t.me/+", 1)[1].split("/", 1)[0]
    if value.startswith("t.me/+"):
        return value.split("t.me/+", 1)[1].split("/", 1)[0]
    if value.startswith("+"):
        return value[1:].split("/", 1)[0]

    if value.startswith("https://t.me/joinchat/"):
        return value.split("https://t.me/joinchat/", 1)[1].split("/", 1)[0]
    if value.startswith("http://t.me/joinchat/"):
        return value.split("http://t.me/joinchat/", 1)[1].split("/", 1)[0]
    if value.startswith("t.me/joinchat/"):
        return value.split("t.me/joinchat/", 1)[1].split("/", 1)[0]
    if value.startswith("joinchat/"):
        return value.split("joinchat/", 1)[1].split("/", 1)[0]

    return None

async def resolve_channel_target(client, channel):
    invite_hash = extract_invite_hash(channel)
    if invite_hash:
        try:
            import_result = await client(ImportChatInviteRequest(invite_hash))
            chats = getattr(import_result, "chats", None)
            if chats:
                return chats[0]
        except Exception as e:
            logger.info(f"Invite import skipped/failed for {channel}: {str(e)}")

        try:
            invite_info = await client(CheckChatInviteRequest(invite_hash))
            if hasattr(invite_info, "chat") and invite_info.chat:
                return invite_info.chat
        except Exception as e:
            logger.error(f"Failed to resolve private invite {channel}: {str(e)}")
            raise

        raise ValueError(f"Cannot resolve private invite link: {channel}")

    parsed = parse_channel_identifier(channel)
    return await client.get_entity(parsed)



async def main():
    logger.info("Starting config+proxy collection process")
    print("🚀 Starting config+proxy collection process...\n")
    invalid_channels = []
    channel_stats = {}

    if not SESSION_STRING:
        logger.error("No session string provided.")
        print("Please set TELEGRAM_SESSION_STRING in environment variables.")
        return
    if not API_ID or not API_HASH:
        logger.error("API ID or API Hash not provided.")
        print("Please set TELEGRAM_API_ID and TELEGRAM_API_HASH in environment variables.")
        return

    try:
        api_id = int(API_ID)
    except ValueError:
        logger.error("Invalid TELEGRAM_API_ID format. It must be a number.")
        print("Invalid TELEGRAM_API_ID format. It must be a number.")
        return

    TELEGRAM_CHANNELS = load_channels()
    session = StringSession(SESSION_STRING)

    try:
        async with TelegramClient(session, api_id, API_HASH) as client:
            if not await client.is_user_authorized():
                logger.error("Invalid session string.")
                print("Invalid session string. Generate a new one using generate_session.py.")
                return

            all_configs = {k: [] for k in CONFIG_PATTERNS.keys()}
            all_operator_configs = defaultdict(list)
            all_proxies = []
            channel_recent_configs = {}
            channel_recent_proxies = {}
            valid_channels = []

            for channel in TELEGRAM_CHANNELS:
                logger.info(f"Fetching configs/proxies from {channel}...")
                print(f"\n📡 Fetching from {channel}...")
                try:
                    channel_configs, channel_config_timeline, channel_operator_configs, channel_proxies, channel_proxy_timeline, is_valid = await fetch_configs_and_proxies_from_channel(client, channel)
                    if not is_valid:
                        print(f"⚠️  [{channel}] Invalid or inaccessible")
                        invalid_channels.append(channel)
                        channel_stats[channel] = {
                            "vless_count": 0,
                            "vmess_count": 0,
                            "shadowsocks_count": 0,
                            "trojan_count": 0,
                            "socks_count": 0,
                            "http_count": 0,
                            "mtproto_count": 0,
                            "proxy_count": 0,
                            "total_configs": 0,
                            "score": 0,
                            "error": "Channel does not exist or is inaccessible"
                        }
                        continue

                    valid_channels.append(channel)
                    total_configs = sum(len(configs) for configs in channel_configs.values())
                    proxy_count = len(channel_proxies)
                    score = total_configs + proxy_count
                    print(f"   └─ vless: {len(channel_configs.get('vless', []))} | vmess: {len(channel_configs.get('vmess', []))} | ss: {len(channel_configs.get('shadowsocks', []))} | trojan: {len(channel_configs.get('trojan', []))} | proxies: {proxy_count}")

                    channel_stats[channel] = {
                        "vless_count": len(channel_configs.get("vless", [])),
                        "vmess_count": len(channel_configs.get("vmess", [])),
                        "shadowsocks_count": len(channel_configs.get("shadowsocks", [])),
                        "trojan_count": len(channel_configs.get("trojan", [])),
                        "socks_count": len(channel_configs.get("socks", [])),
                        "http_count": len(channel_configs.get("http", [])),
                        "mtproto_count": len(channel_configs.get("mtproto", [])),
                        "proxy_count": proxy_count,
                        "total_configs": total_configs,
                        "score": score
                    }

                    for protocol in all_configs:
                        all_configs[protocol].extend(channel_configs[protocol])
                    for op in channel_operator_configs:
                        all_operator_configs[op].extend(channel_operator_configs[op])

                    all_proxies.extend(channel_proxies)
                    channel_recent_configs[channel] = channel_config_timeline
                    channel_recent_proxies[channel] = channel_proxy_timeline
                except Exception as e:
                    print(f"❌ [{channel}] Exception: {str(e)}")
                    invalid_channels.append(channel)
                    channel_stats[channel] = {
                        "vless_count": 0,
                        "vmess_count": 0,
                        "shadowsocks_count": 0,
                        "trojan_count": 0,
                        "socks_count": 0,
                        "http_count": 0,
                        "mtproto_count": 0,
                        "proxy_count": 0,
                        "total_configs": 0,
                        "score": 0,
                        "error": str(e)
                    }
                    logger.error(f"Channel {channel} is invalid: {str(e)}")

            print("\n" + "=" * 60)
            for protocol in all_configs:
                all_configs[protocol] = list(set(all_configs[protocol]))
                print(f"📊 Found {len(all_configs[protocol])} unique {protocol.upper()} configs")
                logger.info(f"Found {len(all_configs[protocol])} unique {protocol} configs")
            for op in all_operator_configs:
                all_operator_configs[op] = list(set(all_operator_configs[op]))
                print(f"📊 Found {len(all_operator_configs[op])} configs for {op}")
                logger.info(f"Found {len(all_operator_configs[op])} unique configs for operator {op}")

            all_proxies = list(dict.fromkeys(all_proxies))
            print(f"📊 Found {len(all_proxies)} unique proxies")
            print("=" * 60 + "\n")

            for protocol in all_configs:
                save_configs(all_configs[protocol], protocol)
            save_operator_configs(all_operator_configs)
            save_proxies(all_proxies)
            save_invalid_channels(invalid_channels)
            save_channel_stats(channel_stats)

            marketplace_data = []
            for ch_configs in channel_recent_configs.values():
                marketplace_data.extend(ch_configs)
            marketplace_data.sort(key=lambda x: x.get("time", ""), reverse=True)
            marketplace_file = os.path.join(OUTPUT_DIR, "marketplace_data.json")
            with open(marketplace_file, "w", encoding="utf-8") as f:
                json.dump(marketplace_data, f, ensure_ascii=False, indent=4)
            logger.info(f"Saved {len(marketplace_data)} marketplace configs to {marketplace_file}")
            print(f"🛍️  Saved {len(marketplace_data)} marketplace configs!")

            update_channels(valid_channels)

    except Exception as e:
        logger.error(f"Error in main loop: {str(e)}")
        print(f"Error in main loop: {str(e)}")
        return

    logger.info("Config+proxy collection process completed")
    print("✅ Config+proxy collection process completed!")


if __name__ == "__main__":
    asyncio.run(main())
