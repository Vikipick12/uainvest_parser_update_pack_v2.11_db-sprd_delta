import json
import time
from typing import Any, Dict, List, Optional


DEFAULT_CONFIG: Dict[str, Any] = {
    # Subset that is actually used for filtering
    "exchanges": [
        "binance",
        "bybit",
        "gate",
        "mexc",
        "bitget",
        "kucoin",
        "htx",
    ],
    # Full set available for selection in the UI
    "all_exchanges": [
        "aster",
        "backpack",
        "balpha",
        "binance",
        "bingx",
        "bitget",
        "bitmart",
        "bitrue",
        "bybit",
        "coinex",
        "edgex",
        "extended",
        "gate",
        "hotcoin",
        "htx",
        "hyperliquid",
        "kucoin",
        "lbank",
        "lighter",
        "mexc",
        "okx",
        "ourbit",
        "paradex",
        "phemex",
        "variational",
        "weex",
        "whitebit",
        "xt"
    ],
    "min_spread_pct": 0.5,
    "data_path": "data.json",
    "last_spreads": [],
    "updated_at": 0,
}


def load_config(path: str = "config.json") -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return DEFAULT_CONFIG.copy()
        # Fill missing keys with defaults
        merged = DEFAULT_CONFIG.copy()
        merged.update({k: v for k, v in cfg.items() if v is not None})
        # Migration helpers: if all_exchanges missing, seed from exchanges; if exchanges missing, seed from all_exchanges
        if not merged.get("all_exchanges"):
            merged["all_exchanges"] = list(merged.get("exchanges", []))
        if not merged.get("exchanges") and merged.get("all_exchanges"):
            merged["exchanges"] = list(merged.get("all_exchanges", []))
        return merged
    except FileNotFoundError:
        return DEFAULT_CONFIG.copy()


def save_config(cfg: Dict[str, Any], path: str = "config.json") -> None:
    # Normalize minimal schema
    normalized = DEFAULT_CONFIG.copy()
    normalized.update(cfg)
    normalized["updated_at"] = int(time.time())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=4)


def update_settings(
    *,
    exchanges: Optional[List[str]] = None,
    min_spread_pct: Optional[float] = None,
    all_exchanges: Optional[List[str]] = None,
    data_path: Optional[str] = None,
    path: str = "config.json",
) -> Dict[str, Any]:
    cfg = load_config(path)
    if exchanges is not None:
        cfg["exchanges"] = [e.strip() for e in exchanges if str(e).strip()]
    if min_spread_pct is not None:
        cfg["min_spread_pct"] = float(min_spread_pct)
    if all_exchanges is not None:
        cfg["all_exchanges"] = [e.strip() for e in all_exchanges if str(e).strip()]
    if data_path is not None and str(data_path).strip():
        cfg["data_path"] = str(data_path).strip()
    save_config(cfg, path)
    return cfg


def save_spreads_cache(
    spreads: List[dict],
    *,
    path: str = "config.json",
) -> Dict[str, Any]:
    # Store a compact subset to keep config small
    compact: List[Dict[str, Any]] = []
    for row in spreads:
        compact.append(
            {
                "symbol": row.get("symbol"),
                "long": row.get("long_exchange"),
                "short": row.get("short_exchange"),
                "long_price": row.get("long_price"),
                "short_price": row.get("short_price"),
                "spread_pct": row.get("spread_pct"),
                "spread_abs": row.get("spread_abs"),
            }
        )

    cfg = load_config(path)
    cfg["last_spreads"] = compact
    cfg["updated_at"] = int(time.time())
    save_config(cfg, path)
    return cfg


