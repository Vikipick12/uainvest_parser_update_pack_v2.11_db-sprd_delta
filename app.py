import asyncio
import logging
import os
import time
from typing import Dict, List, Optional, Set

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
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
    return {
        "exchanges": exchanges,
        "min_spread_pct": min_spread_pct,
        "delta_threshold_pct": delta_threshold_pct,
        "data_path": str(cfg.get("data_path", "data.json")),
        "all_exchanges": all_exchanges,
    }


def build_main_menu_keyboard() -> ReplyKeyboardBuilder:
    kb = ReplyKeyboardBuilder()
    kb.button(text="Показати спреди")
    kb.button(text="Зміна спреду")
    kb.button(text="Вибір бірж")
    kb.button(text="Показати конфіг")
    kb.adjust(2, 2)
    return kb


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
    
    if funding_text:
        text += f"\n{funding_text}"
    
    return text


async def ensure_allowed(message: Message) -> bool:
    if not user_allowed(message.from_user.id):
        await message.answer("Доступ заборонений. Зверніться до адміністратора.")
        return False
    return True


async def start_background_loop(chat_id: int, user_id: int):
    while True:
        try:
            eff = get_effective_settings_for_user(user_id)
            rows = get_spreads_for_exchanges(
                eff.get("exchanges", []),
                min_spread_pct=float(eff.get("min_spread_pct", 0.0)),
                path=str(eff.get("data_path", "data.json")),
            )
            if not rows:
                # Send the 'no offers' message only once until results appear again
                if not no_spreads_notified.get(chat_id, False):
                    await bot.send_message(chat_id, "Немає спредів за заданими фільтрами.")
                    no_spreads_notified[chat_id] = True
            else:
                no_spreads_notified[chat_id] = False
                # Send each opportunity as a separate message
                delta_threshold = float(eff.get("delta_threshold_pct", 1.0))
                for r in rows:
                    try:
                        current = float(r.get("spread_pct", 0.0))
                        symbol = str(r.get("symbol", ""))
                        symbol_upper = symbol.upper()
                        if symbol_upper and is_user_token_blacklisted(user_id, symbol_upper):
                            continue
                        long_ex = str(r.get("long_exchange", ""))
                        short_ex = str(r.get("short_exchange", ""))
                        last = get_last_spread_pct(user_id, symbol, long_ex, short_ex)
                        should_send = False
                        if last is None:
                            # First sighting for this user/opportunity
                            should_send = True
                        else:
                            if abs(current - float(last)) >= delta_threshold:
                                should_send = True
                        if should_send:
                            await bot.send_message(chat_id, format_spread_card(r))
                            upsert_last_spread_pct(user_id, symbol, long_ex, short_ex, current)
                    except Exception as inner_e:  # noqa: BLE001
                        logger.exception("Помилка обробки можливості: %s", inner_e)
        except asyncio.CancelledError:
            # Тихо завершуємо цикл за запитом користувача (/stop)
            break
        except Exception as e:  # noqa: BLE001
            logger.exception("Помилка у фоному циклі: %s", e)
            await bot.send_message(chat_id, f"Виникла помилка у фоні: {e}")
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


@dp.message(Command("set_spread"))
async def cmd_set_spread(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    await state.set_state(SetSpreadState.waiting_value)
    await message.answer("Введіть мінімальний спред у %:")


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


@dp.message(SetSpreadState.waiting_value)
async def process_spread_value(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    text = (message.text or "").strip()
    if text.startswith("/") or text in {"Показати спреди", "Зміна спреду", "Вибір бірж", "Показати конфіг"}:
        await state.clear()
        if text == "Показати спреди" or text.startswith("/spreads"):
            await cmd_spreads(message)
            return
        if text == "Зміна спреду" or text.startswith("/set_spread"):
            await cmd_set_spread(message, state)
            return
        if text == "Вибір бірж" or text.startswith("/exchanges"):
            await cmd_exchanges(message, state)
            return
        if text == "Показати конфіг" or text.startswith("/config"):
            await cmd_config(message)
            return
        if text.startswith("/set_delta"):
            await cmd_set_delta(message, state)
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


@dp.message(F.text == "Показати конфіг")
@dp.message(Command("config"))
async def cmd_config(message: Message):
    if not await ensure_allowed(message):
        return
    eff = get_effective_settings_for_user(message.from_user.id)
    exchanges = ", ".join(eff.get("exchanges", []))
    await message.answer(
        (
            "<b>Поточний конфіг:</b>\n"
            f"Біржі: {exchanges}\n"
            f"Мінімальний спред: {eff.get('min_spread_pct')}\n"
            f"Дельта сповіщень: {eff.get('delta_threshold_pct')}\n"
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


@dp.message(Command("blacklist_list"))
async def cmd_blacklist_list(message: Message):
    if not await ensure_allowed(message):
        return
    rows = list_user_token_blacklist(message.from_user.id)
    if not rows:
        await message.answer("Блекліст порожній.")
        return
    now = int(time.time())
    lines: List[str] = []
    for symbol, expires_at in rows:
        left_seconds = max(0, int(expires_at) - now)
        left_hours = left_seconds / 3600.0
        lines.append(f"{symbol}: залишилось {left_hours:.1f} год.")
    await message.answer("Заблоковані токени:\n" + "\n".join(lines))


@dp.message(Command("set_delta"))
async def cmd_set_delta(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    await state.set_state(SetDeltaState.waiting_value)
    await message.answer("Введіть дельту у % (поріг зміни спреду для сповіщення):")


@dp.message(SetDeltaState.waiting_value)
async def process_delta_value(message: Message, state: FSMContext):
    if not await ensure_allowed(message):
        return
    text = (message.text or "").strip()
    if text.startswith("/") or text in {"Показати спреди", "Зміна спреду", "Вибір бірж", "Показати конфіг"}:
        await state.clear()
        if text == "Показати спреди" or text.startswith("/spreads"):
            await cmd_spreads(message)
            return
        if text == "Зміна спреду" or text.startswith("/set_spread"):
            await cmd_set_spread(message, state)
            return
        if text == "Вибір бірж" or text.startswith("/exchanges"):
            await cmd_exchanges(message, state)
            return
        if text == "Показати конфіг" or text.startswith("/config"):
            await cmd_config(message)
            return
        if text.startswith("/set_delta"):
            await cmd_set_delta(message, state)
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
            BotCommand(command="exchanges", description="Вибір бірж"),
            BotCommand(command="config", description="Показати конфіг"),
            BotCommand(command="blacklist", description="Блок токена на час"),
            BotCommand(command="unblacklist", description="Зняти блок токена"),
            BotCommand(command="blacklist_list", description="Список заблокованих"),
            BotCommand(command="stop", description="Зупинити надсилання у цьому чаті"),
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


