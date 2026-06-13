"""Рейд-система: создание рейда, обработка ходов, генерация лута.

Рейд — последовательность столкновений (encounters) с мобами.
Каждое столкновение — пошаговый бой с инициативой.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.character import Character, Companion
from core.locations import Location as LocationData, MobTemplate, MobAttack
from core.combat import resolve_turn, AttackResult, StatusEffect, StatusEffectInstance, apply_enemy_modifiers
from core.items import Item
from core.weapon_gen import generate_loot_weapons
from utils.rng import secure_randint, roll_chance


class _Enemy:
    """Обёртка для моба в бою. Содержит текущее состояние и выбор атаки."""

    def __init__(self, data: dict, current_hp: int):
        self.hp = current_hp
        self.max_hp = data["hp"]
        self.attack_min = data["atk_min"]
        self.attack_max = data["atk_max"]
        self.defense = data["defense"]
        self.dodge_chance = data["dodge_chance"]
        self.crit_chance = data.get("crit_chance", 0.05)
        self.crit_multiplier = data.get("crit_multiplier", 2.0)
        self.name = data["name"]
        self.attack_damage_type: str = data.get("atk_damage_type", "physical")
        self.attack_secondary: Optional[dict] = data.get("attack_secondary")

    def pick_attack(self) -> tuple[int, int, str, str]:
        """С вероятностью `chance` выбирает secondary-атаку, иначе основную.

        Returns:
            (damage_min, damage_max, description, damage_type)
        """
        if self.attack_secondary and roll_chance(self.attack_secondary["chance"]):
            return (
                self.attack_secondary["damage_min"],
                self.attack_secondary["damage_max"],
                self.attack_secondary.get("description", ""),
                self.attack_secondary.get("damage_type", "physical"),
            )
        return self.attack_min, self.attack_max, "", self.attack_damage_type


class RaidStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class RaidEncounter:
    """Одно столкновение с мобом в рейде.

    Хранит состояние боя: HP врага, очерёдность ходов, активные эффекты.
    """
    enemy_hp: int
    enemy_max_hp: int
    enemy_template: dict
    turn: int = 0
    active_effects: dict[str, list[StatusEffectInstance]] = field(default_factory=dict)
    finished: bool = False
    initiative_order: list[dict] = field(default_factory=list)
    current_turn_index: int = 0
    round_number: int = 0
    turn_timeout_deadline: float = 0.0


@dataclass
class RaidSession:
    """Сессия рейда — от создания до завершения.

    Содержит всех участников, последовательность столкновений,
    накопленный лут и награды.
    """
    raid_id: str
    location_key: str
    status: RaidStatus = RaidStatus.PENDING
    participants: list[Character] = field(default_factory=list)
    encounters: list[RaidEncounter] = field(default_factory=list)
    current_encounter: int = 0
    loot: list[Item] = field(default_factory=list)
    total_exp: int = 0
    total_gold: int = 0
    group_id: Optional[str] = None
    participant_names: dict[int, str] = field(default_factory=dict)
    active_buffs: dict[str, int] = field(default_factory=dict)
    used_event_ids: set[str] = field(default_factory=set)
    turn_pending_uid: Optional[int] = None
    pending_event: Optional[dict] = None


# ─── Сериализация для хранения в БД ─────────────────────────


def _effect_to_dict(e: StatusEffectInstance) -> dict:
    return {"kind": e.kind.value, "remaining": e.duration, "damage_per_tick": e.value}


def _effect_from_dict(d: dict) -> StatusEffectInstance:
    return StatusEffectInstance(StatusEffect(d["kind"]), d.get("remaining", 1), d.get("damage_per_tick", 0.0))


def raid_encounter_to_dict(enc: RaidEncounter) -> dict:
    return {
        "enemy_hp": enc.enemy_hp,
        "enemy_max_hp": enc.enemy_max_hp,
        "enemy_template": enc.enemy_template,
        "turn": enc.turn,
        "active_effects": {
            k: [_effect_to_dict(e) for e in v] for k, v in enc.active_effects.items()
        },
        "finished": enc.finished,
        "initiative_order": enc.initiative_order,
        "current_turn_index": enc.current_turn_index,
        "round_number": enc.round_number,
        "turn_timeout_deadline": enc.turn_timeout_deadline,
    }


def raid_encounter_from_dict(d: dict) -> RaidEncounter:
    return RaidEncounter(
        enemy_hp=d["enemy_hp"],
        enemy_max_hp=d["enemy_max_hp"],
        enemy_template=d["enemy_template"],
        turn=d.get("turn", 0),
        active_effects={
            k: [_effect_from_dict(e) for e in v]
            for k, v in d.get("active_effects", {}).items()
        },
        finished=d.get("finished", False),
        initiative_order=d.get("initiative_order", []),
        current_turn_index=d.get("current_turn_index", 0),
        round_number=d.get("round_number", 0),
        turn_timeout_deadline=d.get("turn_timeout_deadline", 0.0),
    )


def session_to_dict(session: RaidSession) -> dict:
    return {
        "raid_id": session.raid_id,
        "location_key": session.location_key,
        "status": session.status.value,
        "current_encounter": session.current_encounter,
        "total_exp": session.total_exp,
        "total_gold": session.total_gold,
        "group_id": session.group_id,
        "participant_names": {str(k): v for k, v in session.participant_names.items()},
        "active_buffs": session.active_buffs,
        "used_event_ids": list(session.used_event_ids),
        "turn_pending_uid": session.turn_pending_uid,
        "pending_event": session.pending_event,
        "encounters": [raid_encounter_to_dict(e) for e in session.encounters],
    }


def session_from_dict(data: dict) -> RaidSession:
    return RaidSession(
        raid_id=data["raid_id"],
        location_key=data["location_key"],
        status=RaidStatus(data.get("status", "pending")),
        current_encounter=data.get("current_encounter", 0),
        total_exp=data.get("total_exp", 0),
        total_gold=data.get("total_gold", 0),
        group_id=data.get("group_id"),
        participant_names=data.get("participant_names", {}),
        active_buffs=data.get("active_buffs", {}),
        used_event_ids=set(data.get("used_event_ids", [])),
        turn_pending_uid=data.get("turn_pending_uid"),
        pending_event=data.get("pending_event"),
        encounters=[raid_encounter_from_dict(e) for e in data.get("encounters", [])],
    )


# ─── Создание рейда ─────────────────────────────────────────


def generate_enemy_hp(enemy: dict) -> int:
    """Генерирует HP врага с небольшим разбросом."""
    base = enemy.get("hp", 50)
    variance = secure_randint(-10, 10)
    return max(10, base + variance)


def roll_initiative(stat_value: int) -> int:
    """Бросок инициативы: 1d20 + stat/5."""
    return secure_randint(1, 20) + stat_value // 5


def build_initiative_order(
    characters: dict[int, Character],
    enc: RaidEncounter,
) -> list[dict]:
    """Бросает инициативу для всех участников и врага, возвращает сортированный список.

    Каждая запись: {"type":"player"|"companion"|"enemy", "uid":int|str,
                     "name":str, "initiative":int}
    Для игроков uid — tg id, для компаньонов — uid владельца,
    для врага — "enemy".
    Компаньон идёт сразу после своего владельца.
    """
    entries = []
    for uid, char in characters.items():
        if not char.alive or char.hp <= 0:
            continue
        init = roll_initiative(char.stats.agility)
        entries.append({"type": "player", "uid": uid, "name": char.name, "initiative": init})
        if char.companion and char.companion.alive:
            entries.append({
                "type": "companion", "uid": uid,
                "name": f"Страж {char.name}", "initiative": init,
            })
    mob = enc.enemy_template
    enemy_init = roll_initiative(int(mob.get("dodge_chance", 0) * 100))
    entries.append({"type": "enemy", "uid": "enemy", "name": mob["name"], "initiative": enemy_init})

    entries.sort(key=lambda e: (-e["initiative"], 0 if e["type"] != "enemy" else 1))
    return entries


def create_raid(
    character: Character,
    location: LocationData,
    raid_id: str,
    group_id: Optional[str] = None,
    group_size: int = 1,
) -> RaidSession:
    """Создаёт новую рейд-сессию.

    Генерирует случайное количество врагов из пула локации,
    определяет инициативу для каждого. Для групп HP врагов
    масштабируется: +30% за каждого дополнительного участника.
    """
    if character.class_key == "leader":
        character.summon_companion()
    num_enemies = secure_randint(location.min_enemies, location.max_enemies)
    encounters = []
    for _ in range(num_enemies):
        mob = location.enemies[
            secure_randint(0, len(location.enemies) - 1)
        ]
        atk2 = None
        if mob.attack_secondary:
            atk2 = {
                "damage_min": mob.attack_secondary.damage_min,
                "damage_max": mob.attack_secondary.damage_max,
                "chance": mob.attack_secondary.chance,
                "description": mob.attack_secondary.description,
                "damage_type": mob.attack_secondary.damage_type,
            }
        encounters.append(RaidEncounter(
            enemy_hp=generate_enemy_hp({"hp": mob.hp}),
            enemy_max_hp=mob.hp,
            enemy_template={
                "name": mob.name,
                "hp": mob.hp,
                "atk_min": mob.attack.damage_min,
                "atk_max": mob.attack.damage_max,
                "defense": mob.defense,
                "dodge_chance": mob.dodge_chance,
                "crit_chance": mob.crit_chance,
                "crit_multiplier": mob.crit_multiplier,
                "atk_damage_type": mob.attack.damage_type,
                "attack_secondary": atk2,
            },
        ))

    if group_size > 1:
        for enc in encounters:
            enc.enemy_hp = int(enc.enemy_hp * (1 + 0.3 * (group_size - 1)))
            enc.enemy_max_hp = enc.enemy_hp

    return RaidSession(
        raid_id=raid_id,
        location_key=location.key,
        participants=[character],
        encounters=encounters,
        group_id=group_id,
    )


# ─── Обработка ходов ────────────────────────────────────────


def create_enemy(enc: RaidEncounter) -> _Enemy:
    return _Enemy(enc.enemy_template, enc.enemy_hp)


def resolve_player_turn(
    character: Character, enemy: _Enemy, enc: RaidEncounter,
    nn_modifiers: Optional[list[dict]],
    attribute: str = "strength",
) -> AttackResult:
    atk, enc.active_effects = resolve_turn(
        attacker=character, defender=enemy,
        is_player_attacker=True, turn_number=enc.turn,
        active_effects=enc.active_effects, nn_modifiers=nn_modifiers,
        attribute=attribute,
    )
    enc.enemy_hp = enemy.hp
    return atk


def resolve_companion_turn(
    character: Character, enemy: _Enemy, enc: RaidEncounter,
) -> Optional[AttackResult]:
    if not character.companion or not character.companion.alive:
        return None
    comp = character.companion
    owner_stat = character.stats.intelligence
    atk, _ = resolve_turn(
        attacker=comp, defender=enemy,
        is_player_attacker=True, turn_number=enc.turn,
        active_effects={}, nn_modifiers=None,
        attribute="intelligence",
    )
    enc.enemy_hp = enemy.hp
    return atk


def resolve_enemy_turn(
    character: Character, enemy: _Enemy, enc: RaidEncounter,
    enemy_nn_modifiers: Optional[list[dict]],
) -> AttackResult:
    """Разрешает ход врага: выбор атаки, применение NN-модификаторов, наложение статусов."""
    atk_min_saved, atk_max_saved = enemy.attack_min, enemy.attack_max
    atk_min_pick, atk_max_pick, _, atk_damage_type = enemy.pick_attack()
    enemy.attack_min, enemy.attack_max = atk_min_pick, atk_max_pick

    if enemy_nn_modifiers:
        from core.combat import BattleState
        es = BattleState(
            attacker=enemy, defender=character,
            is_player_attacker=False, turn_number=enc.turn,
            active_effects=enc.active_effects,
        )
        apply_enemy_modifiers(es, enemy_nn_modifiers)
        enc.active_effects = es.active_effects

    atk, enc.active_effects = resolve_turn(
        attacker=enemy, defender=character,
        is_player_attacker=False, turn_number=enc.turn,
        active_effects=enc.active_effects, nn_modifiers=None,
        attribute="strength",
    )
    enemy.attack_min, enemy.attack_max = atk_min_saved, atk_max_saved

    if atk.final_damage > 0 and not atk.is_dodged:
        enc.active_effects = apply_mob_status_effects(
            enc.active_effects, atk_damage_type, atk.final_damage,
        )
    return atk


# ─── Multiplayer turn system ────────────────────────────────


def get_current_turn(enc: RaidEncounter) -> Optional[dict]:
    """Возвращает текущего актора в инициативе или None."""
    if not enc.initiative_order or enc.current_turn_index >= len(enc.initiative_order):
        return None
    return enc.initiative_order[enc.current_turn_index]


def advance_turn_core(enc: RaidEncounter) -> Optional[dict]:
    """Переходит к следующему актору в порядке инициативы.

    Если раунд завершён — обнуляет индекс и увеличивает номер раунда.
    Возвращает следующего актора или None.
    """
    enc.current_turn_index += 1
    if enc.current_turn_index >= len(enc.initiative_order):
        enc.current_turn_index = 0
        enc.round_number += 1
    return get_current_turn(enc)


def pick_enemy_target(
    chars: dict[int, Character],
) -> tuple[int, Character]:
    """Выбирает случайную живую цель для атаки врага."""
    alive = [(uid, c) for uid, c in chars.items() if c.alive and c.hp > 0]
    if not alive:
        return 0, list(chars.values())[0]
    import random as _random
    return _random.choice(alive)


# ─── Лут и награды ──────────────────────────────────────────


def generate_loot(
    location: LocationData,
    enemies_defeated: int,
    character_level: int,
    allowed_classes: list[str],
) -> list[Item]:
    """Генерирует дроп на основе параметров локации.

    Для каждого врага бросается шанс выпадения weapon_chance.
    Редкость определяется весами rarity_weights локации.
    """
    items: list[Item] = []
    uid_counter = 0
    rarities: list[str] = []
    for r, w in location.drop_rates.rarity_weights.items():
        rarities.extend([r] * w)

    weapon_min = max(1, location.recommended_level - 2)
    weapon_max = location.recommended_level + 3

    for _ in range(enemies_defeated):
        if not roll_chance(location.drop_rates.weapon_chance):
            continue
        weapon_rarity = rarities[secure_randint(0, len(rarities) - 1)]
        weapon_items = generate_loot_weapons(
            character_level=character_level,
            allowed_classes=allowed_classes,
            num_rolls=1,
            max_rarity=weapon_rarity,
            min_rarity=weapon_rarity,
            min_level=weapon_min,
            max_level=weapon_max,
        )
        for it in weapon_items:
            it.uid = f"loot_{uid_counter}"
            items.append(it)
            uid_counter += 1
    return items


DAMAGE_TYPE_EFFECTS: dict[str, tuple[StatusEffect, float]] = {
    "bleed": (StatusEffect.BLEED, 0.10),
    "poison": (StatusEffect.POISON, 0.06),
    "fire": (StatusEffect.POISON, 0.08),
    "ice": (StatusEffect.STUNNED, 1.0),
}


def apply_mob_status_effects(
    active_effects: dict[str, list[StatusEffectInstance]],
    damage_type: str,
    damage_dealt: int,
) -> dict[str, list[StatusEffectInstance]]:
    """Накладывает статус-эффект на игрока в зависимости от типа атаки моба.

    bleed → BLEED (10% урона/ход, 3 хода)
    poison → POISON (6% урона/ход, 3 хода)
    fire → POISON (8% урона/ход, 3 хода)
    ice → STUNNED (1 ход)
    """
    mapping = DAMAGE_TYPE_EFFECTS.get(damage_type)
    if not mapping or damage_dealt <= 0:
        return active_effects
    effect_kind, multiplier = mapping
    eff_list = active_effects.setdefault("defender", [])
    if effect_kind == StatusEffect.STUNNED:
        if not any(e.kind == StatusEffect.STUNNED for e in eff_list):
            eff_list.append(StatusEffectInstance(StatusEffect.STUNNED, 1))
    else:
        dot_dmg = max(1, int(damage_dealt * multiplier))
        eff_list.append(StatusEffectInstance(effect_kind, 3, float(dot_dmg)))
    return active_effects


def distribute_exp_gold(
    session: RaidSession,
    location: LocationData,
    participants: list[Character],
) -> None:
    """Распределяет опыт и золото между выжившими участниками."""
    base_exp = location.exp_reward
    base_gold = secure_randint(location.gold_min, location.gold_max)
    per_participant_exp = max(1, base_exp // len(participants))
    per_participant_gold = max(1, base_gold // len(participants))
    for char in participants:
        if char.alive and char.hp > 0:
            char.add_experience(per_participant_exp)
            char.gold += per_participant_gold
