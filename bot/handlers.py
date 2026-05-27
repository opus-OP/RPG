"""Telegram-хендлеры: команды, callback-запросы, обработка текста.

Вся логика взаимодействия с пользователем через Telegram.
Каждая команда (/start, /create, /profile и т.д.) и каждый callback_data
имеет соответствующий асинхронный хендлер.
"""

import time
import uuid
import logging
from typing import Optional

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)

from config import MAX_CHARACTERS_PER_PLAYER, ADMIN_PASSWORD, DURABILITY_LOSS_PERCENT, DEATH_DURABILITY_LOSS
from core.character import Character
from core.classes import CLASSES, CLASS_NAMES_RU
from core.locations import LOCATIONS, get_location
from core.raid import create_raid, process_encounter_turn, generate_loot, distribute_exp_gold, RaidSession, RaidStatus, session_to_dict, session_from_dict
from core.economy import MARKET
from data.storage import storage
from ai.narrative import call_narrative_api
from bot.keyboards import (
    main_menu, class_selection, location_list, confirm_raid,
    raid_actions, raid_next, raid_done, raid_failed,
    raid_lobby, raid_lobby_text,
    inventory_pages, item_actions as item_actions_kb,
    char_list, char_delete_confirm, market_listings as market_listings_kb, market_confirm,
)

log = logging.getLogger("rpg.handlers")


def _attack_desc(owner: str, result, noun: str = "нанесли") -> str:
    if result is None:
        return ""
    dmg = result.final_damage
    if result.is_dodged:
        return f"🛡 {owner} промахнулись!"
    crit = " КРИТ!" if result.is_crit else ""
    return f"⚔️ {owner} {noun} {dmg} урона{crit}"


def _enemy_status_line(enc) -> str:
    tpl = enc.enemy_template
    perc = int(enc.enemy_hp / max(enc.enemy_max_hp, 1) * 100)
    bar = "▓" * (perc // 10) + "░" * (10 - perc // 10)
    return f"👾 {tpl['name']} ❤️ {enc.enemy_hp}/{enc.enemy_max_hp}\n{bar}"


def _char_status_line(char: Character) -> str:
    return f"❤️ **{char.hp}**/{char.max_hp} | ⚔️ {char.attack_min}-{char.attack_max} | 🛡 {char.defense}"


def _initiative_line(enc) -> str:
    order = enc.initiative_order
    if not order:
        return ""
    icons = {"player": "🧑", "companion": "🛡", "enemy": "👾"}
    names = {"player": "Вы", "companion": "Страж", "enemy": enc.enemy_template["name"]}
    parts = [f"{icons.get(who, '❓')}{names.get(who, who)}" for who in order]
    return f"⚡ Инициатива: {' → '.join(parts)}"


def _encounter_header(cur: int, total: int, enc, char: Character) -> str:
    header = f"⚔️ Рейд {cur}/{total}\n{_initiative_line(enc)}\n\n"
    header += f"{_enemy_status_line(enc)}\n\n"
    header += _char_status_line(char)
    if char.companion and char.companion.alive:
        header += f"\n🛡 Страж: ❤️ {char.companion.hp}/{char.companion.max_hp}"
    return header


def _slot_item(char: Character, slot: str) -> str:
    item = getattr(char.equipment, slot, None)
    if item:
        return f"{item.name} [{item.rarity.value}]"
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
        await update.message.reply_text("Выберите класс кнопкой выше ☝️")
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

    raid_pending = context.user_data.get("raid_action_pending")
    if raid_pending:
        session = await _get_session(context)
        if not session or session.status != RaidStatus.IN_PROGRESS:
            context.user_data.pop("raid_action_pending", None)
            await update.message.reply_text("Рейд уже завершён.")
            return
        char = await _get_char(uid, context)
        if not char:
            context.user_data.pop("raid_action_pending", None)
            return
        context.user_data.pop("raid_action_pending", None)
        enc = session.encounters[session.current_encounter]
        nn_data = await call_narrative_api(
            location=session.location_key,
            turn=enc.turn,
            player={"name": char.name, "class": char.class_key, "hp": char.hp, "max_hp": char.max_hp},
            enemies=[{"name": enc.enemy_template["name"], "hp": enc.enemy_hp, "max_hp": enc.enemy_max_hp}],
            action_history=[],
            player_action=text,
        )
        player_attack, enemy_attack, companion_attack, finished = await _do_turn(
            update, context, char, session,
            nn_modifiers=nn_data.get("actions") if nn_data else None,
            enemy_nn_modifiers=nn_data.get("enemy_actions") if nn_data else None,
            reply_to=update.message,
        )
        if player_attack is None:
            return

        await _save_session(context, session)
        total = len(session.encounters)
        cur = session.current_encounter + 1

        player_narrative = (nn_data or {}).get("player_narrative", "")
        enemy_narrative = (nn_data or {}).get("enemy_narrative", "")

        async def send_result(kb):
            msg = await update.message.reply_text(
                _encounter_header(cur, total, enc, char),
                reply_markup=kb,
            )
            context.user_data["raid_msg_chat"] = msg.chat_id
            context.user_data["raid_msg_id"] = msg.message_id
            return msg

        # ─── Player action message ───
        player_text = f"{_encounter_header(cur, total, enc, char)}\n\n"
        if player_narrative:
            player_text += f"📖 {player_narrative}\n\n"
        player_text += _attack_desc("Вы", player_attack) + "\n"
        if companion_attack:
            player_text += _attack_desc("🛡 Страж", companion_attack) + "\n"

        # ─── Enemy action message / final ───
        if finished:
            char.count_raid +=1
            if char.hp <= 0:
                player_text += "\n\n💀 **Вы погибли!**"
                session.status = RaidStatus.FAILED
                char.in_raid = False
                char.durability_damage_all(percent=DEATH_DURABILITY_LOSS)
                char.release_companion()
                char.alive = True
                char.hp = char.max_hp
                await _save_char(char)
                _cleanup_raid(context)
                await send_result(raid_failed())
            elif enc.enemy_hp <= 0:
                enc.finished = True
                player_text += f"\n\n✅ {enc.enemy_template['name']} повержен!"
                if session.current_encounter + 1 >= len(session.encounters):
                    session.status = RaidStatus.COMPLETED
                    loc = get_location(session.location_key)
                    if loc:
                        _cleanup_raid(context)
                        rewards = await _reward_all_participants(session, loc, context)
                        my_reward = next((r for r in rewards if r[0] == char.name), None)
                        loot_text = ""
                        if my_reward:
                            loot_text = "\n" + my_reward[1]
                        player_text += f"\n\n🏆 **Рейд пройден!**{loot_text}"
                        await send_result(raid_done())
                    else:
                        player_text += "\n\n⚠️ Ошибка: локация не найдена."
                        await send_result(main_menu())
                else:
                    await _save_char(char)
                    await send_result(raid_next())
            else:
                await _save_char(char)
                await send_result(raid_actions())
        else:
            # Enemy alive — send player msg, then enemy msg
            player_text += "\n\n⏳ Ожидание ответа врага..."
            player_msg = await update.message.reply_text(player_text)
            enemy_text = f"{_encounter_header(cur, total, enc, char)}\n\n"
            if enemy_narrative:
                enemy_text += f"📖 {enemy_narrative}\n\n"
            enemy_text += _attack_desc(f"👾 {enc.enemy_template['name']}", enemy_attack, "наносит") + "\n"
            await _save_char(char)
            msg = await player_msg.reply_text(enemy_text, reply_markup=raid_actions())
            context.user_data["raid_msg_chat"] = msg.chat_id
            context.user_data["raid_msg_id"] = msg.message_id
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
    text = (
        f"👤 **{char.name}** — {t.name} | Ур. {char.level}\n"
        f"📝 {char.description}\n"
        f"✨ Опыт: {char.experience}/{char.exp_to_next}\n\n"
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
    await _reply(update, text, reply_markup=main_menu())

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
    msg = await update.message.reply_text(_encounter_header(cur, total, enc, char), reply_markup=raid_actions())
    context.user_data["raid_msg_chat"] = msg.chat_id
    context.user_data["raid_msg_id"] = msg.message_id


async def _show_market(update: Update, context: ContextTypes.DEFAULT_TYPE, listings: list, page: int):
    text = f"🏪 Рынок — стр. {page + 1}\n"
    per_page = 5
    start = page * per_page
    batch = listings[start:start + per_page]
    for i, listing in enumerate(batch, start=start + 1):
        text += f"\n{i}. {listing.item.name} [{listing.item.rarity.value}] — {listing.price}💰"
    await _reply(update, text, reply_markup=market_listings_kb(listings, page))

# ═══════════════════════════════════════════════════════════════
# Callback: главное меню
# ═══════════════════════════════════════════════════════════════

async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Главное меню:", reply_markup=main_menu())


async def cb_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    char = await _get_char(update.effective_user.id, context)
    if not char:
        await query.edit_message_text("Нет персонажа. /create")
        return
    t = char.template
    s = char.stats
    text = (
        f"👤 **{char.name}** — {t.name} | Ур. {char.level}\n"
        f"✨ Опыт: {char.experience}/{char.exp_to_next}\n"
        f"{_char_status_line(char)}\n"
        f"🎯 Крит: {char.crit_chance*100:.1f}% | Уклон: {char.dodge_chance*100:.1f}%\n"
        f"💰 **{char.gold}** золота\n\n"
        f"🗡 Оружие: {_slot_item(char, 'weapon')}\n"
        f"🛡 Броня: {_slot_item(char, 'armor')}\n"
        f"💍 Аксессуар: {_slot_item(char, 'accessory')}"
    )
    await query.edit_message_text(text, reply_markup=main_menu())


async def cb_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    char = await _get_char(update.effective_user.id, context)
    if not char:
        await query.edit_message_text("Нет персонажа. /create")
        return
    await _show_inventory(update, context, char, 0)


async def cb_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    char = await _get_char(update.effective_user.id, context)
    if not char:
        await query.edit_message_text("Нет персонажа. /create", reply_markup=main_menu())
        return
    await query.edit_message_text("🗺 Выберите локацию:", reply_markup=location_list())


async def cb_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    listings = MARKET.get_active_listings()
    if not listings:
        await query.edit_message_text("🏪 Рынок пуст.", reply_markup=main_menu())
        return
    await _show_market(update, context, listings, 0)


async def cb_market_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await cb_market(update, context)


async def cb_char_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chars = await storage.load_characters(update.effective_user.id)
    if not chars:
        await query.edit_message_text("Нет персонажей. /create")
        return
    await query.edit_message_text("Ваши персонажи:", reply_markup=char_list(chars, chars[0].name))

# ═══════════════════════════════════════════════════════════════
# Callback: выбор класса при создании персонажа
# ═══════════════════════════════════════════════════════════════

async def cb_class_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    state = context.user_data.get("creation")
    if not state or state["step"] != "class":
        await query.edit_message_text("Создание не активно. /create")
        return
    cls_key = query.data[len("class_"):]
    if cls_key not in CLASSES:
        await query.edit_message_text("Неверный класс.")
        return
    state["class_key"] = cls_key
    if cls_key == "leader":
        state["step"] = "companion_name"
        await query.edit_message_text("Лидер может призвать стража.\nВведите имя стража:")
    else:
        await _finish_creation(update, context, state)

# ═══════════════════════════════════════════════════════════════
# Callback: выбор локации → подтверждение рейда
# ═══════════════════════════════════════════════════════════════

async def cb_location_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data[len("loc_"):]
    loc = get_location(key)
    if not loc:
        await query.edit_message_text("Локация не найдена.")
        return
    char = await _get_char(update.effective_user.id, context)
    if not char:
        await query.edit_message_text("Нет персонажа. /create")
        return
    if char.in_raid:
        session = context.user_data.get("raid")
        if session and session.status == RaidStatus.IN_PROGRESS:
            await query.edit_message_text("Вы уже в рейде! Завершите его.")
            return
        char.in_raid = False
        char.release_companion()
        await _save_char(char)
    if not char.can_raid():
        rem = char.raid_cooldown_remaining()
        hrs = int(rem // 3600)
        mins = int((rem % 3600) // 60)
        await query.edit_message_text(f"⏳ До следующего рейда {hrs}ч {mins}м.")
        return
    text = (
        f"🗺 {loc.name} (ур. {loc.recommended_level})\n"
        f"{loc.description}\n"
        f"☠️ Опасность: {loc.danger}/10\n"
        f"💰 Награда: {loc.gold_min}-{loc.gold_max} золота, {loc.exp_reward} опыта"
    )
    await query.edit_message_text(text, reply_markup=confirm_raid(key))


async def cb_raid_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    key = query.data[len("raid_start_"):]
    loc = get_location(key)
    if not loc:
        await query.edit_message_text("Локация не найдена.")
        return
    char = await _get_char(uid, context)
    if not char:
        await query.edit_message_text("Персонаж не найден.")
        return
    if char.in_raid:
        session = context.user_data.get("raid")
        if session and session.status == RaidStatus.IN_PROGRESS:
            await query.edit_message_text("Вы уже в рейде! Завершите его.")
            return
        char.in_raid = False
        char.release_companion()
        await _save_char(char)
        char = await _get_char(uid, context)
        if not char:
            await query.edit_message_text("Персонаж не найден.")
            return

    if not char.can_raid():
        rem = char.raid_cooldown_remaining()
        hrs = int(rem // 3600)
        mins = int((rem % 3600) // 60)
        await query.edit_message_text(f"⏳ До следующего рейда {hrs}ч {mins}м.")
        return

    raid_id = str(uuid.uuid4())[:8]
    session = create_raid(char, loc, raid_id)
    session.status = RaidStatus.IN_PROGRESS
    session.participant_names = {uid: char.name}
    char.in_raid = True
    await _save_char(char)
    context.user_data["raid"] = session

    await _show_encounter(query, context, char, session)


# ═══════════════════════════════════════════════════════════════
# Онлайн-рейд: создание лобби
# ═══════════════════════════════════════════════════════════════

async def cb_raid_online_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    key = query.data[len("raid_online_"):]
    loc = get_location(key)
    if not loc:
        await query.edit_message_text("Локация не найдена.")
        return
    char = await _get_char(uid, context)
    if not char:
        await query.edit_message_text("Персонаж не найден.")
        return
    if not char.can_raid():
        rem = char.raid_cooldown_remaining()
        hrs = int(rem // 3600)
        mins = int((rem % 3600) // 60)
        await query.edit_message_text(f"⏳ До следующего рейда {hrs}ч {mins}м.")
        return

    raid_id = str(uuid.uuid4())[:8]
    session = create_raid(char, loc, raid_id)
    session.status = RaidStatus.PENDING
    session.participant_names = {uid: char.name}
    await storage.save_raid_session(raid_id, session_to_dict(session))
    context.user_data["raid_id"] = raid_id
    await query.edit_message_text(
        raid_lobby_text(loc.name, raid_id, [(uid, char.name)]),
        reply_markup=raid_lobby(loc.name, raid_id, [(uid, char.name)], True),
    )


async def cmd_raid_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await _reply(update, "Использование: /join <код>")
        return
    code = args[0].upper()
    cursor = await storage._conn.execute("SELECT raid_id, data FROM raids")
    rows = await cursor.fetchall()
    target = None
    for row in rows:
        data = json.loads(row["data"])
        if data.get("status") != "pending":
            continue
        sid = row["raid_id"]
        if sid.upper() == code:
            target = row
            break
    if not target:
        await _reply(update, f"Рейд с кодом `{code}` не найден или уже начался.")
        return

    uid = update.effective_user.id
    char = await _get_char(uid, context)
    if not char:
        return
    if not char.can_raid():
        await _reply(update, "Ваш персонаж не может участвовать (кулдаун).")
        return

    data = json.loads(target["data"])
    participants = data.get("participant_names", {})
    if uid in participants:
        await _reply(update, "Вы уже в этом рейде.")
        return
    if len(participants) >= 4:
        await _reply(update, "В рейде уже 4 игрока — максимум.")
        return

    participants[uid] = char.name
    data["participant_names"] = participants
    await storage.save_raid_session(target["raid_id"], data)
    context.user_data["raid_id"] = target["raid_id"]

    loc_name = get_location(data["location_key"]).name if get_location(data["location_key"]) else "?"
    part_list = [(int(k), v) for k, v in participants.items()]
    text = raid_lobby_text(loc_name, target["raid_id"], part_list)
    await _reply(update, f"✅ Вы присоединились к рейду **{loc_name}**!")
    # Notify the owner
    owner_id = int(list(participants.keys())[0])
    try:
        from telegram.helpers import escape_markdown
        await context.bot.send_message(
            owner_id,
            f"👤 {char.name} присоединился к рейду!",
        )
    except Exception:
        pass


async def cb_raid_lobby_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    raid_id = context.user_data.get("raid_id")
    if not raid_id:
        await query.edit_message_text("Рейд не найден.")
        return
    data = await storage.load_raid_session(raid_id)
    if not data:
        await query.edit_message_text("Рейд не найден в БД.")
        return
    participants = data.get("participant_names", {})
    owner_id = int(list(participants.keys())[0])
    if uid != owner_id:
        await query.edit_message_text("Только создатель может начать рейд.")
        return
    if len(participants) < 1:
        await query.edit_message_text("Нужен хотя бы 1 участник.")
        return

    loc = get_location(data["location_key"])
    if not loc:
        await query.edit_message_text("Локация не найдена.")
        return

    raid_id = data["raid_id"]
    session = session_from_dict(data)
    session.status = RaidStatus.IN_PROGRESS
    # Scale HP for group size
    gs = len(participants)
    if gs > 1:
        for enc in session.encounters:
            enc.enemy_hp = int(enc.enemy_hp * (1 + 0.3 * (gs - 1)))
            enc.enemy_max_hp = enc.enemy_hp
    await storage.save_raid_session(raid_id, session_to_dict(session))

    # Notify all participants
    for pid in participants:
        try:
            char = await _get_char(pid, context)
            if char:
                char.in_raid = True
                await _save_char(char)
            enc = session.encounters[0]
            await context.bot.send_message(
                pid,
                f"⚔️ Рейд в **{loc.name}** начался!\n\n"
                f"👾 {enc.enemy_template['name']} ❤️ {enc.enemy_hp}/{enc.enemy_max_hp}",
                reply_markup=raid_actions(),
            )
        except Exception:
            pass

    await query.edit_message_text("⚔️ Рейд начат! Участники оповещены.")


async def cb_raid_lobby_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    raid_id = context.user_data.get("raid_id")
    if not raid_id:
        await query.edit_message_text("Рейд не найден.")
        return
    data = await storage.load_raid_session(raid_id)
    if not data:
        await query.edit_message_text("Рейд не найден в БД.")
        return
    participants = data.get("participant_names", {})
    if uid not in participants:
        await query.edit_message_text("Вы не в этом рейде.")
        return
    del participants[uid]
    context.user_data.pop("raid_id", None)
    if not participants:
        await storage.delete_raid_session(raid_id)
        await query.edit_message_text("Вы вышли. Рейд удалён (нет участников).")
        return
    data["participant_names"] = participants
    await storage.save_raid_session(raid_id, data)
    loc = get_location(data["location_key"])
    loc_name = loc.name if loc else "?"
    part_list = [(int(k), v) for k, v in participants.items()]
    await query.edit_message_text(
        raid_lobby_text(loc_name, raid_id, part_list),
        reply_markup=raid_lobby(loc_name, raid_id, part_list, uid == int(list(participants.keys())[0])),
    )


async def _get_session(context) -> Optional[RaidSession]:
    """Get raid session from user_data or DB."""
    session = context.user_data.get("raid")
    if session:
        return session
    raid_id = context.user_data.get("raid_id")
    if not raid_id:
        return None
    data = await storage.load_raid_session(raid_id)
    if not data:
        return None
    return session_from_dict(data)


def _cleanup_raid(context):
    context.user_data.pop("raid", None)
    context.user_data.pop("raid_id", None)
    context.user_data.pop("raid_action_pending", None)


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
            loot_text = " 🎁 " + ", ".join(f"{it.name} [{it.rarity.value}]" for it in loot)
        results.append((char.name, loot_text))
        try:
            await context.bot.send_message(
                uid, f"🏆 **Рейд пройден!**{loot_text}",
                reply_markup=raid_done(),
            )
        except Exception:
            pass
    return results


async def _save_session(context, session: RaidSession):
    """Save raid session to user_data or DB."""
    raid_id = context.user_data.get("raid_id")
    if raid_id:
        await storage.save_raid_session(raid_id, session_to_dict(session))
    else:
        context.user_data["raid"] = session


async def _notify_participants(context, session: RaidSession, text: str, kb=None):
    """Notify all participants of a raid."""
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
    total = len(session.encounters)
    cur = session.current_encounter + 1
    await query.edit_message_text(_encounter_header(cur, total, enc, char), reply_markup=raid_actions())


async def cb_raid_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = await _get_session(context)
    if not session or session.status != RaidStatus.IN_PROGRESS:
        await query.edit_message_text("Нет активного рейда.", reply_markup=main_menu())
        return
    char = await _get_char(update.effective_user.id, context)
    if not char:
        await query.edit_message_text("Персонаж не найден.", reply_markup=main_menu())
        return
    context.user_data["raid_msg_chat"] = query.message.chat_id
    context.user_data["raid_msg_id"] = query.message.message_id
    context.user_data["raid_action_pending"] = True
    await _save_session(context, session)
    await query.edit_message_text(
        query.message.text + "\n\n✏️ Напишите, что делает ваш персонаж:",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Отмена", callback_data="raid_cancel_action"),
        ]]),
    )


async def cb_raid_cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("raid_action_pending", None)
    session = await _get_session(context)
    if session:
        await _show_encounter(query, context, await _get_char(update.effective_user.id, context), session)
    else:
        await query.edit_message_text("Главное меню:", reply_markup=main_menu())


async def _do_turn(update: Update, context: ContextTypes.DEFAULT_TYPE,
                   char: Character, session: RaidSession, nn_modifiers,
                   enemy_nn_modifiers=None, reply_to=None):
    uid = update.effective_user.id
    enc = session.encounters[session.current_encounter]

    try:
        player_attack, enemy_attack, companion_attack, finished = process_encounter_turn(
            session, char, nn_modifiers=nn_modifiers,
            enemy_nn_modifiers=enemy_nn_modifiers,
        )
    except Exception as e:
        log.exception("process_encounter_turn crashed")
        if reply_to:
            await reply_to.reply_text(f"❌ Ошибка боя: {e}")
        return None, None, None, None

    return player_attack, enemy_attack, companion_attack, finished

# ═══════════════════════════════════════════════════════════════
# Callback: рейд — следующий враг
# ═══════════════════════════════════════════════════════════════

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
    await _save_session(context, session)
    if session.current_encounter >= len(session.encounters):
        loc = get_location(session.location_key)
        if loc:
            session.status = RaidStatus.COMPLETED
            _cleanup_raid(context)
            rewards = await _reward_all_participants(session, loc, context)
            my_reward = next((r for r in rewards if r[0] == char.name), None)
            text = "🏆 **Рейд пройден!**"
            if my_reward:
                text += my_reward[1]
            await query.edit_message_text(text, reply_markup=raid_done())
        else:
            await query.edit_message_text("⚠️ Ошибка: локация не найдена.", reply_markup=main_menu())
        return
    await _show_encounter(query, context, char, session)

# ═══════════════════════════════════════════════════════════════
# Callback: рейд — сбежать
# ═══════════════════════════════════════════════════════════════

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
    _cleanup_raid(context)
    await query.edit_message_text("🏃 Вы сбежали из рейда!", reply_markup=main_menu())

# ═══════════════════════════════════════════════════════════════
# Callback: инвентарь — страницы + предмет
# ═══════════════════════════════════════════════════════════════

async def cb_inv_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    char = await _get_char(update.effective_user.id, context)
    if not char:
        return
    page = int(query.data[len("inv_page_"):])
    await _show_inventory(update, context, char, page)


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
    if eff.atk_bonus: parts.append(f"⚔️ Атака +{eff.atk_bonus}")
    if eff.defense_bonus: parts.append(f"🛡 Защита +{eff.defense_bonus}")
    if eff.hp_bonus: parts.append(f"❤️ HP +{eff.hp_bonus}")
    if eff.crit_chance_bonus: parts.append(f"🎯 Крит +{eff.crit_chance_bonus*100:.0f}%")
    if eff.crit_multiplier_bonus: parts.append(f"🗡 Крит.множитель +{eff.crit_multiplier_bonus:.1f}")
    if eff.dodge_bonus: parts.append(f"💨 Уклон +{eff.dodge_bonus*100:.0f}%")
    if parts:
        text += "\n" + "\n".join(parts)
    await query.edit_message_text(text, reply_markup=item_actions_kb(item_uid))


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

async def cb_market_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = int(query.data[len("market_page_"):])
    listings = MARKET.get_active_listings()
    await _show_market(update, context, listings, page)


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

async def cb_char_create(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await cmd_create(update, context)


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
    await _reply(update, "🔧 Режим администратора активирован.\n"
                         "/debug — меню отладки\n"
                         "/set_level N — установить уровень\n"
                         "/add_gold N — добавить золото\n"
                         "/add_exp N — добавить опыт\n"
                         "/reset_cooldown — сбросить таймер рейда")


async def _resolve_target_char(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> Optional[Character]:
    uid = update.effective_user.id
    username = None
    if args:
        for a in args:
            if a.startswith("@"):
                username = a.lstrip("@")
                break
    if username:
        try:
            chat = await context.bot.get_chat(f"@{username}")
            target_uid = chat.id
        except Exception:
            await _reply(update, f"Пользователь @{username} не найден.")
            return None
        chars = await storage.load_characters(target_uid)
        if not chars:
            await _reply(update, f"У @{username} нет персонажа.")
            return None
        return chars[0]
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
        await _reply(update, "/set_level <число> [@тег]")
        return
    try:
        lvl_str = args[0] if not args[0].startswith("@") else args[1] if len(args) > 1 else ""
        lvl = int(lvl_str)
    except (ValueError, IndexError):
        await _reply(update, "/set_level <число> [@тег]")
        return
    char = await _resolve_target_char(update, context, args)
    if not char:
        return
    char.level = max(1, min(lvl, 50))
    char._recalc_stats()
    char.hp = char.max_hp
    await _save_char(char)
    await _reply(update, f"✅ {char.name}: уровень {char.level}, HP восстановлено.")


async def cmd_add_gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("admin"):
        await _reply(update, "Доступ запрещён.")
        return
    args = context.args
    if not args:
        await _reply(update, "/add_gold <число> [@тег]")
        return
    try:
        amt_str = args[0] if not args[0].startswith("@") else args[1] if len(args) > 1 else ""
        amount = int(amt_str)
    except (ValueError, IndexError):
        await _reply(update, "/add_gold <число> [@тег]")
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
        await _reply(update, "/add_exp <число> [@тег]")
        return
    try:
        amt_str = args[0] if not args[0].startswith("@") else args[1] if len(args) > 1 else ""
        amount = int(amt_str)
    except (ValueError, IndexError):
        await _reply(update, "/add_exp <число> [@тег]")
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

# ═══════════════════════════════════════════════════════════════
# Регистрация всех хендлеров
# ═══════════════════════════════════════════════════════════════

def register_handlers(app: Application):
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("start", cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("create", cmd_create))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("inventory", cmd_inventory))
    app.add_handler(CommandHandler("characters", cmd_characters))
    app.add_handler(CommandHandler("location", cmd_location))
    app.add_handler(CommandHandler("market", cmd_market))
    app.add_handler(CommandHandler("raid", cmd_raid))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(CommandHandler("set_level", cmd_set_level))
    app.add_handler(CommandHandler("add_gold", cmd_add_gold))
    app.add_handler(CommandHandler("add_exp", cmd_add_exp))
    app.add_handler(CommandHandler("reset_cooldown", cmd_reset_cooldown))

    app.add_handler(CallbackQueryHandler(cb_main_menu, pattern=r"^main_menu$"))
    app.add_handler(CallbackQueryHandler(cb_profile, pattern=r"^profile$"))
    app.add_handler(CallbackQueryHandler(cb_inventory, pattern=r"^inventory$"))
    app.add_handler(CallbackQueryHandler(cb_location, pattern=r"^location$"))
    app.add_handler(CallbackQueryHandler(cb_market, pattern=r"^market$"))
    app.add_handler(CallbackQueryHandler(cb_market_refresh, pattern=r"^market_refresh$"))
    app.add_handler(CallbackQueryHandler(cb_char_list, pattern=r"^char_list$"))

    app.add_handler(CallbackQueryHandler(cb_class_select, pattern=r"^class_"))

    app.add_handler(CommandHandler("join", cmd_raid_join))
    app.add_handler(CallbackQueryHandler(cb_location_select, pattern=r"^loc_"))
    app.add_handler(CallbackQueryHandler(cb_raid_start, pattern=r"^raid_start_"))
    app.add_handler(CallbackQueryHandler(cb_raid_online_create, pattern=r"^raid_online_"))
    app.add_handler(CallbackQueryHandler(cb_raid_lobby_start, pattern=r"^raid_lobby_start$"))
    app.add_handler(CallbackQueryHandler(cb_raid_lobby_leave, pattern=r"^raid_lobby_leave$"))

    app.add_handler(CallbackQueryHandler(cb_raid_action, pattern=r"^raid_action$"))
    app.add_handler(CallbackQueryHandler(cb_raid_cancel_action, pattern=r"^raid_cancel_action$"))
    app.add_handler(CallbackQueryHandler(cb_raid_next, pattern=r"^raid_next$"))
    app.add_handler(CallbackQueryHandler(cb_raid_leave, pattern=r"^raid_leave$"))

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

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
