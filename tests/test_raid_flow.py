"""Сквозные тесты рейдового потока в handlers.py (с подменой Telegram и нейросети).

Раньше игрок не мог добить врага, погибший персонаж оставался мёртвым навсегда,
а второй участник онлайн-рейда не получал доступа к рейду.
"""
import sys; sys.path.insert(0, ".")

import asyncio
import time
from collections import defaultdict
from types import SimpleNamespace

import pytest

import bot.handlers as H
from core.character import Character
from core.economy import MARKET
from core.raid import RaidStatus, session_from_dict
from data.storage import Storage, register_templates


# ─── Заглушки Telegram ────────────────────────────────────────────

class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))

    def texts(self, chat_id=None):
        return [t for c, t, _ in self.sent if chat_id is None or c == chat_id]


class FakeApp:
    def __init__(self):
        self.user_data = defaultdict(dict)


class FakeMessage:
    def __init__(self, uid, text="", chat_id=None):
        self.chat_id = chat_id if chat_id is not None else uid
        self.message_thread_id = None
        self.is_topic_message = False
        self.text = text
        self.replies: list[str] = []
        self.chat = SimpleNamespace(type="private" if self.chat_id > 0 else "supergroup", id=self.chat_id)

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        return self


class FakeQuery:
    def __init__(self, uid, data, chat_id=None):
        self.data = data
        self.from_user = SimpleNamespace(id=uid)
        self.message = FakeMessage(uid, chat_id=chat_id)
        self.message.text = "msg"
        self.edits: list[str] = []

    async def answer(self, *a, **kw):
        return True

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


class Env:
    """Один «бот»: БД, приложение и контексты пользователей."""

    def __init__(self, storage):
        self.storage = storage
        self.bot = FakeBot()
        self.app = FakeApp()

    def ctx(self, uid):
        return SimpleNamespace(
            bot=self.bot, application=self.app, user_data=self.app.user_data[uid],
            args=[],
        )

    def update(self, uid, *, text=None, data=None, chat_id=None):
        chat_id = chat_id if chat_id is not None else uid
        chat = SimpleNamespace(type="private" if chat_id > 0 else "supergroup", id=chat_id)
        user = SimpleNamespace(id=uid)
        if data is not None:
            q = FakeQuery(uid, data, chat_id=chat_id)
            return SimpleNamespace(
                callback_query=q, message=None, effective_user=user,
                effective_chat=chat, effective_message=q.message,
            ), q
        m = FakeMessage(uid, text or "", chat_id=chat_id)
        return SimpleNamespace(
            callback_query=None, message=m, effective_user=user,
            effective_chat=chat, effective_message=m,
        ), m

    async def press(self, uid, handler, data, chat_id=None):
        upd, q = self.update(uid, data=data, chat_id=chat_id)
        await handler(upd, self.ctx(uid))
        return q

    async def say(self, uid, text):
        upd, m = self.update(uid, text=text)
        await H.handle_text(upd, self.ctx(uid))
        return m

    async def session_of(self, uid):
        raid_id = self.app.user_data[uid].get("raid_id")
        if not raid_id:
            return None
        data = await self.storage.load_raid_session(raid_id)
        return session_from_dict(data) if data else None

    async def char(self, uid, name):
        return await self.storage.load_character_by_name(uid, name)


async def _fake_nn(location, turn, player, enemies, action_history=None, *,
                   player_action="", damage=None, mode="player_modifiers"):
    return {
        "player_modifiers": {"actions": [], "attribute": "strength"},
        "player_narrative": {"player_narrative": "Вы бьёте."},
        "enemy_modifiers": {"enemy_actions": []},
        "enemy_narrative": {"enemy_narrative": "Враг бьёт."},
    }[mode]


async def _fake_effect(*a, **kw):
    return "none"


@pytest.fixture
async def env(monkeypatch):
    s = Storage(db_path=":memory:")
    await s.connect()
    register_templates()
    monkeypatch.setattr(H, "storage", s)
    monkeypatch.setattr(H, "call_narrative_api", _fake_nn)
    monkeypatch.setattr(H, "decide_status_effect", _fake_effect)
    MARKET.listings.clear()
    H._lobbies.clear()
    yield Env(s)
    for t in list(H._bg_tasks):
        t.cancel()
    await asyncio.sleep(0)
    await s.close()


async def make_char(env, uid, name, cls="warrior", strength=60, agility=0, hp=None):
    c = Character(owner_tg_id=uid, name=name, class_key=cls)
    c.base_stats.strength = strength
    c.base_stats.agility = agility
    c.max_hp = c._calc_max_hp()
    c.hp = c.max_hp if hp is None else hp
    await env.storage.save_character(c)
    return c


async def play_until_done(env, players: dict[int, str], max_steps=300):
    """Играет рейд: каждый, чей ход, жмёт «Действие» и пишет текст; «Следующий враг» — по кнопке."""
    steps = 0
    actors = list(players)
    while steps < max_steps:
        steps += 1
        session = None
        for uid in actors:
            session = await env.session_of(uid)
            if session:
                break
        if not session:
            return steps
        enc = session.encounters[session.current_encounter]
        if session.pending_event:
            uid = next(iter(_alive_participants(session)))
            await env.press(uid, H.cb_raid_event_choice, "raid_ev_opt_0")
            continue
        if enc.finished:
            uid = next(iter(_alive_participants(session)))
            await env.press(uid, H.cb_raid_next, "raid_next")
            continue
        turn = H.get_current_turn(enc)
        assert turn and turn["type"] == "player", f"рейд завис на ходу {turn}"
        uid = turn["uid"]
        q = await env.press(uid, H.cb_raid_action, "raid_action")
        assert env.app.user_data[uid].get("raid_action_pending"), q.edits
        await env.say(uid, "Рублю врага мечом")
    raise AssertionError("рейд не закончился за отведённое число шагов")


def _alive_participants(session):
    return H._participants(session)


# ─── Тесты ────────────────────────────────────────────────────────

async def test_solo_raid_can_be_completed(env):
    """Главный баг: добить врага было нельзя, рейд никогда не завершался."""
    await make_char(env, 1, "Герой")
    await env.press(1, H.cb_raid_start, "raid_start_forest")
    assert await env.session_of(1) is not None
    await play_until_done(env, {1: "Герой"})

    hero = await env.char(1, "Герой")
    assert hero.gold > 0 and hero.experience > 0 or hero.level > 1   # награда получена
    assert hero.alive and hero.hp == hero.max_hp and not hero.in_raid
    assert hero.count_raid == 1
    assert env.app.user_data[1].get("raid_id") is None
    assert any("Рейд пройден" in t for t in env.bot.texts(1))


async def test_player_turn_result_is_shown(env):
    """Результат хода игрока (урон, нарратив) раньше нигде не показывался."""
    await make_char(env, 1, "Герой")
    await env.press(1, H.cb_raid_start, "raid_start_forest")
    session = await env.session_of(1)
    turn = H.get_current_turn(session.encounters[0])
    assert turn["type"] == "player"
    await env.press(1, H.cb_raid_action, "raid_action")
    env.bot.sent.clear()
    await env.say(1, "Атакую")
    joined = "\n".join(env.bot.texts(1))
    assert "Вы бьёте." in joined and "урона" in joined


async def test_dead_character_is_revived_and_can_raid_again(env):
    """Погибший персонаж раньше оставался alive=False навсегда."""
    await make_char(env, 1, "Слабак", strength=0, hp=3)
    # у слабака 50 HP по формуле — ставим мало HP и слабую защиту, враги сильнее
    c = await env.char(1, "Слабак")
    c.max_hp = 5
    c.hp = 5
    await env.storage.save_character(c)
    await env.press(1, H.cb_raid_start, "raid_start_caves")   # пещеры бьют сильно
    await play_until_done(env, {1: "Слабак"})

    hero = await env.char(1, "Слабак")
    assert hero.alive and hero.hp > 0 and not hero.in_raid
    assert hero.count_raid == 1
    assert any("Рейд провален" in t or "погибли" in t for t in env.bot.texts(1))
    # и он может снова начать рейд
    await env.press(1, H.cb_raid_start, "raid_start_forest")
    assert await env.session_of(1) is not None


async def test_two_player_raid_both_participate(env):
    """Раньше raid_id получал только создатель — второй игрок рейда не видел."""
    await make_char(env, 1, "Первый")
    await make_char(env, 2, "Второй", cls="leader")

    await env.press(1, H.cb_raid_online_create, "raid_online_forest")
    code = env.app.user_data[1]["lobby_code"]
    upd, _ = env.update(2, text=f"/raidjoin {code}")
    ctx = env.ctx(2)
    ctx.args = [code]
    await H.cmd_raid_join(upd, ctx)
    assert (2, "Второй") in H._lobbies[code]["participants"]

    await env.press(1, H.cb_raid_lobby_start, "raid_lobby_start")
    assert env.app.user_data[2].get("raid_id") == env.app.user_data[1].get("raid_id")
    session = await env.session_of(2)
    assert session is not None and set(H._participants(session)) == {1, 2}
    assert session.chat_id == 1                      # личный чат: рассылка каждому в ЛС

    # страж лидера призван и в групповом рейде
    leader = await env.char(2, "Второй")
    assert leader.companion is not None

    await play_until_done(env, {1: "Первый", 2: "Второй"})

    for uid, name in ((1, "Первый"), (2, "Второй")):
        c = await env.char(uid, name)
        assert c.alive and not c.in_raid and c.count_raid == 1
        assert env.app.user_data[uid].get("raid_id") is None
    # оба получили сообщения о рейде в свою личку
    assert any("Рейд пройден" in t for t in env.bot.texts(1))
    assert any("Рейд пройден" in t for t in env.bot.texts(2))


async def test_turn_timeout_skips_afk_player(env, monkeypatch):
    """Игрок, не сделавший ход, раньше блокировал рейд навсегда."""
    monkeypatch.setattr(H, "TURN_TIMEOUT", 0.2)
    await make_char(env, 1, "Соня", strength=200)
    await env.press(1, H.cb_raid_start, "raid_start_forest")
    session = await env.session_of(1)
    start_round = session.encounters[0].round_number
    await asyncio.sleep(0.9)
    assert any("не успел" in t for t in env.bot.texts(1))
    session = await env.session_of(1)
    if session:  # рейд мог закончиться, если враги убили персонажа
        assert session.encounters[0].round_number > start_round or session.current_encounter > 0


async def test_leaving_group_raid_does_not_end_it_for_others(env):
    await make_char(env, 1, "Первый")
    await make_char(env, 2, "Второй")
    await env.press(1, H.cb_raid_online_create, "raid_online_forest")
    code = env.app.user_data[1]["lobby_code"]
    upd, _ = env.update(2, text="/raidjoin")
    ctx = env.ctx(2); ctx.args = [code]
    await H.cmd_raid_join(upd, ctx)
    await env.press(1, H.cb_raid_lobby_start, "raid_lobby_start")

    await env.press(2, H.cb_raid_leave, "raid_leave")
    session = await env.session_of(1)
    assert session is not None and set(H._participants(session)) == {1}
    second = await env.char(2, "Второй")
    assert not second.in_raid and second.alive
    assert env.app.user_data[2].get("raid_id") is None
    await play_until_done(env, {1: "Первый"})
    assert (await env.char(1, "Первый")).count_raid == 1


async def test_forfeit_solo_restores_character(env):
    await make_char(env, 1, "Герой")
    await env.press(1, H.cb_raid_start, "raid_start_forest")
    upd, _ = env.update(1, text="/forfeit")
    await H.cmd_forfeit(upd, env.ctx(1))
    hero = await env.char(1, "Герой")
    assert not hero.in_raid and hero.alive and hero.hp == hero.max_hp
    assert await env.session_of(1) is None


async def test_cannot_start_second_raid_while_in_one(env):
    await make_char(env, 1, "Герой")
    await env.press(1, H.cb_raid_start, "raid_start_forest")
    first = env.app.user_data[1]["raid_id"]
    q = await env.press(1, H.cb_raid_start, "raid_start_forest")
    assert "уже в рейде" in q.edits[-1]
    assert env.app.user_data[1]["raid_id"] == first


async def test_next_button_double_click_does_not_skip_enemy(env):
    """Раньше номер боя увеличивался и при победе, и при нажатии «Следующий» — враги пропускались."""
    await make_char(env, 1, "Герой", strength=200)
    await env.press(1, H.cb_raid_start, "raid_start_dungeon")   # 3-6 врагов
    session = await env.session_of(1)
    total = len(session.encounters)
    fought = 0
    seen = set()
    for _ in range(400):
        session = await env.session_of(1)
        if not session:
            break
        enc = session.encounters[session.current_encounter]
        if session.pending_event:
            await env.press(1, H.cb_raid_event_choice, "raid_ev_opt_0")
            continue
        if enc.finished:
            if session.current_encounter not in seen:
                seen.add(session.current_encounter)
                fought += 1
            await env.press(1, H.cb_raid_next, "raid_next")
            await env.press(1, H.cb_raid_next, "raid_next")   # двойной клик
            continue
        turn = H.get_current_turn(enc)
        await env.press(1, H.cb_raid_action, "raid_action")
        await env.say(1, "бью")
    # последний бой завершает рейд сразу (в seen он не попадает)
    assert fought == total - 1


async def test_market_cancel_returns_item(env):
    from core.items import Item, ItemTemplate, ItemEffect, ItemType, Rarity
    c = await make_char(env, 1, "Продавец")
    item = Item(ItemTemplate("Меч", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=1)),
                uid="x1", durability=5, durability_max=5)
    listing = MARKET.create_listing(1, "Продавец", item, 50)
    await MARKET.persist_listing(env.storage, listing)

    q = await env.press(1, H.cb_market_cancel, f"market_cancel_{listing.listing_id}")
    assert "вернул" in q.edits[-1]
    back = await env.char(1, "Продавец")
    assert [i.name for i in back.inventory] == ["Меч"]
    # повторная отмена не создаёт второй копии
    await env.press(1, H.cb_market_cancel, f"market_cancel_{listing.listing_id}")
    back = await env.char(1, "Продавец")
    assert len(back.inventory) == 1


async def test_character_creation_rejects_bad_and_duplicate_names(env):
    await make_char(env, 1, "Герой")
    env.app.user_data[1]["creation"] = {"step": "name"}
    m = await env.say(1, "  ")
    assert "Имя должно быть" in m.replies[-1]
    m = await env.say(1, "герой")                    # дубликат без учёта регистра
    assert "уже есть" in m.replies[-1]
    m = await env.say(1, "Новый")
    assert env.app.user_data[1]["creation"]["step"] == "description"


async def test_sell_does_not_overwrite_fresh_character_state(env):
    """Раньше при продаже сохранялась устаревшая копия персонажа (терялось золото)."""
    from core.items import Item, ItemTemplate, ItemEffect, ItemType, Rarity
    c = await make_char(env, 1, "Торгаш")
    item = Item(ItemTemplate("Кольцо", ItemType.ACCESSORY, Rarity.COMMON, ItemEffect()),
                uid="r1", durability=5, durability_max=5)
    c.inventory.append(item)
    await env.storage.save_character(c)
    await env.press(1, H.cb_inv_sell, "inv_sell_r1")
    # пока игрок вводит цену, его золото изменилось (рейд, продажа и т.п.)
    fresh = await env.char(1, "Торгаш")
    fresh.gold = 777
    await env.storage.save_character(fresh)
    await env.say(1, "100")
    after = await env.char(1, "Торгаш")
    assert after.gold == 777 and after.inventory == []
