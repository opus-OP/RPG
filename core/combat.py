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
    STUN_IMMUNE = "stun_immune"  # защита от «стан-лока» после оглушения


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
    madness_applied: bool = False


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
    dodge = max(0.0, dodge - getattr(attacker, "accuracy", 0.0))  # меткость снижает уклонение цели
    if roll_chance(dodge) and not guaranteed_crit:
        return AttackResult(
            raw_damage=base_dmg, final_damage=0, is_crit=False,
            is_dodged=True, modifier_mult=modifier_mult, attribute=attribute,
        )
    is_crit = guaranteed_crit or roll_chance(attacker.crit_chance)
    advantage = attacker.crit_multiplier if is_crit else 1.0
    attr_mult = attacker.damage_mult_for(attribute) if hasattr(attacker, "damage_mult_for") else 1.0
    dmg = base_dmg * modifier_mult * advantage * attr_mult
    dmg = dmg - defender.defense
    dmg += secure_randint(DMG_RANDOM_MIN, DMG_RANDOM_MAX)
    dmg *= getattr(defender, "incoming_damage_mult", 1.0)
    dmg = max(1, int(round(dmg)))
    if shield_absorb > 0:
        dmg = max(0, dmg - int(shield_absorb))
    return AttackResult(
        raw_damage=base_dmg, final_damage=dmg, is_crit=is_crit,
        is_dodged=False, modifier_mult=modifier_mult, attribute=attribute,
    )


def _to_float(value, default: Optional[float] = 1.0) -> Optional[float]:
    """Безопасно приводит значение от нейросети к float (строки, None, NaN)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return v


def apply_modifiers(
    effects: dict[str, list[StatusEffectInstance]],
    modifiers: list[dict],
    actor_key: str,
    opponent_key: str,
    actor_side: str,
) -> None:
    """Применяет модификаторы нейросети к списку эффектов.

    actor_side — кто ходит сейчас: "player" или "enemy". Поле target модификатора
    ("player"/"enemy") указывает, на кого вешается эффект: на того, кто ходит
    (actor_key), или на его противника (opponent_key).

    Особые случаи:
      * STUN всегда бьёт по противнику ходящего (оглушить себя нельзя);
      * WEAK_SPOT_FOUND эффектов не создаёт — множитель берётся в resolve_turn;
      * эффекты, повешенные на самого ходящего, получают +1 к длительности,
        иначе они исчезли бы в конце этого же хода, не успев сработать.
    """
    for mod in modifiers or []:
        if not isinstance(mod, dict):
            continue
        name = mod.get("modifier", "")
        value = _to_float(mod.get("value", 1.0), default=None)
        if value is None:
            continue  # мусор от нейросети — модификатор игнорируется
        target = mod.get("target")

        if target == actor_side:
            side_key = actor_key
        elif target in ("player", "enemy"):
            side_key = opponent_key
        else:
            side_key = None  # не указан — решает сам модификатор

        if name == "STUN":
            opp = effects.get(opponent_key, [])
            if value >= 0.5 and not any(
                e.kind in (StatusEffect.STUNNED, StatusEffect.STUN_IMMUNE) for e in opp
            ):
                effects.setdefault(opponent_key, []).append(
                    StatusEffectInstance(StatusEffect.STUNNED, 1)
                )
        elif name == "CRIT_BOOST":
            if value >= 0.5:
                key = side_key or actor_key
                effects.setdefault(key, []).append(
                    StatusEffectInstance(StatusEffect.CRIT_BOOST, 1)
                )
        elif name == "DODGE_BONUS":
            if value > 0:
                key = side_key or actor_key
                duration = max(1, int(value * 3)) + (1 if key == actor_key else 0)
                effects.setdefault(key, []).append(
                    StatusEffectInstance(StatusEffect.DODGE_BUFF, duration, value)
                )
        elif name == "TAUNT":
            shield_amount = int(value * 30)
            if shield_amount > 0:
                key = side_key or actor_key
                duration = 1 + (1 if key == actor_key else 0)
                effects.setdefault(key, []).append(
                    StatusEffectInstance(StatusEffect.SHIELD, duration, float(shield_amount))
                )


def apply_nn_modifiers(state: BattleState, modifiers: list[dict]) -> None:
    """Устаревший интерфейс: модификаторы хода игрока (ключи attacker/defender)."""
    apply_modifiers(state.active_effects, modifiers, "attacker", "defender", "player")


def apply_enemy_modifiers(state: BattleState, modifiers: list[dict]) -> None:
    """Устаревший интерфейс: модификаторы хода врага (ключи attacker/defender)."""
    apply_modifiers(state.active_effects, modifiers, "attacker", "defender", "enemy")


def _apply_dot_damage(effects: list[StatusEffectInstance], target) -> None:
    for eff in effects:
        if eff.kind in (StatusEffect.POISON, StatusEffect.BLEED) and eff.value > 0:
            dmg = max(1, int(eff.value))
            target.hp = max(0, target.hp - dmg)


def _get_shield_absorb(effects: list[StatusEffectInstance]) -> float:
    return sum(e.value for e in effects if e.kind == StatusEffect.SHIELD)


def _get_dodge_bonus(effects: list[StatusEffectInstance]) -> float:
    total = 0.0
    for e in effects:
        if e.kind == StatusEffect.DODGE_BUFF:
            total += e.value if e.value > 0 else DODGE_BUFF_AMOUNT
    return total


def resolve_turn(
    attacker: Character,
    defender: Character,
    is_player_attacker: bool,
    turn_number: int,
    active_effects: dict[str, list[StatusEffectInstance]],
    nn_modifiers: Optional[list[dict]] = None,
    attribute: str = "strength",
    attacker_key: str = "attacker",
    defender_key: str = "defender",
) -> tuple[AttackResult, dict[str, list[StatusEffectInstance]]]:
    """Один ход атакующего.

    Эффекты хранятся по ключам участников (attacker_key / defender_key), а не по
    роли в текущем ходу — иначе эффекты врага и игрока перемешиваются.
    Порядок хода атакующего:
      1. урон от своих DoT-эффектов (яд, кровотечение);
      2. оглушён — ход пропущен;
      3. применяются модификаторы нейросети, затем атака;
      4. в конце хода тикают (уменьшаются) только ЕГО эффекты.
    """
    state = BattleState(
        attacker=attacker,
        defender=defender,
        is_player_attacker=is_player_attacker,
        turn_number=turn_number,
        active_effects=active_effects,
    )
    effects = state.active_effects

    def _skip(reason_attr: str = attribute) -> tuple[AttackResult, dict]:
        res = AttackResult(
            raw_damage=0, final_damage=0, is_crit=False,
            is_dodged=False, modifier_mult=0.0, attribute=reason_attr,
        )
        return res, _tick_actor(effects, attacker_key)

    _apply_dot_damage(effects.get(attacker_key, []), attacker)
    if attacker.hp <= 0:
        return _skip()

    if any(e.kind == StatusEffect.STUNNED for e in effects.get(attacker_key, [])):
        return _skip()

    if nn_modifiers:
        apply_modifiers(
            effects, nn_modifiers, attacker_key, defender_key,
            "player" if is_player_attacker else "enemy",
        )

    attacker_effects = effects.get(attacker_key, [])
    defender_effects = effects.get(defender_key, [])

    crit_boost = any(e.kind == StatusEffect.CRIT_BOOST for e in attacker_effects)
    shield_absorb = _get_shield_absorb(defender_effects)
    dodge_bonus = _get_dodge_bonus(defender_effects)

    modifier_mult = 1.0
    for mod in nn_modifiers or []:
        if isinstance(mod, dict) and mod.get("modifier") == "WEAK_SPOT_FOUND":
            modifier_mult = max(0.1, _to_float(mod.get("value", 1.0)))

    dodge_override = None
    if dodge_bonus > 0:
        dodge_override = clamp(defender.dodge_chance + dodge_bonus, 0.0, DODGE_MAX)

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

    return result, _tick_actor(effects, attacker_key)


def _tick_actor(
    effects: dict[str, list[StatusEffectInstance]], actor_key: str,
) -> dict[str, list[StatusEffectInstance]]:
    """Уменьшает длительность эффектов только у того, чей ход закончился."""
    new_effects: dict[str, list[StatusEffectInstance]] = {}
    for key, eff_list in effects.items():
        if key != actor_key:
            if eff_list:
                new_effects[key] = eff_list
            continue
        remaining = []
        was_stunned = False
        for eff in eff_list:
            eff.duration -= 1
            if eff.duration > 0:
                remaining.append(eff)
            elif eff.kind == StatusEffect.STUNNED:
                was_stunned = True
        if was_stunned:
            # после оглушения два хода иммунитета — иначе враг с ледяной атакой
            # оглушал бы игрока бесконечно (и наоборот)
            remaining.append(StatusEffectInstance(StatusEffect.STUN_IMMUNE, 2))
        if remaining:
            new_effects[key] = remaining
    return new_effects


def _tick_effects(
    effects: dict[str, list[StatusEffectInstance]],
) -> dict[str, list[StatusEffectInstance]]:
    """Тикает эффекты у всех ключей (оставлено для обратной совместимости)."""
    new_effects: dict[str, list[StatusEffectInstance]] = {}
    for key in list(effects):
        new_effects.update(_tick_actor(effects, key))
    return new_effects
