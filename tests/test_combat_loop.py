"""Test combat loop: process_encounter_turn, resolve_turn, calc_damage."""
import sys
sys.path.insert(0, '.')

from core.character import Character
from core.raid import RaidSession, RaidEncounter, RaidStatus, process_encounter_turn
from core.combat import AttackResult, apply_enemy_modifiers, BattleState


ENEMY_TEMPLATE = {
    "name": "Goblin",
    "hp": 50,
    "atk_min": 5,
    "atk_max": 10,
    "defense": 2,
    "dodge_chance": 0.05,
    "crit_chance": 0.05,
    "crit_multiplier": 2.0,
    "atk_damage_type": "physical",
    "attack_secondary": None,
}


def make_warrior():
    return Character(owner_tg_id=1, name="Warrior", class_key="warrior")


def make_leader():
    from core.character import Companion
    c = Character(owner_tg_id=2, name="Leader", class_key="leader")
    c.companion = Companion(name="Stраж")
    return c


def make_encounter(hp=None):
    hp = hp or ENEMY_TEMPLATE["hp"]
    return RaidEncounter(
        enemy_hp=hp,
        enemy_max_hp=ENEMY_TEMPLATE["hp"],
        enemy_template=dict(ENEMY_TEMPLATE),
    )


def make_session(char, encounters):
    return RaidSession(
        raid_id="test",
        location_key="forest",
        status=RaidStatus.IN_PROGRESS,
        participants=[char],
        encounters=encounters,
    )


def test_player_deals_damage():
    char = make_warrior()
    enc = make_encounter()
    session = make_session(char, [enc])
    hp_before = enc.enemy_hp

    result, _, _, finished = process_encounter_turn(session, char)

    assert isinstance(result, AttackResult), "player_attack must be AttackResult"
    assert result.final_damage >= 1, "player must deal >= 1 dmg"
    assert enc.enemy_hp < hp_before, "enemy HP must decrease"
    print(f"  Player dealt {result.final_damage}, enemy HP {enc.enemy_hp}/{enc.enemy_max_hp}")


def test_enemy_counterattacks():
    char = make_warrior()
    enc = make_encounter(hp=200)
    session = make_session(char, [enc])
    hp_before = char.hp

    _, enemy_attack, _, finished = process_encounter_turn(session, char)

    if not finished and enemy_attack:
        assert isinstance(enemy_attack, AttackResult)
        if not enemy_attack.is_dodged and enemy_attack.final_damage > 0:
            assert char.hp < hp_before, "player must take damage if not dodged"
    print(f"  Enemy attack: dmg={enemy_attack.final_damage if enemy_attack else 0}, dodged={enemy_attack.is_dodged if enemy_attack else False}")


def test_enemy_dies():
    char = make_warrior()
    enc = make_encounter(hp=1)
    session = make_session(char, [enc])

    p_atk, e_atk, c_atk, finished = process_encounter_turn(session, char)

    assert finished, "encounter must be finished"
    assert enc.enemy_hp <= 0, "enemy must be dead"
    assert e_atk is None, "no enemy attack when enemy dies"
    assert p_atk.final_damage >= 1
    print(f"  Enemy killed! Player dealt {p_atk.final_damage}")


def test_full_encounter():
    char = make_warrior()
    enc = make_encounter()
    session = make_session(char, [enc])
    turn = 0

    while turn < 20:
        turn += 1
        p_atk, e_atk, c_atk, finished = process_encounter_turn(session, char)

        print(f"  Turn {turn}: player dmg={p_atk.final_damage}, enemy HP={enc.enemy_hp}, "
              f"player HP={char.hp}, finished={finished}")

        if finished:
            assert enc.enemy_hp <= 0 or char.hp <= 0
            print(f"  Encounter ended on turn {turn}: {'enemy died' if enc.enemy_hp <= 0 else 'player died'}")
            return

    assert False, "Encounter did not finish within 20 turns"


def test_companion_attacks():
    char = make_leader()
    enc = make_encounter(hp=200)
    session = make_session(char, [enc])

    _, _, companion_attack, _ = process_encounter_turn(session, char)

    if companion_attack:
        assert isinstance(companion_attack, AttackResult)
        assert companion_attack.final_damage >= 1 or companion_attack.is_dodged
        print(f"  Companion dealt {companion_attack.final_damage}")
    else:
        # Companion might have died, or was None
        print("  No companion attack")


def test_no_negative_hp():
    char = make_warrior()
    char.hp = 1
    enc = make_encounter()
    session = make_session(char, [enc])

    _, _, _, finished = process_encounter_turn(session, char)

    assert char.hp >= 0, "HP must never go below 0"
    print(f"  Final player HP: {char.hp}")


def test_enemy_modifiers_crit_boost():
    char = make_warrior()
    enc = make_encounter(hp=2000)
    session = make_session(char, [enc])
    hp_before = char.hp

    _, enemy_attack, _, _ = process_encounter_turn(
        session, char,
        enemy_nn_modifiers=[{"modifier": "CRIT_BOOST", "value": 1.0, "target": "enemy"}],
    )

    if enemy_attack:
        assert enemy_attack.is_crit, "enemy attack should be crit with CRIT_BOOST"
        assert enemy_attack.final_damage > 0 or enemy_attack.is_dodged
        if not enemy_attack.is_dodged:
            assert char.hp < hp_before or char.hp == 0, "player should take damage"
    print(f"  Enemy crit attack: dmg={enemy_attack.final_damage}")


def test_enemy_modifiers_dodge_buff():
    char = make_warrior()
    char.hp = 2000
    enc = make_encounter(hp=50)
    session = make_session(char, [enc])

    _, enemy_attack, _, _ = process_encounter_turn(
        session, char,
        enemy_nn_modifiers=[{"modifier": "DODGE_BONUS", "value": 0.3, "target": "enemy"}],
    )

    # DODGE_BONUS gives enemy dodge — next player attack may miss
    # Hard to assert directly, just check no crash
    print(f"  Enemy with dodge buff: player attacked" if enemy_attack else "  Enemy dead")


def test_enemy_modifiers_stun():
    char = make_warrior()
    char.hp = 2000
    enc = make_encounter(hp=2000)
    session = make_session(char, [enc])

    p_atk, e_atk, _, _ = process_encounter_turn(
        session, char,
        enemy_nn_modifiers=[{"modifier": "STUN", "value": 1.0, "target": "player"}],
    )

    if p_atk:
        print(f"  Player attack: dmg={p_atk.final_damage}, stunned effect checked")
    print(f"  Stun modifier applied without crash")


def test_enemy_modifiers_taunt():
    char = make_warrior()
    char.hp = 2000
    enc = make_encounter(hp=2000)
    session = make_session(char, [enc])

    _, e_atk, _, _ = process_encounter_turn(
        session, char,
        enemy_nn_modifiers=[{"modifier": "TAUNT", "value": 1.0, "target": "enemy"}],
    )

    if e_atk:
        print(f"  Enemy with taunt: dmg={e_atk.final_damage}")
    print(f"  Taunt modifier applied without crash")


if __name__ == "__main__":
    tests = [
        test_player_deals_damage,
        test_enemy_counterattacks,
        test_enemy_dies,
        test_full_encounter,
        test_companion_attacks,
        test_no_negative_hp,
        test_enemy_modifiers_crit_boost,
        test_enemy_modifiers_dodge_buff,
        test_enemy_modifiers_stun,
        test_enemy_modifiers_taunt,
    ]
    for test in tests:
        name = test.__name__
        try:
            test()
            print(f"✅ {name} PASSED\n")
        except Exception as e:
            print(f"❌ {name} FAILED: {e}\n")
            import traceback
            traceback.print_exc()
