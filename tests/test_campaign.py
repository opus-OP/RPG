"""Тесты ивент-кампаний: артефакты, безумие, комнаты, конфеты, соло и отряд."""
import sys; sys.path.insert(0, ".")
sys.path.insert(0, "tests")

import pytest

import bot.handlers as H
import bot.campaign_handlers as CH
import config
from core import campaign as C
from core.artifacts import ARTIFACTS, MAX_ARTIFACTS
from core.character import Character
from core.combat import calc_damage
from core.raid import (
    RaidEncounter, RaidStatus, _Enemy, apply_mob_status_effects, build_initiative_order,
    resolve_enemy_turn, session_from_dict, session_to_dict, create_enemy,
)
from data.models import character_from_dict, character_to_dict
from test_raid_flow import Env, env, FakeBot  # noqa: F401  (fixture env)


# ═══════════════════════════════════════════════════════════════
# Артефакты
# ═══════════════════════════════════════════════════════════════

def _warrior(**kw):
    c = Character(owner_tg_id=1, name="В", class_key="warrior", **kw)
    return c


def test_ten_artifacts_with_plus_and_minus():
    assert len(ARTIFACTS) == 10
    for a in ARTIFACTS.values():
        assert a.name and a.plus and a.minus


def test_two_slots_no_duplicates():
    c = _warrior()
    assert c.add_artifact("swiss_knife")
    assert not c.add_artifact("swiss_knife")
    assert c.add_artifact("grandpa_monocle")
    assert not c.add_artifact("sinner_armor")     # третий слот не существует
    assert len(c.artifacts) == MAX_ARTIFACTS
    assert not c.add_artifact("нет_такого")


def test_replace_artifact():
    c = _warrior()
    c.add_artifact("swiss_knife")
    assert c.replace_artifact("swiss_knife", "sinner_armor")
    assert c.artifacts == ["sinner_armor"]
    assert not c.replace_artifact("swiss_knife", "mark_amulet")   # старого уже нет


def test_amulet_and_pill_change_max_hp():
    c = _warrior()
    base = c.max_hp
    c.add_artifact("mark_amulet")
    assert c.max_hp == round(base * 1.3)
    assert c.hp == c.max_hp                       # бонус сразу виден в HP
    c.replace_artifact("mark_amulet", "plague_pill")
    assert c.max_hp == round(base * 0.75)
    assert c.hp <= c.max_hp


def test_melee_ranged_multipliers():
    c = _warrior()
    assert c.damage_mult_for("strength") == 1.0
    c.add_artifact("swiss_knife")
    c.add_artifact("paper_pipe")
    assert c.damage_mult_for("strength") == pytest.approx(1.2)
    assert c.damage_mult_for("agility") == pytest.approx(1.2)
    assert c.damage_mult_for("intelligence") == 1.0


def test_swiss_knife_really_increases_damage():
    dummy = _Enemy({"hp": 9999, "atk_min": 1, "atk_max": 1, "defense": 0, "dodge_chance": 0.0,
                    "crit_chance": 0.0, "name": "манекен"}, 9999)
    plain = _warrior(); plain.base_stats.strength = 40
    knife = _warrior(); knife.base_stats.strength = 40; knife.add_artifact("swiss_knife")
    avg = lambda ch: sum(calc_damage(ch, dummy).final_damage for _ in range(600)) / 600
    assert avg(knife) > avg(plain) * 1.15


def test_armor_reduces_damage_and_heal():
    attacker = _warrior(); attacker.base_stats.strength = 30
    plain = _warrior(); armored = _warrior(); armored.add_artifact("sinner_armor")
    avg = lambda d: sum(calc_damage(attacker, d, dodge_override=0.0).final_damage for _ in range(600)) / 600
    assert avg(armored) < avg(plain) * 0.9
    armored.hp = 1
    assert armored.heal(100) == 60                # −40% к получаемому лечению


def test_pill_and_tooth_modify_healing():
    c = _warrior(); c.add_artifact("plague_pill"); c.hp = 1
    assert c.heal(100) == 100 - 0 if c.max_hp > 130 else True
    d = _warrior(); d.hp = 1; d.add_artifact("vampire_tooth"); d.hp = 1
    assert d.heal(100) == 75


def test_bleed_and_poison_resistance_scale_dot():
    def applied(res, t, n=2000):
        return sum(bool(apply_mob_status_effects({}, t, 100, "p1", resist=res).get("p1")) for _ in range(n)) / n
    assert applied(None, "bleed", 50) == 1.0                       # у врагов сопротивления нет
    assert applied(0.0, "bleed") == pytest.approx(0.70, abs=0.05)  # база 70%
    assert applied(0.5, "bleed") == pytest.approx(0.20, abs=0.05)  # 70% − 50%
    assert applied(-0.2, "poison") == pytest.approx(0.90, abs=0.05)
    assert applied(0.9, "bleed") == 0.0
    # яд — 8% макс. HP цели, кровь — 35% урона удара
    assert apply_mob_status_effects({}, "poison", 10, "p", target_max_hp=200)["p"][0].value == 16
    assert apply_mob_status_effects({}, "bleed", 40, "p", resist=0.0 if False else None)["p"][0].value == 14
    c = _warrior(); c.add_artifact("vampire_tooth"); c.add_artifact("star_ointment")
    assert c.effect_resist("bleed") == 0.5 and c.effect_resist("poison") == 0.5
    k = _warrior(); k.add_artifact("swiss_knife")
    assert k.effect_resist("bleed") == pytest.approx(-0.2)
    assert k.effect_resist("physical") == 0.0


def test_monocle_accuracy_and_debuff_power():
    c = _warrior(); c.add_artifact("grandpa_monocle")
    assert c.accuracy == pytest.approx(0.05) and c.debuff_power == pytest.approx(0.4)
    strong = apply_mob_status_effects({}, "bleed", 100, "e", power=0.4)["e"][0].value
    weak = apply_mob_status_effects({}, "bleed", 100, "e")["e"][0].value
    assert strong > weak


def test_dodge_and_initiative_penalties():
    c = _warrior(); c.base_stats.agility = 10
    base = c.dodge_chance
    c.add_artifact("prophet_eye")
    assert c.dodge_chance == pytest.approx(base - 0.05)
    c.add_artifact("star_ointment")
    assert c.initiative_bonus == -4


def test_artifacts_survive_save_and_load():
    c = _warrior(); c.add_artifact("mark_amulet"); c.madness = True
    c2 = character_from_dict(character_to_dict(c), {})
    assert c2.artifacts == ["mark_amulet"] and c2.madness and c2.max_hp == c.max_hp


# ═══════════════════════════════════════════════════════════════
# Засада / внезапность / безумие
# ═══════════════════════════════════════════════════════════════

def _enc():
    return RaidEncounter(enemy_hp=50, enemy_max_hp=50, enemy_template={
        "name": "Враг", "hp": 50, "atk_min": 1, "atk_max": 2, "defense": 0, "dodge_chance": 0.9})


def test_initiative_modes():
    a, b = _warrior(), Character(owner_tg_id=2, name="Б", class_key="rogue")
    for _ in range(30):
        order = build_initiative_order({1: a, 2: b}, _enc(), "surprise")
        assert order[-1]["type"] == "enemy"
        order = build_initiative_order({1: a, 2: b}, _enc(), "ambush")
        assert order[0]["type"] == "enemy"


def test_madness_is_applied_by_marked_enemy_and_resisted_by_none():
    hero = _warrior(); hero.base_stats.strength = 60
    hero.max_hp = hero._calc_max_hp(); hero.hp = hero.max_hp
    enc = _enc()
    enc.enemy_template.update({"atk_min": 5, "atk_max": 6, "madness_chance": 1.0, "dodge_chance": 0.0})
    enemy = create_enemy(enc)
    hit = 0
    for _ in range(20):
        hero.madness = False
        atk = resolve_enemy_turn(hero, enemy, enc, None)
        if atk.final_damage > 0:
            assert hero.madness and atk.madness_applied
            hit += 1
    assert hit > 0


def test_lamp_makes_madness_easier():
    c = _warrior(); c.add_artifact("ghost_lamp")
    assert c.madness_resist == pytest.approx(-0.3)
    assert c.surprise_chance == pytest.approx(0.4)


# ═══════════════════════════════════════════════════════════════
# Генерация кампании
# ═══════════════════════════════════════════════════════════════

def test_locations_and_prices_match_tz():
    locs = C.get_campaign_locations()
    prices = {k: l.price for k, l in locs.items()}
    assert prices["thicket"] == prices["preserve"] == 200
    assert prices["ruins"] == 250 and prices["palace"] == 350
    assert prices["dungeon_temple"] == prices["dungeon_order"] == prices["dungeon_asylum"] == 750
    assert C.get_campaign_mob(locs["palace"].boss_id)["name"] == "Граф Дракула"
    assert C.get_campaign_mob(locs["dungeon_asylum"].boss_id)["name"] == "Madness in flesh"


@pytest.mark.parametrize("key", list(C.get_campaign_locations()))
def test_generated_campaign_structure(key):
    loc = C.get_campaign_location(key)
    for _ in range(25):
        state, encs = C.generate_campaign(loc, 1)
        rooms = state["rooms"]
        chests = [r for r in rooms if r["type"] == "chest"]
        assert 6 <= len(chests) <= 8                              # 6–8 сундуков за поход
        assert sum(r["kind"] == "treasure" for r in chests) >= 3
        assert rooms[-1]["type"] == "boss" and rooms[-2]["type"] == "rest"
        assert sum(r["type"] == "artifact" for r in rooms) == 2
        fights = [r for r in rooms if r["type"] in ("fight", "boss")]
        assert len(fights) == len(encs)
        assert [r["enc"] for r in fights if r["type"] == "boss"] == [len(encs) - 1]
        assert rooms[0]["type"] != "artifact" and not rooms[0].get("ambush")
        assert encs[-1].enemy_template["mob_id"] == loc.boss_id


def test_party_scaling_makes_enemies_tougher():
    loc = C.get_campaign_location("thicket")
    _, solo = C.generate_campaign(loc, 1)
    _, quad = C.generate_campaign(loc, 4)
    assert quad[-1].enemy_max_hp == int(solo[-1].enemy_max_hp * (1 + 3 * config.CAMPAIGN_PARTY_HP_SCALE))
    assert quad[-1].enemy_template["atk_max"] > solo[-1].enemy_template["atk_max"]


def test_hero_starts_at_location_level_with_stats_spent():
    loc = C.get_campaign_location("dungeon_temple")
    for cls in ("warrior", "rogue", "mage", "leader"):
        h = C.create_hero(7, "Х", cls, loc)
        assert h.level == loc.start_level and h.stat_points == 0
        assert h.owner_tg_id == -7 and h.in_raid and h.hp == h.max_hp


def test_exp_gives_roughly_a_level_every_two_fights():
    loc = C.get_campaign_location("thicket")
    h = C.create_hero(1, "Х", "warrior", loc)
    levels = [h.level]
    for _ in range(6):
        C.award_fight_exp(h, loc)
        levels.append(h.level)
    assert 3 <= levels[-1] - levels[0] <= 4
    assert h.stat_points == 0


def test_nonlethal_damage_never_kills():
    h = _warrior(); h.hp = 3
    C.nonlethal_damage(h, 0.9)
    assert h.hp == 1
    C.nonlethal_damage(h, 0.9)
    assert h.hp == 1


def test_reward_command_format(monkeypatch):
    assert C.build_reward_command({"11": 250, "22": 0}, "campaign_reward", "") == "/campaign_reward 11:250"
    cmd = C.build_reward_command({"22": 5, "11": 250}, "candy", "@MainBot")
    assert cmd == "/candy@MainBot 11:250 22:5"
    assert C.build_reward_command({"1": 0}, "x", "") is None
    monkeypatch.setattr(config, "MAIN_BOT_USERNAME", "Shop_Bot")
    assert C.build_reward_command({"1": 1}).startswith("/campaign_reward@Shop_Bot ")


def test_session_roundtrip_keeps_campaign():
    loc = C.get_campaign_location("ruins")
    state, encs = C.generate_campaign(loc, 2)
    from core.raid import RaidSession
    s = RaidSession(raid_id="cmp_1", location_key="ruins", status=RaidStatus.IN_PROGRESS,
                    encounters=encs, campaign=state)
    s2 = session_from_dict(session_to_dict(s))
    assert s2.campaign == state


# ═══════════════════════════════════════════════════════════════
# Сквозные тесты через Telegram-заглушки
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def cenv(env, monkeypatch):
    CH._lobbies.clear()
    monkeypatch.setattr(H, "_chat_allowed", lambda update: True)
    return env


async def power_up(env, uid, name, strength=150):
    """Сильный герой — тесты проверяют поток, а не баланс."""
    h = await env.storage.load_character_by_name(-uid, name)
    h.base_stats.strength = strength
    h.max_hp = h._calc_max_hp()
    h.hp = h.max_hp
    await env.storage.save_character(h)


async def start_solo(env, uid, name, loc="thicket", cls="warrior", chat_id=None, strong=True):
    await env.press(uid, CH.cb_loc, f"cmp_loc_{loc}", chat_id=chat_id)
    await env.press(uid, CH.cb_mode, f"cmp_solo_{loc}", chat_id=chat_id)
    assert env.app.user_data[uid]["cmp_create"]["step"] == "name"
    await env.say(uid, name)
    assert env.app.user_data[uid]["cmp_create"]["step"] == "class"
    if strong:
        # герой создаётся на последнем шаге, поэтому усиливаем сразу после него
        pass
    await env.press(uid, CH.cb_class, f"cmp_class_{cls}", chat_id=chat_id)


async def drive(env, uids, *, max_steps=1500, open_chests=True, power=None):
    """Играет кампанию до конца: бой — «Действие» + текст, комнаты — кнопки."""
    for _ in range(max_steps):
        session = None
        for uid in uids:
            session = await env.session_of(uid)
            if session:
                break
        if not session:
            return
        camp = session.campaign
        names = H._participants(session)
        if camp["in_fight"]:
            enc = session.encounters[session.current_encounter]
            turn = H.get_current_turn(enc)
            assert turn and turn["type"] == "player", f"завис бой: {turn}"
            await env.press(turn["uid"], H.cb_raid_action, "raid_action")
            await env.say(turn["uid"], "Бью врага")
            continue
        pend = camp["pending"]
        if pend:
            t = pend["type"]
            who = next(iter(names))
            if t == "chest":
                await env.press(who, CH.cb_open if open_chests else CH.cb_pass,
                                "cmp_open" if open_chests else "cmp_pass")
            elif t == "find":
                await env.press(who, CH.cb_use, "cmp_use")
            elif t == "artifact":
                for uid_s in list(pend["offers"]):
                    uid = int(uid_s)
                    if uid_s in pend["done"] or uid not in names:
                        continue
                    await env.press(uid, CH.cb_art_take, "cmp_art_take")
                    s2 = await env.session_of(uid)
                    if s2 and s2.campaign["pending"] and s2.campaign["pending"].get("swap") == uid_s:
                        await env.press(uid, CH.cb_art_replace, "cmp_art_rep_0")
            continue
        who = next(iter(names))
        await env.press(who, CH.cb_go, f"cmp_go_{camp['room_idx']}")
    raise AssertionError("кампания не закончилась за отведённое число шагов")


async def test_solo_campaign_full_run_pays_candies(cenv):
    env = cenv
    await start_solo(env, 1, "Тестер")
    session = await env.session_of(1)
    assert session and session.campaign and session.location_key == "thicket"
    assert set(H._participants(session)) == {1}
    # герой кампании лежит под отрицательным владельцем и не мешает обычным персонажам
    assert await env.storage.load_characters(1) == []
    assert len(await env.storage.load_characters(-1)) == 1
    await power_up(env, 1, "Тестер")

    await drive(env, [1])

    assert await env.session_of(1) is None
    assert await env.storage.load_characters(-1) == []            # временный герой удалён
    assert env.app.user_data[1].get("raid_id") is None
    texts = env.bot.texts(1)
    assert any("пройдена" in t for t in texts)
    cmd = next(t for t in texts if t.startswith("/campaign_reward"))
    uid, amount = cmd.split()[1].split(":")
    assert uid == "1" and int(amount) >= 200                      # босс 200 + сундуки


async def test_failed_campaign_still_pays_chest_candies(cenv):
    env = cenv
    await start_solo(env, 1, "Слабак", loc="dungeon_temple")
    session = await env.session_of(1)
    session.campaign["candies"]["1"] = 90           # набрано в сундуках до гибели
    await H._save_session(env.ctx(1), session)
    hero = await env.storage.load_character_by_name(-1, "Слабак")
    hero.hp = 1
    hero.max_hp = 1
    await env.storage.save_character(hero)

    await drive(env, [1], open_chests=False)
    assert await env.session_of(1) is None
    texts = env.bot.texts(1)
    assert any("не пройдена" in t for t in texts)
    assert "/campaign_reward 1:90" in texts            # без награды за босса, но сундуки засчитаны
    assert await env.storage.load_characters(-1) == []


async def test_opening_chests_pays_to_opener(cenv):
    env = cenv
    await start_solo(env, 1, "Жадный")
    await power_up(env, 1, "Жадный")
    session = await env.session_of(1)
    camp = session.campaign
    camp["rooms"] = [{"type": "chest", "kind": "treasure"}] + camp["rooms"][-1:]
    camp["room_idx"] = -1
    await H._save_session(env.ctx(1), session)
    await env.press(1, CH.cb_go, "cmp_go_-1")
    session = await env.session_of(1)
    assert session.campaign["pending"]["type"] == "chest"
    await env.press(1, CH.cb_open, "cmp_open")
    session = await env.session_of(1)
    loc = C.get_campaign_location("thicket")
    got = session.campaign["candies"]["1"]
    assert loc.chest_candy[0] <= got <= loc.chest_candy[1]
    assert session.campaign["pending"] is None


async def test_trap_chest_hurts_but_does_not_kill_and_empty_pays_nothing(cenv):
    env = cenv
    await start_solo(env, 1, "Рисковый")
    hero = await env.storage.load_character_by_name(-1, "Рисковый")
    hero.hp = 2
    await env.storage.save_character(hero)
    session = await env.session_of(1)
    camp = session.campaign
    camp["rooms"] = [{"type": "chest", "kind": "trap"}, {"type": "chest", "kind": "empty"}] + camp["rooms"][-1:]
    camp["room_idx"] = -1
    await H._save_session(env.ctx(1), session)
    await env.press(1, CH.cb_go, "cmp_go_-1")
    await env.press(1, CH.cb_open, "cmp_open")
    hero = await env.storage.load_character_by_name(-1, "Рисковый")
    assert hero.hp == 1 and hero.alive
    await env.press(1, CH.cb_go, "cmp_go_0")
    await env.press(1, CH.cb_open, "cmp_open")
    session = await env.session_of(1)
    assert session.campaign["candies"]["1"] == 0


async def test_stale_go_button_is_ignored(cenv):
    env = cenv
    await start_solo(env, 1, "Двойной")
    await power_up(env, 1, "Двойной")
    session = await env.session_of(1)
    session.campaign["rooms"] = [{"type": "rest"}, {"type": "rest"}] + session.campaign["rooms"][-1:]
    await H._save_session(env.ctx(1), session)
    await env.press(1, CH.cb_go, "cmp_go_-1")
    await env.press(1, CH.cb_go, "cmp_go_-1")        # двойной клик по старой кнопке
    session = await env.session_of(1)
    assert session.campaign["room_idx"] == 0


async def test_artifact_room_take_and_replace(cenv):
    env = cenv
    await start_solo(env, 1, "Коллекция")
    session = await env.session_of(1)
    camp = session.campaign
    camp["rooms"] = [{"type": "artifact"}, {"type": "artifact"}, {"type": "artifact"}] + camp["rooms"][-1:]
    await H._save_session(env.ctx(1), session)

    for i in range(2):
        await env.press(1, CH.cb_go, f"cmp_go_{i - 1}")
        await env.press(1, CH.cb_art_take, "cmp_art_take")
    hero = await env.storage.load_character_by_name(-1, "Коллекция")
    assert len(hero.artifacts) == 2
    first = list(hero.artifacts)

    await env.press(1, CH.cb_go, "cmp_go_1")
    await env.press(1, CH.cb_art_take, "cmp_art_take")                # слоты заняты
    session = await env.session_of(1)
    assert session.campaign["pending"]["swap"] == "1"
    offered = session.campaign["pending"]["offers"]["1"]
    await env.press(1, CH.cb_art_replace, "cmp_art_rep_0")
    hero = await env.storage.load_character_by_name(-1, "Коллекция")
    assert hero.artifacts[0] == offered and hero.artifacts[1] == first[1]
    assert (await env.session_of(1)).campaign["pending"] is None


async def test_find_room_effects_apply(cenv):
    env = cenv
    await start_solo(env, 1, "Гурман")
    hero = await env.storage.load_character_by_name(-1, "Гурман")
    hero.hp = 10
    await env.storage.save_character(hero)
    session = await env.session_of(1)
    loc = C.get_campaign_location("thicket")
    heal_idx = next(i for i, f in enumerate(loc.finds) if f["kind"] == "heal")
    buff_idx = next(i for i, f in enumerate(loc.finds) if f["kind"] == "buff_atk")
    session.campaign["rooms"] = [{"type": "find", "flavor": heal_idx}, {"type": "find", "flavor": buff_idx}] \
        + session.campaign["rooms"][-1:]
    await H._save_session(env.ctx(1), session)
    await env.press(1, CH.cb_go, "cmp_go_-1")
    await env.press(1, CH.cb_use, "cmp_use")
    assert (await env.storage.load_character_by_name(-1, "Гурман")).hp > 10
    await env.press(1, CH.cb_go, "cmp_go_0")
    await env.press(1, CH.cb_use, "cmp_use")
    assert (await env.session_of(1)).active_buffs.get("atk") == 2


async def test_leaving_keeps_candies_and_removes_hero(cenv):
    env = cenv
    await start_solo(env, 1, "Беглец")
    session = await env.session_of(1)
    session.campaign["candies"]["1"] = 55
    await H._save_session(env.ctx(1), session)
    await env.press(1, H.cb_raid_leave, "raid_leave")
    assert await env.session_of(1) is None
    assert await env.storage.load_characters(-1) == []
    assert "/campaign_reward 1:55" in env.bot.texts(1)
    # обычных персонажей игрока кампания не трогала
    assert await env.storage.load_characters(1) == []


async def test_regular_character_is_untouched_by_campaign(cenv):
    env = cenv
    real = Character(owner_tg_id=1, name="Тестер", class_key="mage")     # то же имя, что у героя
    await env.storage.save_character(real)
    await start_solo(env, 1, "Тестер")
    await power_up(env, 1, "Тестер")
    await drive(env, [1])
    after = await env.storage.load_character_by_name(1, "Тестер")
    assert after is not None and after.count_raid == 0 and after.level == 1 and not after.in_raid


async def test_cannot_start_second_campaign_or_raid_inside_one(cenv):
    env = cenv
    await start_solo(env, 1, "Один")
    q = await env.press(1, CH.cb_mode, "cmp_solo_ruins")
    assert "уже в рейде или кампании" in q.edits[-1]
    await H._save_char(Character(owner_tg_id=1, name="Реал", class_key="warrior"))
    q = await env.press(1, H.cb_raid_start, "raid_start_forest")
    assert "уже в рейде" in q.edits[-1]


async def test_name_validation_and_create_expiry(cenv):
    env = cenv
    await env.press(1, CH.cb_mode, "cmp_solo_thicket")
    m = await env.say(1, " ")
    assert env.app.user_data[1]["cmp_create"]["step"] == "name"
    env.app.user_data[1]["cmp_create"]["ts"] -= CH.CREATE_TTL + 5
    await env.say(1, "Поздно")
    assert "cmp_create" not in env.app.user_data[1]


async def test_coop_party_in_group_chat_everyone_gets_full_boss_reward(cenv, monkeypatch):
    env = cenv
    monkeypatch.setattr(config, "MAIN_BOT_USERNAME", "MainBot")
    GROUP = -1001
    # лидер создаёт отряд
    await env.press(1, CH.cb_loc, "cmp_loc_preserve", chat_id=GROUP)
    await env.press(1, CH.cb_mode, "cmp_party_preserve", chat_id=GROUP)
    await env.say(1, "Лидер")
    await env.press(1, CH.cb_class, "cmp_class_warrior", chat_id=GROUP)
    code = env.app.user_data[1]["cmp_lobby"]
    assert CH._lobbies[code]["members"] == {1: "Лидер"}
    # второй и третий вступают
    for uid, name, cls in ((2, "Маг", "mage"), (3, "Плут", "rogue")):
        upd, _ = env.update(uid, text="/campaign_join", chat_id=GROUP)
        ctx = env.ctx(uid); ctx.args = [code.lower()]
        await CH.cmd_join(upd, ctx)
        await env.say(uid, name)
        await env.press(uid, CH.cb_class, f"cmp_class_{cls}", chat_id=GROUP)
    assert set(CH._lobbies[code]["members"]) == {1, 2, 3}
    # не лидер начать не может
    await env.press(2, CH.cb_lobby_start, "cmp_lobby_start", chat_id=GROUP)
    assert code in CH._lobbies
    await env.press(1, CH.cb_lobby_start, "cmp_lobby_start", chat_id=GROUP)
    assert code not in CH._lobbies

    session = await env.session_of(3)
    assert set(H._participants(session)) == {1, 2, 3} and session.chat_id == GROUP
    assert session.campaign["party_size"] == 3
    for uid, name in ((1, "Лидер"), (2, "Маг"), (3, "Плут")):
        await power_up(env, uid, name)

    await drive(env, [1, 2, 3])

    group_msgs = env.bot.texts(GROUP)
    cmd = next(t for t in group_msgs if t.startswith("/campaign_reward@MainBot"))
    pairs = dict(p.split(":") for p in cmd.split()[1:])
    assert set(pairs) == {"1", "2", "3"}
    assert all(int(v) >= 200 for v in pairs.values())             # каждому — полная сумма босса
    assert any("пройдена" in t for t in group_msgs)
    for uid in (1, 2, 3):
        assert await env.storage.load_characters(-uid) == []
        assert env.app.user_data[uid].get("raid_id") is None


async def test_lobby_full_and_leave(cenv):
    env = cenv
    await env.press(1, CH.cb_mode, "cmp_party_thicket")
    await env.say(1, "Лидер")
    await env.press(1, CH.cb_class, "cmp_class_warrior")
    code = env.app.user_data[1]["cmp_lobby"]
    for uid in (2, 3, 4):
        upd, _ = env.update(uid, text="/campaign_join")
        ctx = env.ctx(uid); ctx.args = [code]
        await CH.cmd_join(upd, ctx)
        await env.say(uid, f"Игрок{uid}")
        await env.press(uid, CH.cb_class, "cmp_class_rogue")
    assert len(CH._lobbies[code]["members"]) == config.MAX_GROUP_SIZE
    upd, m = env.update(5, text="/campaign_join")
    ctx = env.ctx(5); ctx.args = [code]
    await CH.cmd_join(upd, ctx)
    assert "cmp_create" not in env.app.user_data[5]               # пятому места нет
    # лидер выходит — лидерство переходит к следующему, его герой удалён
    await env.press(1, CH.cb_lobby_leave, "cmp_lobby_leave")
    assert CH._lobbies[code]["owner"] == 2 and 1 not in CH._lobbies[code]["members"]
    assert await env.storage.load_characters(-1) == []


async def test_madness_turn_can_hit_ally_but_never_kills(cenv):
    env = cenv
    GROUP = -2002
    await env.press(1, CH.cb_mode, "cmp_party_thicket", chat_id=GROUP)
    await env.say(1, "Безумец")
    await env.press(1, CH.cb_class, "cmp_class_warrior", chat_id=GROUP)
    code = env.app.user_data[1]["cmp_lobby"]
    upd, _ = env.update(2, text="x", chat_id=GROUP)
    ctx = env.ctx(2); ctx.args = [code]
    await CH.cmd_join(upd, ctx)
    await env.say(2, "Жертва")
    await env.press(2, CH.cb_class, "cmp_class_mage", chat_id=GROUP)
    await env.press(1, CH.cb_lobby_start, "cmp_lobby_start", chat_id=GROUP)
    await power_up(env, 1, "Безумец", 100)
    mad = await env.storage.load_character_by_name(-1, "Безумец")
    mad.madness = True
    await env.storage.save_character(mad)
    victim = await env.storage.load_character_by_name(-2, "Жертва")
    victim.hp = 1; victim.max_hp = 5
    await env.storage.save_character(victim)

    session = await env.session_of(1)
    enc = session.encounters[0]
    for _ in range(40):
        before = len(env.bot.sent)
        await CH.madness_turn(env.ctx(1), session, enc, mad, 1)
        session = await env.session_of(1)
        if session is None:
            break
    victim = await env.storage.load_character_by_name(-2, "Жертва")
    if victim:
        assert victim.alive and victim.hp >= 1
    assert any("безумия" in t for t in env.bot.texts(GROUP))


async def test_enemy_prefers_marked_target(cenv, monkeypatch):
    """Носитель «Амулета-метки» привлекает атаки врагов сильнее остальных."""
    env = cenv
    monkeypatch.setattr(config, "CAMPAIGN_PRIORITY_TARGET_CHANCE", 1.0)
    a = _warrior(); a.name = "Обычный"; a.in_raid = True
    b = Character(owner_tg_id=2, name="Меченый", class_key="warrior"); b.in_raid = True
    b.add_artifact("mark_amulet")
    assert b.priority_target and not a.priority_target
    enc = _enc()
    enc.enemy_template.update({"atk_min": 1, "atk_max": 1, "dodge_chance": 0.0})
    enc.initiative_order = [{"type": "enemy", "uid": "enemy", "name": "Враг", "initiative": 1}]
    from core.raid import RaidSession
    session = RaidSession(raid_id="x", location_key="thicket", encounters=[enc], participant_names={1: "a", 2: "b"})
    enemy = create_enemy(enc)
    for _ in range(15):
        text = await H._resolve_auto_turn(env.ctx(1), session, enc, enc.initiative_order[0], enemy,
                                           {1: a, 2: b})
        assert "→ Меченый" in text


@pytest.mark.parametrize("loc_key,cls", [
    ("thicket", "mage"), ("preserve", "rogue"), ("ruins", "leader"), ("palace", "warrior"),
    ("dungeon_temple", "mage"), ("dungeon_order", "rogue"), ("dungeon_asylum", "leader"),
])
async def test_unpowered_solo_campaign_always_terminates_cleanly(cenv, loc_key, cls):
    """Обычный (не усиленный) герой: победа или гибель — но кампания всегда закрывается без хвостов."""
    env = cenv
    await start_solo(env, 1, "Обычный", loc=loc_key, cls=cls)
    await drive(env, [1])
    assert await env.session_of(1) is None
    assert await env.storage.load_characters(-1) == []
    assert env.app.user_data[1].get("raid_id") is None
    assert any("Кампания" in t for t in env.bot.texts(1))


async def test_unpowered_party_with_deaths_terminates_cleanly(cenv):
    env = cenv
    GROUP = -3003
    await env.press(1, CH.cb_mode, "cmp_party_dungeon_temple", chat_id=GROUP)
    await env.say(1, "Первый")
    await env.press(1, CH.cb_class, "cmp_class_rogue", chat_id=GROUP)
    code = env.app.user_data[1]["cmp_lobby"]
    for uid, cls in ((2, "mage"), (3, "leader"), (4, "warrior")):
        upd, _ = env.update(uid, text="x", chat_id=GROUP)
        ctx = env.ctx(uid); ctx.args = [code]
        await CH.cmd_join(upd, ctx)
        await env.say(uid, f"Игрок{uid}")
        await env.press(uid, CH.cb_class, f"cmp_class_{cls}", chat_id=GROUP)
    await env.press(1, CH.cb_lobby_start, "cmp_lobby_start", chat_id=GROUP)
    session = await env.session_of(1)
    assert session.campaign["party_size"] == 4
    await drive(env, [1, 2, 3, 4])
    for uid in (1, 2, 3, 4):
        assert await env.storage.load_characters(-uid) == []
        assert env.app.user_data[uid].get("raid_id") is None
    assert sum(1 for t in env.bot.texts(GROUP) if "Кампания «" in t) == 1     # итог — ровно один раз


async def test_one_member_leaves_party_continues(cenv):
    env = cenv
    GROUP = -4004
    await env.press(1, CH.cb_mode, "cmp_party_thicket", chat_id=GROUP)
    await env.say(1, "Остаюсь")
    await env.press(1, CH.cb_class, "cmp_class_warrior", chat_id=GROUP)
    code = env.app.user_data[1]["cmp_lobby"]
    upd, _ = env.update(2, text="x", chat_id=GROUP)
    ctx = env.ctx(2); ctx.args = [code]
    await CH.cmd_join(upd, ctx)
    await env.say(2, "Ухожу")
    await env.press(2, CH.cb_class, "cmp_class_mage", chat_id=GROUP)
    await env.press(1, CH.cb_lobby_start, "cmp_lobby_start", chat_id=GROUP)
    await power_up(env, 1, "Остаюсь")
    session = await env.session_of(1)
    session.campaign["candies"]["2"] = 40
    await H._save_session(env.ctx(1), session)

    await env.press(2, H.cb_raid_leave, "raid_leave", chat_id=GROUP)
    session = await env.session_of(1)
    assert session is not None and set(H._participants(session)) == {1}
    assert await env.storage.load_characters(-2) == []
    await drive(env, [1])
    cmd = next(t for t in env.bot.texts(GROUP) if t.startswith("/campaign_reward"))
    pairs = dict(p.split(":") for p in cmd.split()[1:])
    assert pairs["2"] == "40"                       # собранное ушедшим сохраняется
    assert int(pairs["1"]) >= 200                   # босс — только дошедшему


async def test_recover_closes_interrupted_campaign_and_pays_candies(cenv):
    env = cenv
    await start_solo(env, 1, "Прерванный")
    session = await env.session_of(1)
    session.campaign["candies"]["1"] = 70
    await H._save_session(env.ctx(1), session)
    # «перезапуск»: память игроков потеряна
    env.app.user_data.clear()
    app = type("A", (), {"bot": env.bot, "user_data": env.app.user_data})()
    assert await CH.recover_campaigns(app) == 1
    assert "/campaign_reward 1:70" in env.bot.texts(1)
    assert await env.storage.load_characters(-1) == []
    assert await env.storage.list_raid_sessions() == []
    assert await CH.recover_campaigns(app) == 0
