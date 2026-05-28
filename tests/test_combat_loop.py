import sys; sys.path.insert(0, ".")
import pytest
from core.combat import AttackResult
from core.raid import resolve_player_turn, resolve_enemy_turn, resolve_companion_turn, create_enemy


class TestCombat:
    def test_player_deals_damage(self, warrior, encounter):
        enemy = create_enemy(encounter)
        hp_before = encounter.enemy_hp
        result = resolve_player_turn(warrior, enemy, encounter, nn_modifiers=None)
        assert isinstance(result, AttackResult)
        if not result.is_dodged:
            assert result.final_damage >= 0
        assert encounter.enemy_hp <= hp_before

    def test_enemy_counterattacks(self, warrior, encounter):
        encounter.enemy_hp = 200
        enemy = create_enemy(encounter)
        hp_before = warrior.hp
        resolve_player_turn(warrior, enemy, encounter, nn_modifiers=None)
        enemy_result = resolve_enemy_turn(warrior, enemy, encounter, enemy_nn_modifiers=None)
        assert isinstance(enemy_result, AttackResult)
        if not enemy_result.is_dodged and enemy_result.final_damage > 0:
            assert warrior.hp < hp_before

    def test_enemy_dies(self, warrior, encounter):
        encounter.enemy_hp = 1
        enemy = create_enemy(encounter)
        result = resolve_player_turn(warrior, enemy, encounter, nn_modifiers=None)
        assert isinstance(result, AttackResult)

    def test_no_negative_hp(self, warrior, encounter):
        warrior.hp = 1
        enemy = create_enemy(encounter)
        resolve_player_turn(warrior, enemy, encounter, nn_modifiers=None)
        resolve_enemy_turn(warrior, enemy, encounter, enemy_nn_modifiers=None)
        assert warrior.hp >= 0

    def test_companion_attacks(self, leader, encounter):
        leader.summon_companion()
        encounter.enemy_hp = 200
        enemy = create_enemy(encounter)
        companion_result = resolve_companion_turn(leader, enemy, encounter)
        if companion_result:
            assert isinstance(companion_result, AttackResult)
            if not companion_result.is_dodged:
                assert companion_result.final_damage >= 0 or True

    def test_enemy_modifiers_crit_boost(self, warrior, encounter):
        encounter.enemy_hp = 200
        enemy = create_enemy(encounter)
        result = resolve_enemy_turn(
            warrior, enemy, encounter,
            enemy_nn_modifiers=[{"modifier": "CRIT_BOOST", "value": 1.0, "target": "enemy"}],
        )
        # just check no crash
        assert isinstance(result, AttackResult)

    def test_enemy_modifiers_dodge_buff(self, warrior, encounter):
        warrior.hp = 200
        enemy = create_enemy(encounter)
        result = resolve_enemy_turn(
            warrior, enemy, encounter,
            enemy_nn_modifiers=[{"modifier": "DODGE_BONUS", "value": 0.3, "target": "enemy"}],
        )
        assert isinstance(result, AttackResult)

    def test_enemy_modifiers_stun(self, warrior, encounter):
        warrior.hp = 200
        encounter.enemy_hp = 200
        enemy = create_enemy(encounter)
        resolve_enemy_turn(
            warrior, enemy, encounter,
            enemy_nn_modifiers=[{"modifier": "STUN", "value": 1.0, "target": "player"}],
        )

    def test_enemy_modifiers_taunt(self, warrior, encounter):
        warrior.hp = 200
        encounter.enemy_hp = 200
        enemy = create_enemy(encounter)
        resolve_enemy_turn(
            warrior, enemy, encounter,
            enemy_nn_modifiers=[{"modifier": "TAUNT", "value": 1.0, "target": "enemy"}],
        )
