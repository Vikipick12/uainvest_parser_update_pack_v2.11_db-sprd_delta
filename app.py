import asyncio
import logging
import os
import time
from typing import Dict, List, Optional, Set

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import Message, CallbackQuery, BotCommand
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

from config_manager import load_config, update_settings
from db import (
    init_db,
    upsert_user,
    get_user_settings,
    set_user_min_spread,
    set_user_exchanges,
    set_user_delta_threshold,
    set_user_special_delta_threshold,
    delete_user_special_delta_threshold,
    set_user_exchange_min_spread,
    delete_user_exchange_min_spread,
    get_last_spread_pct,
    upsert_last_spread_pct,
    upsert_user_token_blacklist,
    delete_user_token_blacklist,
    is_user_token_blacklisted,
    list_user_token_blacklist,
)
from spread_processor import get_spreads_for_exchanges
from request_data import fetch_arb_opportunities, ALL_EXCHANGES


load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_IDS_ENV = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
FETCH_INTERVAL_SECONDS = int(os.getenv("FETCH_INTERVAL_SECONDS", "15"))
SEND_MESSAGE_DELAY_SECONDS = float(os.getenv("SEND_MESSAGE_DELAY_SECONDS", "1.1"))
FLOOD_RETRY_BUFFER_SECONDS = float(os.getenv("FLOOD_RETRY_BUFFER_SECONDS", "1.0"))
MAX_ALERTS_PER_CYCLE = int(os.getenv("MAX_ALERTS_PER_CYCLE", "20"))

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")


def parse_allowed_ids(env_value: str) -> Optional[Set[int]]:
    if not env_value:
        return None
    ids: Set[int] = set()
    for part in env_value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return ids if ids else None


ALLOWED_USER_IDS = parse_allowed_ids(ALLOWED_IDS_ENV)


def user_allowed(user_id: int) -> bool:
    if ALLOWED_USER_IDS is None:
        return True
    return user_id in ALLOWED_USER_IDS


class SetSpreadState(StatesGroup):
    waiting_value = State()


class ManageExchangesState(StatesGroup):
    selecting = State()


class SetDeltaState(StatesGroup):
    waiting_value = State()


class SetExchangeSpreadState(StatesGroup):
    waiting_value = State()


class ResetExchangeSpreadState(StatesGroup):
    waiting_value = State()


class SetSpecialDeltaState(StatesGroup):
    waiting_value = State()


class ResetSpecialDeltaState(StatesGroup):
    waiting_value = State()


class AddBlacklistState(StatesGroup):
    waiting_value = State()


class RemoveBlacklistState(StatesGroup):
    waiting_value = State()


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


# Track running background tasks per chat
running_tasks: Dict[int, asyncio.Task] = {}
# Track whether we've already notified a chat that there are no spreads
no_spreads_notified: Dict[int, bool] = {}
# Single global data fetcher task
global_fetch_task: Optional[asyncio.Task] = None


BTN_SPREADS = "Показати спреди"
BTN_STOP = "Зупинити"
BTN_CONFIG = "Показати конфіг"
BTN_EXCHANGES = "Вибір бірж"
BTN_SET_SPREAD = "Мін. спред"
BTN_SET_DELTA = "Глобальна дельта"
BTN_EXCHANGE_THRESHOLDS = "Пороги бірж"
BTN_SPECIAL_DELTAS = "Дельти тікерів"
BTN_BLACKLIST = "Блекліст"


def get_effective_settings_for_user(user_id: int) -> Dict[str, object]:
    cfg = load_config()
    user_cfg = get_user_settings(user_id)
    user_exchanges = user_cfg.get("exchanges") if user_cfg else None
    allowed = {ex.lower() for ex in ALL_EXCHANGES}
    all_exchanges = [ex for ex in list(dict.fromkeys(list(ALL_EXCHANGES) + list(cfg.get("all_exchanges", cfg.get("exchanges", []))))) if str(ex).lower() in allowed]
    exchanges = [ex for ex in user_exchanges if str(ex).lower() in allowed] if user_exchanges else all_exchanges
    user_min = user_cfg.get("min_spread_pct") if user_cfg else None
    min_spread_pct = float(user_min) if user_min is not None else float(cfg.get("min_spread_pct", 0.0))
    user_delta = user_cfg.get("delta_threshold_pct") if user_cfg else None
    delta_threshold_pct = float(user_delta) if user_delta is not None else 1.0
    special_delta_thresholds = user_cfg.get("special_delta_thresholds") if user_cfg else {}
    exchange_min_spreads = user_cfg.get("exchange_min_spreads") if user_cfg else {}
    return {
        "exchanges": exchanges,
        "min_spread_pct": min_spread_pct,
        "exchange_min_spreads": exchange_min_spreads or {},
        "delta_threshold_pct": delta_threshold_pct,
        "special_delta_thresholds": special_delta_thresholds or {},
        "data_path": str(cfg.get("data_path", "data.json")),
        "all_exchanges": all_exchanges,
    }


def build_main_menu_keyboard() -> ReplyKeyboardBuilder:
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_SPREADS)
    kb.button(text=BTN_STOP)
    kb.button(text=BTN_CONFIG)
    kb.button(text=BTN_EXCHANGES)
    kb.button(text=BTN_SET_SPREAD)
    kb.button(text=BTN_SET_DELTA)
    kb.button(text=BTN_EXCHANGE_THRESHOLDS)
    kb.button(text=BTN_SPECIAL_DELTAS)
    kb.button(text=BTN_BLACKLIST)
    kb.adjust(2, 2, 2, 2, 1)
    return kb


def build_exchange_thresholds_menu() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Показати пороги", callback_data="exthr:list")
    builder.button(text="Додати / змінити", callback_data="exthr:set")
    builder.button(text="Скинути", callback_data="exthr:reset")
    builder.adjust(1)
    return builder


def build_special_deltas_menu() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Показати список", callback_data="sdelta:list")
    builder.button(text="Додати / змінити", callback_data="sdelta:set")
    builder.button(text="Скинути", callback_data="sdelta:reset")
    builder.adjust(1)
    return builder


def build_blacklist_menu() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="Показати список", callback_data="bl:list")
    builder.button(text="Додати тікер", callback_data="bl:add")
    builder.button(text="Зняти тікер", callback_data="bl:remove")
    builder.adjust(1)
    return builder


def build_exchanges_keyboard(selected: Set[str], all_exchanges: List[str]) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    # Sort to keep stable order
    for ex in sorted(all_exchanges):
        is_on = ex.lower() in selected
        label = ("✅ " + ex) if is_on else ex
        builder.button(text=label, callback_data=f"exch:{ex}")
    # bulk actions
    builder.button(text="Вибрати всі", callback_data="exch_all")
    builder.button(text="Скасувати всі", callback_data="exch_none")
    # control row
    builder.button(text="Зберегти", callback_data="exch_save")
    builder.button(text="Скасувати", callback_data="exch_cancel")
    # layout: 3 per row for exchanges, then 2 + 2 for bulk/control rows
    rows = [3] * max(1, (len(all_exchanges) + 2) // 3)
    builder.adjust(*rows, 2, 2)
    return builder


def format_spread_card(r: dict) -> str:
    def _fmt_money(value: Optional[float]) -> str:
        if value is None:
            return "n/a"
        # Format as human readable with suffix
        abs_v = abs(value)
        if abs_v >= 1_000_000_000:
            return f"{value/1_000_000_000:.2f}b$"
        if abs_v >= 1_000_000:
            return f"{value/1_000_000:.2f}m$"
        if abs_v >= 1_000:
            return f"{value/1_000:.2f}k$"
        return f"{value:.2f}$"

    def _fmt_funding(value: Optional[float]) -> str:
        if value is None:
            return "n/a"
        return f"{value:.4f}%"

    pct = r.get("spread_pct")
    pct_str = f"{pct:.2f}%" if isinstance(pct, (int, float)) else "n/a"

    long_vol = r.get("long_volume_usdt")
    short_vol = r.get("short_volume_usdt")
    
    # Funding info
    funding_long = r.get("funding_long")
    funding_short = r.get("funding_short")
    funding_interval_long = r.get("funding_interval_long")
    funding_interval_short = r.get("funding_interval_short")
    
    funding_text = ""
    if funding_interval_long is not None or funding_interval_short is not None:
        if funding_interval_long is not None:
            funding_text += f"Фандинг {r.get('long_exchange')}: {_fmt_funding(funding_long)} ({funding_interval_long}г): \n"
        if funding_interval_short is not None:
            funding_text += f"Фандинг {r.get('short_exchange')}: {_fmt_funding(funding_short)} ({funding_interval_short}г) "

    text = (
        f"<code>{r.get('symbol')}</code>\n"
        f"Long: {r.get('long_exchange')}\n"
        f"Short: {r.get('short_exchange')}\n"
        f"Spread: {pct_str}\n"
        f"Об'єм на {r.get('long_exchange')}: {_fmt_money(long_vol)}\n"
        f"Об'єм на {r.get('short_exchange')}: {_fmt_money(short_vol)}"
    )

    chart_url = r.get("chart_url")
    if chart_url:
        text += f"\nГрафік: <a href=\"{chart_url}\">відкрити</a>"
    
    if funding_text:
        text += f"\n{funding_text}"
    
    return text


async def ensure_allowed(message: Message) -> bool:
    if not user_allowed(message.from_user.id):
        await message.answer("Доступ заборонений. Зверніться до адміністратора.")
        return False
    return True


async def send_message_limited(chat_id: int, text: str) -> None:
    while True:
        try:
            await bot.send_message(chat_id, text)
            if SEND_MESSAGE_DELAY_SECONDS > 0:
                await asyncio.sleep(SEND_MESSAGE_DELAY_SECONDS)
            return
        except TelegramRetryAfter as e:
            retry_after = float(getattr(e, "retry_after", 1))
            wait_seconds = retry_after + FLOOD_RETRY_BUFFER_SECONDS
            logger.warning("Telegram flood control for chat %s, retrying in %.1f seconds", chat_id, wait_seconds)
            await asyncio.sleep(wait_seconds)


async def start_background_loop(chat_id: int, user_id: int):
    while True:
        try:
            eff = get_effective_settings_for_user(user_id)
            rows = get_spreads_for_exchanges(
                eff.get("exchanges", []),
                min_spread_pct=float(eff.get("min_spread_pct", 0.0)),
                exchange_min_spreads=eff.get("exchange_min_spreads", {}),
                path=str(eff.get("data_path", "data.json")),
            )
            if not rows:
                # Send the 'no offers' message only once until results appear again
                if not no_spreads_notified.get(chat_id, False):
                    await send_message_limited(chat_id, "Немає спредів за заданими фільтрами.")
                    no_spreads_notified[chat_id] = True
            else:
                no_spreads_notified[chat_id] = False
                # Send each opportunity as a separate message
                delta_threshold = float(eff.get("delta_threshold_pct", 1.0))
                special_delta_thresholds = eff.get("special_delta_thresholds", {}) or {}
                sent_count = 0
                for r in rows:
                    if MAX_ALERTS_PER_CYCLE > 0 and sent_count >= MAX_ALERTS_PER_CYCLE:
                        break
                    try:
                        current = float(r.get("spread_pct", 0.0))
                        symbol = str(r.get("symbol", ""))
                        symbol_upper = symbol.upper()
                        if symbol_upper and is_user_token_blacklisted(user_id, symbol_upper):
                            continue
                        effective_delta_threshold = float(
                            special_delta_thresholds.get(symbol_upper, delta_threshold)
                        )
                        long_ex = str(r.get("long_exchange", ""))
                        short_ex = str(r.get("short_exchange", ""))
                        last = get_last_spread_pct(user_id, symbol, long_ex, short_ex)
                        should_send = False
                        if last is None:
                            # First sighting for this user/opportunity
                            should_send = True
                        else:
                            if abs(current - float(last)) >= effective_delta_threshold:
                                should_send = True
                        if should_send:
                            await send_message_limited(chat_id, format_spread_card(r))
                            upsert_last_spread_pct(user_id, symbol, long_ex, short_ex, current)
                            sent_count += 1
                    except Exception as inner_e:  # noqa: BLE001
                        logger.exception("Помилка обробки можливості: %s", inner_e)
        except asyncio.CancelledError:
            # Тихо завершуємо цикл за запитом користувача (/stop)
            break
        except Exception as e:  # noqa: BLE001
            logger.exception("Помилка у фоному циклі: %s", e)
            await send_message_limited(chat_id, f"Виникла помилка у фоні: {e}")
            break
        await asyncio.sleep(FETCH_INTERVAL_SECONDS)


async def global_fetch_loop():
    while True:
        try:
            cfg = load_config()
            # Додаємо await для асинхронної функції
            await fetch_arb_opportunities(path=str(cfg.get("data_path", "data.json")))
            
            # Використовуємо logger (з малої літери, як у тебе в коді)
            current_time = time.strftime('%H:%M:%S')
            logger.info(f"Глобальне оновлення завершено о {current_time}")
            
        except Exception as e:
            logger.exception("Помилка глобального фетчера: %s", e)
        
        # Чекаємо 60 секунд перед наступним повним циклом збору по 28 біржах
        await asyncio.sleep(60)


async def ensure_global_fetcher_started() -> None:
    global global_fetch_task
    if global_fetch_task is None or global_fetch_task.done():
        global_fetch_task = asyncio.create_task(global_fetch_loop())


@dp.message(CommandStart())
async def cmd_start(message: Message):
    if not await ensure_allowed(message):
        return
    await ensure_global_fetcher_started()
    # Persist user
    try:
        upsert_user(
            user_id=message.from_user.id,
            chat_id=message.chat.id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name,
            language_code=message.from_user.language_code,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("Не вдалося оновити дані користувача: %s", e)
    kb = build_main_menu_keyboard().as_markup(resize_keyboard=True)
    await message.answer(
        "Привіт! 👋🏻👋🏻👋🏻\n\nДавай домовимось, я тобі кидаю арбітражні можливості, а ти їбеш біржі.\n\nВикористовуй меню або чотири крапки знизу.",
        reply_markup=kb,
    )

    chat_id = message.chat.id
    if chat_id in running_tasks and not running_tasks[chat_id].done():
        await message.answer("Парсинг уже запущений для цього чату.")
    else:
        task = asyncio.create_task(start_background_loop(chat_id, message.from_user.id))
        running_tasks[chat_id] = task
        no_spreads_notified[chat_id] = False
        await message.answer("Запущено парсинг спредів кожні 15 секунд.")


@dp.message(Command("spreads"))
async def cmd_spreads(message: Message):
    if not await ensure_allowed(message):
        return
    await ensure_global_fetcher_started()
    chat_id = message.chat.id
    if chat_id in running_tasks and not running_tasks[chat_id].done():
        await message.answer("Парсинг спредів уже запущений для цього чату.")
        return
    task = asyncio.create_task(start_background_loop(chat_id, message.from_user.id))
    running_tasks[chat_id] = task
    no_spreads_notified[chat_id] = False
    await message.answer("Запущено парсинг спредів кожні 15 секунд.")


@dp.message(Command("stop"))
async def cmd_stop(message: Message):
    if not await ensure_allowed(message):
        return
    chat_id = message.chat.id
    task = running_tasks.get(chat_id)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        running_tasks.pop(chat_id, None)
        no_spreads_notified.pop(chat_id, None)
        await message.answer("Зупинено надсилання спредів у цьому чаті.")
    else:
        await message.answer("Нічого зупиняти: фонове надсилання не запущене.")


@dp.message(F.text == "Показати спреди")
async def btn_spreads(message: Message):
    await cmd_spreads(message)


@dp.message(F.text == BTN_STOP)
async def btn_stop(message: Message):
    await cmd_stop(message)


@dp.message(Command("set_spread"))
async def cmd_set_spread(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    await state.set_state(SetSpreadState.waiting_value)
    await message.answer("Введіть мінімальний спред у %:")


@dp.message(F.text == BTN_SET_SPREAD)
async def btn_set_min_spread(message: Message, state: FSMContext):
    await cmd_set_spread(message, state)


def _parse_percent(text: str) -> Optional[float]:
    if text is None:
        return None
    cleaned = text.strip().replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_blacklist_duration_hours(raw: str) -> Optional[int]:
    if raw is None:
        return None
    cleaned = raw.strip().lower()
    if not cleaned:
        return None
    try:
        if cleaned.endswith("h"):
            return int(cleaned[:-1])
        if cleaned.endswith("d"):
            return int(cleaned[:-1]) * 24
        if cleaned.endswith("m"):
            minutes = int(cleaned[:-1])
            return max(1, (minutes + 59) // 60)
        return int(cleaned)
    except ValueError:
        return None


def _normalize_exchange(raw: str) -> Optional[str]:
    exchange = str(raw or "").strip().lower()
    allowed = {ex.lower() for ex in ALL_EXCHANGES}
    return exchange if exchange in allowed else None


def _normalize_symbol(raw: str) -> Optional[str]:
    symbol = str(raw or "").strip().upper()
    return symbol if symbol else None


@dp.message(Command("set_exchange_spread"))
async def cmd_set_exchange_spread(message: Message):
    if not await ensure_allowed(message):
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 3:
        await message.answer("Використання: /set_exchange_spread mexc 10")
        return

    exchange = _normalize_exchange(parts[1])
    if exchange is None:
        await message.answer("Невідома біржа. Приклад: /set_exchange_spread mexc 10")
        return

    value = _parse_percent(parts[2])
    if value is None or value < 0:
        await message.answer("Некоректний відсоток. Приклад: /set_exchange_spread bybit 3")
        return

    set_user_exchange_min_spread(message.from_user.id, exchange, value)
    await message.answer(f"Мінімальний спред для {exchange} встановлено на {value:.2f}%")


@dp.message(Command("reset_exchange_spread"))
async def cmd_reset_exchange_spread(message: Message):
    if not await ensure_allowed(message):
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.answer("Використання: /reset_exchange_spread mexc")
        return

    exchange = _normalize_exchange(parts[1])
    if exchange is None:
        await message.answer("Невідома біржа. Приклад: /reset_exchange_spread mexc")
        return

    deleted = delete_user_exchange_min_spread(message.from_user.id, exchange)
    if deleted:
        await message.answer(f"Окремий мінімальний спред для {exchange} скинуто.")
    else:
        await message.answer(f"Для {exchange} не було окремого мінімального спреду.")


async def send_exchange_spreads_list(target: Message, user_id: int) -> None:
    eff = get_effective_settings_for_user(user_id)
    exchange_min_spreads = eff.get("exchange_min_spreads", {}) or {}
    if not exchange_min_spreads:
        await target.answer("Окремі мінімальні спреди для бірж не налаштовані.")
        return

    lines = [
        f"{exchange}: {float(value):.2f}%"
        for exchange, value in sorted(exchange_min_spreads.items())
    ]
    await target.answer("Мінімальні спреди для бірж:\n" + "\n".join(lines))


@dp.message(Command("exchange_spreads"))
async def cmd_exchange_spreads(message: Message):
    if not await ensure_allowed(message):
        return
    await send_exchange_spreads_list(message, message.from_user.id)


@dp.message(Command("set_special_delta"))
async def cmd_set_special_delta(message: Message):
    if not await ensure_allowed(message):
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 3:
        await message.answer("Використання: /set_special_delta BTCUSDT 4")
        return

    symbol = _normalize_symbol(parts[1])
    if symbol is None:
        await message.answer("Вкажіть символ. Приклад: /set_special_delta BTCUSDT 4")
        return

    value = _parse_percent(parts[2])
    if value is None or value < 0:
        await message.answer("Некоректна дельта. Приклад: /set_special_delta BTCUSDT 4")
        return

    set_user_special_delta_threshold(message.from_user.id, symbol, value)
    await message.answer(f"Спеціальну дельту для {symbol} встановлено на {value:.2f}%")


@dp.message(Command("reset_special_delta"))
async def cmd_reset_special_delta(message: Message):
    if not await ensure_allowed(message):
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.answer("Використання: /reset_special_delta BTCUSDT")
        return

    symbol = _normalize_symbol(parts[1])
    if symbol is None:
        await message.answer("Вкажіть символ. Приклад: /reset_special_delta BTCUSDT")
        return

    deleted = delete_user_special_delta_threshold(message.from_user.id, symbol)
    if deleted:
        await message.answer(f"Спеціальну дельту для {symbol} скинуто.")
    else:
        await message.answer(f"Для {symbol} не було спеціальної дельти.")


async def send_special_deltas_list(target: Message, user_id: int) -> None:
    eff = get_effective_settings_for_user(user_id)
    special_delta_thresholds = eff.get("special_delta_thresholds", {}) or {}
    if not special_delta_thresholds:
        await target.answer("Спеціальні дельти для тікерів не налаштовані.")
        return

    lines = [
        f"{symbol}: {float(value):.2f}%"
        for symbol, value in sorted(special_delta_thresholds.items())
    ]
    await target.answer("Спеціальні дельти для тікерів:\n" + "\n".join(lines))


@dp.message(Command("special_deltas"))
@dp.message(Command("special_delta_list"))
async def cmd_special_deltas(message: Message):
    if not await ensure_allowed(message):
        return
    await send_special_deltas_list(message, message.from_user.id)


@dp.message(SetSpreadState.waiting_value)
async def process_spread_value(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    text = (message.text or "").strip()
    if text.startswith("/") or text in {
        BTN_SPREADS,
        "Зміна спреду",
        BTN_SET_SPREAD,
        BTN_SET_DELTA,
        BTN_EXCHANGES,
        BTN_CONFIG,
        BTN_STOP,
        BTN_EXCHANGE_THRESHOLDS,
        BTN_SPECIAL_DELTAS,
        BTN_BLACKLIST,
    }:
        await state.clear()
        if text == BTN_SPREADS or text.startswith("/spreads"):
            await cmd_spreads(message)
            return
        if text in {"Зміна спреду", BTN_SET_SPREAD} or text.startswith("/set_spread"):
            await cmd_set_spread(message, state)
            return
        if text == BTN_SET_DELTA or text.startswith("/set_delta"):
            await cmd_set_delta(message, state)
            return
        if text == BTN_EXCHANGES or text.startswith("/exchanges"):
            await cmd_exchanges(message, state)
            return
        if text == BTN_CONFIG or text.startswith("/config"):
            await cmd_config(message)
            return
        if text == BTN_STOP or text.startswith("/stop"):
            await cmd_stop(message)
            return
        if text == BTN_EXCHANGE_THRESHOLDS:
            await btn_exchange_thresholds(message)
            return
        if text == BTN_SPECIAL_DELTAS:
            await btn_special_deltas_menu(message)
            return
        if text == BTN_BLACKLIST:
            await btn_blacklist_menu(message)
            return
        if text.startswith("/set_exchange_spread"):
            await cmd_set_exchange_spread(message)
            return
        if text.startswith("/reset_exchange_spread"):
            await cmd_reset_exchange_spread(message)
            return
        if text.startswith("/exchange_spreads"):
            await cmd_exchange_spreads(message)
            return
        if text.startswith("/set_special_delta"):
            await cmd_set_special_delta(message)
            return
        if text.startswith("/reset_special_delta"):
            await cmd_reset_special_delta(message)
            return
        if text.startswith("/special_deltas") or text.startswith("/special_delta_list"):
            await cmd_special_deltas(message)
            return
        if text.startswith("/blacklist_list"):
            await cmd_blacklist_list(message)
            return
        if text.startswith("/blacklist"):
            await cmd_blacklist(message)
            return
        if text.startswith("/unblacklist"):
            await cmd_unblacklist(message)
            return
        if text.startswith("/stop"):
            await cmd_stop(message)
            return
        if text.startswith("/start"):
            await cmd_start(message)
            return
    value = _parse_percent(message.text)
    if value is None:
        await message.answer("Некоректний формат. Спробуйте ще раз (наприклад: 1.25 або 1,25)")
        return
    set_user_min_spread(message.from_user.id, value)
    await message.answer(f"Мінімальний спред встановлено на {value:.2f}%")
    await state.clear()


@dp.message(F.text == "Зміна спреду")
async def btn_set_spread(message: Message, state: FSMContext):
    await cmd_set_spread(message, state)


@dp.message(F.text == "Вибір бірж")
async def btn_exchanges(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    cfg = load_config()
    user_cfg = get_user_settings(message.from_user.id)
    allowed = {ex.lower() for ex in ALL_EXCHANGES}
    all_ex = [ex for ex in list(dict.fromkeys(list(ALL_EXCHANGES) + list(cfg.get("all_exchanges", cfg.get("exchanges", []))))) if str(ex).lower() in allowed]
    base_selected = [ex for ex in user_cfg.get("exchanges") if str(ex).lower() in allowed] if user_cfg and user_cfg.get("exchanges") else all_ex
    selected = {ex.lower() for ex in base_selected}
    await state.set_state(ManageExchangesState.selecting)
    await state.update_data(selected=selected, all_ex=all_ex)
    kb = build_exchanges_keyboard(selected, all_ex).as_markup()
    await message.answer("Оберіть біржі (натискайте для перемикання), потім натисніть 'Зберегти'", reply_markup=kb)


@dp.message(Command("exchanges"))
async def cmd_exchanges(message: Message, state: FSMContext):
    await btn_exchanges(message, state)


@dp.callback_query(ManageExchangesState.selecting, F.data.startswith("exch:"))
async def toggle_exchange(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected: Set[str] = set(data.get("selected", set()))
    all_ex: List[str] = list(data.get("all_ex", []))
    ex = cb.data.split(":", 1)[1]
    ex_low = ex.lower()
    if ex_low in selected:
        selected.remove(ex_low)
    else:
        selected.add(ex_low)
    await state.update_data(selected=selected)
    kb = build_exchanges_keyboard(selected, all_ex).as_markup()
    await cb.message.edit_reply_markup(reply_markup=kb)
    await cb.answer()


@dp.callback_query(ManageExchangesState.selecting, F.data == "exch_all")
async def select_all_exchanges(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    all_ex: List[str] = list(data.get("all_ex", []))
    selected = {ex.lower() for ex in all_ex}
    await state.update_data(selected=selected)
    kb = build_exchanges_keyboard(selected, all_ex).as_markup()
    await cb.message.edit_reply_markup(reply_markup=kb)
    await cb.answer("Вибрано всі")


@dp.callback_query(ManageExchangesState.selecting, F.data == "exch_none")
async def select_none_exchanges(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    all_ex: List[str] = list(data.get("all_ex", []))
    selected: Set[str] = set()
    await state.update_data(selected=selected)
    kb = build_exchanges_keyboard(selected, all_ex).as_markup()
    await cb.message.edit_reply_markup(reply_markup=kb)
    await cb.answer("Скасовано всі")


@dp.callback_query(ManageExchangesState.selecting, F.data == "exch_save")
async def save_exchanges(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected: Set[str] = set(data.get("selected", set()))
    # Preserve original order from the known list where possible
    all_ex: List[str] = list(data.get("all_ex", []))
    new_list = [ex for ex in all_ex if ex.lower() in selected]
    if not new_list:
        await cb.answer("Оберіть принаймні одну біржу", show_alert=True)
        return
    set_user_exchanges(cb.from_user.id, new_list)
    await cb.message.edit_text("Налаштування бірж збережено.")
    await cb.answer("Збережено")
    await state.clear()


@dp.callback_query(ManageExchangesState.selecting, F.data == "exch_cancel")
async def cancel_exchanges(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Скасовано вибір бірж.")
    await cb.answer()
    await state.clear()


@dp.message(F.text == BTN_EXCHANGE_THRESHOLDS)
async def btn_exchange_thresholds(message: Message):
    if not await ensure_allowed(message):
        return
    await message.answer("Пороги бірж:", reply_markup=build_exchange_thresholds_menu().as_markup())


@dp.callback_query(F.data == "exthr:list")
async def cb_exchange_thresholds_list(cb: CallbackQuery):
    if not user_allowed(cb.from_user.id):
        await cb.answer("Доступ заборонений", show_alert=True)
        return
    await send_exchange_spreads_list(cb.message, cb.from_user.id)
    await cb.answer()


@dp.callback_query(F.data == "exthr:set")
async def cb_exchange_thresholds_set(cb: CallbackQuery, state: FSMContext):
    if not user_allowed(cb.from_user.id):
        await cb.answer("Доступ заборонений", show_alert=True)
        return
    await state.set_state(SetExchangeSpreadState.waiting_value)
    await cb.message.answer("Введіть біржу і мінімальний спред, наприклад: mexc 10")
    await cb.answer()


@dp.callback_query(F.data == "exthr:reset")
async def cb_exchange_thresholds_reset(cb: CallbackQuery, state: FSMContext):
    if not user_allowed(cb.from_user.id):
        await cb.answer("Доступ заборонений", show_alert=True)
        return
    await state.set_state(ResetExchangeSpreadState.waiting_value)
    await cb.message.answer("Введіть біржу для скидання, наприклад: mexc")
    await cb.answer()


@dp.message(F.text == BTN_SPECIAL_DELTAS)
async def btn_special_deltas_menu(message: Message):
    if not await ensure_allowed(message):
        return
    await message.answer("Дельти тікерів:", reply_markup=build_special_deltas_menu().as_markup())


@dp.callback_query(F.data == "sdelta:list")
async def cb_special_deltas_list(cb: CallbackQuery):
    if not user_allowed(cb.from_user.id):
        await cb.answer("Доступ заборонений", show_alert=True)
        return
    await send_special_deltas_list(cb.message, cb.from_user.id)
    await cb.answer()


@dp.callback_query(F.data == "sdelta:set")
async def cb_special_deltas_set(cb: CallbackQuery, state: FSMContext):
    if not user_allowed(cb.from_user.id):
        await cb.answer("Доступ заборонений", show_alert=True)
        return
    await state.set_state(SetSpecialDeltaState.waiting_value)
    await cb.message.answer("Введіть тікер і дельту, наприклад: BTCUSDT 4")
    await cb.answer()


@dp.callback_query(F.data == "sdelta:reset")
async def cb_special_deltas_reset(cb: CallbackQuery, state: FSMContext):
    if not user_allowed(cb.from_user.id):
        await cb.answer("Доступ заборонений", show_alert=True)
        return
    await state.set_state(ResetSpecialDeltaState.waiting_value)
    await cb.message.answer("Введіть тікер для скидання, наприклад: BTCUSDT")
    await cb.answer()


@dp.message(F.text == BTN_BLACKLIST)
async def btn_blacklist_menu(message: Message):
    if not await ensure_allowed(message):
        return
    await message.answer("Блекліст:", reply_markup=build_blacklist_menu().as_markup())


@dp.callback_query(F.data == "bl:list")
async def cb_blacklist_list(cb: CallbackQuery):
    if not user_allowed(cb.from_user.id):
        await cb.answer("Доступ заборонений", show_alert=True)
        return
    await send_blacklist_list(cb.message, cb.from_user.id)
    await cb.answer()


@dp.callback_query(F.data == "bl:add")
async def cb_blacklist_add(cb: CallbackQuery, state: FSMContext):
    if not user_allowed(cb.from_user.id):
        await cb.answer("Доступ заборонений", show_alert=True)
        return
    await state.set_state(AddBlacklistState.waiting_value)
    await cb.message.answer("Введіть тікер і тривалість, наприклад: BTCUSDT 24h")
    await cb.answer()


@dp.callback_query(F.data == "bl:remove")
async def cb_blacklist_remove(cb: CallbackQuery, state: FSMContext):
    if not user_allowed(cb.from_user.id):
        await cb.answer("Доступ заборонений", show_alert=True)
        return
    await state.set_state(RemoveBlacklistState.waiting_value)
    await cb.message.answer("Введіть тікер для зняття з блекліста, наприклад: BTCUSDT")
    await cb.answer()


@dp.message(SetExchangeSpreadState.waiting_value)
async def process_exchange_spread_button_value(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.answer("Введіть біржу і відсоток, наприклад: mexc 10")
        return

    exchange = _normalize_exchange(parts[0])
    if exchange is None:
        await message.answer("Невідома біржа. Приклад: mexc 10")
        return

    value = _parse_percent(parts[1])
    if value is None or value < 0:
        await message.answer("Некоректний відсоток. Приклад: mexc 10")
        return

    set_user_exchange_min_spread(message.from_user.id, exchange, value)
    await message.answer(f"Мінімальний спред для {exchange} встановлено на {value:.2f}%")
    await state.clear()


@dp.message(ResetExchangeSpreadState.waiting_value)
async def process_exchange_spread_reset_value(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    exchange = _normalize_exchange((message.text or "").strip().split()[0] if (message.text or "").strip() else "")
    if exchange is None:
        await message.answer("Введіть біржу для скидання, наприклад: mexc")
        return

    deleted = delete_user_exchange_min_spread(message.from_user.id, exchange)
    if deleted:
        await message.answer(f"Окремий мінімальний спред для {exchange} скинуто.")
    else:
        await message.answer(f"Для {exchange} не було окремого мінімального спреду.")
    await state.clear()


@dp.message(SetSpecialDeltaState.waiting_value)
async def process_special_delta_button_value(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.answer("Введіть тікер і дельту, наприклад: BTCUSDT 4")
        return

    symbol = _normalize_symbol(parts[0])
    if symbol is None:
        await message.answer("Вкажіть тікер. Приклад: BTCUSDT 4")
        return

    value = _parse_percent(parts[1])
    if value is None or value < 0:
        await message.answer("Некоректна дельта. Приклад: BTCUSDT 4")
        return

    set_user_special_delta_threshold(message.from_user.id, symbol, value)
    await message.answer(f"Спеціальну дельту для {symbol} встановлено на {value:.2f}%")
    await state.clear()


@dp.message(ResetSpecialDeltaState.waiting_value)
async def process_special_delta_reset_value(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    symbol = _normalize_symbol((message.text or "").strip().split()[0] if (message.text or "").strip() else "")
    if symbol is None:
        await message.answer("Введіть тікер для скидання, наприклад: BTCUSDT")
        return

    deleted = delete_user_special_delta_threshold(message.from_user.id, symbol)
    if deleted:
        await message.answer(f"Спеціальну дельту для {symbol} скинуто.")
    else:
        await message.answer(f"Для {symbol} не було спеціальної дельти.")
    await state.clear()


@dp.message(AddBlacklistState.waiting_value)
async def process_blacklist_add_value(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    parts = (message.text or "").strip().split()
    if not parts:
        await message.answer("Введіть тікер і тривалість, наприклад: BTCUSDT 24h")
        return

    symbol = _normalize_symbol(parts[0])
    if symbol is None:
        await message.answer("Вкажіть тікер. Приклад: BTCUSDT 24h")
        return

    hours = 24
    if len(parts) >= 2:
        parsed_hours = _parse_blacklist_duration_hours(parts[1])
        if parsed_hours is None or parsed_hours <= 0:
            await message.answer("Некоректна тривалість. Використовуйте: 24h, 24, 1d, 90m")
            return
        hours = parsed_hours

    expires_at = int(time.time()) + (hours * 3600)
    upsert_user_token_blacklist(message.from_user.id, symbol, expires_at)
    await message.answer(f"Токен {symbol} вимкнено на {hours} год.")
    await state.clear()


@dp.message(RemoveBlacklistState.waiting_value)
async def process_blacklist_remove_value(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    symbol = _normalize_symbol((message.text or "").strip().split()[0] if (message.text or "").strip() else "")
    if symbol is None:
        await message.answer("Введіть тікер для зняття з блекліста, наприклад: BTCUSDT")
        return

    deleted = delete_user_token_blacklist(message.from_user.id, symbol)
    if deleted:
        await message.answer(f"Блокування для {symbol} знято.")
    else:
        await message.answer(f"{symbol} не був у блеклісті.")
    await state.clear()


@dp.message(F.text == "Показати конфіг")
@dp.message(Command("config"))
async def cmd_config(message: Message):
    if not await ensure_allowed(message):
        return
    eff = get_effective_settings_for_user(message.from_user.id)
    exchanges = ", ".join(eff.get("exchanges", []))
    exchange_min_spreads = eff.get("exchange_min_spreads", {}) or {}
    special_delta_thresholds = eff.get("special_delta_thresholds", {}) or {}
    blacklist_rows = list_user_token_blacklist(message.from_user.id)
    exchange_spreads_text = (
        "\n".join(f"{exchange}: {float(value):.2f}%" for exchange, value in sorted(exchange_min_spreads.items()))
        if exchange_min_spreads
        else "не налаштовані"
    )
    special_deltas_text = (
        "\n".join(f"{symbol}: {float(value):.2f}%" for symbol, value in sorted(special_delta_thresholds.items()))
        if special_delta_thresholds
        else "не налаштовані"
    )
    now = int(time.time())
    blacklist_text = (
        "\n".join(
            f"{symbol}: {max(1, (int(expires_at) - now + 3599) // 3600)}h"
            for symbol, expires_at in blacklist_rows
        )
        if blacklist_rows
        else "не налаштований"
    )
    sep = "\n---------------------------------------------\n"
    await message.answer(
        (
            "<b>Поточний конфіг:</b>\n"
            f"Біржі: {exchanges}"
            f"{sep}"
            f"Мінімальний спред: {eff.get('min_spread_pct')}\n"
            f"Мінімальні спреди для бірж:\n{exchange_spreads_text}"
            f"{sep}"
            f"Дельта сповіщень: {eff.get('delta_threshold_pct')}\n"
            f"Спеціальні дельти:\n{special_deltas_text}"
            f"{sep}"
            f"Чорний список:\n{blacklist_text}"
        )
    )


@dp.message(Command("blacklist"))
async def cmd_blacklist(message: Message):
    if not await ensure_allowed(message):
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.answer("Використання: /blacklist SYMBOL [24h|24|1d]")
        return
    symbol = parts[1].strip().upper()
    if not symbol:
        await message.answer("Вкажіть символ, наприклад: /blacklist BTCUSDT 24h")
        return
    hours = 24
    if len(parts) >= 3:
        parsed_hours = _parse_blacklist_duration_hours(parts[2])
        if parsed_hours is None or parsed_hours <= 0:
            await message.answer("Некоректна тривалість. Використовуйте: 24h, 24, 1d, 90m")
            return
        hours = parsed_hours
    now = int(time.time())
    expires_at = now + (hours * 3600)
    upsert_user_token_blacklist(message.from_user.id, symbol, expires_at)
    await message.answer(f"Токен {symbol} вимкнено на {hours} год.")


@dp.message(Command("unblacklist"))
async def cmd_unblacklist(message: Message):
    if not await ensure_allowed(message):
        return
    parts = (message.text or "").strip().split()
    if len(parts) < 2:
        await message.answer("Використання: /unblacklist SYMBOL")
        return
    symbol = parts[1].strip().upper()
    if not symbol:
        await message.answer("Вкажіть символ, наприклад: /unblacklist BTCUSDT")
        return
    deleted = delete_user_token_blacklist(message.from_user.id, symbol)
    if deleted:
        await message.answer(f"Блокування для {symbol} знято.")
    else:
        await message.answer(f"{symbol} не був у блеклисті.")


async def send_blacklist_list(target: Message, user_id: int) -> None:
    rows = list_user_token_blacklist(user_id)
    if not rows:
        await target.answer("Блекліст порожній.")
        return
    now = int(time.time())
    lines: List[str] = []
    for symbol, expires_at in rows:
        left_seconds = max(0, int(expires_at) - now)
        left_hours = left_seconds / 3600.0
        lines.append(f"{symbol}: залишилось {left_hours:.1f} год.")
    await target.answer("Заблоковані токени:\n" + "\n".join(lines))


@dp.message(Command("blacklist_list"))
async def cmd_blacklist_list(message: Message):
    if not await ensure_allowed(message):
        return
    await send_blacklist_list(message, message.from_user.id)


@dp.message(Command("set_delta"))
async def cmd_set_delta(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    await state.set_state(SetDeltaState.waiting_value)
    await message.answer("Введіть дельту у % (поріг зміни спреду для сповіщення):")


@dp.message(F.text == BTN_SET_DELTA)
async def btn_set_delta(message: Message, state: FSMContext):
    await cmd_set_delta(message, state)


@dp.message(SetDeltaState.waiting_value)
async def process_delta_value(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    text = (message.text or "").strip()
    if text.startswith("/") or text in {
        BTN_SPREADS,
        "Зміна спреду",
        BTN_SET_SPREAD,
        BTN_SET_DELTA,
        BTN_EXCHANGES,
        BTN_CONFIG,
        BTN_STOP,
        BTN_EXCHANGE_THRESHOLDS,
        BTN_SPECIAL_DELTAS,
        BTN_BLACKLIST,
    }:
        await state.clear()
        if text == BTN_SPREADS or text.startswith("/spreads"):
            await cmd_spreads(message)
            return
        if text in {"Зміна спреду", BTN_SET_SPREAD} or text.startswith("/set_spread"):
            await cmd_set_spread(message, state)
            return
        if text == BTN_SET_DELTA or text.startswith("/set_delta"):
            await cmd_set_delta(message, state)
            return
        if text == BTN_EXCHANGES or text.startswith("/exchanges"):
            await cmd_exchanges(message, state)
            return
        if text == BTN_CONFIG or text.startswith("/config"):
            await cmd_config(message)
            return
        if text == BTN_STOP or text.startswith("/stop"):
            await cmd_stop(message)
            return
        if text == BTN_EXCHANGE_THRESHOLDS:
            await btn_exchange_thresholds(message)
            return
        if text == BTN_SPECIAL_DELTAS:
            await btn_special_deltas_menu(message)
            return
        if text == BTN_BLACKLIST:
            await btn_blacklist_menu(message)
            return
        if text.startswith("/set_exchange_spread"):
            await cmd_set_exchange_spread(message)
            return
        if text.startswith("/reset_exchange_spread"):
            await cmd_reset_exchange_spread(message)
            return
        if text.startswith("/exchange_spreads"):
            await cmd_exchange_spreads(message)
            return
        if text.startswith("/set_special_delta"):
            await cmd_set_special_delta(message)
            return
        if text.startswith("/reset_special_delta"):
            await cmd_reset_special_delta(message)
            return
        if text.startswith("/special_deltas") or text.startswith("/special_delta_list"):
            await cmd_special_deltas(message)
            return
        if text.startswith("/blacklist_list"):
            await cmd_blacklist_list(message)
            return
        if text.startswith("/blacklist"):
            await cmd_blacklist(message)
            return
        if text.startswith("/unblacklist"):
            await cmd_unblacklist(message)
            return
        if text.startswith("/stop"):
            await cmd_stop(message)
            return
        if text.startswith("/start"):
            await cmd_start(message)
            return
    value = _parse_percent(message.text)
    if value is None:
        await message.answer("Некоректний формат. Спробуйте ще раз (наприклад: 1.0 або 1,0)")
        return
    set_user_delta_threshold(message.from_user.id, value)
    await message.answer(f"Дельту сповіщень встановлено на {value:.2f}%")
    await state.clear()


async def on_startup() -> None:
    # Ensure database is initialized
    init_db()
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Запуск і фонові спреди"),
            BotCommand(command="spreads", description="Запустити фоновий збір"),
            BotCommand(command="set_spread", description="Змінити мінімальний спред"),
            BotCommand(command="set_delta", description="Змінити поріг дельти"),
            BotCommand(command="set_exchange_spread", description="Мінімальний спред для біржі"),
            BotCommand(command="reset_exchange_spread", description="Скинути спред для біржі"),
            BotCommand(command="exchange_spreads", description="Показати спреди для бірж"),
            BotCommand(command="exchanges", description="Вибір бірж"),
            BotCommand(command="config", description="Показати конфіг"),
            BotCommand(command="blacklist", description="Блок токена на час"),
            BotCommand(command="unblacklist", description="Зняти блок токена"),
            BotCommand(command="blacklist_list", description="Список заблокованих"),
            BotCommand(command="stop", description="Зупинити надсилання у цьому чаті"),
            BotCommand(command="set_special_delta", description="Дельта для окремого тікера"),
            BotCommand(command="reset_special_delta", description="Скинути дельту тікера"),
            BotCommand(command="special_deltas", description="Показати дельти тікерів"),
        ]
    )
    await ensure_global_fetcher_started()


async def main() -> None:
    await on_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот зупинено")


