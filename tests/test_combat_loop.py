"""Test combat loop: process_encounter_turn, resolve_turn, calc_damage."""
import sys; sys.path.insert(0, ".")
import pytest
from core.raid import process_encounter_turn
from core.combat import AttackResult


class TestCombat:
    def test_player_deals_damage(self, warrior, session, encounter):
        hp_before = encounter.enemy_hp
        result, _, _, finished = process_encounter_turn(session, warrior)
        assert isinstance(result, AttackResult)
        assert result.final_damage >= 1
        assert encounter.enemy_hp < hp_before

    def test_enemy_counterattacks(self, warrior, session):
        session.encounters[0].enemy_hp = 200
        hp_before = warrior.hp
        _, enemy_attack, _, finished = process_encounter_turn(session, warrior)
        if not finished and enemy_attack:
            assert isinstance(enemy_attack, AttackResult)
            if not enemy_attack.is_dodged and enemy_attack.final_damage > 0:
                assert warrior.hp < hp_before

    def test_enemy_dies(self, warrior, session):
        session.encounters[0].enemy_hp = 1
        p_atk, e_atk, _, finished = process_encounter_turn(session, warrior)
        assert finished
        assert session.encounters[0].enemy_hp <= 0
        assert e_atk is None

    def test_full_encounter(self, warrior, session):
        for turn in range(1, 20):
            p_atk, e_atk, _, finished = process_encounter_turn(session, warrior)
            if finished:
                assert session.encounters[0].enemy_hp <= 0 or warrior.hp <= 0
                return
        pytest.fail("Encounter did not finish within 20 turns")

    def test_no_negative_hp(self, warrior, session):
        warrior.hp = 1
        _, _, _, finished = process_encounter_turn(session, warrior)
        assert warrior.hp >= 0

    def test_companion_attacks(self, leader, session):
        session.encounters[0].enemy_hp = 200
        session.participants = [leader]
        _, _, companion_attack, _ = process_encounter_turn(session, leader)
        if companion_attack:
            assert isinstance(companion_attack, AttackResult)
            assert companion_attack.final_damage >= 1 or companion_attack.is_dodged

    def test_enemy_modifiers_crit_boost(self, warrior, session):
        session.encounters[0].enemy_hp = 2000
        _, enemy_attack, _, _ = process_encounter_turn(
            session, warrior,
            enemy_nn_modifiers=[{"modifier": "CRIT_BOOST", "value": 1.0, "target": "enemy"}],
        )
        if enemy_attack:
            assert enemy_attack.is_crit

    def test_enemy_modifiers_dodge_buff(self, warrior, session):
        warrior.hp = 2000
        _, enemy_attack, _, _ = process_encounter_turn(
            session, warrior,
            enemy_nn_modifiers=[{"modifier": "DODGE_BONUS", "value": 0.3, "target": "enemy"}],
        )
        # just check no crash

    def test_enemy_modifiers_stun(self, warrior, session):
        warrior.hp = 2000
        session.encounters[0].enemy_hp = 2000
        process_encounter_turn(
            session, warrior,
            enemy_nn_modifiers=[{"modifier": "STUN", "value": 1.0, "target": "player"}],
        )

    def test_enemy_modifiers_taunt(self, warrior, session):
        warrior.hp = 2000
        session.encounters[0].enemy_hp = 2000
        process_encounter_turn(
            session, warrior,
            enemy_nn_modifiers=[{"modifier": "TAUNT", "value": 1.0, "target": "enemy"}],
        )
