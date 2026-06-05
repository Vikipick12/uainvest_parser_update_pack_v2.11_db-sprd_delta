import aiohttp
import asyncio
import json
import os
import time
from dotenv import load_dotenv

load_dotenv()

API_URL = "https://uainvest.com.ua/api/arbitrage/offers"
ALL_EXCHANGES = [
    "aster", "backpack", "balpha", "binance", "bingx", "bitget", "bitmart", 
    "bitrue", "bybit", "coinex", "edgex", "extended", "gate", "hotcoin", 
    "htx", "hyperliquid", "kucoin", "lbank", "lighter", "mexc", "okx", 
    "ourbit", "paradex", "phemex", "variational", "weex", "whitebit", "xt"
]

def get_headers():
    return {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "Cookie": os.getenv("VIP_COOKIE"),
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://uainvest.com.ua/arbitrage?type=spread"
    }

async def fetch_exchange_data(session, exchange, unique_spreads):
    params = {"type": "spread", "exchanges": exchange}
    try:
        async with session.get(API_URL, headers=get_headers(), params=params, timeout=aiohttp.ClientTimeout(total=15)) as response:
            if response.status == 200:
                data = (await response.json()).get("data", [])
                for item in data:
                    item_id = item.get("id")
                    if item_id:
                        unique_spreads[item_id] = item
    except Exception:
        pass

async def fetch_arb_opportunities(path: str = "data.json"):
    """Ця функція тепер збирає дані по кожній біржі окремо"""
    unique_spreads = {}
    async with aiohttp.ClientSession() as session:
        batch_size = 5
        for i in range(0, len(ALL_EXCHANGES), batch_size):
            batch = ALL_EXCHANGES[i:i + batch_size]
            tasks = [fetch_exchange_data(session, ex, unique_spreads) for ex in batch]
            await asyncio.gather(*tasks)
            await asyncio.sleep(0.1)

    final_data = {
        "total_count": len(unique_spreads),
        "updated_at": int(time.time()),
        "data": list(unique_spreads.values())
    }
    
    with open(path, "w", encoding="utf-8") as f:
        json.dump(final_data, f, indent=4, ensure_ascii=False)
    return final_data