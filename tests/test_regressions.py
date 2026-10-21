"""Регрессионные тесты на баги боевой системы, персонажей, рынка и валидации."""
import sys; sys.path.insert(0, ".")

import math
import pytest

from core.character import Character
from core.classes import CLASSES
from core.combat import (
    StatusEffect, StatusEffectInstance, resolve_turn, apply_modifiers, calc_damage,
)
from core.economy import Market
from core.items import Item, ItemTemplate, ItemEffect, ItemType, Rarity
from core.raid import (
    RaidEncounter, create_enemy, resolve_player_turn, resolve_enemy_turn,
    apply_mob_status_effects, player_key, ENEMY_KEY, generate_loot,
)
from utils.validators import validate_nn_response, validate_character_name


def _enemy_template(**over):
    tpl = {
        "name": "Dummy", "hp": 1000, "atk_min": 10, "atk_max": 10,
        "defense": 0, "dodge_chance": 0.0, "crit_chance": 0.0,
        "crit_multiplier": 2.0, "atk_damage_type": "physical", "attack_secondary": None,
    }
    tpl.update(over)
    return tpl


@pytest.fixture
def enc():
    return RaidEncounter(enemy_hp=1000, enemy_max_hp=1000, enemy_template=_enemy_template())


@pytest.fixture
def hero():
    c = Character(owner_tg_id=7, name="Hero", class_key="warrior")
    c.base_stats.agility = 0  # без уклонения и крита — детерминированнее
    c.hp = c.max_hp = 1000
    return c


# ─── Персонаж ─────────────────────────────────────────────────────

class TestCharacter:
    def test_class_template_not_mutated_by_stat_allocation(self):
        before = CLASSES["warrior"].base_stats.strength
        c = Character(owner_tg_id=1, name="A", class_key="warrior")
        c.stat_points = 5
        for _ in range(5):
            assert c.allocate_stat("strength")
        assert CLASSES["warrior"].base_stats.strength == before
        fresh = Character(owner_tg_id=2, name="B", class_key="warrior")
        assert fresh.base_stats.strength == before

    def test_item_hp_bonus_raises_max_hp(self):
        c = Character(owner_tg_id=1, name="A", class_key="warrior")
        base = c.max_hp
        ring = Item(
            ItemTemplate("Ring", ItemType.ACCESSORY, Rarity.COMMON, ItemEffect(hp_bonus=40)),
            uid="r1", durability=10, durability_max=10,
        )
        c.inventory.append(ring)
        assert c.equip(ring)
        assert c.max_hp == base + 40
        c.unequip(ring)
        assert c.max_hp == base

    def test_experience_without_levelup_does_not_heal(self):
        c = Character(owner_tg_id=1, name="A", class_key="warrior")
        c.hp = 10
        assert c.add_experience(1) is False
        assert c.hp == 10

    def test_dead_character_keeps_zero_hp_after_reload(self):
        from data.models import character_to_dict, character_from_dict
        c = Character(owner_tg_id=1, name="A", class_key="warrior")
        c.alive = False
        c.hp = 0
        restored = character_from_dict(character_to_dict(c), {})
        assert restored.hp == 0 and restored.alive is False

    def test_restore_after_raid(self):
        c = Character(owner_tg_id=1, name="A", class_key="leader")
        c.summon_companion()
        c.alive = False
        c.hp = 0
        c.in_raid = True
        c.restore_after_raid()
        assert c.alive and c.hp == c.max_hp and not c.in_raid and c.companion is None

    def test_cooldown_every_third_raid(self):
        c = Character(owner_tg_id=1, name="A", class_key="warrior")
        assert c.can_raid()
        c.mark_raid_done()  # 1
        assert c.can_raid()
        c.mark_raid_done()  # 2
        assert c.can_raid()
        c.mark_raid_done()  # 3 -> кулдаун
        assert c.count_raid == 3
        assert not c.can_raid()
        assert c.raid_cooldown_remaining() > 0
        c.last_raid_time -= 10 ** 6  # время прошло
        assert c.can_raid()

    def test_duplicate_item_uids_are_fixed_on_load(self):
        from data.models import character_to_dict, character_from_dict
        c = Character(owner_tg_id=1, name="A", class_key="warrior")
        for name in ("Sword", "Axe"):
            c.inventory.append(Item(
                ItemTemplate(name, ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=1)),
                uid="loot_0", durability=5, durability_max=5,
            ))
        restored = character_from_dict(character_to_dict(c), {})
        uids = [i.uid for i in restored.inventory]
        assert len(set(uids)) == 2


# ─── Статус-эффекты ───────────────────────────────────────────────

class TestStatusEffects:
    def test_mob_poison_hurts_player_not_enemy(self, hero, enc):
        enemy = create_enemy(enc)
        enc.active_effects = apply_mob_status_effects(
            {}, "poison", 100, target_key=player_key(hero))
        hp0, ehp0 = hero.hp, enemy.hp
        resolve_player_turn(hero, enemy, enc, None)
        assert hero.hp == hp0 - 6          # яд тикнул по игроку
        # враг получил только прямой урон от атаки, без яда игрока
        assert enemy.hp <= ehp0

    def test_player_bleed_hurts_enemy_not_player(self, hero, enc):
        enemy = create_enemy(enc)
        enc.active_effects = apply_mob_status_effects(
            {}, "bleed", 100, target_key=ENEMY_KEY)
        hp0 = hero.hp
        resolve_enemy_turn(hero, enemy, enc, None)
        assert enc.enemy_hp == 1000 - 10   # кровотечение тикнуло по врагу в его ход
        # игрок получил лишь удар врага (не больше 13), а не удар + кровотечение (≥16)
        assert hp0 - hero.hp <= 13

    def test_dot_ticks_once_per_own_turn(self, hero, enc):
        enemy = create_enemy(enc)
        key = player_key(hero)
        enc.active_effects = {key: [StatusEffectInstance(StatusEffect.POISON, 3, 5.0)]}
        resolve_player_turn(hero, enemy, enc, None)       # ход игрока: тик
        resolve_enemy_turn(hero, enemy, enc, None)        # ход врага: яд игрока не тикает
        poison = enc.active_effects[key][0]
        assert poison.duration == 2
        assert hero.hp >= 1000 - 5 - 14

    def test_dot_duration_is_three_turns(self, hero, enc):
        enemy = create_enemy(enc)
        key = player_key(hero)
        enc.active_effects = {key: [StatusEffectInstance(StatusEffect.BLEED, 3, 4.0)]}
        for _ in range(3):
            resolve_player_turn(hero, enemy, enc, None)
        assert key not in enc.active_effects or not enc.active_effects[key]

    def test_taunt_from_player_shields_player_not_enemy(self):
        effects = {}
        apply_modifiers(effects, [{"modifier": "TAUNT", "value": 1.0, "target": "player"}],
                        "p1", "enemy", "player")
        assert [e.kind for e in effects["p1"]] == [StatusEffect.SHIELD]
        assert "enemy" not in effects

    def test_player_stun_hits_enemy_and_player_still_attacks(self, hero, enc):
        enemy = create_enemy(enc)
        result = resolve_player_turn(
            hero, enemy, enc,
            [{"modifier": "STUN", "value": 1.0, "target": "enemy"}],
        )
        assert result.final_damage > 0                     # игрок не оглушил сам себя
        assert any(e.kind == StatusEffect.STUNNED for e in enc.active_effects[ENEMY_KEY])

    def test_stunned_enemy_skips_turn_then_gets_immunity(self, hero, enc):
        enemy = create_enemy(enc)
        enc.active_effects = {ENEMY_KEY: [StatusEffectInstance(StatusEffect.STUNNED, 1)]}
        hp0 = hero.hp
        atk = resolve_enemy_turn(hero, enemy, enc, None)
        assert atk.final_damage == 0 and hero.hp == hp0
        kinds = [e.kind for e in enc.active_effects[ENEMY_KEY]]
        assert StatusEffect.STUNNED not in kinds and StatusEffect.STUN_IMMUNE in kinds
        # повторное оглушение сразу после первого невозможно
        apply_mob_status_effects(enc.active_effects, "stun", 50, target_key=ENEMY_KEY)
        assert not any(e.kind == StatusEffect.STUNNED for e in enc.active_effects[ENEMY_KEY])

    def test_self_buff_survives_until_enemy_attacks(self, hero, enc):
        """DODGE_BONUS, повешенный игроком на себя, не должен исчезать в конце его же хода."""
        enemy = create_enemy(enc)
        resolve_player_turn(hero, enemy, enc,
                            [{"modifier": "DODGE_BONUS", "value": 0.3, "target": "player"}])
        buffs = enc.active_effects.get(player_key(hero), [])
        assert any(e.kind == StatusEffect.DODGE_BUFF for e in buffs)

    def test_dodge_buff_makes_player_dodge(self, hero, enc):
        enemy = create_enemy(enc)
        enc.active_effects = {player_key(hero): [StatusEffectInstance(StatusEffect.DODGE_BUFF, 2, 1.0)]}
        # бонус 1.0 -> уклонение ограничено DODGE_MAX=0.5, значит часть ударов промахнётся
        results = []
        for _ in range(60):
            enc.active_effects[player_key(hero)] = [StatusEffectInstance(StatusEffect.DODGE_BUFF, 2, 1.0)]
            results.append(resolve_enemy_turn(hero, enemy, enc, None).is_dodged)
        assert any(results) and not all(results)

    def test_enemy_weak_spot_modifier_is_applied(self, hero, enc):
        enemy = create_enemy(enc)
        hero.defense  # свойство; защита у воина мала
        strong = [resolve_enemy_turn(hero, enemy, enc, [
            {"modifier": "WEAK_SPOT_FOUND", "value": 2.0, "target": "enemy"}]).final_damage
            for _ in range(10)]
        weak = [resolve_enemy_turn(hero, enemy, enc, [
            {"modifier": "WEAK_SPOT_FOUND", "value": 0.3, "target": "enemy"}]).final_damage
            for _ in range(10)]
        assert min(strong) > max(weak)

    def test_zero_valued_stun_and_crit_do_nothing(self):
        effects = {}
        apply_modifiers(effects, [
            {"modifier": "STUN", "value": 0, "target": "enemy"},
            {"modifier": "CRIT_BOOST", "value": 0, "target": "player"},
            {"modifier": "DODGE_BONUS", "value": 0, "target": "player"},
        ], "p1", "enemy", "player")
        assert effects == {}

    def test_garbage_modifier_values_do_not_crash(self):
        effects = {}
        apply_modifiers(effects, [
            {"modifier": "STUN", "value": "abc", "target": "enemy"},
            {"modifier": "TAUNT", "value": None, "target": "player"},
            {"modifier": "DODGE_BONUS", "value": float("nan"), "target": "player"},
            "not a dict",
        ], "p1", "enemy", "player")
        assert not effects.get("enemy")

    def test_dot_death_of_attacker_skips_attack(self, hero, enc):
        enemy = create_enemy(enc)
        hero.hp = 3
        enc.active_effects = {player_key(hero): [StatusEffectInstance(StatusEffect.POISON, 3, 9.0)]}
        result = resolve_player_turn(hero, enemy, enc, None)
        assert hero.hp == 0 and result.final_damage == 0
        assert enemy.hp == 1000

    def test_repeated_dot_refreshes_instead_of_stacking(self):
        eff = apply_mob_status_effects({}, "poison", 100, target_key="p1")
        eff = apply_mob_status_effects(eff, "poison", 100, target_key="p1")
        eff = apply_mob_status_effects(eff, "poison", 200, target_key="p1")
        poisons = [e for e in eff["p1"] if e.kind == StatusEffect.POISON]
        assert len(poisons) == 1 and poisons[0].value == 12.0

    def test_enemy_dies_from_own_dot(self, hero, enc):
        enc.enemy_hp = 2
        enemy = create_enemy(enc)
        enc.active_effects = {ENEMY_KEY: [StatusEffectInstance(StatusEffect.BLEED, 3, 5.0)]}
        resolve_enemy_turn(hero, enemy, enc, None)
        assert enc.enemy_hp == 0


# ─── Лут, рынок, валидация ────────────────────────────────────────

class TestLootAndMarket:
    def test_loot_uids_are_unique(self, location):
        items = []
        for _ in range(5):
            items += generate_loot(location, 30, 5, ["warrior", "rogue", "mage", "leader"])
        assert len(items) > 3
        assert len({i.uid for i in items}) == len(items)

    def test_cancel_inactive_listing_fails(self, warrior):
        m = Market()
        item = Item(ItemTemplate("X", ItemType.WEAPON, Rarity.COMMON, ItemEffect()),
                    uid="a", durability=1, durability_max=1)
        listing = m.create_listing(1, "Hero", item, 10)
        buyer = Character(owner_tg_id=2, name="B", class_key="warrior", gold=100)
        ok, *_ = m.buy_listing(listing.listing_id, buyer)
        assert ok
        # после продажи продавец уже не может «отменить» и получить предмет второй раз
        assert m.cancel_listing(listing.listing_id, 1) is False

    def test_cancel_active_listing_ok_once(self):
        m = Market()
        item = Item(ItemTemplate("X", ItemType.WEAPON, Rarity.COMMON, ItemEffect()),
                    uid="a", durability=1, durability_max=1)
        listing = m.create_listing(1, "Hero", item, 10)
        assert m.cancel_listing(listing.listing_id, 2) is False
        assert m.cancel_listing(listing.listing_id, 1) is True
        assert m.cancel_listing(listing.listing_id, 1) is False


class TestValidators:
    def test_nn_values_of_wrong_type(self):
        data = {"actions": [
            {"modifier": "WEAK_SPOT_FOUND", "value": "1.5", "target": "player"},
            {"modifier": "STUN", "value": None, "target": "enemy"},
            {"modifier": "TAUNT", "value": [1], "target": "player"},
            {"modifier": "CRIT_BOOST", "value": float("nan"), "target": "player"},
        ]}
        out = validate_nn_response(data, key="actions", check_narrative=False)
        assert [a["modifier"] for a in out] == ["WEAK_SPOT_FOUND"]
        assert out[0]["value"] == 1.5

    def test_nn_target_normalized(self):
        out = validate_nn_response(
            {"actions": [{"modifier": "STUN", "value": 1, "target": "ENEMY"},
                         {"modifier": "TAUNT", "value": 1, "target": "dragon"}]},
            check_narrative=False)
        assert out[0]["target"] == "enemy"
        assert out[1]["target"] == ""

    @pytest.mark.parametrize("name", ["  ", "--", "__", " Bob", "Bob ", "A  B", "X", "", "a" * 25,
                                      "Бо😀б", "名" * 20])
    def test_bad_names(self, name):
        assert validate_character_name(name) is False

    @pytest.mark.parametrize("name", ["Hero", "Дарк Рыцарь", "a-b", "Иван_2", "Aб"])
    def test_good_names(self, name):
        assert validate_character_name(name) is True


def test_group_without_topics_works_after_set_here(monkeypatch):
    """/set_here в обычной группе (без топиков) писал «отвечаю во всём чате», но бот молчал."""
    import bot.handlers as H
    from types import SimpleNamespace as NS
    monkeypatch.setattr(H, "_allowed_chat_id", -100)
    monkeypatch.setattr(H, "_allowed_topic_id", None)
    plain = NS(is_topic_message=False, message_thread_id=None)
    topic = NS(is_topic_message=True, message_thread_id=7)
    upd = lambda cid, m: NS(effective_chat=NS(type="supergroup", id=cid), effective_message=m)
    assert H._chat_allowed(upd(-100, plain)) and H._chat_allowed(upd(-100, topic))
    assert not H._chat_allowed(upd(-200, plain))
    monkeypatch.setattr(H, "_allowed_topic_id", 7)
    assert H._chat_allowed(upd(-100, topic)) and not H._chat_allowed(upd(-100, plain))
    assert not H._chat_allowed(upd(-100, NS(is_topic_message=True, message_thread_id=8)))
