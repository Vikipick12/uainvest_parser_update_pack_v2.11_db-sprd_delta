import json
import argparse
import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Iterable
from config_manager import load_config, save_spreads_cache, update_settings


UAINVEST_ARBITRAGE_BASE_URL = "https://uainvest.com.ua/arbitrage"
QUOTE_SUFFIXES = ("fdusd", "usdt", "usdc", "busd", "usd")


@dataclass
class OfferSpread:
    id: str
    symbol: str
    long_exchange: str
    short_exchange: str
    long_price: float
    short_price: float
    spread_abs: float
    spread_pct: float
    funding_long: float
    funding_short: float
    funding_long_24: float
    funding_short_24: float
    funding_interval_long: Optional[int]
    funding_interval_short: Optional[int]
    open_spread_percentage: Optional[float]
    long_volume_usdt: Optional[float]
    short_volume_usdt: Optional[float]
    chart_url: str


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _market_type_slug(value: Any) -> str:
    cleaned = str(value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-") or "swap"


def _coin_slug_part(value: Any) -> str:
    cleaned = str(value or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9:_]", "", cleaned)
    for suffix in QUOTE_SUFFIXES:
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned


def _build_coin_slug(main_coin: str, long_coin: str, short_coin: str) -> str:
    exchange_coins = [coin for coin in (long_coin, short_coin) if coin]
    namespaced = [coin for coin in exchange_coins if ":" in coin]

    if namespaced:
        base_parts = [coin.rsplit(":", 1)[-1] for coin in exchange_coins]
        if main_coin and all(part == main_coin for part in base_parts if part):
            return main_coin

        ordered = namespaced + [coin for coin in exchange_coins if ":" not in coin]
        coin = ordered[0]
        for part in ordered[1:]:
            part_base = part.rsplit(":", 1)[-1]
            coin_bases = [item.rsplit(":", 1)[-1] for item in coin.split("_")]
            if part != coin and part_base not in coin_bases:
                coin = f"{coin}_{part}"
        return coin

    return main_coin or (exchange_coins[0] if exchange_coins else "unknown")


def build_uainvest_chart_url(offer: Dict[str, Any]) -> str:
    long_item = offer.get("long_item") or {}
    short_item = offer.get("short_item") or {}

    main_coin = _coin_slug_part(offer.get("symbol"))
    long_coin = _coin_slug_part(long_item.get("symbol"))
    short_coin = _coin_slug_part(short_item.get("symbol"))

    coin = _build_coin_slug(main_coin, long_coin, short_coin)

    long_exchange = str(offer.get("long") or long_item.get("exchange", "")).strip().lower()
    short_exchange = str(offer.get("short") or short_item.get("exchange", "")).strip().lower()
    long_type = _market_type_slug(long_item.get("type"))
    short_type = _market_type_slug(short_item.get("type"))

    slug = f"{coin}-{long_exchange}-{long_type}-{short_exchange}-{short_type}"
    return f"{UAINVEST_ARBITRAGE_BASE_URL}/{slug}"


def load_offers_from_file(path: str = "data.json") -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    data = payload.get("data", [])
    if not isinstance(data, list):
        return []
    return data


def compute_offer_spread(offer: Dict[str, Any]) -> Optional[OfferSpread]:
    long_item = offer.get("long_item") or {}
    short_item = offer.get("short_item") or {}

    long_price = _to_float(long_item.get("price"))
    short_price = _to_float(short_item.get("price"))
    long_vol = _to_float(long_item.get("24usdt"))
    short_vol = _to_float(short_item.get("24usdt"))

    if long_price is None or short_price is None:
        return None

    spread_abs = short_price - long_price
    # Use long price as denominator to express buy-low/sell-high edge
    spread_pct = (spread_abs / long_price) * 100 if long_price else 0.0

    return OfferSpread(
        id=str(offer.get("id", "")),
        symbol=str(offer.get("symbol", "")),
        long_exchange=str(offer.get("long", long_item.get("exchange", ""))),
        short_exchange=str(offer.get("short", short_item.get("exchange", ""))),
        long_price=long_price,
        short_price=short_price,
        spread_abs=spread_abs,
        spread_pct=spread_pct,
        funding_long=_to_float(long_item.get("funding")),
        funding_short=_to_float(short_item.get("funding")),
        funding_long_24=_to_float(long_item.get("funding_24")),
        funding_short_24=_to_float(short_item.get("funding_24")),
        funding_interval_long=long_item.get("funding_interval"),
        funding_interval_short=short_item.get("funding_interval"),
        open_spread_percentage=_to_float(offer.get("open_spread_percentage")),
        long_volume_usdt=long_vol,
        short_volume_usdt=short_vol,
        chart_url=build_uainvest_chart_url(offer),
    )


def filter_spreads_by_exchanges(
    offers: Iterable[Dict[str, Any]],
    allowed_exchanges: Iterable[str],
    min_spread_pct: float = 0.0,
) -> List[OfferSpread]:
    allowed = {e.strip().lower() for e in allowed_exchanges if e and str(e).strip()}
    results: List[OfferSpread] = []

    for offer in offers:
        long_ex = str(offer.get("long") or offer.get("long_item", {}).get("exchange", "")).lower()
        short_ex = str(offer.get("short") or offer.get("short_item", {}).get("exchange", "")).lower()

        if long_ex in allowed and short_ex in allowed:
            spread = compute_offer_spread(offer)
            if spread and spread.spread_pct >= min_spread_pct:
                results.append(spread)

    # Sort by spread percentage descending
    results.sort(key=lambda s: s.spread_pct, reverse=True)
    return results


def get_spreads_for_exchanges(
    exchanges: List[str],
    *,
    min_spread_pct: float = 0.0,
    path: str = "data.json",
) -> List[Dict[str, Any]]:
    offers = load_offers_from_file(path)
    spreads = filter_spreads_by_exchanges(offers, exchanges, min_spread_pct)
    return [asdict(s) for s in spreads]


def _format_row(spread: OfferSpread) -> str:
    api_pct = (
        f"{spread.open_spread_percentage:>7.3f}%" if spread.open_spread_percentage is not None else "   n/a  "
    )
    
    # Funding info
    funding_info = ""
    if spread.funding_interval_long is not None or spread.funding_interval_short is not None:
        funding_parts = []
        if spread.funding_interval_long is not None:
            funding_long = f"{spread.funding_long:.4f}%" if spread.funding_long is not None else "n/a"
            funding_parts.append(f"L({spread.funding_interval_long}h)={funding_long}")
        if spread.funding_interval_short is not None:
            funding_short = f"{spread.funding_short:.4f}%" if spread.funding_short is not None else "n/a"
            funding_parts.append(f"S({spread.funding_interval_short}h)={funding_short}")
        funding_info = f"  funding: {' '.join(funding_parts)}"
    
    return (
        f"{spread.symbol:>12}  "
        f"{spread.long_exchange:>8} -> {spread.short_exchange:<8}  "
        f"{spread.long_price:>12.8f} -> {spread.short_price:<12.8f}  "
        f"abs={spread.spread_abs:>10.8f}  pct={spread.spread_pct:>7.3f}%  api={api_pct}{funding_info}"
    )


def main_cli() -> None:
    parser = argparse.ArgumentParser(
        description="Filter arbitrage offers by exchanges and compute spreads from data.json",
    )
    parser.add_argument(
        "--exchanges",
        required=False,
        help="Comma-separated list of exchanges (e.g. binance,bybit,gate,mexc,bitget,kucoin,htx). If omitted, will use config.json",
    )
    parser.add_argument(
        "--min-pct",
        type=float,
        default=None,
        help="Minimum spread percentage to include. If omitted, will use config.json",
    )
    parser.add_argument(
        "--path",
        default=None,
        help="Path to JSON file. If omitted, will use config.json",
    )
    parser.add_argument(
        "--save-config",
        action="store_true",
        help="Save provided CLI options (exchanges, min-pct, limit, path) into config.json",
    )
    parser.add_argument(
        "--save-cache",
        action="store_true",
        help="Save computed spreads (limited by limit) into config.json:last_spreads",
    )

    args = parser.parse_args()

    cfg = load_config()

    exchanges = (
        [e.strip() for e in args.exchanges.split(",") if e.strip()]
        if args.exchanges
        else cfg.get("exchanges", [])
    )
    min_pct = args.min_pct if args.min_pct is not None else float(cfg.get("min_spread_pct", 0.0))
    data_path = args.path if args.path is not None else str(cfg.get("data_path", "data.json"))
    # No limit anymore; show all results >= min_spread_pct

    if args.save_config:
        update_settings(
            exchanges=exchanges,
            min_spread_pct=min_pct,
            data_path=data_path,
        )

    rows = get_spreads_for_exchanges(exchanges, min_spread_pct=min_pct, path=data_path)

    if not rows:
        print("Немає пропозицій за заданими біржами/фільтрами.")
        return

    # Pretty print
    print(
        "symbol        long -> short           long_price      -> short_price     abs           pct      api_pct"
    )
    print("-" * 120)
    for row in rows:
        print(_format_row(OfferSpread(**row)))

    if args.save_cache:
        save_spreads_cache(rows)


if __name__ == "__main__":
    main_cli()


