"""Telegram-хендлеры: команды, callback-запросы, обработка текста.

Вся логика взаимодействия с пользователем через Telegram.
Каждая команда (/start, /create, /profile и т.д.) и каждый callback_data
имеет соответствующий асинхронный хендлер.
"""

import time
import uuid
import logging
import functools
import random as _random
from secrets import token_hex
from typing import Optional

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)
from telegram.ext.filters import MessageFilter

import config
from config import MAX_CHARACTERS_PER_PLAYER, MAX_GROUP_SIZE, ADMIN_PASSWORD, DURABILITY_LOSS_PERCENT, DEATH_DURABILITY_LOSS, TURN_TIMEOUT
from core.character import Character
from core.classes import CLASSES, CLASS_NAMES_RU, StatBlock
from core.locations import LOCATIONS, get_location
from core.raid import create_raid, generate_loot, distribute_exp_gold, RaidSession, RaidEncounter, RaidStatus, session_to_dict, session_from_dict, create_enemy, resolve_player_turn, resolve_companion_turn, resolve_enemy_turn, build_initiative_order, get_current_turn, advance_turn_core, pick_enemy_target
from core.economy import MARKET
from data.storage import storage
from ai.narrative import call_narrative_api
from core.events import resolve_event, RaidEvent, EventReward
from core.items import Item as InventoryItem
from utils.rng import roll_chance
from data.storage import get_template
from bot.keyboards import (
    main_menu, class_selection, location_list, confirm_raid,
    raid_actions, raid_next, raid_done, raid_failed,
    raid_lobby, raid_lobby_text,
    inventory_pages, item_actions as item_actions_kb,
    char_list, char_delete_confirm, market_listings as market_listings_kb, market_confirm,
)

log = logging.getLogger("rpg.handlers")

_lobbies: dict[str, dict] = {}

# Динамические настройки (загружаются из БД, могут быть перезаписаны админом)
_allowed_chat_id: Optional[int] = config.ALLOWED_CHAT_ID
_allowed_topic_id: Optional[int] = None
_settings_loaded: bool = False


async def _load_settings():
    global _allowed_chat_id, _allowed_topic_id, _settings_loaded
    if _settings_loaded:
        return
    _settings_loaded = True
    raw_chat = await storage.get_setting("allowed_chat_id", "")
    raw_topic = await storage.get_setting("allowed_topic_id", "")
    if raw_chat:
        _allowed_chat_id = int(raw_chat)
    if raw_topic:
        _allowed_topic_id = int(raw_topic)


async def _save_setting(key: str, value: str):
    global _allowed_chat_id, _allowed_topic_id
    await storage.set_setting(key, value)
    if key == "allowed_chat_id":
        _allowed_chat_id = int(value) if value else config.ALLOWED_CHAT_ID
    elif key == "allowed_topic_id":
        _allowed_topic_id = int(value) if value else None


class _ChatTopicFilter(MessageFilter):
    """Пропускает ЛС и сообщения из указанного топика группы."""

    def filter(self, message):
        chat = message.chat
        if chat.type == "private":
            return True
        if _allowed_chat_id is not None and chat.id == _allowed_chat_id:
            if message.is_topic_message:
                if _allowed_topic_id is not None:
                    return message.message_thread_id == _allowed_topic_id
                return True
            return False
        return False


ALLOWED_CHAT_FILTER = _ChatTopicFilter()


def _auth_cb(fn):
    """Декоратор для callback-хендлеров: проверяет _chat_allowed перед выполнением."""
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not _chat_allowed(update):
            if update.callback_query:
                await update.callback_query.answer()
            return
        return await fn(update, context)
    return wrapper


def _chat_allowed(update: Update) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False
    if chat.type == "private":
        return True
    if _allowed_chat_id is not None and chat.id == _allowed_chat_id:
        msg = update.effective_message
        if msg and msg.is_topic_message:
            if _allowed_topic_id is not None:
                return msg.message_thread_id == _allowed_topic_id
            return True
        return False
    return False


def _attack_desc(owner: str, result, noun: str = "нанесли") -> str:
    if result is None:
        return ""
    dmg = result.final_damage
    if result.is_dodged:
        return f"🛡 {owner} промахнулись!"
    crit = " КРИТ!" if result.is_crit else ""
    attr_label = {"strength": "💪", "agility": "🏃", "intelligence": "🧠"}.get(result.attribute, "")
    return f"⚔️ {owner} {noun} {dmg} урона{crit} {attr_label}"


def _enemy_status_line(enc) -> str:
    tpl = enc.enemy_template
    perc = int(enc.enemy_hp / max(enc.enemy_max_hp, 1) * 100)
    bar = "▓" * (perc // 10) + "░" * (10 - perc // 10)
    return f"👾 {tpl['name']} ❤️ {enc.enemy_hp}/{enc.enemy_max_hp}\n{bar}"


def _char_status_line(char: Character) -> str:
    return f"❤️ **{char.hp}**/{char.max_hp} | ⚔️ {char.attack_min}-{char.attack_max} | 🛡 {char.defense}"


def _initiative_line(enc, uid: int, char: Character) -> str:
    order = enc.initiative_order
    if not order:
        return ""
    icons = {"player": "🧑", "companion": "🛡", "enemy": "👾"}
    current = enc.current_turn_index if enc.initiative_order else 0
    parts = []
    for i, entry in enumerate(order):
        icon = icons.get(entry["type"], "❓")
        name = entry["name"]
        if entry["type"] == "player" and entry["uid"] == uid:
            name = "Вы"
        elif entry["type"] == "companion" and entry["uid"] == uid:
            name = f"Страж"
        prefix = "➡️ " if i == current else ""
        parts.append(f"{prefix}{icon}{name} ({entry['initiative']})")
    return f"⚡ Инициатива: {' → '.join(parts)}"


def _encounter_header(cur: int, total: int, enc, char: Character, uid: int = 0) -> str:
    header = f"⚔️ Рейд {cur}/{total}\n{_initiative_line(enc, uid, char)}\n\n"
    header += f"{_enemy_status_line(enc)}\n\n"
    header += _char_status_line(char)
    if char.companion and char.companion.alive:
        header += f"\n🛡 Страж: ❤️ {char.companion.hp}/{char.companion.max_hp}"
    return header


def _slot_item(char: Character, slot: str) -> str:
    item = getattr(char.equipment, slot, None)
    if item:
        return f"{item.name} ({item.rarity.value})"
    return "пусто"


async def _get_char(user_id: int, context: ContextTypes.DEFAULT_TYPE | None = None) -> Optional[Character]:
    chars = await storage.load_characters(user_id)
    if not chars:
        return None
    if context:
        active_name = context.user_data.get("active_char")
        if active_name:
            for c in chars:
                if c.name == active_name:
                    return c
    return chars[0]


async def _save_char(char: Character):
    await storage.save_character(char)


async def _ensure_char(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[Character]:
    char = await _get_char(update.effective_user.id, context)
    if not char:
        await _reply(update, "У вас нет персонажа. Создайте: /create")
        return None
    return char


async def _reply(update: Update, text: str, **kwargs):
    kwargs.setdefault("parse_mode", ParseMode.MARKDOWN)
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, **kwargs)
        except Exception:
            await update.callback_query.message.reply_text(text, **kwargs)
    elif update.message:
        await update.message.reply_text(text, **kwargs)





# ─── /menu /start ──────────────────────────────────────────

async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chars = await storage.load_characters(update.effective_user.id)
    if chars:
        await _reply(update, f"С возвращением, {chars[0].name}!", reply_markup=main_menu())
    else:
        await _reply(update,
            "🎲 Добро пожаловать в RPG!\n"
            "Здесь нет персонажей — создайте первого:\n\n"
            "/create — создать персонажа\n"
            "/help — список команд",
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _reply(update,
        "📖 Команды:\n"
        "/create — новый персонаж\n"
        "/profile — статистика\n"
        "/inventory — инвентарь\n"
        "/location — список локаций\n"
        "/market — рынок\n"
        "/raid — вернуться в рейд\n"
        "/forfeit — сдаться в рейде\n"
        "/characters — мои персонажи\n"
        "/menu — главное меню",
    )

# ─── /create — создание персонажа ───────────────────────────

async def cmd_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ok = await storage.can_create_character(uid)
    if not ok:
        await _reply(update, f"Достигнут лимит ({MAX_CHARACTERS_PER_PLAYER} персонажей).")
        return
    context.user_data["creation"] = {"step": "name"}
    await _reply(update, "Введите имя персонажа (2–24 символа):")


async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get("creation")
    if not state:
        await _reply(update, "Нет активного создания персонажа. /create")
        return
    if state["step"] == "description":
        state["description"] = ""
        state["step"] = "class"
        await _reply(update, "Выберите класс:", reply_markup=class_selection())
    elif state["step"] == "companion_desc":
        state["companion_description"] = ""
        await _finish_creation(update, context, state)
    else:
        await _reply(update, "Сейчас нельзя пропустить этот шаг.")


async def _finish_creation(update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict):
    uid = update.effective_user.id
    char = Character(
        owner_tg_id=uid,
        name=state["name"],
        class_key=state["class_key"],
        description=state.get("description", ""),
        companion_name=state.get("companion_name", "Призванный страж"),
        companion_description=state.get("companion_description", ""),
    )
    await _save_char(char)
    context.user_data["active_char"] = char.name
    context.user_data.pop("creation", None)
    await _reply(update,
        f"✅ Персонаж **{char.name}** ({CLASS_NAMES_RU[char.class_key]}) создан!",
        reply_markup=main_menu(),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _chat_allowed(update):
        return
    uid = update.effective_user.id
    text = update.message.text.strip()

    state = context.user_data.get("creation")
    if state and state["step"] == "name":
        if len(text) < 2 or len(text) > 24:
            await update.message.reply_text("Имя должно быть от 2 до 24 символов.")
            return
        state["name"] = text
        state["step"] = "description"
        await update.message.reply_text("Введите описание персонажа (или /skip):")
        return

    if state and state["step"] == "description":
        state["description"] = text
        state["step"] = "class"
        await update.message.reply_text("Выберите класс:", reply_markup=class_selection())
        return

    if state and state["step"] == "class":
        return

    if state and state["step"] == "companion_name":
        state["companion_name"] = text
        state["step"] = "companion_desc"
        await update.message.reply_text("Введите описание стража (или /skip):")
        return

    if state and state["step"] == "companion_desc":
        state["companion_description"] = text
        await _finish_creation(update, context, state)
        return

    session = await _get_session(context)
    if session and session.status == RaidStatus.IN_PROGRESS:
        enc = session.encounters[session.current_encounter]
        turn = get_current_turn(enc)
        if turn and turn["type"] == "player" and turn["uid"] == uid:
            context.user_data.pop("raid_action_pending", None)
            if enc.turn_timeout_deadline and time.time() > enc.turn_timeout_deadline:
                await update.message.reply_text("⏰ Ваш ход истёк! Вы промедлили…")
                advance_turn_core(enc)
                await _advance_turn(context, session)
                return
            await _handle_player_turn(update, context, session, uid, text)
            return
        if context.user_data.get("raid_action_pending"):
            context.user_data.pop("raid_action_pending", None)
            await update.message.reply_text("Сейчас не ваш ход. Ожидайте.")
            return
    elif context.user_data.get("raid_action_pending"):
        context.user_data.pop("raid_action_pending", None)
        await update.message.reply_text("Рейд уже завершён.")
        return

    sell = context.user_data.get("sell")
    if sell and sell["step"] == "price":
        try:
            price = int(text)
        except ValueError:
            await update.message.reply_text("Введите число — цену в золоте.")
            return
        if price <= 0:
            await update.message.reply_text("Цена должна быть положительной.")
            return
        char = sell["char"]
        item_uid = sell["item_uid"]
        item = next((i for i in char.inventory if i.uid == item_uid), None)
        if not item:
            await update.message.reply_text("Предмет не найден.")
            context.user_data.pop("sell", None)
            return
        char.inventory.remove(item)
        listing = MARKET.create_listing(char.owner_tg_id, char.name, item, price)
        if not listing:
            await update.message.reply_text("Ошибка создания объявления.")
            return
        await MARKET.persist_listing(storage, listing)
        await _save_char(char)
        context.user_data.pop("sell", None)
        await update.message.reply_text(f"✅ {item.name} выставлен на рынок за {price}💰")
        return

#    await update.message.reply_text("Неизвестная команда. /help")

# ═══════════════════════════════════════════════════════════════
# /profile — просмотр персонажа
# ═══════════════════════════════════════════════════════════════

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    char = await _ensure_char(update, context)
    if not char:
        return
    t = char.template
    s = char.stats
    raid_info = ""
    if char.in_raid:
        raid_info = "\n⚠️ В рейде"
    elif not char.can_raid():
        rem = char.raid_cooldown_remaining()
        hrs = int(rem // 3600)
        mins = int((rem % 3600) // 60)
        raid_info = f"\n⏳ Рейд через {hrs}ч {mins}м"
    points_line = ""
    if char.stat_points > 0:
        points_line = f"\n📌 **{char.stat_points}** очков для распределения"
    text = (
        f"👤 **{char.name}** — {t.name} | Ур. {char.level}\n"
        f"📝 {char.description}\n"
        f"✨ Опыт: {char.experience}/{char.exp_to_next}{points_line}\n\n"
        f"{_char_status_line(char)}\n"
        f"🎯 Крит: {char.crit_chance*100:.1f}% | Уклон: {char.dodge_chance*100:.1f}%\n"
        f"📊 Сила {s.strength} | Ловк {s.agility} | Инт {s.intelligence}\n\n"
        f"💰 **{char.gold}** золота{raid_info}\n\n"
        f"🗡 Оружие: {_slot_item(char, 'weapon')}\n"
        f"🛡 Броня: {_slot_item(char, 'armor')}\n"
        f"💍 Аксессуар: {_slot_item(char, 'accessory')}"
    )
    if char.companion and char.companion.alive:
        c = char.companion
        text += f"\n\n🛡 Страж: {c.name}\n❤️ {c.hp}/{c.max_hp} | ⚔️ {c.attack_min}-{c.attack_max}"
    kb = main_menu()
    if char.stat_points > 0:
        from bot.keyboards import stats_distribution
        kb = stats_distribution()
    await _reply(update, text, reply_markup=kb)


# ═══════════════════════════════════════════════════════════════
# /toprpg — топ персонажей
# ═══════════════════════════════════════════════════════════════

async def cmd_toprpg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_chars = await storage.load_all_characters()
    if not all_chars:
        await _reply(update, "Пока нет ни одного персонажа.")
        return

    by_level = sorted(all_chars, key=lambda x: x[1].level, reverse=True)[:10]
    by_gold = sorted(all_chars, key=lambda x: x[1].gold, reverse=True)[:10]

    lines = ["🏆 **Топ по уровню:**"]
    for i, (_, c) in enumerate(by_level, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} **{c.name}** — ур. {c.level} ({CLASS_NAMES_RU.get(c.class_key, c.class_key)})")

    lines.append("")
    lines.append("💰 **Топ по золоту:**")
    for i, (_, c) in enumerate(by_gold, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(f"{medal} **{c.name}** — {c.gold}💰")

    await _reply(update, "\n".join(lines))


# ═══════════════════════════════════════════════════════════════
# /inventory — инвентарь
# ═══════════════════════════════════════════════════════════════

async def cmd_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    char = await _ensure_char(update, context)
    if not char:
        return
    await _show_inventory(update, context, char, 0)


async def _show_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE, char: Character, page: int):
    text = f"🎒 Инвентарь **{char.name}**\n"
    if not char.inventory:
        text += "Пусто."
    else:
        start = page * 6
        items = char.inventory[start:start + 6]
        for i, item in enumerate(items, start=start + 1):
            dur = f" ({item.durability}/{item.durability_max})" if hasattr(item, 'durability') and item.durability_max else ""
            text += f"\n{i}. {item.icon or '📦'} **{item.name}**{dur}"
    await _reply(update, text, reply_markup=inventory_pages(char.inventory, page))

# ═══════════════════════════════════════════════════════════════
# /characters — управление персонажами
# ═══════════════════════════════════════════════════════════════

async def cmd_characters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chars = await storage.load_characters(update.effective_user.id)
    if not chars:
        await _reply(update, "Нет персонажей. /create")
        return
    current = context.user_data.get("active_char") or chars[0].name
    await _reply(update, "Ваши персонажи:", reply_markup=char_list(chars, current))

# ═══════════════════════════════════════════════════════════════
# /location — список локаций → рейд
# ═══════════════════════════════════════════════════════════════

async def cmd_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    char = await _ensure_char(update, context)
    if not char:
        return
    await _reply(update, "🗺 Выберите локацию:", reply_markup=location_list())

# ═══════════════════════════════════════════════════════════════
# /market — рынок
# ═══════════════════════════════════════════════════════════════

async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    listings = MARKET.get_active_listings()
    if not listings:
        await _reply(update, "🏪 Рынок пуст.", reply_markup=main_menu())
        return
    await _show_market(update, context, listings, 0)


# ═══════════════════════════════════════════════════════════════
# /raid — вернуться в активный рейд
# ═══════════════════════════════════════════════════════════════

async def cmd_raid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = await _get_session(context)
    if not session or session.status != RaidStatus.IN_PROGRESS:
        char = await _get_char(update.effective_user.id, context)
        if char and char.in_raid:
            char.in_raid = False
            char.release_companion()
            await _save_char(char)
            await _reply(update, "Обнаружен зависший флаг рейда — сброшен. /location чтобы начать заново.")
            return
        await _reply(update, "Нет активного рейда. /location чтобы начать.")
        return
    char = await _get_char(update.effective_user.id, context)
    if not char:
        await _reply(update, "Персонаж не найден.")
        return
    enc = session.encounters[session.current_encounter]
    total = len(session.encounters)
    cur = session.current_encounter + 1
    msg = await update.message.reply_text(_encounter_header(cur, total, enc, char, update.effective_user.id), reply_markup=raid_actions())
    context.user_data["raid_msg_chat"] = msg.chat_id
    context.user_data["raid_msg_id"] = msg.message_id
    context.user_data["raid_msg_thread"] = msg.message_thread_id


async def _show_market(update: Update, context: ContextTypes.DEFAULT_TYPE, listings: list, page: int):
    text = f"🏪 Рынок — стр. {page + 1}\n"
    per_page = 5
    start = page * per_page
    batch = listings[start:start + per_page]
    for i, listing in enumerate(batch, start=start + 1):
        text += f"\n{i}. {listing.item.name} ({listing.item.rarity.value}) — {listing.price}💰"
    await _reply(update, text, reply_markup=market_listings_kb(listings, page))

# ═══════════════════════════════════════════════════════════════
# Callback: главное меню
# ═══════════════════════════════════════════════════════════════

@_auth_cb
async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _chat_allowed(update):
        return
    query = update.callback_query
    await query.answer()
    context.user_data.pop("raid", None)
    context.user_data.pop("raid_id", None)
    context.user_data.pop("raid_action_pending", None)
    uid = update.effective_user.id
    chars = await storage.load_characters(uid)
    if chars:
        await query.edit_message_text(f"С возвращением, {chars[0].name}!", reply_markup=main_menu())
    else:
        await query.edit_message_text(
            "🎲 Добро пожаловать в RPG!\n"
            "Здесь нет персонажей — создайте первого:\n\n"
            "/create — создать персонажа\n"
            "/help — список команд",
        )


async def _reward_all_participants(session: RaidSession, location, context) -> list[tuple[str, str]]:
    """Give loot/exp/gold to all raid participants. Returns [(char_name, loot_text), ...]."""
    results = []
    for uid_str, char_name in list(session.participant_names.items()):
        uid = int(uid_str)
        chars = await storage.load_characters(uid)
        if not chars:
            continue
        char = next((c for c in chars if c.name == char_name), None)
        if not char:
            continue
        loot = generate_loot(location, len(session.encounters), char.level, [char.class_key])
        distribute_exp_gold(session, location, [char])
        char.inventory.extend(loot)
        char.in_raid = False
        char.mark_raid_done()
        char.durability_damage_all(percent=DURABILITY_LOSS_PERCENT / 100.0)
        char.release_companion()
        char.hp = char.max_hp
        await _save_char(char)
        loot_text = ""
        if loot:
            loot_text = " 🎁 " + ", ".join(f"{it.name} ({it.rarity.value})" for it in loot)
        results.append((char.name, loot_text))
    return results


def _cleanup_raid(context):
    """Clear raid state from context."""
    context.user_data.pop("raid", None)
    context.user_data.pop("raid_id", None)
    context.user_data.pop("raid_action_pending", None)
    context.user_data.pop("raid_msg_chat", None)
    context.user_data.pop("raid_msg_id", None)
    context.user_data.pop("raid_msg_thread", None)


async def _get_session(context) -> Optional[RaidSession]:
    """Load raid session from user_data or DB."""
    session = context.user_data.get("raid")
    if session is not None:
        return session
    raid_id = context.user_data.get("raid_id")
    if raid_id:
        data = await storage.load_raid_session(raid_id)
        if data:
            return session_from_dict(data)
    return None


async def _save_session(context, session: RaidSession):
    """Save raid session to user_data or DB."""
    raid_id = context.user_data.get("raid_id")
    if raid_id:
        await storage.save_raid_session(raid_id, session_to_dict(session))
    else:
        context.user_data["raid"] = session


async def _notify_participants(context, session: RaidSession, text: str, kb=None):
    """Notify all participants of a raid — send to raid chat (or PM fallback)."""
    chat_id = context.user_data.get("raid_msg_chat")
    thread_id = context.user_data.get("raid_msg_thread")
    if chat_id:
        try:
            kwargs = {}
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            if kb:
                await context.bot.send_message(chat_id, text, reply_markup=kb, **kwargs)
            else:
                await context.bot.send_message(chat_id, text, **kwargs)
        except Exception:
            pass
        return
    for uid_str in session.participant_names:
        uid = int(uid_str)
        try:
            if kb:
                await context.bot.send_message(uid, text, reply_markup=kb)
            else:
                await context.bot.send_message(uid, text)
        except Exception:
            pass


async def _show_encounter(query, context, char: Character, session: RaidSession):
    enc = session.encounters[session.current_encounter]
    context.user_data["raid_msg_chat"] = query.message.chat_id
    context.user_data["raid_msg_id"] = query.message.message_id
    context.user_data["raid_msg_thread"] = query.message.message_thread_id
    total = len(session.encounters)
    cur = session.current_encounter + 1
    uid = query.from_user.id if query.from_user else 0
    await query.edit_message_text(_encounter_header(cur, total, enc, char, uid), reply_markup=raid_actions())


@_auth_cb
async def cb_raid_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    session = await _get_session(context)
    if not session or session.status != RaidStatus.IN_PROGRESS:
        await query.edit_message_text("Нет активного рейда.", reply_markup=main_menu())
        return
    enc = session.encounters[session.current_encounter]
    turn = get_current_turn(enc)
    if turn and turn["type"] == "player" and turn["uid"] != uid:
        await query.edit_message_text("Сейчас не ваш ход. Ожидайте.", reply_markup=raid_actions())
        return
    char = await _get_char(uid, context)
    if not char:
        await query.edit_message_text("Персонаж не найден.", reply_markup=main_menu())
        return
    context.user_data["raid_msg_chat"] = query.message.chat_id
    context.user_data["raid_msg_id"] = query.message.message_id
    context.user_data["raid_msg_thread"] = query.message.message_thread_id
    context.user_data["raid_action_pending"] = True
    await _save_session(context, session)
    await query.edit_message_text(
        query.message.text + "\n\n✏️ Напишите, что делает ваш персонаж:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="raid_cancel_action"),
        ]]),
    )


@_auth_cb
async def cb_raid_cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("raid_action_pending", None)
    session = await _get_session(context)
    if session:
        await _show_encounter(query, context, await _get_char(update.effective_user.id, context), session)
    else:
        await query.edit_message_text("Главное меню:", reply_markup=main_menu())


# ─── Multiplayer turn system ─────────────────────────────────


async def _handle_player_turn(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    session: RaidSession, uid: int, text: str,
):
    """Process one player's turn action and advance."""
    char = await _get_char(uid, context)
    if not char:
        await update.message.reply_text("Персонаж не найден.")
        return
    enc = session.encounters[session.current_encounter]
    enc.turn += 1
    enemy = create_enemy(enc)

    if session.active_buffs:
        char.set_buffs(
            atk=session.active_buffs.get("atk", 0),
            def_=session.active_buffs.get("def", 0),
        )

    weapon_name = ""
    weapon_attrs = []
    if char.equipment.weapon:
        weapon_name = char.equipment.weapon.name
        weapon_attrs = char.equipment.weapon.attributes

    s = char.stats
    player_info = {
        "name": char.name, "class": char.class_key,
        "hp": char.hp, "max_hp": char.max_hp,
        "weapon": f"{weapon_name} ({', '.join(weapon_attrs)})" if weapon_name else "",
        "strength": s.strength, "agility": s.agility, "intelligence": s.intelligence,
    }

    async def _nn(mode, **kw):
        return await call_narrative_api(
            location=session.location_key, turn=enc.turn,
            player=player_info,
            enemies=[{"name": enc.enemy_template["name"], "hp": enemy.hp, "max_hp": enc.enemy_max_hp}],
            action_history=[], player_action=text, mode=mode, **kw,
        )

    attribute = "strength"
    try:
        nn = await _nn("player_modifiers")
        player_mods = nn.get("actions") if nn else None
        if nn and "attribute" in nn:
            attribute = nn["attribute"]
    except Exception:
        log.exception("NN player_modifiers failed")
        player_mods = None

    try:
        player_attack = resolve_player_turn(char, enemy, enc, player_mods, attribute=attribute)
    except Exception as e:
        log.exception("resolve_player_turn failed")
        await update.message.reply_text(f"❌ Ошибка боя: {e}")
        return

    try:
        nn = await _nn("player_narrative", damage=player_attack.final_damage if player_attack else 0)
        player_narrative = nn.get("player_narrative", "") if nn else "Вы атакуете."
    except Exception:
        log.exception("NN player_narrative failed")
        player_narrative = "Вы атакуете."

    if session.active_buffs:
        char.clear_buffs()

    await _save_char(char)

    total = len(session.encounters)
    cur = session.current_encounter + 1
    header = _encounter_header(cur, total, enc, char, uid)
    lines = [header, ""]
    if player_narrative:
        lines.append(f"📖 {player_narrative}")
    desc = _attack_desc("Вы", player_attack)
    if desc:
        lines.append(desc)

    if char.hp <= 0:
        lines.append("\n💀 **Вы погибли от ран!**")
        char.count_raid += 1
        char.in_raid = False
        char.durability_damage_all(percent=DEATH_DURABILITY_LOSS)
        char.release_companion()
        char.alive = True
        char.hp = char.max_hp
        await _save_char(char)
        if len(session.participant_names) <= 1:
            session.status = RaidStatus.FAILED
            _cleanup_raid(context)
            await update.message.reply_text("\n".join(lines), reply_markup=raid_failed())
        else:
            await update.message.reply_text("\n".join(lines))
            advance_turn_core(enc)
            await _advance_turn(context, session)
        return

    if enemy.hp <= 0:
        enc.finished = True
        lines.append(f"\n✅ {enc.enemy_template['name']} повержен!")
        full = "\n".join(lines)
        await update.message.reply_text(full)
        await _encounter_ended(context, session, enc, query=None, msg_text=full)
        return

    session.turn_pending_uid = None
    await _save_session(context, session)
    await update.message.reply_text("\n".join(lines))

    advance_turn_core(enc)
    await _advance_turn(context, session)


async def _get_participant_chars(context, session: RaidSession) -> dict[int, Character]:
    """Load all participant characters for a raid session."""
    chars = {}
    for uid_str in session.participant_names:
        uid = int(uid_str)
        c = await _get_char(uid, context)
        if c:
            chars[uid] = c
    return chars


async def _resolve_auto_turn(
    context, session: RaidSession, enc: RaidEncounter,
    entry: dict, enemy_obj,
) -> Optional[str]:
    """Auto-resolve companion or enemy turn. Returns description text or None."""
    if entry["type"] == "companion":
        owner_uid = entry["uid"]
        owner_char = await _get_char(owner_uid, context)
        if not owner_char or not owner_char.companion or not owner_char.companion.alive:
            return None
        atk = resolve_companion_turn(owner_char, enemy_obj, enc)
        await _save_char(owner_char)
        if enc.enemy_hp <= 0:
            enc.finished = True
        return _attack_desc(f"🛡 Страж {owner_char.name}", atk)

    elif entry["type"] == "enemy":
        chars = await _get_participant_chars(context, session)
        alive = [(uid, c) for uid, c in chars.items() if c.alive and c.hp > 0 and c.in_raid]
        if not alive:
            return None
        target_uid, target_char = _random.choice(alive)

        weapon_name = ""
        weapon_attrs = []
        if target_char.equipment and target_char.equipment.weapon:
            weapon_name = target_char.equipment.weapon.name
            weapon_attrs = target_char.equipment.weapon.attributes
        s = target_char.stats
        player_info = {
            "name": target_char.name, "class": target_char.class_key,
            "hp": target_char.hp, "max_hp": target_char.max_hp,
            "weapon": f"{weapon_name} ({', '.join(weapon_attrs)})" if weapon_name else "",
            "strength": s.strength, "agility": s.agility, "intelligence": s.intelligence,
        }
        enemy_info = [{"name": enc.enemy_template["name"], "hp": enc.enemy_hp, "max_hp": enc.enemy_max_hp}]

        enemy_mods = None
        try:
            nn = await call_narrative_api(
                location=session.location_key, turn=enc.turn,
                player=player_info, enemies=enemy_info,
                action_history=[], player_action="", mode="enemy_modifiers",
            )
            if nn:
                enemy_mods = nn.get("enemy_actions")
        except Exception:
            log.exception("NN enemy_modifiers failed")

        atk = resolve_enemy_turn(target_char, enemy_obj, enc, enemy_mods)
        died = target_char.hp <= 0
        if died:
            target_char.count_raid += 1
            target_char.in_raid = False
            target_char.durability_damage_all(percent=DEATH_DURABILITY_LOSS)
            target_char.release_companion()
            target_char.alive = True
            target_char.hp = target_char.max_hp
        await _save_char(target_char)

        enemy_narrative = ""
        try:
            nn = await call_narrative_api(
                location=session.location_key, turn=enc.turn,
                player=player_info, enemies=enemy_info,
                action_history=[], player_action="",
                damage=atk.final_damage if atk else 0,
                mode="enemy_narrative",
            )
            if nn:
                enemy_narrative = nn.get("enemy_narrative", "")
        except Exception:
            log.exception("NN enemy_narrative failed")

        parts = []
        if enemy_narrative:
            parts.append(f"📖 {enemy_narrative}")
        desc = _attack_desc(f"👾 {entry['name']}", atk, "наносит")
        if desc:
            parts.append(desc)
        if not parts:
            return None
        death = " 💀" if died else ""
        return "\n".join(parts) + death
    return None


async def _advance_turn(context, session: RaidSession):
    """Advance initiative and process auto-turns until a player's turn or round end."""
    enc = session.encounters[session.current_encounter]
    enemy_obj = create_enemy(enc)
    parts = []

    while not enc.finished:
        turn = get_current_turn(enc)
        if not turn:
            break

        if turn["type"] == "player":
            session.turn_pending_uid = turn["uid"]
            enc.turn_timeout_deadline = time.time() + TURN_TIMEOUT
            await _save_session(context, session)
            chat_id = context.user_data.get("raid_msg_chat") or turn["uid"]
            thread_id = context.user_data.get("raid_msg_thread")
            notif = _build_turn_notification(session, enc, turn)
            kwargs = {"reply_markup": raid_actions()}
            if thread_id:
                kwargs["message_thread_id"] = thread_id
            try:
                await context.bot.send_message(chat_id, notif, **kwargs)
            except Exception:
                pass
            break

        text = await _resolve_auto_turn(context, session, enc, turn, enemy_obj)
        if text:
            parts.append(text)

        if enc.finished:
            break

        advance_turn_core(enc)

    if parts:
        full = "\n".join(parts)
        await _notify_participants(context, session, full, raid_actions())

    if enc.finished:
        await _encounter_ended(context, session, enc, query=None)
    else:
        await _save_session(context, session)


def _build_turn_notification(session: RaidSession, enc: RaidEncounter, turn: dict) -> str:
    """Build the 'your turn' notification for a player."""
    name = turn.get("name", "Игрок")
    mob_name = enc.enemy_template["name"]
    hp_perc = int(enc.enemy_hp / max(enc.enemy_max_hp, 1) * 100)
    bar = "▓" * (hp_perc // 10) + "░" * (10 - hp_perc // 10)
    remaining = max(0, int(enc.turn_timeout_deadline - time.time())) if enc.turn_timeout_deadline else TURN_TIMEOUT
    return (
        f"⚔️ **Раунд {enc.round_number + 1} — ваш ход, {name}!**\n\n"
        f"👾 {mob_name} ❤️ {enc.enemy_hp}/{enc.enemy_max_hp}\n{bar}\n\n"
        f"⏰ {remaining} сек на ход\n"
        f"✏️ Напишите, что делает ваш персонаж."
    )


async def _encounter_ended(
    context, session: RaidSession, enc: RaidEncounter,
    query=None, msg_text: str = "",
):
    """Handle encounter completion (enemy killed). Single player death does NOT fail raid."""
    await _save_session(context, session)
    enemy_dead = enc.enemy_hp <= 0

    if enemy_dead:
        session.current_encounter += 1
        if session.current_encounter >= len(session.encounters):
            session.status = RaidStatus.COMPLETED
            loc = get_location(session.location_key)
            if loc:
                rewards = await _reward_all_participants(session, loc, context)
                loot_lines = ["🏆 **Рейд пройден!**"]
                for rname, rtext in rewards:
                    loot_lines.append(f"• {rname}: {rtext}")
                text = "\n".join(loot_lines)
                if query:
                    await _notify_participants(context, session, text, raid_done())
                    _cleanup_raid(context)
                    await query.edit_message_text(msg_text + f"\n\n{text}", reply_markup=raid_done())
                else:
                    await _notify_participants(context, session, text, raid_done())
                    _cleanup_raid(context)
            else:
                err = "⚠️ Ошибка: локация не найдена."
                if query:
                    await query.edit_message_text(err, reply_markup=main_menu())
        else:
            if query:
                await query.edit_message_text(msg_text, reply_markup=raid_next())
            else:
                await _notify_participants(context, session, "✅ Враг повержен!", raid_next())
        return

    await _save_session(context, session)


# ═══════════════════════════════════════════════════════════════
# Callback: рейд — следующий враг
# ═══════════════════════════════════════════════════════════════

@_auth_cb
async def cb_raid_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    session = await _get_session(context)
    if not session:
        await query.edit_message_text("Рейд не найден.")
        return
    char = await _get_char(uid, context)
    if not char:
        await query.edit_message_text("Персонаж не найден.")
        return
    session.current_encounter += 1
    event_fired = False

    loc = get_location(session.location_key)
    event_text = ""
    if loc and loc.events and roll_chance(0.30):
        available = [e for e in loc.events if e["id"] not in session.used_event_ids]
        if not available:
            available = loc.events
        raw = _random.choice(available)
        ev = RaidEvent(**raw)
        session.used_event_ids.add(ev.id)
        success, reward = resolve_event(ev, char)

        lines = [f"📜 {ev.text}"]
        if success:
            lines.append("✅ **Успех!**")
        elif ev.fail:
            lines.append("❌ **Провал!**")

        parts = []
        if reward.gold:
            char.gold += reward.gold
            parts.append(f"{reward.gold}💰")
        if reward.heal:
            old = char.hp
            char.hp = min(char.max_hp, char.hp + reward.heal)
            parts.append(f"+{char.hp - old}❤️")
        if reward.damage:
            char.hp = max(0, char.hp - reward.damage)
            parts.append(f"-{reward.damage}❤️")
        if reward.item_template:
            tpl = get_template(reward.item_template)
            if tpl:
                new_item = InventoryItem(
                    template=tpl, uid=f"ev_{uuid.uuid4().hex[:8]}",
                    durability=tpl.durability_max, durability_max=tpl.durability_max,
                )
                char.inventory.append(new_item)
                parts.append(f"🎒 {new_item.name}")
        if reward.buff_atk or reward.buff_def:
            session.active_buffs["atk"] = session.active_buffs.get("atk", 0) + reward.buff_atk
            session.active_buffs["def"] = session.active_buffs.get("def", 0) + reward.buff_def
            b = []
            if reward.buff_atk:
                b.append(f"+{reward.buff_atk}⚔️")
            if reward.buff_def:
                b.append(f"+{reward.buff_def}🛡")
            parts.append(f" ({', '.join(b)} до конца рейда)")
        if parts:
            lines.append("  " + " | ".join(parts))

        event_text = "\n".join(lines)
        event_fired = True
        await _save_char(char)

    if char.hp <= 0:
        if event_fired:
            session.current_encounter -= 1
        await _save_session(context, session)
        char.count_raid += 1
        session.status = RaidStatus.FAILED
        char.in_raid = False
        char.durability_damage_all(percent=DEATH_DURABILITY_LOSS)
        char.release_companion()
        char.alive = True
        char.hp = char.max_hp
        await _save_char(char)
        _cleanup_raid(context)
        text = "💀 **Вы погибли от ран!**"
        if event_text:
            text = f"{event_text}\n\n{text}"
        await query.edit_message_text(text, reply_markup=raid_failed())
        return

    if session.current_encounter >= len(session.encounters):
        if event_fired:
            session.current_encounter -= 1
        await _save_session(context, session)
        if loc:
            session.status = RaidStatus.COMPLETED
            rewards = await _reward_all_participants(session, loc, context)
            loot_lines = ["🏆 **Рейд пройден!**"]
            for rname, rtext in rewards:
                loot_lines.append(f"• {rname}: {rtext}")
            text = "\n".join(loot_lines)
            if event_text:
                text = f"{event_text}\n\n{text}"
            await _notify_participants(context, session, text, raid_done())
            _cleanup_raid(context)
            await query.edit_message_text(text, reply_markup=raid_done())
        else:
            await query.edit_message_text("⚠️ Ошибка: локация не найдена.", reply_markup=main_menu())
        return

    if event_fired:
        session.current_encounter -= 1
    await _save_session(context, session)

    if event_text:
        await query.edit_message_text(event_text, reply_markup=raid_next())
        return

    # Build initiative order for the new encounter
    chars = await _get_participant_chars(context, session)
    if not chars:
        chars = {uid: char}
    enc = session.encounters[session.current_encounter]
    enc.initiative_order = build_initiative_order(chars, enc)
    enc.current_turn_index = 0
    enc.round_number = 0
    await _save_session(context, session)

    loc_name = get_location(session.location_key).name if get_location(session.location_key) else "?"
    mob_name = enc.enemy_template["name"]
    await query.edit_message_text(
        f"⚔️ **{loc_name}** — новый противник!\n\n"
        f"👾 **{mob_name}** ❤️ {enc.enemy_hp}/{enc.enemy_max_hp}\n\n"
        f"Бросаем инициативу…"
    )
    await _advance_turn(context, session)

# ═══════════════════════════════════════════════════════════════
# /forfeit — сдаться в рейде
# ═══════════════════════════════════════════════════════════════

async def cmd_forfeit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сдаться и покинуть текущий рейд (фейл рейда для соло)."""
    session = await _get_session(context)
    if not session or session.status != RaidStatus.IN_PROGRESS:
        char = await _get_char(update.effective_user.id, context)
        if char and char.in_raid:
            char.in_raid = False
            char.release_companion()
            await _save_char(char)
        await _reply(update, "Нет активного рейда. /location чтобы начать.")
        return
    char = await _get_char(update.effective_user.id, context)
    if char:
        char.in_raid = False
        char.release_companion()
        char.mark_raid_done()
        char.hp = char.max_hp
        await _save_char(char)
    if len(session.participant_names) <= 1:
        session.status = RaidStatus.FAILED
        await _save_session(context, session)
        _cleanup_raid(context)
        await _reply(update, "🏳️ Вы сдались! Рейд провален.", reply_markup=main_menu())
    else:
        session.status = RaidStatus.FAILED
        await _save_session(context, session)
        _cleanup_raid(context)
        await _notify_participants(context, session, "🏳️ Один из участников сдался. Рейд завершён.", main_menu())
        await _reply(update, "🏳️ Вы сдались!", reply_markup=main_menu())

# ═══════════════════════════════════════════════════════════════
# Callback: рейд — сбежать
# ═══════════════════════════════════════════════════════════════

@_auth_cb
async def cb_raid_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    session = await _get_session(context)
    if not session:
        await query.edit_message_text("Рейд не найден.", reply_markup=main_menu())
        return
    char = await _get_char(uid, context)
    if char:
        char.in_raid = False
        char.release_companion()
        char.mark_raid_done()
        char.hp = char.max_hp
        await _save_char(char)
    session.status = RaidStatus.FAILED
    await _save_session(context, session)
    _cleanup_raid(context)
    await query.edit_message_text("🏃 Вы сбежали из рейда!", reply_markup=main_menu())

# ═══════════════════════════════════════════════════════════════
# Callback: инвентарь — страницы + предмет
# ═══════════════════════════════════════════════════════════════

@_auth_cb
async def cb_inv_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    char = await _get_char(update.effective_user.id, context)
    if not char:
        return
    page = int(query.data[len("inv_page_"):])
    await _show_inventory(update, context, char, page)


@_auth_cb
async def cb_inv_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    item_uid = query.data[len("inv_item_"):]
    char = await _get_char(uid, context)
    if not char:
        return
    item = next((i for i in char.inventory if i.uid == item_uid), None)
    if not item:
        await query.edit_message_text("Предмет не найден.")
        return
    text = (
        f"📦 **{item.name}**\n"
        f"Редкость: {item.rarity.value}\n"
        f"Тип: {item.item_type.value}\n"
        f"Прочность: {item.durability}/{item.durability_max}\n"
    )
    eff = item.effect
    parts = []
    if eff.strength_bonus: parts.append(f"💪 Сила +{eff.strength_bonus}")
    if eff.agility_bonus: parts.append(f"🏃 Ловкость +{eff.agility_bonus}")
    if eff.intelligence_bonus: parts.append(f"🧠 Интеллект +{eff.intelligence_bonus}")
    if eff.defense_bonus: parts.append(f"🛡 Защита +{eff.defense_bonus}")
    if eff.hp_bonus: parts.append(f"❤️ HP +{eff.hp_bonus}")
    if eff.crit_chance_bonus: parts.append(f"🎯 Крит +{eff.crit_chance_bonus*100:.0f}%")
    if eff.crit_multiplier_bonus: parts.append(f"🗡 Крит.множитель +{eff.crit_multiplier_bonus:.1f}")
    if eff.dodge_bonus: parts.append(f"💨 Уклон +{eff.dodge_bonus*100:.0f}%")
    if item.attributes:
        from core.weapon_gen import get_attributes_descriptions
        descs = get_attributes_descriptions({"attributes": item.attributes})
        parts.append(f"🔰 {', '.join(descs)}")
    if parts:
        text += "\n" + "\n".join(parts)
    await query.edit_message_text(text, reply_markup=item_actions_kb(item_uid))


@_auth_cb
async def cb_inv_equip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    item_uid = query.data[len("inv_equip_"):]
    char = await _get_char(uid, context)
    if not char:
        return
    item = next((i for i in char.inventory if i.uid == item_uid), None)
    if not item:
        await query.edit_message_text("Предмет не найден.")
        return
    ok = char.equip(item)
    if not ok:
        await query.edit_message_text("Нельзя надеть этот предмет (уровень/класс/слот).")
        return
    await _save_char(char)
    await query.edit_message_text(f"✅ {item.name} экипирован!", reply_markup=main_menu())


@_auth_cb
async def cb_inv_unequip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    item_uid = query.data[len("inv_unequip_"):]
    char = await _get_char(uid, context)
    if not char:
        return
    item = None
    for slot in ("weapon", "armor", "accessory"):
        eq = getattr(char.equipment, slot, None)
        if eq and eq.uid == item_uid:
            item = eq
            break
    if not item:
        await query.edit_message_text("Предмет не экипирован.")
        return
    ok = char.unequip(item)
    if not ok:
        await query.edit_message_text("Не удалось снять предмет.")
        return
    await _save_char(char)
    await query.edit_message_text(f"📦 {item.name} снят!", reply_markup=main_menu())


@_auth_cb
async def cb_inv_drop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    item_uid = query.data[len("inv_drop_"):]
    char = await _get_char(uid, context)
    if not char:
        return
    item = next((i for i in char.inventory if i.uid == item_uid), None)
    if not item:
        await query.edit_message_text("Предмет не найден.")
        return
    char.inventory.remove(item)
    await _save_char(char)
    await query.edit_message_text(f"🗑 {item.name} выброшен.", reply_markup=main_menu())


@_auth_cb
async def cb_inv_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    item_uid = query.data[len("inv_sell_"):]
    char = await _get_char(uid, context)
    if not char:
        return
    item = next((i for i in char.inventory if i.uid == item_uid), None)
    if not item:
        await query.edit_message_text("Предмет не найден.")
        return
    context.user_data["sell"] = {"step": "price", "item_uid": item_uid, "char": char}
    await query.edit_message_text(f"💰 Введите цену для {item.name}:")

# ═══════════════════════════════════════════════════════════════
# Callback: рынок — страницы + покупка
# ═══════════════════════════════════════════════════════════════

@_auth_cb
async def cb_market_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data[len("market_page_"):])
    listings = MARKET.get_active_listings()
    await _show_market(update, context, listings, page)


@_auth_cb
async def cb_market_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    lid = query.data[len("market_buy_"):]
    listing = MARKET.listings.get(lid)
    if not listing or not listing.active:
        await query.edit_message_text("Объявление уже неактивно.", reply_markup=main_menu())
        return
    text = (
        f"🏪 **{listing.item.name}** — {listing.price}💰\n"
        f"Продавец: {listing.character_name}\n\n"
        f"Подтвердите покупку:"
    )
    await query.edit_message_text(text, reply_markup=market_confirm(lid))


@_auth_cb
async def cb_market_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    lid = query.data[len("market_confirm_"):]
    char = await _get_char(uid, context)
    if not char:
        await query.edit_message_text("Нет персонажа.")
        return

    listing = MARKET.listings.get(lid)
    if not listing:
        await query.edit_message_text("Объявление не найдено.", reply_markup=main_menu())
        return

    success, item, seller_id, seller_name, seller_earns = MARKET.buy_listing(lid, char)
    if not success:
        await query.edit_message_text(
            "Не удалось купить: недостаточно золота или объявление неактивно.",
            reply_markup=main_menu(),
        )
        return

    char.inventory.append(item)
    await _save_char(char)
    await MARKET.persist_deactivate(storage, lid)
    await storage.credit_gold(seller_id, seller_name, seller_earns)

    await query.edit_message_text(f"✅ Куплен {item.name} за {listing.price}💰", reply_markup=main_menu())


@_auth_cb
async def cb_market_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    lid = query.data[len("market_cancel_"):]
    ok = MARKET.cancel_listing(lid, uid)
    if ok:
        await MARKET.persist_deactivate(storage, lid)
        await query.edit_message_text("Объявление отменено.", reply_markup=main_menu())
    else:
        await query.edit_message_text("Не удалось отменить.", reply_markup=main_menu())

# ═══════════════════════════════════════════════════════════════
# Callback: управление персонажами
# ═══════════════════════════════════════════════════════════════

@_auth_cb
async def cb_char_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await cmd_create(update, context)


@_auth_cb
async def cb_char_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    name = query.data[len("char_switch_"):]
    chars = await storage.load_characters(uid)
    target = next((c for c in chars if c.name == name), None)
    if not target:
        await query.edit_message_text("Персонаж не найден.")
        return
    context.user_data["active_char"] = target.name
    await query.edit_message_text(f"✅ Переключено на **{target.name}**", reply_markup=main_menu())


@_auth_cb
async def cb_char_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data[len("char_del_"):]
    chars = await storage.load_characters(update.effective_user.id)
    target = next((c for c in chars if c.name == name), None)
    if not target:
        await query.edit_message_text("Персонаж не найден.")
        return
    await query.edit_message_text(
        f"🗑 Удалить **{target.name}** ({CLASS_NAMES_RU.get(target.class_key, target.class_key)})?\n\n"
        "Все предметы, золото и прогресс исчезнут безвозвратно.",
        reply_markup=char_delete_confirm(name),
    )


@_auth_cb
async def cb_char_delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    name = query.data[len("char_del_yes_"):]
    uid = update.effective_user.id
    chars = await storage.load_characters(uid)
    if len(chars) <= 1:
        await query.edit_message_text("Нельзя удалить единственного персонажа. Сначала создайте нового.")
        return
    await storage.delete_character(uid, name)
    if context.user_data.get("active_char") == name:
        context.user_data.pop("active_char", None)
    await query.edit_message_text(f"🗑 Персонаж **{name}** удалён.", reply_markup=main_menu())


# ═══════════════════════════════════════════════════════════════
# Админ-команды (требуют пароль)
# ═══════════════════════════════════════════════════════════════

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await _reply(update, "Использование: /admin <пароль>")
        return
    if args[0] != ADMIN_PASSWORD:
        await _reply(update, "Неверный пароль.")
        return
    context.user_data["admin"] = True
    await _load_settings()
    await _reply(update, "🔧 Режим администратора активирован.\n"
                         "/debug — меню отладки\n"
                         "/set_level N — установить уровень\n"
                         "/add_gold N — добавить золото\n"
                         "/add_exp N — добавить опыт\n"
                         "/reset_cooldown — сбросить таймер рейда\n"
                         "/set_here — разрешить бота в этом чате/топике",
                parse_mode=None)


async def _resolve_target_char(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> Optional[Character]:
    uid = update.effective_user.id
    char_name = None
    if args:
        for a in args:
            if a.startswith("@"):
                char_name = a.lstrip("@")
                break
    if char_name:
        result = await storage.find_character_global(char_name)
        if result is None:
            await _reply(update, f"Персонаж @{char_name} не найден.")
            return None
        return result[1]
    return await _get_char(uid, context)


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("admin"):
        await _reply(update, "Доступ запрещён.")
        return
    args = context.args
    char = await _resolve_target_char(update, context, args)
    if not char:
        return
    await _reply(update,
        f"🔧 Отладка — {char.name}\n"
        f"Уровень: {char.level}\n"
        f"Опыт: {char.experience}/{char.exp_to_next}\n"
        f"HP: {char.hp}/{char.max_hp}\n"
        f"💰 {char.gold}\n"
        f"in_raid: {char.in_raid}\n"
        f"alive: {char.alive}\n"
        f"last_raid: {char.last_raid_time}",
    )


async def cmd_set_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("admin"):
        await _reply(update, "Доступ запрещён.")
        return
    args = context.args
    if not args:
        await _reply(update, "/set_level <число> (@тег)")
        return
    try:
        lvl_str = args[0] if not args[0].startswith("@") else args[1] if len(args) > 1 else ""
        lvl = int(lvl_str)
    except (ValueError, IndexError):
        await _reply(update, "/set_level <число> (@тег)")
        return
    char = await _resolve_target_char(update, context, args)
    if not char:
        return
    char.level = max(1, min(lvl, 50))
    char.max_hp = char._calc_max_hp()
    char.hp = char.max_hp
    await _save_char(char)
    await _reply(update, f"✅ {char.name}: уровень {char.level}, HP восстановлено.")


async def cmd_add_gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("admin"):
        await _reply(update, "Доступ запрещён.")
        return
    args = context.args
    if not args:
        await _reply(update, "/add_gold <число> (@тег)")
        return
    try:
        amt_str = args[0] if not args[0].startswith("@") else args[1] if len(args) > 1 else ""
        amount = int(amt_str)
    except (ValueError, IndexError):
        await _reply(update, "/add_gold <число> (@тег)")
        return
    char = await _resolve_target_char(update, context, args)
    if not char:
        return
    char.gold += amount
    await _save_char(char)
    await _reply(update, f"✅ {char.name}: +{amount}💰, теперь {char.gold}💰")


async def cmd_add_exp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("admin"):
        await _reply(update, "Доступ запрещён.")
        return
    args = context.args
    if not args:
        await _reply(update, "/add_exp <число> (@тег)")
        return
    try:
        amt_str = args[0] if not args[0].startswith("@") else args[1] if len(args) > 1 else ""
        amount = int(amt_str)
    except (ValueError, IndexError):
        await _reply(update, "/add_exp <число> (@тег)")
        return
    char = await _resolve_target_char(update, context, args)
    if not char:
        return
    char.add_experience(amount)
    await _save_char(char)
    await _reply(update, f"✅ {char.name}: +{amount} XP, уровень {char.level}")


async def cmd_reset_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("admin"):
        await _reply(update, "Доступ запрещён.")
        return
    args = context.args
    char = await _resolve_target_char(update, context, args)
    if not char:
        return
    char.last_raid_time = 0.0
    await _save_char(char)
    await _reply(update, f"✅ {char.name}: кулдаун рейда сброшен.")


async def cmd_set_here(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("admin"):
        await _reply(update, "Доступ запрещён. Сначала /admin <пароль> в ЛС.")
        return
    chat = update.effective_chat
    if chat is None or chat.type == "private":
        await _reply(update, "Эта команда работает только в групповом чате.")
        return
    chat_id = chat.id
    msg = update.effective_message
    topic_id = msg.message_thread_id if (msg and msg.is_topic_message) else None
    await _save_setting("allowed_chat_id", str(chat_id))
    if topic_id:
        await _save_setting("allowed_topic_id", str(topic_id))
        await _reply(update, f"✅ Бот будет отвечать только в этом топике (чат {chat_id}, топик {topic_id}).")
    else:
        await _save_setting("allowed_topic_id", "")
        await _reply(update, f"✅ Бот будет отвечать во всём чате {chat_id}.")

# ═══════════════════════════════════════════════════════════════
# cb_stat_alloc — распределение очков характеристик
# ═══════════════════════════════════════════════════════════════

@_auth_cb
async def cb_stat_alloc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    attr_map = {"stat_str": "strength", "stat_agi": "agility", "stat_int": "intelligence"}
    attr = attr_map.get(query.data)
    if not attr:
        return
    char = await _get_char(uid, context)
    if not char:
        await query.edit_message_text("Нет персонажа.")
        return
    if not char.allocate_stat(attr):
        await query.edit_message_text("Нет очков для распределения.")
        return
    await _save_char(char)
    await cmd_profile(update, context)

# ═══════════════════════════════════════════════════════════════
# Missing callbacks: profile / inventory / location / market / char list / class select / location select / raid / lobby
# ═══════════════════════════════════════════════════════════════

@_auth_cb
async def cb_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await cmd_profile(update, context)

@_auth_cb
async def cb_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await cmd_inventory(update, context)

@_auth_cb
async def cb_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await cmd_location(update, context)

@_auth_cb
async def cb_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await cmd_market(update, context)

@_auth_cb
async def cb_market_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    listings = MARKET.get_active_listings()
    if not listings:
        await query.edit_message_text("🏪 Рынок пуст.", reply_markup=main_menu())
        return
    await _show_market(update, context, listings, 0)

@_auth_cb
async def cb_char_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await cmd_characters(update, context)

@_auth_cb
async def cb_class_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    state = context.user_data.get("creation")
    if not state or state["step"] != "class":
        await query.edit_message_text("Создание не активно. /create")
        return
    class_key = query.data[len("class_"):]
    if class_key not in CLASSES:
        await query.edit_message_text("Неверный класс.")
        return
    state["class_key"] = class_key
    if class_key == "leader":
        state["step"] = "companion_name"
        await query.edit_message_text("Введите имя стража (или /skip):")
    else:
        await _finish_creation(update, context, state)

@_auth_cb
async def cb_location_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    loc_key = query.data[len("loc_"):]
    loc = get_location(loc_key)
    if not loc:
        await query.edit_message_text("Локация не найдена.")
        return
    char = await _get_char(update.effective_user.id, context)
    if not char:
        await query.edit_message_text("Нет персонажа.")
        return
    if not char.can_raid():
        rem = char.raid_cooldown_remaining()
        hrs = int(rem // 3600)
        mins = int((rem % 3600) // 60)
        await query.edit_message_text(f"⏳ Кулдаун рейда: {hrs}ч {mins}м")
        return
    text = (
        f"🗺 **{loc.name}**\n{loc.description}\n\n"
        f"🎯 Уровень: {loc.recommended_level} | ☠️ {loc.danger}/10\n"
        f"👾 Врагов: {loc.min_enemies}-{loc.max_enemies}\n"
        f"💰 {loc.gold_min}-{loc.gold_max} золота | ✨ {loc.exp_reward} опыта"
    )
    await query.edit_message_text(text, reply_markup=confirm_raid(loc_key))

@_auth_cb
async def cb_raid_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    loc_key = query.data[len("raid_start_"):]
    loc = get_location(loc_key)
    if not loc:
        await query.edit_message_text("Локация не найдена.")
        return
    char = await _get_char(uid, context)
    if not char:
        await query.edit_message_text("Нет персонажа.")
        return
    raid_id = f"solo_{uid}_{int(time.time())}"
    session = create_raid(char, loc, raid_id)
    chars = {uid: char}
    enc = session.encounters[0]
    enc.initiative_order = build_initiative_order(chars, enc)
    enc.current_turn_index = 0
    enc.round_number = 0
    session.status = RaidStatus.IN_PROGRESS
    session.participant_names = {uid: char.name}
    char.in_raid = True
    await _save_char(char)
    context.user_data["raid"] = session
    context.user_data["raid_id"] = raid_id
    context.user_data["raid_msg_chat"] = query.message.chat_id
    context.user_data["raid_msg_id"] = query.message.message_id
    context.user_data["raid_msg_thread"] = query.message.message_thread_id
    total = len(session.encounters)
    await query.edit_message_text(
        f"⚔️ **{loc.name}** — рейд начат!\n"
        f"Всего врагов: {total}\n\nБросаем инициативу…",
    )
    await _advance_turn(context, session)

@_auth_cb
async def cb_raid_online_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    loc_key = query.data[len("raid_online_"):]
    loc = get_location(loc_key)
    if not loc:
        await query.edit_message_text("Локация не найдена.")
        return
    char = await _get_char(uid, context)
    if not char:
        await query.edit_message_text("Нет персонажа.")
        return
    if not char.can_raid():
        await query.edit_message_text("Персонаж недоступен (возможно, ещё не прошёл кулдаун после рейда).")
        return
    code = token_hex(4).upper()
    _lobbies[code] = {
        "owner_uid": uid, "location_key": loc_key,
        "code": code, "participants": [(uid, char.name)],
        "started": False,
    }
    context.user_data["lobby_code"] = code
    text = raid_lobby_text(loc.name, code, [(uid, char.name)])
    await query.edit_message_text(text, reply_markup=raid_lobby(loc.name, code, [(uid, char.name)], True))

@_auth_cb
async def cb_raid_lobby_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    code = context.user_data.get("lobby_code")
    lobby = _lobbies.get(code) if code else None
    if not lobby:
        await query.edit_message_text("Лобби не найдено.")
        return
    if lobby["owner_uid"] != uid:
        await query.edit_message_text("Только создатель может начать рейд.")
        return
    if lobby["started"]:
        await query.edit_message_text("Рейд уже начат.")
        return
    lobby["started"] = True
    loc = get_location(lobby["location_key"])
    if not loc:
        await query.edit_message_text("Локация не найдена.")
        return
    raid_id = f"group_{uid}_{int(time.time())}"
    owner_char = await _get_char(lobby["owner_uid"], context)
    if not owner_char:
        await query.edit_message_text("Персонаж не найден.")
        return
    group_size = len(lobby["participants"])
    session = create_raid(owner_char, loc, raid_id, group_id=raid_id, group_size=group_size)
    chars = {}
    for puid, pname in lobby["participants"]:
        pchars = await storage.load_characters(puid)
        pc = next((c for c in pchars if c.name == pname), None)
        if pc:
            chars[puid] = pc
            pc.in_raid = True
            await _save_char(pc)
            session.participant_names[puid] = pname
    enc = session.encounters[0]
    enc.initiative_order = build_initiative_order(chars, enc)
    enc.current_turn_index = 0
    enc.round_number = 0
    session.status = RaidStatus.IN_PROGRESS
    context.user_data["raid_id"] = raid_id
    await _save_session(context, session)
    _lobbies.pop(code, None)
    context.user_data.pop("lobby_code", None)
    context.user_data["raid"] = session
    context.user_data["raid_msg_chat"] = query.message.chat_id
    context.user_data["raid_msg_id"] = query.message.message_id
    context.user_data["raid_msg_thread"] = query.message.message_thread_id
    await query.edit_message_text(f"⚔️ Рейд начат! Участников: {len(chars)}")
    await _advance_turn(context, session)

@_auth_cb
async def cb_raid_lobby_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    code = context.user_data.get("lobby_code")
    lobby = _lobbies.get(code) if code else None
    if not lobby:
        await query.edit_message_text("Нет лобби.")
        return
    lobby["participants"] = [(puid, pname) for puid, pname in lobby["participants"] if puid != uid]
    loc = get_location(lobby["location_key"])
    loc_name = loc.name if loc else "?"
    if not lobby["participants"]:
        _lobbies.pop(code, None)
        context.user_data.pop("lobby_code", None)
        await query.edit_message_text("Вы покинули лобби.", reply_markup=main_menu())
        return
    is_owner = lobby["owner_uid"] == uid
    if is_owner and lobby["participants"]:
        lobby["owner_uid"] = lobby["participants"][0][0]
    text = raid_lobby_text(loc_name, lobby["code"], lobby["participants"])
    is_owner = lobby["owner_uid"] == uid
    await query.edit_message_text(text, reply_markup=raid_lobby(loc_name, lobby["code"], lobby["participants"], is_owner))


@_auth_cb
async def cb_raid_lobby_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()


async def cmd_raid_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = context.args
    if not args:
        await _reply(update, "Использование: /raidjoin <код>")
        return
    code = args[0].upper()
    char = await _get_char(uid, context)
    if not char:
        await _reply(update, "Нет персонажа.")
        return
    if char.in_raid:
        await _reply(update, "Вы уже в рейде.")
        return
    if not char.can_raid():
        await _reply(update, "Персонаж недоступен (кулдаун после рейда).")
        return
    lobby = _lobbies.get(code)
    if not lobby or lobby["started"]:
        await _reply(update, "Лобби не найдено или рейд уже начат.")
        return
    if len(lobby["participants"]) >= MAX_GROUP_SIZE:
        await _reply(update, "Лобби заполнено.")
        return
    lobby["participants"].append((uid, char.name))
    context.user_data["lobby_code"] = code
    loc = get_location(lobby["location_key"])
    loc_name = loc.name if loc else "?"
    is_owner = lobby["owner_uid"] == uid
    await update.message.reply_text(
        f"✅ Вы присоединились к лобби **{loc_name}**!",
        reply_markup=raid_lobby(loc_name, lobby["code"], lobby["participants"], is_owner),
    )

# ═══════════════════════════════════════════════════════════════
# Регистрация всех хендлеров
# ═══════════════════════════════════════════════════════════════

def register_handlers(app: Application):
    app.add_handler(CommandHandler("menu", cmd_menu, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("start", cmd_menu, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("help", cmd_help, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("create", cmd_create, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("skip", cmd_skip, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("profile", cmd_profile, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("inventory", cmd_inventory, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("characters", cmd_characters, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("location", cmd_location, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("market", cmd_market, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("raid", cmd_raid, filters=ALLOWED_CHAT_FILTER))
    app.add_handler(CommandHandler("forfeit", cmd_forfeit, filters=ALLOWED_CHAT_FILTER))
    app.add_handler(CommandHandler("admin", cmd_admin, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("debug", cmd_debug, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("set_level", cmd_set_level, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("add_gold", cmd_add_gold, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("add_exp", cmd_add_exp, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("reset_cooldown", cmd_reset_cooldown, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CommandHandler("set_here", cmd_set_here))

    app.add_handler(CommandHandler("toprpg", cmd_toprpg, filters=ALLOWED_CHAT_FILTER))

    app.add_handler(CallbackQueryHandler(cb_main_menu, pattern=r"^main_menu$"))
    app.add_handler(CallbackQueryHandler(cb_profile, pattern=r"^profile$"))
    app.add_handler(CallbackQueryHandler(cb_inventory, pattern=r"^inventory$"))
    app.add_handler(CallbackQueryHandler(cb_location, pattern=r"^location$"))
    app.add_handler(CallbackQueryHandler(cb_market, pattern=r"^market$"))
    app.add_handler(CallbackQueryHandler(cb_market_refresh, pattern=r"^market_refresh$"))
    app.add_handler(CallbackQueryHandler(cb_char_list, pattern=r"^char_list$"))

    app.add_handler(CallbackQueryHandler(cb_class_select, pattern=r"^class_"))

    app.add_handler(CommandHandler("raidjoin", cmd_raid_join))
    app.add_handler(CallbackQueryHandler(cb_location_select, pattern=r"^loc_"))
    app.add_handler(CallbackQueryHandler(cb_raid_start, pattern=r"^raid_start_"))
    app.add_handler(CallbackQueryHandler(cb_raid_online_create, pattern=r"^raid_online_"))
    app.add_handler(CallbackQueryHandler(cb_raid_lobby_start, pattern=r"^raid_lobby_start$"))
    app.add_handler(CallbackQueryHandler(cb_raid_lobby_leave, pattern=r"^raid_lobby_leave$"))
    app.add_handler(CallbackQueryHandler(cb_raid_lobby_noop, pattern=r"^raid_lobby_noop$"))

    app.add_handler(CallbackQueryHandler(cb_raid_action, pattern=r"^raid_action$"))
    app.add_handler(CallbackQueryHandler(cb_raid_cancel_action, pattern=r"^raid_cancel_action$"))
    app.add_handler(CallbackQueryHandler(cb_raid_next, pattern=r"^raid_next$"))
    app.add_handler(CallbackQueryHandler(cb_raid_leave, pattern=r"^raid_leave$"))

    app.add_handler(CallbackQueryHandler(cb_stat_alloc, pattern=r"^stat_(str|agi|int)$"))

    app.add_handler(CallbackQueryHandler(cb_inv_page, pattern=r"^inv_page_"))
    app.add_handler(CallbackQueryHandler(cb_inv_item, pattern=r"^inv_item_"))
    app.add_handler(CallbackQueryHandler(cb_inv_equip, pattern=r"^inv_equip_"))
    app.add_handler(CallbackQueryHandler(cb_inv_unequip, pattern=r"^inv_unequip_"))
    app.add_handler(CallbackQueryHandler(cb_inv_drop, pattern=r"^inv_drop_"))
    app.add_handler(CallbackQueryHandler(cb_inv_sell, pattern=r"^inv_sell_"))

    app.add_handler(CallbackQueryHandler(cb_market_page, pattern=r"^market_page_"))
    app.add_handler(CallbackQueryHandler(cb_market_buy, pattern=r"^market_buy_"))
    app.add_handler(CallbackQueryHandler(cb_market_confirm, pattern=r"^market_confirm_"))
    app.add_handler(CallbackQueryHandler(cb_market_cancel, pattern=r"^market_cancel_"))

    app.add_handler(CallbackQueryHandler(cb_char_create, pattern=r"^char_create$"))
    app.add_handler(CallbackQueryHandler(cb_char_switch, pattern=r"^char_switch_"))
    app.add_handler(CallbackQueryHandler(cb_char_delete_confirm, pattern=r"^char_del_yes_"))
    app.add_handler(CallbackQueryHandler(cb_char_delete, pattern=r"^char_del_"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & ALLOWED_CHAT_FILTER, handle_text))
