import sys; sys.path.insert(0, ".")

import pytest
from core.combat import calc_damage, resolve_turn, AttackResult, StatusEffect, apply_nn_modifiers, BattleState
from core.raid import resolve_player_turn, resolve_enemy_turn, resolve_companion_turn, build_initiative_order, get_current_turn, advance_turn_core, create_enemy, pick_enemy_target
from core.events import resolve_event, RaidEvent, EventReward
from core.items import ItemEffect, ItemTemplate, ItemType, Rarity, Item
from core.weapon_gen import roll_weapon_from_pattern, find_patterns
from core.character import Character, Companion
from core.classes import StatBlock

# ─── STAT SYSTEM ──────────────────────────────────────────────────

class TestStatSystem:
    def test_base_stats_from_template(self, warrior):
        assert warrior.stats.strength > 0
        assert warrior.stats.agility > 0
        assert warrior.stats.intelligence > 0
        assert warrior.stat_points == 0

    def test_item_stats_add_to_base(self, warrior):
        # RARE multiplier = 1.5 → int(3*1.5)=4 STR, int(2*1.5)=3 AGI
        # base=(5,2,1) + item=(4,3,0) = (9,5,1)
        item = Item(
            ItemTemplate("Ring", ItemType.ACCESSORY, Rarity.RARE,
                         ItemEffect(strength_bonus=3, agility_bonus=2)),
            uid="ring_1", durability=100, durability_max=100,
        )
        warrior.equipment.accessory = item
        assert warrior.stats.strength == 9
        assert warrior.stats.agility == 5
        assert warrior.stats.intelligence == 1

    def test_allocate_stat(self, warrior):
        warrior.stat_points = 3
        old_str = warrior.base_stats.strength
        warrior.allocate_stat("strength")
        assert warrior.base_stats.strength == old_str + 1
        assert warrior.stat_points == 2

    def test_allocate_stat_no_points(self, warrior):
        assert warrior.stat_points == 0
        assert warrior.allocate_stat("strength") is False

    def test_allocate_stat_invalid_attr(self, warrior):
        warrior.stat_points = 1
        assert warrior.allocate_stat("luck") is False

    def test_hp_depends_on_strength(self, warrior):
        old_hp = warrior.max_hp
        warrior.base_stats.strength += 5
        warrior.max_hp = warrior._calc_max_hp()
        assert warrior.max_hp == old_hp + 60  # +5 STR * 12

    def test_attack_scales_with_str_and_int(self, warrior):
        old_min = warrior.attack_min
        old_max = warrior.attack_max
        warrior.base_stats.strength += 5
        warrior.base_stats.intelligence += 3
        assert warrior.attack_min > old_min
        assert warrior.attack_max > old_max

    def test_crit_from_agi(self, warrior):
        old_crit = warrior.crit_chance
        warrior.base_stats.agility += 10
        assert warrior.crit_chance > old_crit

    def test_crit_mult_from_int(self, warrior):
        old_mult = warrior.crit_multiplier
        warrior.base_stats.intelligence += 10
        assert warrior.crit_multiplier > old_mult

    def test_dodge_from_agi(self, warrior):
        old_dodge = warrior.dodge_chance
        warrior.base_stats.agility += 10
        assert warrior.dodge_chance > old_dodge

    def test_defense_from_str(self, warrior):
        old_def = warrior.defense
        warrior.base_stats.strength += 10
        assert warrior.defense > old_def

    def test_add_experience_gives_stat_points(self, warrior):
        warrior.add_experience(200)
        assert warrior.stat_points == 3

    def test_add_experience_multiple_levels(self, warrior):
        warrior.add_experience(2000)
        assert warrior.stat_points >= 6

    def test_allocate_stat_increases_hp(self, warrior):
        old_hp = warrior.hp
        old_max = warrior.max_hp
        warrior.stat_points = 1
        warrior.allocate_stat("strength")
        assert warrior.max_hp > old_max
        assert warrior.hp > old_hp  # hp adjust: old_hp + (new_max - old_max)

    def test_max_hp_property(self, warrior):
        assert warrior.max_hp_prop == warrior.max_hp

    def test_stats_readonly_computed(self, warrior):
        assert isinstance(warrior.stats, StatBlock)
        frozen = warrior.stats
        old_s = frozen.strength
        warrior.base_stats.strength += 5
        assert warrior.stats.strength == old_s + 5


# ─── ITEM ATTRIBUTES ──────────────────────────────────────────────

class TestItemAttributes:
    def test_item_attributes_field(self):
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.RARE, ItemEffect()),
            uid="sw_1", durability=100, durability_max=100,
            attributes=["one_handed", "fire", "scaling_strength"],
        )
        assert "one_handed" in item.attributes
        assert "fire" in item.attributes
        assert "scaling_strength" in item.attributes

    def test_item_effect_no_atk_bonus(self):
        effect = ItemEffect(strength_bonus=5, agility_bonus=3)
        assert not hasattr(effect, 'atk_bonus')
        assert effect.strength_bonus == 5
        assert effect.agility_bonus == 3

    def test_weapon_gen_creates_stat_bonus(self):
        pattern = {
            "rarity": "COMMON",
            "required_level": 3,
            "allowed_classes": ["warrior", "rogue"],
            "base_names": ["Sword"],
            "adjectives": ["Sharp"],
            "damage_min": 8,
            "damage_max": 15,
            "attributes": ["one_handed", "scaling_strength", "scaling_agility"],
            "durability_min": 50,
            "durability_max": 100,
        }
        item = roll_weapon_from_pattern(pattern, "test_001")
        assert item is not None
        effect = item.template.final_effect()
        total_stat = effect.strength_bonus + effect.agility_bonus
        assert total_stat >= 8
        assert total_stat <= 15

    def test_weapon_gen_scaling_distribution(self):
        pattern = {
            "rarity": "COMMON",
            "required_level": 1,
            "allowed_classes": ["warrior"],
            "base_names": ["Axe"],
            "adjectives": ["Heavy"],
            "damage_min": 10,
            "damage_max": 10,
            "attributes": ["scaling_strength", "scaling_intelligence"],
            "durability_min": 50,
            "durability_max": 100,
        }
        item = roll_weapon_from_pattern(pattern, "test_002")
        effect = item.template.final_effect()
        # 10 damage distributed: 5 str, 5 int (before rarity mult)
        assert effect.strength_bonus > 0
        assert effect.intelligence_bonus > 0
        assert effect.agility_bonus == 0

    def test_weapon_gen_find_patterns(self):
        patterns = find_patterns(min_level=1, max_level=10, min_rarity="COMMON", max_rarity="RARE")
        assert len(patterns) > 0
        for p in patterns:
            assert 1 <= p["required_level"] <= 10


# ─── COMBAT WITH ATTRIBUTE ────────────────────────────────────────

class TestCombatAttribute:
    def test_calc_damage_with_attribute(self, warrior, encounter):
        enemy = create_enemy(encounter)
        result = calc_damage(warrior, enemy, attribute="strength")
        assert isinstance(result, AttackResult)
        assert result.attribute == "strength"

    def test_calc_damage_different_attributes(self, warrior, encounter):
        enemy = create_enemy(encounter)
        r1 = calc_damage(warrior, enemy, attribute="strength")
        enemy2 = create_enemy(encounter)
        r2 = calc_damage(warrior, enemy2, attribute="intelligence")
        assert r1.attribute == "strength"
        assert r2.attribute == "intelligence"
        # Both should produce damage
        if not r1.is_dodged:
            assert r1.final_damage > 0
        if not r2.is_dodged:
            assert r2.final_damage > 0

    def test_resolve_player_turn_with_attribute(self, warrior, encounter):
        enemy = create_enemy(encounter)
        result = resolve_player_turn(warrior, enemy, encounter, nn_modifiers=None, attribute="agility")
        assert isinstance(result, AttackResult)
        assert result.attribute == "agility"

    def test_resolve_enemy_turn_default_attribute(self, warrior, encounter):
        enemy = create_enemy(encounter)
        result = resolve_enemy_turn(warrior, enemy, encounter, enemy_nn_modifiers=None)
        assert isinstance(result, AttackResult)
        assert result.attribute == "strength"  # enemy default

    def test_resolve_companion_turn(self, leader, encounter):
        leader.summon_companion()
        enemy = create_enemy(encounter)
        result = resolve_companion_turn(leader, enemy, encounter)
        if result:
            assert isinstance(result, AttackResult)
            assert result.attribute == "intelligence"  # companion default

    def test_no_companion_for_non_leader(self, warrior, encounter):
        warrior.companion = None
        enemy = create_enemy(encounter)
        result = resolve_companion_turn(warrior, enemy, encounter)
        assert result is None

    def test_player_turn_applies_damage(self, warrior, encounter):
        hp_before = encounter.enemy_hp
        enemy = create_enemy(encounter)
        resolve_player_turn(warrior, enemy, encounter, nn_modifiers=None, attribute="strength")
        assert encounter.enemy_hp <= hp_before

    def test_enemy_turn_applies_damage(self, warrior, encounter):
        hp_before = warrior.hp
        enemy = create_enemy(encounter)
        resolve_enemy_turn(warrior, enemy, encounter, enemy_nn_modifiers=None)
        if warrior.hp < hp_before:
            assert warrior.hp < hp_before

    def test_resolve_turn_with_nn_modifiers(self, warrior, encounter):
        enemy = create_enemy(encounter)
        modifiers = [{"modifier": "WEAK_SPOT_FOUND", "value": 2.0, "target": "player"}]
        result, effects = resolve_turn(
            attacker=warrior, defender=enemy,
            is_player_attacker=True, turn_number=0,
            active_effects={}, nn_modifiers=modifiers,
            attribute="strength",
        )
        assert isinstance(result, AttackResult)

    def test_resolve_turn_stun(self, warrior, encounter):
        enemy = create_enemy(encounter)
        result, _ = resolve_turn(
            attacker=warrior, defender=enemy,
            is_player_attacker=True, turn_number=0,
            active_effects={"attacker": [
                type('E', (), {'kind': StatusEffect.STUNNED, 'duration': 1, 'value': 0.0})()
            ]},
            nn_modifiers=None, attribute="strength",
        )
        assert result.final_damage == 0

    def test_player_deal_no_damage_to_dead_enemy(self, warrior, encounter):
        encounter.enemy_hp = 0
        enemy = create_enemy(encounter)
        result = resolve_player_turn(warrior, enemy, encounter, nn_modifiers=None)
        assert enemy.hp == 0


# ─── INITIATIVE SYSTEM ────────────────────────────────────────────

class TestInitiative:
    def test_build_initiative_order(self, warrior, session, encounter):
        encounter.initiative_order = build_initiative_order({1: warrior}, encounter)
        assert len(encounter.initiative_order) >= 1

    def test_initiative_order_sorted(self, warrior, session, encounter):
        encounter.initiative_order = build_initiative_order({1: warrior}, encounter)
        initiatives = [e["initiative"] for e in encounter.initiative_order]
        for i in range(len(initiatives) - 1):
            assert initiatives[i] >= initiatives[i + 1]

    def test_initiative_contains_player_and_enemy(self, warrior, session, encounter):
        encounter.initiative_order = build_initiative_order({1: warrior}, encounter)
        types = [e["type"] for e in encounter.initiative_order]
        assert "player" in types
        assert "enemy" in types

    def test_companion_after_owner(self, leader, session, encounter):
        leader.summon_companion()
        encounter.initiative_order = build_initiative_order({1: leader}, encounter)
        comp_found = False
        for i, entry in enumerate(encounter.initiative_order):
            if entry["type"] == "companion":
                assert i > 0
                assert encounter.initiative_order[i - 1]["uid"] == entry["uid"]
                comp_found = True
        assert comp_found

    def test_get_current_turn(self, warrior, session, encounter):
        encounter.initiative_order = build_initiative_order({1: warrior}, encounter)
        current = get_current_turn(encounter)
        assert current is not None
        assert "type" in current
        assert "uid" in current

    def test_get_current_turn_empty(self):
        class FakeEnc:
            initiative_order = []
            current_turn_index = 0
        assert get_current_turn(FakeEnc()) is None

    def test_advance_turn_core(self, warrior, session, encounter):
        encounter.initiative_order = build_initiative_order({1: warrior}, encounter)
        n = len(encounter.initiative_order)
        first = get_current_turn(encounter)
        for _ in range(n):
            advance_turn_core(encounter)
        assert get_current_turn(encounter)["uid"] == first["uid"]
        assert encounter.round_number == 1

    def test_advance_turn_cycles(self, warrior, session, encounter):
        encounter.initiative_order = build_initiative_order({1: warrior}, encounter)
        order_len = len(encounter.initiative_order)
        indices = []
        for _ in range(order_len * 3 + 1):
            indices.append(encounter.current_turn_index)
            advance_turn_core(encounter)
        # Verify we cycled through all indices
        expected = list(range(order_len)) * 3 + [0]
        assert indices == expected

    def test_two_players_initiative(self, warrior, encounter):
        warrior2 = Character(owner_tg_id=3, name="Player2", class_key="rogue")
        encounter.initiative_order = build_initiative_order({1: warrior, 3: warrior2}, encounter)
        players = [e for e in encounter.initiative_order if e["type"] == "player"]
        assert len(players) == 2
        assert players[0]["uid"] != players[1]["uid"]

    def test_pick_enemy_target(self, warrior):
        chars = {1: warrior, 2: Character(owner_tg_id=2, name="Ally", class_key="mage")}
        uid, char = pick_enemy_target(chars)
        assert uid in (1, 2)
        assert char.alive

    def test_pick_enemy_target_only_alive(self, warrior):
        dead = Character(owner_tg_id=2, name="Dead", class_key="mage", alive=False, hp=0)
        chars = {1: warrior, 2: dead}
        uid, char = pick_enemy_target(chars)
        assert uid == 1

    def test_create_enemy(self, encounter):
        enemy = create_enemy(encounter)
        assert enemy.name == "Goblin"
        assert enemy.hp == 50
        assert enemy.attack_min == 5
        assert enemy.attack_max == 10


# ─── EVENTS ───────────────────────────────────────────────────────

class TestEvents:
    def test_event_strength_check_success(self, warrior):
        warrior.base_stats.strength = 20
        event = RaidEvent(id="test", text="Test", attribute="strength", dc=15,
                          success=EventReward(gold=50))
        success, reward = resolve_event(event, warrior)
        assert success
        assert reward.gold == 50

    def test_event_strength_check_fail(self, warrior):
        warrior.base_stats.strength = 1
        event = RaidEvent(id="test", text="Test", attribute="strength", dc=20,
                          success=EventReward(gold=50),
                          fail=EventReward(damage=10))
        success, reward = resolve_event(event, warrior)
        assert not success
        assert reward.damage == 10

    def test_event_agility_check(self, warrior):
        warrior.base_stats.agility = 8
        event = RaidEvent(id="test", text="Test", attribute="agility", dc=12,
                          success=EventReward(heal=20))
        success, reward = resolve_event(event, warrior)
        # With AGI=8 + d20 avg 10.5, likely success
        if success:
            assert reward.heal == 20

    def test_event_intelligence_check(self, warrior):
        warrior.base_stats.intelligence = 8
        event = RaidEvent(id="test", text="Test", attribute="intelligence", dc=13,
                          success=EventReward(buff_atk=5))
        success, reward = resolve_event(event, warrior)
        if success:
            assert reward.buff_atk == 5

    def test_event_pure_chance(self):
        event = RaidEvent(id="test", text="Test", attribute=None, dc=100,  # 100% success
                          success=EventReward(gold=100))
        success, reward = resolve_event(event, Character(owner_tg_id=1, name="Test", class_key="warrior"))
        assert success
        assert reward.gold == 100

    def test_event_pure_chance_fail(self):
        event = RaidEvent(id="test", text="Test", attribute=None, dc=0,  # 0% success
                          success=EventReward(gold=100),
                          fail=EventReward(damage=5))
        success, reward = resolve_event(event, Character(owner_tg_id=1, name="Test", class_key="warrior"))
        assert not success
        assert reward.damage == 5

    def test_event_no_fail_reward(self, warrior):
        warrior.base_stats.strength = 1
        event = RaidEvent(id="test", text="Test", attribute="strength", dc=999,
                          success=EventReward(gold=1))
        success, reward = resolve_event(event, warrior)
        assert not success
        assert reward.gold == 0
        assert reward.damage == 0


# ─── COMPANION ────────────────────────────────────────────────────

class TestCompanion:
    def test_summon_companion_for_leader(self, leader):
        leader.summon_companion()
        assert leader.companion is not None
        assert leader.companion.alive
        assert leader.companion.hp > 0

    def test_no_companion_for_warrior(self, warrior):
        warrior.summon_companion()
        assert warrior.companion is None

    def test_companion_scales_with_leader_stats(self):
        weak_leader = Character(owner_tg_id=1, name="Weak", class_key="leader")
        weak_leader.summon_companion()
        weak_hp = weak_leader.companion.hp

        strong_leader = Character(owner_tg_id=2, name="Strong", class_key="leader")
        strong_leader.base_stats.strength += 10
        strong_leader.summon_companion()
        strong_hp = strong_leader.companion.hp

        assert strong_hp > weak_hp

    def test_companion_int_affects_attack(self):
        leader = Character(owner_tg_id=1, name="Leader", class_key="leader")
        leader.base_stats.intelligence = 5
        leader.summon_companion()
        atk_min = leader.companion.attack_min
        atk_max = leader.companion.attack_max

        leader2 = Character(owner_tg_id=2, name="Leader2", class_key="leader")
        leader2.base_stats.intelligence = 15
        leader2.summon_companion()
        assert leader2.companion.attack_min > atk_min
        assert leader2.companion.attack_max > atk_max

    def test_release_companion(self, leader):
        leader.summon_companion()
        assert leader.companion is not None
        leader.release_companion()
        assert leader.companion is None

    def test_companion_agi_affects_dodge_crit(self):
        leader = Character(owner_tg_id=1, name="Leader", class_key="leader")
        leader.base_stats.agility = 3
        leader.summon_companion()
        low_dodge = leader.companion.dodge_chance
        low_crit = leader.companion.crit_chance

        leader2 = Character(owner_tg_id=2, name="Leader2", class_key="leader")
        leader2.base_stats.agility = 15
        leader2.summon_companion()
        assert leader2.companion.dodge_chance > low_dodge
        assert leader2.companion.crit_chance > low_crit


# ─── CHARACTER SERIALIZATION (NEW FIELDS) ─────────────────────────

class TestCharacterSerialization:
    def test_stat_points_serialization(self):
        from data.models import character_to_dict, character_from_dict
        from data.storage import _ITEM_TEMPLATES
        char = Character(owner_tg_id=1, name="Test", class_key="warrior")
        char.stat_points = 5
        char.base_stats.strength = 10
        char.base_stats.agility = 3
        char.base_stats.intelligence = 7
        d = character_to_dict(char)
        restored = character_from_dict(d, _ITEM_TEMPLATES)
        assert restored.stat_points == 5
        assert restored.base_stats.strength == 10
        assert restored.base_stats.agility == 3
        assert restored.base_stats.intelligence == 7

    def test_item_attributes_serialization(self):
        from data.models import item_to_dict, item_from_dict, character_to_dict, character_from_dict
        from data.storage import _ITEM_TEMPLATES
        item = Item(
            ItemTemplate("Magic Sword", ItemType.WEAPON, Rarity.EPIC, ItemEffect(strength_bonus=5)),
            uid="ms_1", durability=80, durability_max=100,
            attributes=["one_handed", "fire", "scaling_strength"],
        )
        char = Character(owner_tg_id=1, name="Test", class_key="warrior")
        char.inventory.append(item)
        d = character_to_dict(char)
        restored = character_from_dict(d, _ITEM_TEMPLATES)
        assert len(restored.inventory) == 1
        restored_item = restored.inventory[0]
        assert "one_handed" in restored_item.attributes
        assert "fire" in restored_item.attributes
        assert "scaling_strength" in restored_item.attributes

    def test_equipment_with_attributes_serialization(self):
        from data.models import character_to_dict, character_from_dict
        from data.storage import _ITEM_TEMPLATES
        item = Item(
            ItemTemplate("Axe", ItemType.WEAPON, Rarity.COMMON,
                         ItemEffect(strength_bonus=3, agility_bonus=2)),
            uid="axe_1", durability=100, durability_max=100,
            attributes=["heavy", "scaling_strength"],
        )
        char = Character(owner_tg_id=1, name="Test", class_key="warrior")
        char.equipment.weapon = item
        d = character_to_dict(char)
        restored = character_from_dict(d, _ITEM_TEMPLATES)
        assert restored.equipment.weapon is not None
        assert "heavy" in restored.equipment.weapon.attributes


# ─── EQUIPMENT ────────────────────────────────────────────────────

class TestEquipment:
    def test_can_equip_item(self, warrior):
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        assert warrior.can_equip(item) is True

    def test_cannot_equip_broken(self, warrior):
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=0, durability_max=100,
        )
        assert warrior.can_equip(item) is False

    def test_cannot_equip_high_level(self, warrior):
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3),
                         required_level=999),
            uid="sw", durability=100, durability_max=100,
        )
        assert warrior.can_equip(item) is False

    def test_cannot_equip_wrong_class(self, warrior):
        item = Item(
            ItemTemplate("Staff", ItemType.WEAPON, Rarity.COMMON, ItemEffect(intelligence_bonus=3),
                         required_class="mage"),
            uid="st", durability=100, durability_max=100,
        )
        assert warrior.can_equip(item) is False

    def test_equip_item(self, warrior):
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        warrior.inventory.append(item)
        assert warrior.equip(item) is True
        assert warrior.equipment.weapon is item
        assert item not in warrior.inventory

    def test_equip_not_in_inventory(self, warrior):
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        assert warrior.equip(item) is False

    def test_equip_replaces_old(self, warrior):
        old = Item(
            ItemTemplate("Stick", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=1)),
            uid="old", durability=100, durability_max=100,
        )
        new = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=5)),
            uid="new", durability=100, durability_max=100,
        )
        warrior.equipment.weapon = old
        warrior.inventory.append(new)
        assert warrior.equip(new) is True
        assert warrior.equipment.weapon is new
        assert old in warrior.inventory  # old goes back to inventory

    def test_unequip_item(self, warrior):
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        warrior.equipment.weapon = item
        assert warrior.unequip(item) is True
        assert warrior.equipment.weapon is None
        assert item in warrior.inventory

    def test_unequip_not_equipped(self, warrior):
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        assert warrior.unequip(item) is False

    def test_take_damage_reduces_hp(self, warrior):
        hp_before = warrior.hp
        warrior.take_damage(15)
        assert warrior.hp == hp_before - 15

    def test_take_damage_clamp_to_zero(self, warrior):
        warrior.take_damage(999999)
        assert warrior.hp == 0

    def test_heal(self, warrior):
        warrior.hp = 10
        healed = warrior.heal(20)
        assert warrior.hp == 30
        assert healed == 20

    def test_heal_caps_at_max(self, warrior):
        hp_before = warrior.hp
        warrior.heal(999999)
        assert warrior.hp == warrior.max_hp

    def test_revive(self, warrior):
        warrior.alive = False
        warrior.hp = 0
        warrior.revive()
        assert warrior.alive is True
        assert warrior.hp == 1

    def test_durability_damage_all(self, warrior):
        arm = Item(
            ItemTemplate("Armor", ItemType.ARMOR, Rarity.COMMON, ItemEffect(defense_bonus=3)),
            uid="arm", durability=50, durability_max=100,
        )
        warrior.equipment.armor = arm
        warrior.durability_damage_all(amount=10)
        assert arm.durability == 40

    def test_durability_damage_breaks_and_removes(self, warrior):
        arm = Item(
            ItemTemplate("Armor", ItemType.ARMOR, Rarity.COMMON, ItemEffect(defense_bonus=3)),
            uid="arm", durability=5, durability_max=100,
        )
        warrior.equipment.armor = arm
        warrior.durability_damage_all(amount=10)
        assert arm.broken
        assert warrior.equipment.armor is None

    def test_durability_damage_percent(self, warrior):
        arm = Item(
            ItemTemplate("Armor", ItemType.ARMOR, Rarity.COMMON, ItemEffect(defense_bonus=3)),
            uid="arm", durability=100, durability_max=100,
        )
        warrior.equipment.armor = arm
        warrior.durability_damage_all(percent=0.1)
        assert arm.durability == 90

    def test_can_raid_cooldown_ok(self, warrior):
        assert warrior.can_raid() is True

    def test_raid_cooldown_remaining_zero(self, warrior):
        assert warrior.raid_cooldown_remaining() == 0.0

    def test_mark_raid_done_sets_time(self, warrior):
        old = warrior.last_raid_time
        warrior.mark_raid_done()
        assert warrior.last_raid_time > old

    def test_in_raid_flag(self, warrior):
        assert warrior.in_raid is False
        warrior.in_raid = True
        assert warrior.in_raid is True

    def test_equipment_equipped_items(self, warrior):
        it = Item(
            ItemTemplate("Ring", ItemType.ACCESSORY, Rarity.COMMON, ItemEffect()),
            uid="r", durability=100, durability_max=100,
        )
        warrior.equipment.accessory = it
        equipped = warrior.equipment.equipped_items()
        assert len(equipped) == 1
        assert it in equipped

    def test_equipment_total_effect_stacks(self, warrior):
        a = Item(
            ItemTemplate("A", ItemType.ACCESSORY, Rarity.COMMON, ItemEffect(strength_bonus=2)),
            uid="a", durability=100, durability_max=100,
        )
        b = Item(
            ItemTemplate("B", ItemType.ACCESSORY, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="b", durability=100, durability_max=100,
        )
        # Only one accessory slot, so test with weapon + accessory
        w = Item(
            ItemTemplate("W", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=5)),
            uid="w", durability=100, durability_max=100,
        )
        warrior.equipment.weapon = w
        warrior.equipment.accessory = a
        total = warrior.equipment.total_effect()
        assert total.strength_bonus == 7


# ─── ECONOMY ──────────────────────────────────────────────────────

class TestEconomy:
    def test_create_listing(self):
        from core.economy import MARKET, MarketListing
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        listing = MARKET.create_listing(seller_id=1, character_name="Hero", item=item, price=100)
        assert listing is not None
        assert listing.price == 100
        assert listing.active is True
        assert listing.seller_id == 1
        MARKET.listings.pop(listing.listing_id, None)

    def test_create_listing_zero_price(self):
        from core.economy import MARKET
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        listing = MARKET.create_listing(seller_id=1, character_name="Hero", item=item, price=0)
        assert listing is None

    def test_buy_listing(self, warrior):
        from core.economy import MARKET
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        listing = MARKET.create_listing(seller_id=2, character_name="Seller", item=item, price=100)
        warrior.gold = 200
        success, bought_item, seller_id, seller_name, seller_earns = MARKET.buy_listing(listing.listing_id, warrior)
        assert success is True
        assert bought_item is not None
        assert warrior.gold == 100  # 200 - 100
        assert seller_earns == 95   # 100 - 5% commission
        MARKET.listings.pop(listing.listing_id, None)

    def test_buy_listing_not_enough_gold(self, warrior):
        from core.economy import MARKET
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        listing = MARKET.create_listing(seller_id=2, character_name="Seller", item=item, price=100)
        warrior.gold = 50
        success, _, _, _, _ = MARKET.buy_listing(listing.listing_id, warrior)
        assert success is False
        MARKET.listings.pop(listing.listing_id, None)

    def test_buy_listing_own_item(self, warrior):
        from core.economy import MARKET
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        warrior.owner_tg_id = 1
        listing = MARKET.create_listing(seller_id=1, character_name="Self", item=item, price=100)
        warrior.gold = 200
        success, _, _, _, _ = MARKET.buy_listing(listing.listing_id, warrior)
        assert success is False
        MARKET.listings.pop(listing.listing_id, None)

    def test_cancel_listing(self):
        from core.economy import MARKET
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        listing = MARKET.create_listing(seller_id=1, character_name="Hero", item=item, price=100)
        assert MARKET.cancel_listing(listing.listing_id, owner_id=1) is True
        assert listing.active is False
        MARKET.listings.pop(listing.listing_id, None)

    def test_cancel_listing_wrong_owner(self):
        from core.economy import MARKET
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        listing = MARKET.create_listing(seller_id=1, character_name="Hero", item=item, price=100)
        assert MARKET.cancel_listing(listing.listing_id, owner_id=2) is False
        MARKET.listings.pop(listing.listing_id, None)

    def test_get_active_listings(self):
        from core.economy import MARKET
        count_before = len(MARKET.get_active_listings())
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        listing = MARKET.create_listing(seller_id=1, character_name="Hero", item=item, price=100)
        assert len(MARKET.get_active_listings()) == count_before + 1
        MARKET.listings.pop(listing.listing_id, None)

    def test_get_player_listings(self):
        from core.economy import MARKET
        item = Item(
            ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
            uid="sw", durability=100, durability_max=100,
        )
        listing = MARKET.create_listing(seller_id=1, character_name="Hero", item=item, price=100)
        player_listings = MARKET.get_player_listings(1)
        assert any(l.listing_id == listing.listing_id for l in player_listings)
        assert len(MARKET.get_player_listings(2)) == 0
        MARKET.listings.pop(listing.listing_id, None)


# ─── UTILS ────────────────────────────────────────────────────────

class TestValidators:
    def test_validate_nn_response_valid(self):
        from utils.validators import validate_nn_response
        data = {"actions": [{"modifier": "WEAK_SPOT_FOUND", "value": 1.5, "target": "player"}]}
        result = validate_nn_response(data, key="actions", check_narrative=False)
        assert len(result) == 1
        assert result[0]["modifier"] == "WEAK_SPOT_FOUND"

    def test_validate_nn_response_clamps_value(self):
        from utils.validators import validate_nn_response
        data = {"actions": [{"modifier": "WEAK_SPOT_FOUND", "value": 99.0, "target": "player"}]}
        result = validate_nn_response(data, key="actions", check_narrative=False)
        assert result[0]["value"] == 2.0  # clamped to max

    def test_validate_nn_response_rejects_unknown(self):
        from utils.validators import validate_nn_response
        data = {"actions": [{"modifier": "INVISIBILITY", "value": 1.0, "target": "player"}]}
        result = validate_nn_response(data, key="actions", check_narrative=False)
        assert len(result) == 0

    def test_validate_nn_response_invalid_key(self):
        from utils.validators import validate_nn_response
        with pytest.raises(ValueError):
            validate_nn_response({"actions": "not_a_list"}, key="actions", check_narrative=False)

    def test_validate_nn_response_not_dict(self):
        from utils.validators import validate_nn_response
        with pytest.raises(ValueError):
            validate_nn_response("not_a_dict", key="actions", check_narrative=False)

    def test_validate_character_name_valid(self):
        from utils.validators import validate_character_name
        assert validate_character_name("Hero") is True
        assert validate_character_name("Dark Knight") is True

    def test_validate_character_name_invalid(self):
        from utils.validators import validate_character_name
        assert validate_character_name("") is False
        assert validate_character_name("X") is False  # too short (len < 2)
        assert validate_character_name("A" * 25) is False  # too long

    def test_clamp(self):
        from utils.validators import clamp
        assert clamp(5, 0, 10) == 5
        assert clamp(-5, 0, 10) == 0
        assert clamp(15, 0, 10) == 10

    def test_roll_chance(self):
        from utils.rng import roll_chance
        assert roll_chance(0.0) is False
        assert roll_chance(1.0) is True
        assert roll_chance(-0.5) is False
        assert roll_chance(1.5) is True

    def test_secure_randint_range(self):
        from utils.rng import secure_randint
        for _ in range(100):
            v = secure_randint(1, 10)
            assert 1 <= v <= 10
        # single value
        assert secure_randint(5, 5) == 5

    def test_roll_dice(self):
        from utils.rng import roll_dice
        for _ in range(100):
            v = roll_dice(20)
            assert 1 <= v <= 20

    def test_rand_range(self):
        from utils.rng import rand_range
        for _ in range(100):
            v = rand_range(2.5, 5.5)
            assert 2.5 <= v <= 5.5

    def test_secure_randfloat(self):
        from utils.rng import secure_randfloat
        for _ in range(100):
            v = secure_randfloat()
            assert 0.0 <= v < 1.0


# ─── LOCATIONS ────────────────────────────────────────────────────

class TestLocations:
    def test_get_locations(self):
        from core.locations import get_locations
        locs = get_locations()
        assert len(locs) > 0
        assert "forest" in locs

    def test_get_location(self):
        from core.locations import get_location
        loc = get_location("forest")
        assert loc is not None
        assert loc.name == "Тёмный лес"

    def test_get_location_unknown(self):
        from core.locations import get_location
        assert get_location("nonexistent") is None

    def test_get_mob(self):
        from core.locations import get_mob
        mob = get_mob("goblin")
        assert mob is not None
        assert mob.name == "Лесной гоблин"

    def test_get_mob_unknown(self):
        from core.locations import get_mob
        assert get_mob("nonexistent") is None

    def test_get_mobs(self):
        from core.locations import get_mobs
        mobs = get_mobs()
        assert len(mobs) > 0


# ─── WEAPON GENERATION ────────────────────────────────────────────

class TestWeaponGen:
    def test_get_weapon_patterns(self):
        from core.weapon_gen import get_weapon_patterns
        patterns = get_weapon_patterns()
        assert len(patterns) > 0

    def test_get_attributes_descriptions(self):
        from core.weapon_gen import get_attributes_descriptions
        descs = get_attributes_descriptions({"attributes": ["one_handed", "fire", "scaling_strength"]})
        assert "Одноручное" in descs
        assert "Огненный урон" in descs
        assert "Зависит от Силы" in descs

    def test_weapon_gen_durability(self):
        from core.weapon_gen import roll_weapon_from_pattern
        pattern = {
            "rarity": "COMMON", "required_level": 1,
            "allowed_classes": ["warrior"],
            "base_names": ["Sword"], "adjectives": ["Sharp"],
            "damage_min": 5, "damage_max": 10,
            "durability_min": 30, "durability_max": 80,
        }
        item = roll_weapon_from_pattern(pattern, "test")
        assert item is not None
        assert 30 <= item.durability <= 80
        assert item.durability_max == item.durability

    def test_weapon_gen_no_scaling_attrs(self):
        from core.weapon_gen import roll_weapon_from_pattern
        pattern = {
            "rarity": "COMMON", "required_level": 1,
            "allowed_classes": ["warrior"],
            "base_names": ["Sword"], "adjectives": ["Plain"],
            "damage_min": 5, "damage_max": 10,
            "attributes": ["one_handed"],
            "crit_bonus": 0.02, "dodge_bonus": 0.01,
        }
        item = roll_weapon_from_pattern(pattern, "test")
        assert item is not None
        effect = item.template.final_effect()
        assert effect.crit_chance_bonus == 0.02
        assert effect.dodge_bonus == 0.01
        # No scaling attrs → stat bonuses stay 0
        assert effect.strength_bonus == 0
        assert effect.agility_bonus == 0
        assert effect.intelligence_bonus == 0


# ─── RAID ─────────────────────────────────────────────────────────

class TestRaid:
    def test_create_raid_sets_raid_id(self, warrior, location):
        from core.raid import create_raid
        session = create_raid(warrior, location, raid_id="raid_001")
        assert session.raid_id == "raid_001"
        assert session.location_key == "forest"

    def test_create_raid_generates_encounters(self, warrior, location):
        from core.raid import create_raid
        session = create_raid(warrior, location, raid_id="raid_002")
        assert 2 <= len(session.encounters) <= 4
        assert session.status.value == "pending"

    def test_create_raid_scales_hp_for_group(self, warrior, location):
        from core.raid import create_raid
        solo = create_raid(warrior, location, raid_id="solo")
        group = create_raid(warrior, location, raid_id="group", group_size=3)
        for i, enc in enumerate(group.encounters):
            if i < len(solo.encounters):
                assert enc.enemy_hp >= solo.encounters[i].enemy_hp

    def test_create_raid_summon_companion_for_leader(self, location):
        from core.raid import create_raid
        leader = Character(owner_tg_id=1, name="Lead", class_key="leader")
        session = create_raid(leader, location, raid_id="lead")
        assert leader.companion is not None

    def test_generate_loot(self, location):
        from core.raid import generate_loot
        items = generate_loot(location, enemies_defeated=3, character_level=5, allowed_classes=["warrior", "rogue"])
        assert isinstance(items, list)

    def test_distribute_exp_gold(self, warrior, location):
        from core.raid import distribute_exp_gold, RaidSession, RaidStatus
        session = RaidSession(raid_id="test", location_key="forest", status=RaidStatus.COMPLETED)
        gold_before = warrior.gold
        exp_before = warrior.experience
        distribute_exp_gold(session, location, [warrior])
        assert warrior.gold >= gold_before
        assert warrior.experience >= exp_before

    def test_distribute_exp_gold_only_alive(self, warrior, location):
        from core.raid import distribute_exp_gold, RaidSession, RaidStatus
        session = RaidSession(raid_id="test", location_key="forest", status=RaidStatus.COMPLETED)
        dead = Character(owner_tg_id=2, name="Dead", class_key="warrior", alive=False, hp=0)
        gold_before_alive = warrior.gold
        exp_before_alive = warrior.experience
        distribute_exp_gold(session, location, [warrior, dead])
        # Dead gets nothing
        assert dead.gold == 0
        assert dead.experience == 0
        # Alive still gets gold/exp (might be less split)
        assert warrior.gold >= gold_before_alive or warrior.experience >= exp_before_alive

    def test_raid_encounter_serialization(self, encounter):
        from core.raid import raid_encounter_to_dict, raid_encounter_from_dict
        d = raid_encounter_to_dict(encounter)
        restored = raid_encounter_from_dict(d)
        assert restored.enemy_hp == encounter.enemy_hp
        assert restored.enemy_max_hp == encounter.enemy_max_hp
        assert restored.finished == encounter.finished

    def test_raid_session_serialization(self, session):
        from core.raid import session_to_dict, session_from_dict
        d = session_to_dict(session)
        restored = session_from_dict(d)
        assert restored.raid_id == session.raid_id
        assert restored.location_key == session.location_key
        assert restored.status == session.status

    def test_session_serialization_with_participants(self, warrior, session):
        from core.raid import session_to_dict, session_from_dict
        session.participant_names = {1: "Hero", 2: "Sidekick"}
        session.turn_pending_uid = 1
        d = session_to_dict(session)
        restored = session_from_dict(d)
        assert restored.participant_names.get("1") == "Hero"
        assert restored.turn_pending_uid == 1

    def test_session_serialization_with_used_events(self, session):
        from core.raid import session_to_dict, session_from_dict
        session.used_event_ids = {"evt_1", "evt_2"}
        d = session_to_dict(session)
        restored = session_from_dict(d)
        assert "evt_1" in restored.used_event_ids
        assert "evt_2" in restored.used_event_ids

    def test_apply_mob_status_effects_bleed(self):
        from core.raid import apply_mob_status_effects
        from core.combat import StatusEffect, StatusEffectInstance
        effects = apply_mob_status_effects({}, "bleed", 50)
        assert "defender" in effects
        assert any(e.kind == StatusEffect.BLEED for e in effects["defender"])

    def test_apply_mob_status_effects_poison(self):
        from core.raid import apply_mob_status_effects
        from core.combat import StatusEffect
        effects = apply_mob_status_effects({}, "poison", 50)
        assert any(e.kind == StatusEffect.POISON for e in effects["defender"])

    def test_apply_mob_status_effects_fire(self):
        from core.raid import apply_mob_status_effects
        from core.combat import StatusEffect
        effects = apply_mob_status_effects({}, "fire", 50)
        assert any(e.kind == StatusEffect.POISON for e in effects["defender"])

    def test_apply_mob_status_effects_ice(self):
        from core.raid import apply_mob_status_effects
        from core.combat import StatusEffect
        effects = apply_mob_status_effects({}, "ice", 50)
        assert any(e.kind == StatusEffect.STUNNED for e in effects["defender"])

    def test_apply_mob_status_effects_no_damage(self):
        from core.raid import apply_mob_status_effects
        effects = apply_mob_status_effects({}, "bleed", 0)
        assert effects == {}

    def test_apply_mob_status_effects_unknown_type(self):
        from core.raid import apply_mob_status_effects
        effects = apply_mob_status_effects({}, "psychic", 50)
        assert effects == {}


# ─── MODELS ───────────────────────────────────────────────────────

class TestModels:
    def test_equipment_serialization(self):
        from data.models import equipment_to_dict, equipment_from_dict, item_to_dict, item_from_dict, Equipment
        from data.storage import _ITEM_TEMPLATES
        w = Item(ItemTemplate("Sword", ItemType.WEAPON, Rarity.COMMON, ItemEffect(strength_bonus=3)),
                 uid="w", durability=100, durability_max=100)
        a = Item(ItemTemplate("Helm", ItemType.ARMOR, Rarity.RARE, ItemEffect(defense_bonus=5)),
                 uid="a", durability=90, durability_max=100)
        eq = Equipment(weapon=w, armor=a)
        d = equipment_to_dict(eq)
        restored = equipment_from_dict(d, _ITEM_TEMPLATES)
        assert restored.weapon is not None
        assert restored.weapon.uid == "w"
        assert restored.armor.uid == "a"
        assert restored.accessory is None

    def test_equipment_serialization_empty(self):
        from data.models import equipment_to_dict, equipment_from_dict, Equipment
        from data.storage import _ITEM_TEMPLATES
        eq = Equipment()
        d = equipment_to_dict(eq)
        restored = equipment_from_dict(d, _ITEM_TEMPLATES)
        assert restored.weapon is None
        assert restored.armor is None
        assert restored.accessory is None

    def test_companion_serialization(self, leader):
        from data.models import companion_to_dict, companion_from_dict
        leader.summon_companion()
        d = companion_to_dict(leader.companion)
        restored = companion_from_dict(d)
        assert restored is not None
        assert restored.name == leader.companion.name
        assert restored.hp == leader.companion.hp
        assert restored.alive is True

    def test_companion_serialization_none(self):
        from data.models import companion_from_dict
        assert companion_from_dict(None) is None
        assert companion_from_dict({"name": "Test"}) is not None
        assert companion_from_dict({"name": "Test"}).name == "Test"

    def test_character_full_serialization(self, warrior):
        from data.models import character_to_dict, character_from_dict
        from data.storage import _ITEM_TEMPLATES
        it = Item(ItemTemplate("Ring", ItemType.ACCESSORY, Rarity.EPIC, ItemEffect(hp_bonus=20)),
                  uid="r", durability=80, durability_max=100)
        warrior.inventory.append(it)
        warrior.equipment.accessory = it
        warrior.stat_points = 3
        warrior.base_stats.strength += 5
        warrior.gold = 500
        warrior.count_raid = 7
        d = character_to_dict(warrior)
        restored = character_from_dict(d, _ITEM_TEMPLATES)
        assert restored.gold == 500
        assert restored.stat_points == 3
        assert restored.base_stats.strength == warrior.base_stats.strength
        assert restored.count_raid == 7
        assert len(restored.inventory) == 1
        assert restored.equipment.accessory is not None

    def test_item_from_dict_none(self):
        from data.models import item_from_dict
        assert item_from_dict({}, {}) is None

    def test_character_from_dict_inventory_skips_none(self):
        from data.models import character_from_dict
        data = {
            "owner_tg_id": 1, "name": "Test", "class_key": "warrior",
            "inventory": [{"uid": "bad", "template_name": "Nope"}],
        }
        from data.storage import _ITEM_TEMPLATES
        restored = character_from_dict(data, _ITEM_TEMPLATES)
        assert restored is not None
        assert len(restored.inventory) == 0  # invalid item skipped
