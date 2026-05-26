import time
import uuid
import logging
from typing import Optional

from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command

from config import RAID_COOLDOWN_HOURS, MAX_CHARACTERS_PER_PLAYER
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

router = Router()

active_raids: dict[int, RaidSession] = {}
creation_states: dict[int, dict] = {}
pending_sells: dict[int, dict] = {}

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

async def _get_char(user_id: int) -> Optional[Character]:
    chars = await storage.load_characters(user_id)
    if not chars:
        return None
    return chars[0]

async def _save_char(char: Character):
    await storage.save_character(char)

async def _ensure_char(message: Message) -> Optional[Character]:
    char = await _get_char(message.from_user.id)
    if not char:
        await message.answer("У вас нет персонажа. Создайте: /create")
        return None
    return char

# ─── /start ─────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message):
    chars = await storage.load_characters(message.from_user.id)
    if chars:
        await message.answer(
            f"С возвращением, {chars[0].name}!",
            reply_markup=main_menu(),
        )
    else:
        await message.answer(
            "🎲 Добро пожаловать в RPG!\n"
            "Здесь нет персонажей — создайте первого:\n\n"
            "/create — создать персонажа\n"
            "/help — список команд",
        )

@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 Команды:\n"
        "/create — новый персонаж\n"
        "/profile — статистика\n"
        "/inventory — инвентарь\n"
        "/location — список локаций\n"
        "/market — рынок\n"
        "/characters — мои персонажи\n"
        "/start — главное меню",
    )

# ─── /create — создание персонажа ───────────────────────────

@router.message(Command("create"))
async def cmd_create(message: Message):
    uid = message.from_user.id
    ok = await storage.can_create_character(uid)
    if not ok:
        await message.answer(f"Достигнут лимит ({MAX_CHARACTERS_PER_PLAYER} персонажей).")
        return
    creation_states[uid] = {"step": "name"}
    await message.answer("Введите имя персонажа (2–24 символа):")

@router.message(Command("skip"))
async def cmd_skip(message: Message):
    uid = message.from_user.id
    state = creation_states.get(uid)
    if not state:
        await message.answer("Нет активного создания персонажа. /create")
        return
    if state["step"] == "description":
        state["description"] = ""
        state["step"] = "class"
        await message.answer("Выберите класс:", reply_markup=class_selection())
    elif state["step"] == "companion_desc":
        state["companion_description"] = ""
        await _finish_creation(message, state)
    else:
        await message.answer("Сейчас нельзя пропустить этот шаг.")

async def _finish_creation(message: Message, state: dict):
    uid = message.from_user.id
    char = Character(
        owner_tg_id=uid,
        name=state["name"],
        class_key=state["class_key"],
        description=state.get("description", ""),
        companion_name=state.get("companion_name", "Призванный страж"),
        companion_description=state.get("companion_description", ""),
    )
    await _save_char(char)
    creation_states.pop(uid, None)
    await message.answer(
        f"✅ Персонаж **{char.name}** ({CLASS_NAMES_RU[char.class_key]}) создан!",
        reply_markup=main_menu(),
    )

@router.message(F.text)
async def handle_text(message: Message):
    uid = message.from_user.id
    text = message.text.strip()

    state = creation_states.get(uid)
    if state and state["step"] == "name":
        if len(text) < 2 or len(text) > 24:
            await message.answer("Имя должно быть от 2 до 24 символов.")
            return
        state["name"] = text
        state["step"] = "description"
        await message.answer("Введите описание персонажа (или /skip):")
        return

    if state and state["step"] == "description":
        state["description"] = text
        state["step"] = "class"
        await message.answer("Выберите класс:", reply_markup=class_selection())
        return

    sell = pending_sells.get(uid)
    if sell and sell["step"] == "price":
        try:
            price = int(text)
        except ValueError:
            await message.answer("Введите число — цену в золоте.")
            return
        if price <= 0:
            await message.answer("Цена должна быть положительной.")
            return
        char = sell["char"]
        item_uid = sell["item_uid"]
        item = next((i for i in char.inventory if i.uid == item_uid), None)
        if not item:
            await message.answer("Предмет не найден.")
            pending_sells.pop(uid, None)
            return
        char.inventory.remove(item)
        listing = MARKET.create_listing(char.owner_tg_id, char.name, item, price)
        if not listing:
            await message.answer("Ошибка создания объявления.")
            return
        await MARKET.persist_listing(storage, listing)
        await _save_char(char)
        pending_sells.pop(uid, None)
        await message.answer(f"✅ {item.name} выставлен на рынок за {price}💰")
        return

    if state and state["step"] == "class":
        await message.answer("Выберите класс кнопкой выше ☝️")
        return

    if state and state["step"] == "companion_name":
        state["companion_name"] = text
        state["step"] = "companion_desc"
        await message.answer("Введите описание стража (или /skip):")
        return

    if state and state["step"] == "companion_desc":
        state["companion_description"] = text
        await _finish_creation(message, state)
        return

    await message.answer("Неизвестная команда. /help")

# ─── /profile ───────────────────────────────────────────────

@router.message(Command("profile"))
async def cmd_profile(message: Message):
    char = await _ensure_char(message)
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
    if char.companion:
        c = char.companion
        text += (
            f"\n\n🛡 Страж: {c.name}\n"
            f"❤️ {c.hp}/{c.max_hp} | ⚔️ {c.attack_min}-{c.attack_max}"
        )
    await message.answer(text, reply_markup=main_menu())

# ─── /inventory ─────────────────────────────────────────────

@router.message(Command("inventory"))
async def cmd_inventory(message: Message):
    char = await _ensure_char(message)
    if not char:
        return
    await _show_inventory(message, char, 0)

async def _show_inventory(message_or_cb, char: Character, page: int):
    text = f"🎒 Инвентарь **{char.name}**\n"
    if not char.inventory:
        text += "Пусто."
    await _edit_or_answer(message_or_cb, text, reply_markup=inventory_pages(char.inventory, page))

# ─── /characters ────────────────────────────────────────────

@router.message(Command("characters"))
async def cmd_characters(message: Message):
    chars = await storage.load_characters(message.from_user.id)
    if not chars:
        await message.answer("Нет персонажей. /create")
        return
    current = chars[0]
    await message.answer("Ваши персонажи:", reply_markup=char_list(chars, current.name))

# ─── /location ──────────────────────────────────────────────

@router.message(Command("location"))
async def cmd_location(message: Message):
    char = await _ensure_char(message)
    if not char:
        return
    await message.answer("🗺 Выберите локацию:", reply_markup=location_list())

# ─── /market ────────────────────────────────────────────────

@router.message(Command("market"))
async def cmd_market(message: Message):
    listings = MARKET.get_active_listings()
    if not listings:
        await message.answer("🏪 Рынок пуст.", reply_markup=main_menu())
        return
    await _show_market(message, listings, 0)

async def _show_market(message_or_cb, listings: list, page: int):
    text = f"🏪 Рынок — стр. {page+1}\n"
    per_page = 5
    start = page * per_page
    batch = listings[start:start + per_page]
    for i, listing in enumerate(batch, start=start+1):
        item = listing.item
        text += f"\n{i}. {item.name} [{item.rarity.value}] — {listing.price}💰"
    await _edit_or_answer(message_or_cb, text, reply_markup=market_listings_kb(listings, page))

# ─── Callback: main_menu ────────────────────────────────────

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu())

@router.callback_query(F.data == "profile")
async def cb_profile(callback: CallbackQuery):
    await callback.answer()
    char = await _get_char(callback.from_user.id)
    if not char:
        await callback.message.edit_text("Нет персонажа. /create")
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
    await callback.message.edit_text(text, reply_markup=main_menu())

@router.callback_query(F.data == "inventory")
async def cb_inventory(callback: CallbackQuery):
    await callback.answer()
    char = await _get_char(callback.from_user.id)
    if not char:
        await callback.message.edit_text("Нет персонажа. /create")
        return
    await _show_inventory(callback, char, 0)

@router.callback_query(F.data == "location")
async def cb_location(callback: CallbackQuery):
    await callback.answer()
    char = await _get_char(callback.from_user.id)
    if not char:
        await callback.message.edit_text("Нет персонажа. /create", reply_markup=main_menu())
        return
    await callback.message.edit_text("🗺 Выберите локацию:", reply_markup=location_list())

@router.callback_query(F.data == "market")
async def cb_market(callback: CallbackQuery):
    await callback.answer()
    listings = MARKET.get_active_listings()
    if not listings:
        await callback.message.edit_text("🏪 Рынок пуст.", reply_markup=main_menu())
        return
    await _show_market(callback, listings, 0)

@router.callback_query(F.data == "market_refresh")
async def cb_market_refresh(callback: CallbackQuery):
    await cb_market(callback)

@router.callback_query(F.data == "char_list")
async def cb_char_list(callback: CallbackQuery):
    await callback.answer()
    chars = await storage.load_characters(callback.from_user.id)
    if not chars:
        await callback.message.edit_text("Нет персонажей. /create")
        return
    await callback.message.edit_text("Ваши персонажи:", reply_markup=char_list(chars, chars[0].name))

# ─── Callback: класс ────────────────────────────────────────

@router.callback_query(F.data.startswith("class_"))
async def cb_class_select(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    state = creation_states.get(uid)
    if not state or state["step"] != "class":
        await callback.message.edit_text("Создание не активно. /create")
        return
    cls_key = callback.data[len("class_"):]
    if cls_key not in CLASSES:
        await callback.message.edit_text("Неверный класс.")
        return
    state["class_key"] = cls_key
    if cls_key == "leader":
        state["step"] = "companion_name"
        await callback.message.edit_text(
            "Лидер может призвать стража.\nВведите имя стража:"
        )
    else:
        await _finish_creation(callback.message, state)

# ─── Callback: локация → подтверждение рейда ───────────────

@router.callback_query(F.data.startswith("loc_"))
async def cb_location_select(callback: CallbackQuery):
    await callback.answer()
    key = callback.data[len("loc_"):]
    loc = get_location(key)
    if not loc:
        await callback.message.edit_text("Локация не найдена.")
        return
    char = await _get_char(callback.from_user.id)
    if not char:
        await callback.message.edit_text("Нет персонажа. /create")
        return
    if char.in_raid:
        await callback.message.edit_text("Вы уже в рейде! Завершите его.")
        return
    if not char.can_raid():
        rem = char.raid_cooldown_remaining()
        hrs = int(rem // 3600)
        mins = int((rem % 3600) // 60)
        await callback.message.edit_text(
            f"⏳ До следующего рейда {hrs}ч {mins}м."
        )
        return
    text = (
        f"🗺 {loc.name} (ур. {loc.recommended_level})\n"
        f"{loc.description}\n"
        f"☠️ Опасность: {loc.danger}/10\n"
        f"💰 Награда: {loc.gold_min}-{loc.gold_max} золота, {loc.exp_reward} опыта"
    )
    await callback.message.edit_text(text, reply_markup=confirm_raid(key))

@router.callback_query(F.data.startswith("raid_start_"))
async def cb_raid_start(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    key = callback.data[len("raid_start_"):]
    loc = get_location(key)
    if not loc:
        await callback.message.edit_text("Локация не найдена.")
        return
    char = await _get_char(uid)
    if not char or char.in_raid:
        return

    raid_id = str(uuid.uuid4())[:8]
    session = create_raid(char, loc, raid_id)
    session.status = RaidStatus.IN_PROGRESS
    char.in_raid = True
    await _save_char(char)
    active_raids[uid] = session

    await _show_encounter(callback.message, char, session)

async def _show_encounter(msg_or_cb, char: Character, session: RaidSession):
    enc = session.encounters[session.current_encounter]
    total = len(session.encounters)
    cur = session.current_encounter + 1
    text = (
        f"⚔️ Рейд {cur}/{total}\n\n"
        f"{_enemy_status_line(enc)}\n\n"
        f"{_char_status_line(char)}\n"
    )
    if char.companion:
        text += f"🛡 Страж: ❤️ {char.companion.hp}/{char.companion.max_hp}\n"
    await _edit_or_answer(msg_or_cb, text, reply_markup=raid_actions())

# ─── Callback: действия в бою ───────────────────────────────

@router.callback_query(F.data == "raid_attack")
async def cb_raid_attack(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    session = active_raids.get(uid)
    if not session or session.status != RaidStatus.IN_PROGRESS:
        return
    char = await _get_char(uid)
    if not char:
        return
    await _do_turn(callback, char, session, nn_modifiers=None)

@router.callback_query(F.data == "raid_nn")
async def cb_raid_nn(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    session = active_raids.get(uid)
    if not session or session.status != RaidStatus.IN_PROGRESS:
        return
    char = await _get_char(uid)
    if not char:
        return
    enc = session.encounters[session.current_encounter]
    nn_data = await call_narrative_api(
        location=session.location_key,
        turn=enc.turn,
        player={"name": char.name, "class": char.class_key, "hp": char.hp, "max_hp": char.max_hp},
        enemies=[{"name": enc.enemy_template["name"], "hp": enc.enemy_hp, "max_hp": enc.enemy_max_hp}],
        action_history=[],
    )
    await _do_turn(callback, char, session, nn_modifiers=nn_data.get("actions") if nn_data else None)

async def _do_turn(callback: CallbackQuery, char: Character, session: RaidSession, nn_modifiers):
    uid = callback.from_user.id
    enc = session.encounters[session.current_encounter]

    player_attack, enemy_attack, finished = process_encounter_turn(
        session, char, nn_modifiers=nn_modifiers,
    )

    total = len(session.encounters)
    cur = session.current_encounter + 1
    text = f"⚔️ Рейд {cur}/{total}\n\n"
    text += _enemy_status_line(enc) + "\n\n"

    text += _attack_desc("Вы", player_attack) + "\n"

    if enemy_attack:
        text += _attack_desc(f"👾 {enc.enemy_template['name']}", enemy_attack, "наносит") + "\n"

    text += f"\n{_char_status_line(char)}"
    if char.companion:
        text += f"\n🛡 Страж: ❤️ {char.companion.hp}/{char.companion.max_hp}"

    if finished:
        if char.hp <= 0:
            text += "\n\n💀 **Вы погибли!**"
            session.status = RaidStatus.FAILED
            char.in_raid = False
            char.release_companion()
            char.alive = False
            await _save_char(char)
            active_raids.pop(uid, None)
            await callback.message.edit_text(text, reply_markup=raid_failed())
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
                    char.release_companion()
                    await _save_char(char)
                    loot_text = ""
                    if loot:
                        loot_text = "\n🎁 Добыча: " + ", ".join(f"{it.name} [{it.rarity.value}]" for it in loot)
                    text += f"\n\n🏆 **Рейд пройден!**{loot_text}"
                    active_raids.pop(uid, None)
                    await callback.message.edit_text(text, reply_markup=raid_done())
                else:
                    text += "\n\n⚠️ Ошибка: локация не найдена."
                    await callback.message.edit_text(text, reply_markup=main_menu())
            else:
                await callback.message.edit_text(text, reply_markup=raid_next())
        else:
            await callback.message.edit_text(text, reply_markup=raid_actions())
    else:
        await callback.message.edit_text(text, reply_markup=raid_actions())

# ─── Callback: рейд — следующий враг ───────────────────────

@router.callback_query(F.data == "raid_next")
async def cb_raid_next(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    session = active_raids.get(uid)
    if not session:
        return
    char = await _get_char(uid)
    if not char:
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
            active_raids.pop(uid, None)
            await callback.message.edit_text(text, reply_markup=raid_done())
        return
    await _show_encounter(callback.message, char, session)

# ─── Callback: рейд — сбежать ──────────────────────────────

@router.callback_query(F.data == "raid_leave")
async def cb_raid_leave(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    session = active_raids.pop(uid, None)
    if not session:
        return
    char = await _get_char(uid)
    if char:
        char.in_raid = False
        char.release_companion()
        await _save_char(char)
    await callback.message.edit_text("🏃 Вы сбежали из рейда!", reply_markup=main_menu())

# ─── Callback: инвентарь — страницы + предмет ──────────────

@router.callback_query(F.data.startswith("inv_page_"))
async def cb_inv_page(callback: CallbackQuery):
    await callback.answer()
    char = await _get_char(callback.from_user.id)
    if not char:
        return
    page = int(callback.data[len("inv_page_"):])
    await _show_inventory(callback, char, page)

@router.callback_query(F.data.startswith("inv_item_"))
async def cb_inv_item(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    item_uid = callback.data[len("inv_item_"):]
    char = await _get_char(uid)
    if not char:
        return
    item = next((i for i in char.inventory if i.uid == item_uid), None)
    if not item:
        await callback.message.edit_text("Предмет не найден.")
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
    await callback.message.edit_text(text, reply_markup=item_actions_kb(item_uid))

@router.callback_query(F.data.startswith("inv_equip_"))
async def cb_inv_equip(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    item_uid = callback.data[len("inv_equip_"):]
    char = await _get_char(uid)
    if not char:
        return
    item = next((i for i in char.inventory if i.uid == item_uid), None)
    if not item:
        await callback.message.edit_text("Предмет не найден.")
        return
    ok = char.equip(item)
    if not ok:
        await callback.message.edit_text("Нельзя надеть этот предмет (уровень/класс/слот).")
        return
    await _save_char(char)
    await callback.message.edit_text(f"✅ {item.name} экипирован!", reply_markup=main_menu())

@router.callback_query(F.data.startswith("inv_unequip_"))
async def cb_inv_unequip(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    item_uid = callback.data[len("inv_unequip_"):]
    char = await _get_char(uid)
    if not char:
        return
    item = None
    for slot in ("weapon", "armor", "accessory"):
        eq = getattr(char.equipment, slot, None)
        if eq and eq.uid == item_uid:
            item = eq
            break
    if not item:
        await callback.message.edit_text("Предмет не экипирован.")
        return
    ok = char.unequip(item)
    if not ok:
        await callback.message.edit_text("Не удалось снять предмет.")
        return
    await _save_char(char)
    await callback.message.edit_text(f"📦 {item.name} снят!", reply_markup=main_menu())

@router.callback_query(F.data.startswith("inv_drop_"))
async def cb_inv_drop(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    item_uid = callback.data[len("inv_drop_"):]
    char = await _get_char(uid)
    if not char:
        return
    item = next((i for i in char.inventory if i.uid == item_uid), None)
    if not item:
        await callback.message.edit_text("Предмет не найден.")
        return
    char.inventory.remove(item)
    await _save_char(char)
    await callback.message.edit_text(f"🗑 {item.name} выброшен.", reply_markup=main_menu())

@router.callback_query(F.data.startswith("inv_sell_"))
async def cb_inv_sell(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    item_uid = callback.data[len("inv_sell_"):]
    char = await _get_char(uid)
    if not char:
        return
    item = next((i for i in char.inventory if i.uid == item_uid), None)
    if not item:
        await callback.message.edit_text("Предмет не найден.")
        return
    pending_sells[uid] = {"step": "price", "item_uid": item_uid, "char": char}
    await callback.message.edit_text(f"💰 Введите цену для {item.name}:")

# ─── Callback: рынок — страницы + покупка ──────────────────

@router.callback_query(F.data.startswith("market_page_"))
async def cb_market_page(callback: CallbackQuery):
    await callback.answer()
    page = int(callback.data[len("market_page_"):])
    listings = MARKET.get_active_listings()
    await _show_market(callback, listings, page)

@router.callback_query(F.data.startswith("market_buy_"))
async def cb_market_buy(callback: CallbackQuery):
    await callback.answer()
    lid = callback.data[len("market_buy_"):]
    listing = MARKET.listings.get(lid)
    if not listing or not listing.active:
        await callback.message.edit_text("Объявление уже неактивно.", reply_markup=main_menu())
        return
    text = (
        f"🏪 **{listing.item.name}** — {listing.price}💰\n"
        f"Продавец: {listing.character_name}\n\n"
        f"Подтвердите покупку:"
    )
    await callback.message.edit_text(text, reply_markup=market_confirm(lid))

@router.callback_query(F.data.startswith("market_confirm_"))
async def cb_market_confirm(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    lid = callback.data[len("market_confirm_"):]
    char = await _get_char(uid)
    if not char:
        await callback.message.edit_text("Нет персонажа.")
        return

    success, item, seller_id, seller_name, seller_earns = MARKET.buy_listing(lid, char)
    if not success:
        await callback.message.edit_text(
            "Не удалось купить: недостаточно золота или объявление неактивно.",
            reply_markup=main_menu(),
        )
        return

    char.inventory.append(item)
    await _save_char(char)
    await MARKET.persist_deactivate(storage, lid)
    await storage.credit_gold(seller_id, seller_name, seller_earns)

    await callback.message.edit_text(
        f"✅ Куплен {item.name} за {listing.price}💰",
        reply_markup=main_menu(),
    )

@router.callback_query(F.data.startswith("market_cancel_"))
async def cb_market_cancel(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    lid = callback.data[len("market_cancel_"):]
    ok = MARKET.cancel_listing(lid, uid)
    if ok:
        await MARKET.persist_deactivate(storage, lid)
        await callback.message.edit_text("Объявление отменено.", reply_markup=main_menu())
    else:
        await callback.message.edit_text("Не удалось отменить.", reply_markup=main_menu())

# ─── Callback: персонажи ───────────────────────────────────

@router.callback_query(F.data == "char_create")
async def cb_char_create(callback: CallbackQuery):
    await callback.answer()
    await cmd_create(callback.message)

@router.callback_query(F.data.startswith("char_switch_"))
async def cb_char_switch(callback: CallbackQuery):
    await callback.answer()
    uid = callback.from_user.id
    name = callback.data[len("char_switch_"):]
    chars = await storage.load_characters(uid)
    target = next((c for c in chars if c.name == name), None)
    if not target:
        await callback.message.edit_text("Персонаж не найден.")
        return
    await callback.message.edit_text(
        f"✅ Переключено на **{target.name}**",
        reply_markup=main_menu(),
    )

# ─── Helpers ────────────────────────────────────────────────

async def _edit_or_answer(msg_or_cb, text: str, **kwargs):
    if isinstance(msg_or_cb, CallbackQuery):
        try:
            await msg_or_cb.message.edit_text(text, **kwargs)
        except Exception:
            await msg_or_cb.message.answer(text, **kwargs)
    else:
        await msg_or_cb.answer(text, **kwargs)
