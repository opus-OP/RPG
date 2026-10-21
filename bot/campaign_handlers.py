"""Telegram-поток ивент-кампаний: /campaign_begin → герой → (лобби) → комнаты → босс → конфеты.

Бой, ходы, таймауты и уведомления — общие с обычными рейдами (bot/handlers.py);
здесь только то, что относится к кампании: выбор локации, создание временного героя,
лобби, комнаты (сундуки, находки, артефакты, ловушки, отдых), безумие и итоговая
команда с конфетами для основного бота.

Тексты намеренно без Markdown: имена героев задаёт игрок, а `_` и `*` ломают разметку.
"""

from __future__ import annotations

import logging
import random as _random
import time
from secrets import token_hex
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

import bot.handlers as H
import config
from bot.keyboards import raid_done, raid_failed
from core.artifacts import ARTIFACTS, MAX_ARTIFACTS
from core.campaign import (
    CampaignLocation, apply_find, artifact_line, artifact_text, award_fight_exp, boss_reward,
    build_reward_command, chest_candies, create_hero, generate_campaign, get_campaign_location,
    get_campaign_locations, heal_fraction, hero_owner, nonlethal_damage, room_hint,
    roll_artifact_offer,
)
from core.classes import CLASSES
from core.combat import calc_damage
from core.raid import (
    RaidSession, RaidStatus, advance_turn_core, build_initiative_order, session_from_dict,
)
from utils.rng import roll_chance
from utils.validators import validate_character_name

log = logging.getLogger("rpg.campaign")

_lobbies: dict[str, dict] = {}
CREATE_TTL = 600  # сек: сколько живёт незавершённое создание героя


# ═══════════════════════════════════════════════════════════════
# Клавиатуры
# ═══════════════════════════════════════════════════════════════

def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


def kb_locations() -> InlineKeyboardMarkup:
    rows = [[(f"{loc.icon} {loc.name} — {loc.price}🍬", f"cmp_loc_{key}")]
            for key, loc in get_campaign_locations().items()]
    rows.append([("Назад", "main_menu")])
    return _kb(rows)


def kb_loc_card(key: str) -> InlineKeyboardMarkup:
    return _kb([
        [("⚔️ Идти одному", f"cmp_solo_{key}")],
        [(f"👥 Собрать отряд (до {config.MAX_GROUP_SIZE})", f"cmp_party_{key}")],
        [("◀️ К списку", "cmp_menu")],
    ])


def kb_classes() -> InlineKeyboardMarkup:
    return _kb([[(c.name, f"cmp_class_{k}") for k, c in list(CLASSES.items())[i:i + 2]]
                for i in range(0, len(CLASSES), 2)])


def kb_lobby(is_owner: bool) -> InlineKeyboardMarkup:
    rows = [[("🔄 Обновить", "cmp_lobby_refresh")]]
    if is_owner:
        rows.append([("▶️ Начать кампанию", "cmp_lobby_start")])
    rows.append([("🏃 Выйти", "cmp_lobby_leave")])
    return _kb(rows)


def kb_go(room_idx: int, label: str = "🚪 Дальше") -> InlineKeyboardMarkup:
    return _kb([[(label, f"cmp_go_{room_idx}")]])


# ═══════════════════════════════════════════════════════════════
# Тексты
# ═══════════════════════════════════════════════════════════════

def _loc_card(loc: CampaignLocation) -> str:
    return (
        f"{loc.icon} {loc.name}\n"
        f"Сложность: {loc.difficulty} | герои стартуют с {loc.start_level} уровня\n\n"
        f"{loc.description}\n\n"
        f"👑 Награда за босса: {loc.price}🍬 (каждому участнику)\n"
        f"📦 Сундуки: {loc.chest_candy[0]}–{loc.chest_candy[1]}🍬 каждый (6–8 за поход)\n"
        f"Вход свободный. Герой создаётся только для этой кампании."
    )


def _lobby_text(lobby: dict) -> str:
    loc = get_campaign_location(lobby["loc"])
    lines = [f"🎮 Кампания: {loc.icon} {loc.name} — сбор отряда", f"🔑 Код: {lobby['code']}", "", "Отряд:"]
    for uid, name in lobby["members"].items():
        mark = " (лидер)" if uid == lobby["owner"] else ""
        lines.append(f"• {name}{mark}")
    lines += ["", f"Мест: {len(lobby['members'])}/{config.MAX_GROUP_SIZE}",
              f"Друзья заходят командой: /campaign_join {lobby['code']}"]
    return "\n".join(lines)


def status_text(session: RaidSession, hero) -> str:
    camp = session.campaign
    loc = get_campaign_location(camp["loc"])
    total = len(camp["rooms"])
    idx = max(0, camp["room_idx"] + 1)
    candies = camp["candies"].get(str(abs(hero.owner_tg_id)), 0)
    lines = [
        f"{loc.icon} {loc.name} — комната {idx}/{total}",
        f"🧙 {hero.name} ({CLASSES[hero.class_key].name}), ур. {hero.level}",
        H._char_status_line(hero),
        f"📊 Сила {hero.stats.strength} | Ловк {hero.stats.agility} | Инт {hero.stats.intelligence}",
        f"🎭 Артефакты ({len(hero.artifacts)}/{MAX_ARTIFACTS}): {artifact_line(hero)}",
        f"🍬 Собрано конфет: {candies}",
    ]
    for k in hero.artifacts:
        if k in ARTIFACTS:
            a = ARTIFACTS[k]
            lines.append(f"   {a.icon} +{a.plus} / −{a.minus}")
    return "\n".join(lines)


async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE, session: RaidSession):
    uid = update.effective_user.id
    hero = await H._load_raid_char(uid, session, context)
    if not hero:
        await H._reply(update, "Вы не участвуете в этой кампании.", parse_mode=None)
        return
    await H._reply(update, status_text(session, hero), parse_mode=None)


# ═══════════════════════════════════════════════════════════════
# Начало: локация → режим → герой
# ═══════════════════════════════════════════════════════════════

async def _purge_heroes(uid: int):
    """Удаляет «осиротевших» героев прошлых кампаний (владелец −uid)."""
    for c in await H.storage.load_characters(hero_owner(uid)):
        await H.storage.delete_character(hero_owner(uid), c.name)


async def _blocked(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Нельзя начинать/вступать, если игрок уже в рейде или в лобби."""
    if await H._get_session(context):
        await H._reply(update, "Вы уже в рейде или кампании. Продолжить: /raid, выйти: /forfeit.", parse_mode=None)
        return True
    code = context.user_data.get("cmp_lobby")
    if code and code in _lobbies:
        await H._reply(update, "Вы уже в лобби кампании. Выйдите из него кнопкой «Выйти».", parse_mode=None)
        return True
    context.user_data.pop("cmp_lobby", None)
    return False


async def cmd_begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await _blocked(update, context):
        return
    context.user_data.pop("cmp_create", None)
    lines = ["🎃 Ивент-кампании: пройдите цепочку комнат и победите босса.",
             "За сундуки и босса вы получаете конфеты 🍬. Выберите локацию:"]
    await H._reply(update, "\n".join(lines), reply_markup=kb_locations(), parse_mode=None)


@H._auth_cb
async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await cmd_begin(update, context)


@H._auth_cb
async def cb_loc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    loc = get_campaign_location(query.data[len("cmp_loc_"):])
    if not loc:
        await query.edit_message_text("Локация не найдена.")
        return
    await H._reply(update, _loc_card(loc), reply_markup=kb_loc_card(loc.key), parse_mode=None)


@H._auth_cb
async def cb_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode, key = query.data[len("cmp_"):].split("_", 1)
    loc = get_campaign_location(key)
    if not loc:
        await query.edit_message_text("Локация не найдена.")
        return
    if await _blocked(update, context):
        return
    context.user_data["cmp_create"] = {"step": "name", "mode": mode, "loc": key, "ts": time.time()}
    await H._reply(update, f"{loc.icon} {loc.name}\n\nКак зовут вашего героя? (2–24 символа)", parse_mode=None)


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await H._reply(update, "Использование: /campaign_join <код>", parse_mode=None)
        return
    if await _blocked(update, context):
        return
    code = context.args[0].upper()
    lobby = _lobbies.get(code)
    if not lobby or lobby["started"]:
        await H._reply(update, "Лобби не найдено или кампания уже началась.", parse_mode=None)
        return
    if len(lobby["members"]) >= config.MAX_GROUP_SIZE:
        await H._reply(update, "Отряд уже полон.", parse_mode=None)
        return
    loc = get_campaign_location(lobby["loc"])
    context.user_data["cmp_create"] = {"step": "name", "mode": "join", "loc": lobby["loc"],
                                       "code": code, "ts": time.time()}
    await H._reply(update, f"Вы вступаете в отряд: {loc.icon} {loc.name}\n\nКак зовут вашего героя? (2–24 символа)",
                   parse_mode=None)


async def handle_create_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Текстовый шаг создания героя. True — сообщение обработано."""
    state = context.user_data.get("cmp_create")
    if not state:
        return False
    if time.time() - state.get("ts", 0) > CREATE_TTL:
        context.user_data.pop("cmp_create", None)
        return False
    if state["step"] != "name":
        return False
    if not validate_character_name(text):
        await update.message.reply_text(
            "Имя должно быть от 2 до 24 символов: буквы, цифры, пробел, «_» и «-» (без пробелов по краям).")
        return True
    state["name"] = text
    state["step"] = "class"
    lines = ["Выберите класс героя:"]
    lines += [f"• {c.name}: {c.description}" for c in CLASSES.values()]
    await update.message.reply_text("\n".join(lines), reply_markup=kb_classes())
    return True


@H._auth_cb
async def cb_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    state = context.user_data.get("cmp_create")
    if not state or state.get("step") != "class":
        await query.edit_message_text("Создание героя не активно. /campaign_begin")
        return
    class_key = query.data[len("cmp_class_"):]
    if class_key not in CLASSES:
        await query.edit_message_text("Неверный класс.")
        return
    loc = get_campaign_location(state["loc"])
    if not loc:
        context.user_data.pop("cmp_create", None)
        await query.edit_message_text("Локация не найдена.")
        return
    mode = state["mode"]
    lobby = _lobbies.get(state.get("code", "")) if mode == "join" else None
    if mode == "join" and (not lobby or lobby["started"] or len(lobby["members"]) >= config.MAX_GROUP_SIZE):
        context.user_data.pop("cmp_create", None)
        await query.edit_message_text("Лобби уже недоступно.")
        return

    await _purge_heroes(uid)
    hero = create_hero(uid, state["name"], class_key, loc)
    await H.storage.save_character(hero)
    context.user_data.pop("cmp_create", None)

    if mode == "solo":
        await start_campaign(context, loc, {uid: hero}, query.message.chat_id,
                             query.message.message_thread_id, query)
        return

    if mode == "party":
        code = token_hex(3).upper()
        lobby = _lobbies[code] = {
            "code": code, "owner": uid, "loc": loc.key, "members": {uid: hero.name},
            "chat_id": query.message.chat_id, "thread_id": query.message.message_thread_id,
            "msg_id": getattr(query.message, "message_id", None), "started": False,
        }
        context.user_data["cmp_lobby"] = code
        await H._reply(update, _lobby_text(lobby), reply_markup=kb_lobby(True), parse_mode=None)
        return

    # join
    lobby["members"][uid] = hero.name
    context.user_data["cmp_lobby"] = state["code"]
    await H._reply(update, _lobby_text(lobby), reply_markup=kb_lobby(False), parse_mode=None)
    await _refresh_lobby_message(context, lobby)


async def _refresh_lobby_message(context, lobby: dict):
    """Обновляет сообщение лидера (если получится) — иначе он нажмёт «Обновить»."""
    try:
        await context.bot.edit_message_text(
            _lobby_text(lobby), chat_id=lobby["chat_id"], message_id=lobby["msg_id"],
            reply_markup=kb_lobby(True))
    except Exception:
        log.debug("Не удалось обновить сообщение лобби", exc_info=True)


# ─── Лобби ──────────────────────────────────────────────────

def _my_lobby(context) -> Optional[dict]:
    code = context.user_data.get("cmp_lobby")
    return _lobbies.get(code) if code else None


@H._auth_cb
async def cb_lobby_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    lobby = _my_lobby(context)
    if not lobby:
        await update.callback_query.edit_message_text("Лобби не найдено.")
        return
    is_owner = lobby["owner"] == update.effective_user.id
    await H._reply(update, _lobby_text(lobby), reply_markup=kb_lobby(is_owner), parse_mode=None)


@H._auth_cb
async def cb_lobby_leave(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    lobby = _my_lobby(context)
    context.user_data.pop("cmp_lobby", None)
    if not lobby:
        await query.edit_message_text("Лобби не найдено.")
        return
    lobby["members"].pop(uid, None)
    await _purge_heroes(uid)
    if not lobby["members"]:
        _lobbies.pop(lobby["code"], None)
    elif lobby["owner"] == uid:
        lobby["owner"] = next(iter(lobby["members"]))
        await _refresh_lobby_message(context, lobby)
    else:
        await _refresh_lobby_message(context, lobby)
    await query.edit_message_text("Вы покинули лобби кампании.", reply_markup=_kb([[("В меню", "main_menu")]]))


@H._auth_cb
async def cb_lobby_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = update.effective_user.id
    lobby = _my_lobby(context)
    if not lobby:
        await query.answer()
        await query.edit_message_text("Лобби не найдено.")
        return
    if lobby["owner"] != uid:
        await query.answer("Начать может только лидер отряда.", show_alert=True)
        return
    await query.answer()
    if lobby["started"]:
        return
    loc = get_campaign_location(lobby["loc"])
    heroes = {}
    for muid, name in lobby["members"].items():
        hero = await H.storage.load_character_by_name(hero_owner(muid), name)
        if hero:
            heroes[muid] = hero
    if uid not in heroes:
        await query.edit_message_text("Герой лидера не найден. Создайте лобби заново: /campaign_begin")
        return
    lobby["started"] = True
    _lobbies.pop(lobby["code"], None)
    for muid in heroes:
        H_ud = context.application.user_data[muid]
        H_ud.pop("cmp_lobby", None)
    await start_campaign(context, loc, heroes, query.message.chat_id, query.message.message_thread_id, query)


# ═══════════════════════════════════════════════════════════════
# Старт кампании
# ═══════════════════════════════════════════════════════════════

async def start_campaign(context, loc: CampaignLocation, heroes: dict, chat_id: int,
                         thread_id: Optional[int], query=None) -> RaidSession:
    """Создаёт сессию кампании для готовых героев и показывает вход в первую комнату."""
    leader_uid = next(iter(heroes))
    state, encounters = generate_campaign(loc, len(heroes))
    raid_id = f"cmp_{leader_uid}_{int(time.time())}"
    session = RaidSession(
        raid_id=raid_id, location_key=loc.key, status=RaidStatus.IN_PROGRESS,
        encounters=encounters, group_id=raid_id if len(heroes) > 1 else None,
        chat_id=chat_id, thread_id=thread_id, campaign=state,
    )
    for uid, hero in heroes.items():
        hero.alive = True
        hero.in_raid = True
        hero.madness = False
        hero.summon_companion()
        await H.storage.save_character(hero)
        session.participant_names[uid] = hero.name
        state["heroes"][str(uid)] = hero.name
        state["candies"][str(uid)] = 0
    app_ud = context.application.user_data
    for uid in heroes:
        app_ud[uid]["raid_id"] = raid_id
        app_ud[uid].pop("cmp_lobby", None)
    await H._save_session(context, session)

    names = ", ".join(h.name for h in heroes.values())
    text = (f"{loc.icon} {loc.name}\n{loc.intro}\n\n"
            f"Отряд: {names}\n"
            f"Впереди {len(state['rooms'])} комнат, в конце — {get_campaign_mob_name(loc)}.\n"
            f"Свои артефакты и состояние можно посмотреть командой /raid.")
    if query is not None:
        try:
            await query.edit_message_text(text)
        except Exception:
            log.debug("edit_message_text при старте кампании не удался", exc_info=True)
    else:
        await H._notify_participants(context, session, text)
    await H._notify_participants(context, session, "Готовы? Вход открыт.", kb_go(-1, "🚪 Войти"))
    return session


def get_campaign_mob_name(loc: CampaignLocation) -> str:
    from core.campaign import get_campaign_mob
    mob = get_campaign_mob(loc.boss_id)
    return f"босс: {mob['name']}" if mob else "босс"


# ═══════════════════════════════════════════════════════════════
# Движение по комнатам
# ═══════════════════════════════════════════════════════════════

async def _guard(update: Update, context):
    """Общая проверка для кнопок кампании: возвращает (session, hero) или (None, None)."""
    query = update.callback_query
    uid = update.effective_user.id
    session = await H._get_session(context)
    if not session or not session.campaign:
        await query.answer("Кампания не найдена или уже закончилась.", show_alert=True)
        return None, None
    hero = await H._load_raid_char(uid, session, context)
    if not hero:
        await query.answer("Вы не участвуете в этой кампании.", show_alert=True)
        return None, None
    return session, hero


async def _scout_next(session: RaidSession, chars: dict) -> str:
    camp = session.campaign
    nxt = camp["room_idx"] + 1
    if nxt >= len(camp["rooms"]):
        return ""
    for c in chars.values():
        if c.alive and c.hp > 0 and roll_chance(c.scout_chance):
            camp["scouted"] = nxt
            return f"\n\n👁 Глаз пророка: впереди — {room_hint(camp['rooms'][nxt])}"
    return ""


async def _send_go(context, session: RaidSession, text: str):
    """Сообщение с кнопкой «Дальше» (+ возможная разведка следующей комнаты)."""
    chars = await H._get_participant_chars(context, session)
    text += await _scout_next(session, chars)
    await H._save_session(context, session)
    await H._notify_participants(context, session, text, kb_go(session.campaign["room_idx"]))


@H._auth_cb
async def cb_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = update.effective_user.id
    token = int(query.data[len("cmp_go_"):])
    session = await H._get_session(context)
    if not session or not session.campaign:
        await query.answer("Кампания уже закончилась.", show_alert=True)
        return
    async with H._raid_lock(session.raid_id):
        session = await H._get_session(context)
        if not session or not session.campaign:
            await query.answer()
            return
        camp = session.campaign
        if token != camp["room_idx"] or camp["in_fight"] or camp["pending"]:
            await query.answer("Эта кнопка уже неактуальна.")
            return
        hero = await H._load_raid_char(uid, session, context)
        if not hero:
            await query.answer("Вы не участвуете в этой кампании.", show_alert=True)
            return
        if not hero.alive or hero.hp <= 0:
            await query.answer("Павший герой не ведёт отряд — ждите товарищей.", show_alert=True)
            return
        await query.answer()
        try:
            await query.edit_message_text("🚪 Отряд идёт дальше…")
        except Exception:
            log.debug("edit_message_text не удался", exc_info=True)
        await _enter_next_room(context, session)


async def _enter_next_room(context, session: RaidSession):
    camp = session.campaign
    loc = get_campaign_location(camp["loc"])
    camp["room_idx"] += 1
    idx = camp["room_idx"]
    if idx >= len(camp["rooms"]):
        await H._complete_raid(context, session)
        return
    room = camp["rooms"][idx]
    scouted = camp.get("scouted") == idx
    t = room["type"]
    head = f"Комната {idx + 1}/{len(camp['rooms'])}"

    if t in ("fight", "boss"):
        await _start_fight(context, session, room, scouted, head)
        return

    if t == "chest":
        camp["pending"] = {"type": "chest", "kind": room["kind"]}
        text = f"{head}\n📦 В углу стоит сундук. Открыть?"
        if scouted:
            text += f"\n\n👁 Глаз пророка: {room_hint(room)}"
        await H._save_session(context, session)
        await H._notify_participants(context, session, text,
                                     _kb([[("📦 Открыть", "cmp_open"), ("🚶 Пройти мимо", "cmp_pass")]]))
        return

    if t == "find":
        flavor = loc.finds[room["flavor"]] if loc.finds else {"kind": "nothing", "text": "Пустая комната.", "use": "🔍 Осмотреть"}
        camp["pending"] = {"type": "find", "flavor": room["flavor"]}
        await H._save_session(context, session)
        await H._notify_participants(context, session, f"{head}\n{flavor['text']}",
                                     _kb([[(flavor["use"], "cmp_use"), ("🚶 Пройти мимо", "cmp_pass")]]))
        return

    if t == "artifact":
        await _open_artifact_room(context, session, head)
        return

    if t == "trap":
        lines = [f"{head}\n🪤 Коридор усеян ловушками."]
        chars = await H._get_participant_chars(context, session)
        if scouted:
            lines.append("👁 Глаз пророка заметил механизмы — отряд обходит их без потерь.")
        else:
            frac = 0.12 + 0.02 * loc.tier
            for c in chars.values():
                if not c.alive or c.hp <= 0:
                    continue
                if roll_chance(min(0.9, c.dodge_chance + 0.30)):
                    lines.append(f"✅ {c.name} ловко проскакивает.")
                else:
                    dmg = nonlethal_damage(c, frac)
                    lines.append(f"💥 {c.name} попадает в ловушку: −{dmg}❤️")
                    await H._save_char(c)
        await _send_go(context, session, "\n".join(lines))
        return

    if t == "rest":
        lines = [f"{head}\n🔥 Тихий уголок: можно перевести дух и перевязать раны."]
        chars = await H._get_participant_chars(context, session)
        for c in chars.values():
            if c.alive and c.hp > 0:
                got = heal_fraction(c, 0.40)
                lines.append(f"💚 {c.name}: +{got}❤️ ({c.hp}/{c.max_hp})")
                await H._save_char(c)
        lines.append("Дальше — покои босса.")
        await _send_go(context, session, "\n".join(lines))
        return

    await _send_go(context, session, f"{head}\nПустая комната.")


async def _start_fight(context, session: RaidSession, room: dict, scouted: bool, head: str):
    camp = session.campaign
    enc = session.encounters[room["enc"]]
    session.current_encounter = room["enc"]
    chars = await H._get_participant_chars(context, session)
    alive = {u: c for u, c in chars.items() if c.alive and c.hp > 0}
    mode, note = "normal", ""
    if room.get("ambush"):
        if scouted:
            note = "\n👁 Глаз пророка заранее заметил засаду — враги не застали вас врасплох."
        else:
            mode, note = "ambush", "\n⚠️ Засада! Враги нападают первыми."
    if mode == "normal":
        chance = max((c.surprise_chance for c in alive.values()), default=0.0)
        if roll_chance(chance):
            mode, note = "surprise", "\n🏮 Призрачная лампа: вы застали врагов врасплох — первыми ходят герои!"
    enc.initiative_order = build_initiative_order(alive, enc, mode)
    enc.current_turn_index = 0
    enc.round_number = 0
    camp["in_fight"] = True
    icon = "👑 БОСС" if room["type"] == "boss" else "⚔️ Бой"
    await H._save_session(context, session)
    await H._notify_participants(
        context, session,
        f"{head}\n{icon}: {enc.enemy_template['name']}\n"
        f"{enc.enemy_template.get('description', '')}\n"
        f"❤️ {enc.enemy_hp}/{enc.enemy_max_hp}{note}",
    )
    await H._advance_turn(context, session)


# ─── Итог боя ───────────────────────────────────────────────

async def on_fight_won(context, session: RaidSession):
    camp = session.campaign
    if not camp["in_fight"]:
        return  # повторный вызов
    camp["in_fight"] = False
    room = camp["rooms"][camp["room_idx"]]
    loc = get_campaign_location(camp["loc"])
    enc = session.encounters[session.current_encounter]
    chars = await H._get_participant_chars(context, session)
    lines = [f"✅ {enc.enemy_template['name']} повержен!"]
    healed = False
    for c in chars.values():
        if not c.alive or c.hp <= 0:
            continue
        c.clear_buffs()
        leveled = award_fight_exp(c, loc)
        if leveled:
            lines.append(f"⬆️ {c.name} достигает {c.level} уровня! (очки характеристик распределены автоматически)")
        elif room["type"] != "boss":
            heal_fraction(c, loc.heal_after_fight)
            healed = True
        await H._save_char(c)
    if room["type"] == "boss":
        await H._save_session(context, session)
        await H._complete_raid(context, session)
        return
    if healed:
        lines.append(f"🩹 Отряд переводит дух: +{int(loc.heal_after_fight * 100)}% здоровья.")
    await _send_go(context, session, "\n".join(lines))


# ─── Сундук / находка ───────────────────────────────────────

def _is_group(session: RaidSession) -> bool:
    return session.chat_id is not None and session.chat_id < 0


async def _resolve_pending(update, context, session: RaidSession, text: str, *, go: bool = True):
    """Закрывает комнату: правит нажатое сообщение, рассылает итог остальным (в ЛС), даёт «Дальше»."""
    query = update.callback_query
    session.campaign["pending"] = None
    try:
        await query.edit_message_text(text)
    except Exception:
        log.debug("edit_message_text не удался", exc_info=True)
    if not _is_group(session):
        uid = update.effective_user.id
        for puid in H._participants(session):
            if puid != uid:
                try:
                    await context.bot.send_message(puid, text)
                except Exception:
                    log.debug("не удалось отправить итог игроку %s", puid, exc_info=True)
    if go:
        await _send_go(context, session, "Отряд готов идти дальше.")
    else:
        await H._save_session(context, session)


@H._auth_cb
async def cb_open(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    session, hero = await _guard(update, context)
    if not session:
        return
    async with H._raid_lock(session.raid_id):
        session = await H._get_session(context)
        camp = session.campaign if session else None
        pend = camp["pending"] if camp else None
        if not pend or pend["type"] != "chest":
            await update.callback_query.answer("Сундук уже обработан.")
            return
        if not hero.alive or hero.hp <= 0:
            await update.callback_query.answer("Павшие не открывают сундуки.", show_alert=True)
            return
        await update.callback_query.answer()
        loc = get_campaign_location(camp["loc"])
        kind = pend["kind"]
        if kind == "treasure":
            n = chest_candies(loc)
            camp["candies"][str(uid)] = camp["candies"].get(str(uid), 0) + n
            camp["chests_opened"] = camp.get("chests_opened", 0) + 1
            text = f"📦 {hero.name} открывает сундук: сокровище! +{n}🍬"
        elif kind == "trap":
            dmg = nonlethal_damage(hero, 0.15 + 0.02 * loc.tier)
            await H._save_char(hero)
            text = f"💥 {hero.name} открывает сундук — ловушка! −{dmg}❤️ ({hero.hp}/{hero.max_hp})"
        else:
            text = f"📦 {hero.name} открывает сундук — внутри пусто."
        await _resolve_pending(update, context, session, text)


@H._auth_cb
async def cb_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session, hero = await _guard(update, context)
    if not session:
        return
    async with H._raid_lock(session.raid_id):
        session = await H._get_session(context)
        camp = session.campaign if session else None
        pend = camp["pending"] if camp else None
        if not pend or pend["type"] not in ("chest", "find"):
            await update.callback_query.answer("Уже обработано.")
            return
        await update.callback_query.answer()
        await _resolve_pending(update, context, session, f"🚶 {hero.name} решает не рисковать, отряд идёт мимо.")


@H._auth_cb
async def cb_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session, hero = await _guard(update, context)
    if not session:
        return
    async with H._raid_lock(session.raid_id):
        session = await H._get_session(context)
        camp = session.campaign if session else None
        pend = camp["pending"] if camp else None
        if not pend or pend["type"] != "find":
            await update.callback_query.answer("Уже обработано.")
            return
        if not hero.alive or hero.hp <= 0:
            await update.callback_query.answer("Павшие ничего не трогают.", show_alert=True)
            return
        await update.callback_query.answer()
        loc = get_campaign_location(camp["loc"])
        flavor = loc.finds[pend["flavor"]]
        result = apply_find(flavor["kind"], hero, session.active_buffs)
        await H._save_char(hero)
        await _resolve_pending(update, context, session, f"{flavor['text']}\n{hero.name}: {result}")


# ─── Артефакты ──────────────────────────────────────────────

async def _open_artifact_room(context, session: RaidSession, head: str):
    camp = session.campaign
    chars = await H._get_participant_chars(context, session)
    offers = {}
    for uid, c in chars.items():
        if c.alive and c.hp > 0:
            key = roll_artifact_offer(c)
            if key:
                offers[str(uid)] = key
    if not offers:
        await _send_go(context, session, f"{head}\n✨ Тайник пуст.")
        return
    camp["pending"] = {"type": "artifact", "offers": offers, "done": [], "swap": None}
    await H._save_session(context, session)
    await H._notify_participants(context, session, _artifact_room_text(session, chars, head), _kb_artifact())


def _kb_artifact() -> InlineKeyboardMarkup:
    return _kb([[("✅ Взять", "cmp_art_take"), ("❌ Оставить", "cmp_art_skip")],
                [("🚪 Идти дальше", "cmp_art_done")]])


def _artifact_room_text(session: RaidSession, chars: dict, head: str = "") -> str:
    pend = session.campaign["pending"]
    lines = [head, "✨ Вы находите реликвии. Каждый герой может взять свою (слотов: "
             f"{MAX_ARTIFACTS}, плюс всегда идёт с минусом):"] if head else \
            ["✨ Реликвии (слотов: " f"{MAX_ARTIFACTS}):"]
    for uid_s, key in pend["offers"].items():
        c = chars.get(int(uid_s))
        who = c.name if c else uid_s
        state = ""
        if uid_s in pend["done"]:
            state = " — решение принято"
        lines.append(f"\n🧙 {who}{state}:\n{artifact_text(key)}")
    return "\n".join(x for x in lines if x != "")


async def _artifact_progress(update, context, session: RaidSession, note: str):
    """После выбора героя: обновляет сообщение или завершает комнату, когда решили все."""
    camp = session.campaign
    pend = camp["pending"]
    chars = await H._get_participant_chars(context, session)
    alive_ids = [str(u) for u, c in chars.items() if c.alive and c.hp > 0 and str(u) in pend["offers"]]
    if all(u in pend["done"] for u in alive_ids):
        await _resolve_pending(update, context, session, f"{note}\n\n" + _artifact_room_text(session, chars))
        return
    try:
        await update.callback_query.edit_message_text(
            f"{note}\n\n" + _artifact_room_text(session, chars), reply_markup=_kb_artifact())
    except Exception:
        log.debug("edit_message_text не удался", exc_info=True)
    await H._save_session(context, session)


@H._auth_cb
async def cb_art_take(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    session, hero = await _guard(update, context)
    if not session:
        return
    async with H._raid_lock(session.raid_id):
        session = await H._get_session(context)
        camp = session.campaign if session else None
        pend = camp["pending"] if camp else None
        uid_s = str(uid)
        if not pend or pend["type"] != "artifact" or uid_s not in pend["offers"] or uid_s in pend["done"]:
            await update.callback_query.answer("Для вас здесь ничего нет.")
            return
        key = pend["offers"][uid_s]
        if hero.add_artifact(key):
            await update.callback_query.answer()
            await H._save_char(hero)
            pend["done"].append(uid_s)
            await _artifact_progress(update, context, session, f"✨ {hero.name} берёт {ARTIFACTS[key].name}.")
            return
        # слоты заняты — спрашиваем, что заменить
        await update.callback_query.answer()
        pend["swap"] = uid_s
        rows = [[(f"♻️ Заменить: {ARTIFACTS[k].name}", f"cmp_art_rep_{i}")] for i, k in enumerate(hero.artifacts)]
        rows.append([("❌ Передумал", "cmp_art_rep_x")])
        await H._save_session(context, session)
        await context.bot.send_message(
            update.effective_chat.id if update.effective_chat else uid,
            f"{hero.name}, слоты заняты. Что заменить на {ARTIFACTS[key].name}?",
            reply_markup=_kb(rows),
            **({"message_thread_id": session.thread_id} if session.thread_id and _is_group(session) else {}),
        )


@H._auth_cb
async def cb_art_replace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    query = update.callback_query
    session, hero = await _guard(update, context)
    if not session:
        return
    async with H._raid_lock(session.raid_id):
        session = await H._get_session(context)
        camp = session.campaign if session else None
        pend = camp["pending"] if camp else None
        uid_s = str(uid)
        if not pend or pend["type"] != "artifact" or pend.get("swap") != uid_s:
            await query.answer("Этот выбор не для вас.", show_alert=True)
            return
        arg = query.data[len("cmp_art_rep_"):]
        pend["swap"] = None
        await query.answer()
        if arg == "x":
            await query.edit_message_text("Решение отложено — нажмите «Взять» или «Оставить» ещё раз.")
            await H._save_session(context, session)
            return
        key = pend["offers"][uid_s]
        old = hero.artifacts[int(arg)] if int(arg) < len(hero.artifacts) else None
        if old and hero.replace_artifact(old, key):
            await H._save_char(hero)
            pend["done"].append(uid_s)
            await _artifact_progress(update, context, session,
                                     f"✨ {hero.name} меняет {ARTIFACTS[old].name} на {ARTIFACTS[key].name}.")
        else:
            await query.edit_message_text("Не получилось заменить артефакт.")
            await H._save_session(context, session)


@H._auth_cb
async def cb_art_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    session, hero = await _guard(update, context)
    if not session:
        return
    async with H._raid_lock(session.raid_id):
        session = await H._get_session(context)
        camp = session.campaign if session else None
        pend = camp["pending"] if camp else None
        uid_s = str(uid)
        if not pend or pend["type"] != "artifact" or uid_s not in pend["offers"] or uid_s in pend["done"]:
            await update.callback_query.answer("Для вас здесь ничего нет.")
            return
        await update.callback_query.answer()
        pend["done"].append(uid_s)
        pend["swap"] = None
        await _artifact_progress(update, context, session, f"❌ {hero.name} оставляет реликвию.")


@H._auth_cb
async def cb_art_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session, hero = await _guard(update, context)
    if not session:
        return
    async with H._raid_lock(session.raid_id):
        session = await H._get_session(context)
        camp = session.campaign if session else None
        pend = camp["pending"] if camp else None
        if not pend or pend["type"] != "artifact":
            await update.callback_query.answer("Уже обработано.")
            return
        await update.callback_query.answer()
        await _resolve_pending(update, context, session, "🚪 Отряд оставляет реликвии, кто не успел решить, и идёт дальше.")


# ═══════════════════════════════════════════════════════════════
# Безумие
# ═══════════════════════════════════════════════════════════════

async def madness_turn(context, session: RaidSession, enc, char, uid: int):
    """25% хода безумного героя проходят вопреки воле игрока.

    Безумец бьёт случайного союзника или себя. Урон не смертелен (минимум 1 HP остаётся).
    """
    chars = await H._get_participant_chars(context, session)
    allies = [c for u, c in chars.items() if u != uid and c.alive and c.hp > 0]
    if allies and roll_chance(0.6):
        victim = _random.choice(allies)
        atk = calc_damage(char, victim)
        if atk.is_dodged:
            text = f"🌀 {char.name} во власти безумия кидается на {victim.name}, но промахивается!"
        else:
            dmg = min(atk.final_damage, max(0, victim.hp - 1))
            victim.hp -= dmg
            await H._save_char(victim)
            text = f"🌀 {char.name} во власти безумия бьёт союзника {victim.name}: −{dmg}❤️"
    else:
        dmg = nonlethal_damage(char, 0.10)
        await H._save_char(char)
        text = f"🌀 {char.name} во власти безумия бьёт сам себя: −{dmg}❤️"
    await H._notify_participants(context, session, text + "\n(действие игрока перехвачено безумием)")
    advance_turn_core(enc)
    await H._advance_turn(context, session)


# ═══════════════════════════════════════════════════════════════
# Завершение
# ═══════════════════════════════════════════════════════════════

async def drop_hero(session: RaidSession, uid: int):
    """Игрок покинул кампанию: его герой удаляется, собранные конфеты остаются в зачёте."""
    name = session.campaign["heroes"].get(str(uid))
    if name:
        await H.storage.delete_character(hero_owner(uid), name)


async def complete(context, session: RaidSession):
    """Босс повержен: награда за босса каждому, кто дошёл до конца (павшим — по настройке)."""
    camp = session.campaign
    loc = get_campaign_location(camp["loc"])
    session.status = RaidStatus.COMPLETED
    in_party = H._participants(session)
    for uid_s, name in camp["heroes"].items():
        uid = int(uid_s)
        if uid not in in_party:
            continue  # вышел из кампании — награды за босса нет
        hero = await H.storage.load_character_by_name(hero_owner(uid), name)
        fallen = (not hero) or (not hero.alive) or hero.hp <= 0
        camp["candies"][uid_s] = camp["candies"].get(uid_s, 0) + boss_reward(loc, fallen)
    await H._notify_participants(context, session, "👑 Босс повержен!")
    await H._finish_session(context, session)


async def finalize(context, session: RaidSession):
    """Итог кампании: сводка, команда с конфетами для основного бота, удаление героев."""
    camp = session.campaign
    if not camp or camp.get("finalized"):
        return
    camp["finalized"] = True
    loc = get_campaign_location(camp["loc"])
    title = loc.name if loc else camp["loc"]
    if session.status == RaidStatus.COMPLETED:
        head, kb = f"🏆 Кампания «{title}» пройдена!", raid_done()
    else:
        head, kb = f"💀 Кампания «{title}» не пройдена.", raid_failed()
    lines = [head, ""]
    for uid_s, name in camp["heroes"].items():
        lines.append(f"• {name}: {camp['candies'].get(uid_s, 0)}🍬")
    cmd = build_reward_command(camp["candies"])
    if cmd:
        lines += ["", "Конфеты передаются основному боту командой ниже."]
    else:
        lines += ["", "В этот раз без конфет — сундуки не открыты."]
    await H._notify_participants(context, session, "\n".join(lines), kb)

    if cmd:
        target = config.CANDY_REWARD_CHAT_ID
        if target is None and session.chat_id is not None and session.chat_id < 0:
            target = session.chat_id
        kwargs = {}
        if target is not None and target == session.chat_id and session.thread_id:
            kwargs["message_thread_id"] = session.thread_id
        try:
            if target is not None:
                await context.bot.send_message(target, cmd, **kwargs)
            else:
                # ЛС: основной бот не увидит; отправляем игрокам, чтобы переслали в общий чат
                for puid in camp["heroes"]:
                    await context.bot.send_message(int(puid), cmd)
        except Exception:
            log.warning("Не удалось отправить команду с конфетами: %s", cmd, exc_info=True)

    for uid_s, name in camp["heroes"].items():
        try:
            await H.storage.delete_character(hero_owner(int(uid_s)), name)
        except Exception:
            log.warning("Не удалось удалить героя кампании %s", name, exc_info=True)


async def recover_campaigns(app) -> int:
    """После перезапуска бота: прерванные кампании закрываются, собранные конфеты засчитываются.

    user_data игроков хранится в памяти и при перезапуске теряется, продолжить такую кампанию
    нельзя, а герои и конфеты иначе остались бы в БД навсегда.
    """
    from types import SimpleNamespace
    ctx = SimpleNamespace(bot=app.bot, application=app, user_data={})
    done = 0
    for raid_id, data in await H.storage.list_raid_sessions():
        if not data.get("campaign") or data.get("status") != RaidStatus.IN_PROGRESS.value:
            continue
        try:
            session = session_from_dict(data)
            session.status = RaidStatus.FAILED
            await H._notify_participants(ctx, session, "♻️ Бот был перезапущен, кампания прервана. "
                                                       "Собранные конфеты засчитываются.")
            await H._finish_session(ctx, session)
            done += 1
        except Exception:
            log.exception("Не удалось закрыть прерванную кампанию %s", raid_id)
    return done


# ═══════════════════════════════════════════════════════════════
# Регистрация
# ═══════════════════════════════════════════════════════════════

def register(app: Application):
    f = H.ALLOWED_CHAT_FILTER
    app.add_handler(CommandHandler("campaign_begin", cmd_begin, filters=f))
    app.add_handler(CommandHandler("campaign_join", cmd_join, filters=f))
    app.add_handler(CallbackQueryHandler(cb_menu, pattern=r"^cmp_menu$"))
    app.add_handler(CallbackQueryHandler(cb_loc, pattern=r"^cmp_loc_"))
    app.add_handler(CallbackQueryHandler(cb_mode, pattern=r"^cmp_(solo|party)_"))
    app.add_handler(CallbackQueryHandler(cb_class, pattern=r"^cmp_class_"))
    app.add_handler(CallbackQueryHandler(cb_lobby_refresh, pattern=r"^cmp_lobby_refresh$"))
    app.add_handler(CallbackQueryHandler(cb_lobby_leave, pattern=r"^cmp_lobby_leave$"))
    app.add_handler(CallbackQueryHandler(cb_lobby_start, pattern=r"^cmp_lobby_start$"))
    app.add_handler(CallbackQueryHandler(cb_go, pattern=r"^cmp_go_-?\d+$"))
    app.add_handler(CallbackQueryHandler(cb_open, pattern=r"^cmp_open$"))
    app.add_handler(CallbackQueryHandler(cb_pass, pattern=r"^cmp_pass$"))
    app.add_handler(CallbackQueryHandler(cb_use, pattern=r"^cmp_use$"))
    app.add_handler(CallbackQueryHandler(cb_art_take, pattern=r"^cmp_art_take$"))
    app.add_handler(CallbackQueryHandler(cb_art_skip, pattern=r"^cmp_art_skip$"))
    app.add_handler(CallbackQueryHandler(cb_art_done, pattern=r"^cmp_art_done$"))
    app.add_handler(CallbackQueryHandler(cb_art_replace, pattern=r"^cmp_art_rep_"))
