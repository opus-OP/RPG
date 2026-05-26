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
from core.raid import create_raid, process_encounter_turn, generate_loot, distribute_exp_gold, RaidSession, RaidStatus
from core.economy import MARKET
from data.storage import storage
from ai.narrative import call_narrative_api
from bot.keyboards import (
    main_menu, class_selection, location_list, confirm_raid,
    raid_actions, raid_next, raid_done, raid_failed,
    inventory_pages, item_actions as item_actions_kb,
    char_list, market_listings as market_listings_kb, market_confirm,
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





# ─── /start ─────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "/start — главное меню",
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
        session = context.user_data.get("raid")
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
        if nn_data and nn_data.get("narrative"):
            await update.message.reply_text(f"📖 {nn_data['narrative']}")
        await _do_turn(update, context, char, session,
                       nn_modifiers=nn_data.get("actions") if nn_data else None,
                       narrative=nn_data.get("narrative", "") if nn_data else "",
                       reply_to=update.message)
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

    await update.message.reply_text("Неизвестная команда. /help")

# ─── /profile ───────────────────────────────────────────────

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

# ─── /inventory ─────────────────────────────────────────────

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

# ─── /characters ────────────────────────────────────────────

async def cmd_characters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chars = await storage.load_characters(update.effective_user.id)
    if not chars:
        await _reply(update, "Нет персонажей. /create")
        return
    current = context.user_data.get("active_char") or chars[0].name
    await _reply(update, "Ваши персонажи:", reply_markup=char_list(chars, current))

# ─── /location ──────────────────────────────────────────────

async def cmd_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    char = await _ensure_char(update, context)
    if not char:
        return
    await _reply(update, "🗺 Выберите локацию:", reply_markup=location_list())

# ─── /market ────────────────────────────────────────────────

async def cmd_market(update: Update, context: ContextTypes.DEFAULT_TYPE):
    listings = MARKET.get_active_listings()
    if not listings:
        await _reply(update, "🏪 Рынок пуст.", reply_markup=main_menu())
        return
    await _show_market(update, context, listings, 0)


async def cmd_raid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = context.user_data.get("raid")
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
    text = f"⚔️ Рейд {cur}/{total}\n\n{_enemy_status_line(enc)}\n\n{_char_status_line(char)}\n"
    if char.companion and char.companion.alive:
        text += f"🛡 Страж: ❤️ {char.companion.hp}/{char.companion.max_hp}\n"
    msg = await update.message.reply_text(text, reply_markup=raid_actions())
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

# ─── Callback: main_menu ────────────────────────────────────

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

# ─── Callback: класс ────────────────────────────────────────

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

# ─── Callback: локация → подтверждение рейда ───────────────

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
    char.in_raid = True
    await _save_char(char)
    context.user_data["raid"] = session

    await _show_encounter(query, context, char, session)


async def _show_encounter(query, context, char: Character, session: RaidSession):
    enc = session.encounters[session.current_encounter]
    context.user_data["raid_msg_chat"] = query.message.chat_id
    context.user_data["raid_msg_id"] = query.message.message_id
    total = len(session.encounters)
    cur = session.current_encounter + 1
    text = f"⚔️ Рейд {cur}/{total}\n\n{_enemy_status_line(enc)}\n\n{_char_status_line(char)}\n"
    if char.companion and char.companion.alive:
        text += f"🛡 Страж: ❤️ {char.companion.hp}/{char.companion.max_hp}\n"
    await query.edit_message_text(text, reply_markup=raid_actions())


async def cb_raid_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    session = context.user_data.get("raid")
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
    session = context.user_data.get("raid")
    if session:
        enc = session.encounters[session.current_encounter]
        await _show_encounter(query, context, await _get_char(update.effective_user.id, context), session)
    else:
        await query.edit_message_text("Главное меню:", reply_markup=main_menu())


async def _do_turn(update: Update, context: ContextTypes.DEFAULT_TYPE,
                   char: Character, session: RaidSession, nn_modifiers, narrative: str = "",
                   reply_to=None):
    uid = update.effective_user.id
    enc = session.encounters[session.current_encounter]

    try:
        player_attack, enemy_attack, companion_attack, finished = process_encounter_turn(
            session, char, nn_modifiers=nn_modifiers,
        )
    except Exception as e:
        log.exception("process_encounter_turn crashed")
        if reply_to:
            await reply_to.reply_text(f"❌ Ошибка боя: {e}")
        return

    total = len(session.encounters)
    cur = session.current_encounter + 1
    text = f"⚔️ Рейд {cur}/{total}\n\n"
    if narrative:
        text += f"📖 {narrative}\n\n"
    text += _enemy_status_line(enc) + "\n\n"
    text += _attack_desc("Вы", player_attack) + "\n"
    if companion_attack:
        text += _attack_desc("🛡 Страж", companion_attack) + "\n"
    if enemy_attack:
        text += _attack_desc(f"👾 {enc.enemy_template['name']}", enemy_attack, "наносит") + "\n"
    text += f"\n{_char_status_line(char)}"
    if char.companion and char.companion.alive:
        text += f"\n🛡 Страж: ❤️ {char.companion.hp}/{char.companion.max_hp}"

    if finished:
        if char.hp <= 0:
            text += "\n\n💀 **Вы погибли!**"
            session.status = RaidStatus.FAILED
            char.in_raid = False
            char.durability_damage_all(percent=DEATH_DURABILITY_LOSS)
            char.release_companion()
            char.alive = False
            await _save_char(char)
            context.user_data.pop("raid", None)
            await _edit_turn_msg(context, text, raid_failed(), reply_to)
        elif enc.enemy_hp <= 0:
            text += f"\n\n✅ {enc.enemy_template['name']} повержен!"
            enc.finished = True
            if session.current_encounter + 1 >= len(session.encounters):
                session.status = RaidStatus.COMPLETED
                loc = get_location(session.location_key)
                if loc:
                    loot = generate_loot(loc, len(session.encounters), char.level, [char.class_key])
                    distribute_exp_gold(session, loc, [char])
                    char.inventory.extend(loot)
                    char.in_raid = False
                    char.mark_raid_done()
                    char.durability_damage_all(percent=DURABILITY_LOSS_PERCENT / 100.0)
                    char.release_companion()
                    await _save_char(char)
                    loot_text = ""
                    if loot:
                        loot_text = "\n🎁 Добыча: " + ", ".join(f"{it.name} [{it.rarity.value}]" for it in loot)
                    text += f"\n\n🏆 **Рейд пройден!**{loot_text}"
                    context.user_data.pop("raid", None)
                    await _edit_turn_msg(context, text, raid_done(), reply_to)
                else:
                    text += "\n\n⚠️ Ошибка: локация не найдена."
                    await _edit_turn_msg(context, text, main_menu(), reply_to)
            else:
                await _save_char(char)
                await _edit_turn_msg(context, text, raid_next(), reply_to)
        else:
            await _save_char(char)
            await _edit_turn_msg(context, text, raid_actions(), reply_to)
    else:
        await _save_char(char)
        await _edit_turn_msg(context, text, raid_actions(), reply_to)


async def _edit_turn_msg(context, text: str, kb, reply_to=None):
    chat_id = context.user_data.get("raid_msg_chat")
    msg_id = context.user_data.get("raid_msg_id")
    if chat_id and msg_id:
        try:
            await context.bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=kb)
            return
        except Exception as e:
            log.warning("_edit_turn_msg edit failed: %s", e)
    if reply_to:
        try:
            msg = await reply_to.reply_text(text, reply_markup=kb)
            context.user_data["raid_msg_chat"] = msg.chat_id
            context.user_data["raid_msg_id"] = msg.message_id
        except Exception as e:
            log.warning("_edit_turn_msg reply failed: %s", e)

# ─── Callback: рейд — следующий враг ───────────────────────

async def cb_raid_next(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    session = context.user_data.get("raid")
    if not session:
        await query.edit_message_text("Рейд не найден.")
        return
    char = await _get_char(uid, context)
    if not char:
        await query.edit_message_text("Персонаж не найден.")
        return
    session.current_encounter += 1
    if session.current_encounter >= len(session.encounters):
        loc = get_location(session.location_key)
        if loc:
            session.status = RaidStatus.COMPLETED
            loot = generate_loot(loc, len(session.encounters), char.level, [char.class_key])
            distribute_exp_gold(session, loc, [char])
            char.inventory.extend(loot)
            char.in_raid = False
            char.mark_raid_done()
            char.release_companion()
            await _save_char(char)
            text = "🏆 **Рейд пройден!**\n"
            if loot:
                text += "🎁 Добыча: " + ", ".join(f"{it.name} [{it.rarity.value}]" for it in loot)
            context.user_data.pop("raid", None)
            await query.edit_message_text(text, reply_markup=raid_done())
        else:
            await query.edit_message_text("⚠️ Ошибка: локация не найдена.", reply_markup=main_menu())
        return
    await _show_encounter(query, context, char, session)

# ─── Callback: рейд — сбежать ──────────────────────────────

async def cb_raid_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    context.user_data.pop("raid_action_pending", None)
    session = context.user_data.pop("raid", None)
    if not session:
        await query.edit_message_text("Рейд не найден.", reply_markup=main_menu())
        return
    char = await _get_char(uid, context)
    if char:
        char.in_raid = False
        char.release_companion()
        await _save_char(char)
    await query.edit_message_text("🏃 Вы сбежали из рейда!", reply_markup=main_menu())

# ─── Callback: инвентарь — страницы + предмет ──────────────

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

# ─── Callback: рынок — страницы + покупка ──────────────────

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

# ─── Callback: персонажи ───────────────────────────────────

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

# ─── Админ-команды ─────────────────────────────────────────

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


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("admin"):
        await _reply(update, "Доступ запрещён.")
        return
    char = await _get_char(update.effective_user.id, context)
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
        await _reply(update, "/set_level <число>")
        return
    try:
        lvl = int(args[0])
    except ValueError:
        await _reply(update, "Введите число.")
        return
    char = await _get_char(update.effective_user.id, context)
    if not char:
        return
    char.level = max(1, min(lvl, 50))
    char._recalc_stats()
    char.hp = char.max_hp
    await _save_char(char)
    await _reply(update, f"✅ Уровень {char.level}, HP восстановлено.")


async def cmd_add_gold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("admin"):
        await _reply(update, "Доступ запрещён.")
        return
    args = context.args
    if not args:
        await _reply(update, "/add_gold <число>")
        return
    try:
        amount = int(args[0])
    except ValueError:
        await _reply(update, "Введите число.")
        return
    char = await _get_char(update.effective_user.id, context)
    if not char:
        return
    char.gold += amount
    await _save_char(char)
    await _reply(update, f"✅ +{amount}💰, теперь {char.gold}💰")


async def cmd_add_exp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("admin"):
        await _reply(update, "Доступ запрещён.")
        return
    args = context.args
    if not args:
        await _reply(update, "/add_exp <число>")
        return
    try:
        amount = int(args[0])
    except ValueError:
        await _reply(update, "Введите число.")
        return
    char = await _get_char(update.effective_user.id, context)
    if not char:
        return
    char.add_experience(amount)
    await _save_char(char)
    await _reply(update, f"✅ +{amount} XP, уровень {char.level}")


async def cmd_reset_cooldown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("admin"):
        await _reply(update, "Доступ запрещён.")
        return
    char = await _get_char(update.effective_user.id, context)
    if not char:
        return
    char.last_raid_time = 0.0
    await _save_char(char)
    await _reply(update, "✅ Кулдаун рейда сброшен.")

# ─── Регистрация ───────────────────────────────────────────

def register_handlers(app: Application):
    app.add_handler(CommandHandler("start", cmd_start))
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

    app.add_handler(CallbackQueryHandler(cb_location_select, pattern=r"^loc_"))
    app.add_handler(CallbackQueryHandler(cb_raid_start, pattern=r"^raid_start_"))

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

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
