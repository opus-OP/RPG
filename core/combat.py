from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.character import Character
from utils.rng import secure_randint, roll_chance, rand_range
from utils.validators import clamp
from config import DMG_RANDOM_MIN, DMG_RANDOM_MAX, DODGE_BUFF_AMOUNT, DODGE_MAX


class StatusEffect(Enum):
    POISON = "poison"
    BLEED = "bleed"
    SHIELD = "shield"
    STUNNED = "stunned"
    DODGE_BUFF = "dodge_buff"
    CRIT_BOOST = "crit_boost"


@dataclass
class StatusEffectInstance:
    kind: StatusEffect
    duration: int
    value: float = 0.0


@dataclass
class BattleState:
    attacker: Character
    defender: Character
    is_player_attacker: bool
    turn_number: int = 0
    active_effects: dict[str, list[StatusEffectInstance]] = field(default_factory=dict)


@dataclass
class AttackResult:
    raw_damage: int
    final_damage: int
    is_crit: bool
    is_dodged: bool
    modifier_mult: float
    modifier_name: str = ""
    status_applied: Optional[StatusEffect] = None
    attribute: str = ""


def calc_damage(
    attacker,
    defender,
    modifier_mult: float = 1.0,
    guaranteed_crit: bool = False,
    shield_absorb: float = 0.0,
    dodge_override: float | None = None,
    attribute: str = "strength",
) -> AttackResult:
    base_dmg = secure_randint(attacker.attack_min, attacker.attack_max)
    if hasattr(attacker, 'stats'):
        s = getattr(attacker, 'stats')
        stat_val = getattr(s, attribute, s.strength)
        base_dmg += stat_val
    if base_dmg <= 0:
        base_dmg = secure_randint(1, 4)

    dodge = dodge_override if dodge_override is not None else defender.dodge_chance
    if roll_chance(dodge) and not guaranteed_crit:
        return AttackResult(
            raw_damage=base_dmg, final_damage=0, is_crit=False,
            is_dodged=True, modifier_mult=modifier_mult, attribute=attribute,
        )
    is_crit = guaranteed_crit or roll_chance(attacker.crit_chance)
    advantage = attacker.crit_multiplier if is_crit else 1.0
    dmg = base_dmg * modifier_mult * advantage
    dmg = dmg - defender.defense
    dmg += secure_randint(DMG_RANDOM_MIN, DMG_RANDOM_MAX)
    dmg = max(1, int(round(dmg)))
    if shield_absorb > 0:
        dmg = max(0, dmg - int(shield_absorb))
    return AttackResult(
        raw_damage=base_dmg, final_damage=dmg, is_crit=is_crit,
        is_dodged=False, modifier_mult=modifier_mult, attribute=attribute,
    )


def apply_nn_modifiers(state: BattleState, modifiers: list[dict]) -> None:
    for mod in modifiers:
        modifier = mod.get("modifier", "")
        value = mod.get("value", 1.0)
        target_key = "attacker" if mod.get("target") == "player" else "defender"
        if modifier == "WEAK_SPOT_FOUND":
            pass
        elif modifier == "DODGE_BONUS":
            eff_list = state.active_effects.setdefault(target_key, [])
            duration = max(1, int(value * 3))
            eff_list.append(StatusEffectInstance(StatusEffect.DODGE_BUFF, duration))
        elif modifier == "STUN":
            eff_key = "defender" if target_key == "attacker" else "attacker"
            eff_list = state.active_effects.setdefault(eff_key, [])
            eff_list.append(StatusEffectInstance(StatusEffect.STUNNED, 1))
        elif modifier == "CRIT_BOOST":
            eff_list = state.active_effects.setdefault(target_key, [])
            eff_list.append(StatusEffectInstance(StatusEffect.CRIT_BOOST, 1))
        elif modifier == "TAUNT":
            eff_list = state.active_effects.setdefault("defender", [])
            shield_amount = int(value * 30)
            eff_list.append(StatusEffectInstance(StatusEffect.SHIELD, 1, float(shield_amount)))


def apply_enemy_modifiers(state: BattleState, modifiers: list[dict]) -> None:
    for mod in modifiers:
        modifier = mod.get("modifier", "")
        value = mod.get("value", 1.0)
        target_key = "attacker" if mod.get("target") == "enemy" else "defender"
        if modifier == "WEAK_SPOT_FOUND":
            pass
        elif modifier == "DODGE_BONUS":
            eff_list = state.active_effects.setdefault(target_key, [])
            duration = max(1, int(value * 3))
            eff_list.append(StatusEffectInstance(StatusEffect.DODGE_BUFF, duration))
        elif modifier == "STUN":
            eff_key = "defender" if target_key == "attacker" else "attacker"
            eff_list = state.active_effects.setdefault(eff_key, [])
            eff_list.append(StatusEffectInstance(StatusEffect.STUNNED, 1))
        elif modifier == "CRIT_BOOST":
            eff_list = state.active_effects.setdefault(target_key, [])
            eff_list.append(StatusEffectInstance(StatusEffect.CRIT_BOOST, 1))
        elif modifier == "TAUNT":
            eff_list = state.active_effects.setdefault(target_key, [])
            shield_amount = int(value * 30)
            eff_list.append(StatusEffectInstance(StatusEffect.SHIELD, 1, float(shield_amount)))


def _apply_dot_damage(effects: list[StatusEffectInstance], target) -> None:
    for eff in effects:
        if eff.kind in (StatusEffect.POISON, StatusEffect.BLEED) and eff.value > 0:
            dmg = max(1, int(eff.value))
            target.hp = max(0, target.hp - dmg)


def _get_shield_absorb(effects: list[StatusEffectInstance]) -> float:
    for eff in effects:
        if eff.kind == StatusEffect.SHIELD:
            return eff.value
    return 0.0


def resolve_turn(
    attacker: Character,
    defender: Character,
    is_player_attacker: bool,
    turn_number: int,
    active_effects: dict[str, list[StatusEffectInstance]],
    nn_modifiers: Optional[list[dict]] = None,
    attribute: str = "strength",
) -> tuple[AttackResult, dict[str, list[StatusEffectInstance]]]:
    state = BattleState(
        attacker=attacker,
        defender=defender,
        is_player_attacker=is_player_attacker,
        turn_number=turn_number,
        active_effects=active_effects,
    )

    if nn_modifiers:
        apply_nn_modifiers(state, nn_modifiers)

    attacker_key = "attacker"
    defender_key = "defender"

    attacker_effects = state.active_effects.get(attacker_key, [])
    defender_effects = state.active_effects.get(defender_key, [])

    _apply_dot_damage(attacker_effects, state.attacker)
    _apply_dot_damage(defender_effects, state.defender)

    stunned = any(e.kind == StatusEffect.STUNNED for e in attacker_effects)
    if stunned:
        result = AttackResult(
            raw_damage=0, final_damage=0, is_crit=False,
            is_dodged=False, modifier_mult=0.0, attribute=attribute,
        )
        state.active_effects = _tick_effects(state.active_effects)
        return result, state.active_effects

    crit_boost = any(e.kind == StatusEffect.CRIT_BOOST for e in attacker_effects)
    dodge_buff = any(e.kind == StatusEffect.DODGE_BUFF for e in defender_effects)
    shield_absorb = _get_shield_absorb(defender_effects)

    modifier_mult = 1.0
    if nn_modifiers:
        for mod in nn_modifiers:
            if mod.get("modifier") == "WEAK_SPOT_FOUND":
                modifier_mult = max(0.1, float(mod.get("value", 1.0)))

    dodge_override = None
    if dodge_buff:
        dodge_override = clamp(defender.dodge_chance + DODGE_BUFF_AMOUNT, 0.0, DODGE_MAX)

    result = calc_damage(
        attacker, defender,
        modifier_mult=modifier_mult,
        guaranteed_crit=crit_boost,
        shield_absorb=shield_absorb,
        dodge_override=dodge_override,
        attribute=attribute,
    )

    if result.final_damage > 0 and not result.is_dodged:
        defender.hp = max(0, defender.hp - result.final_damage)

    state.active_effects = _tick_effects(state.active_effects)
    return result, state.active_effects


def _tick_effects(
    effects: dict[str, list[StatusEffectInstance]],
) -> dict[str, list[StatusEffectInstance]]:
    new_effects: dict[str, list[StatusEffectInstance]] = {}
    for key, eff_list in effects.items():
        remaining = []
        for eff in eff_list:
            eff.duration -= 1
            if eff.duration > 0:
                remaining.append(eff)
        if remaining:
            new_effects[key] = remaining
    return new_effects
