from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.character import Character, Companion
from core.locations import Location as LocationData, MobTemplate, MobAttack
from core.combat import resolve_turn, AttackResult, StatusEffect, StatusEffectInstance
from core.items import Item
from core.weapon_gen import generate_loot_weapons
from utils.rng import secure_randint, roll_chance


class _Enemy:
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
    enemy_hp: int
    enemy_max_hp: int
    enemy_template: dict
    turn: int = 0
    active_effects: dict[str, list[StatusEffectInstance]] = field(default_factory=dict)
    finished: bool = False


@dataclass
class RaidSession:
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


def generate_enemy_hp(enemy: dict) -> int:
    base = enemy.get("hp", 50)
    variance = secure_randint(-10, 10)
    return max(10, base + variance)


def create_raid(
    character: Character,
    location: LocationData,
    raid_id: str,
    group_id: Optional[str] = None,
    group_size: int = 1,
) -> RaidSession:
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
        location_key=location.name,
        participants=[character],
        encounters=encounters,
        group_id=group_id,
    )


def process_encounter_turn(
    session: RaidSession,
    character: Character,
    nn_modifiers: Optional[list[dict]] = None,
) -> tuple[AttackResult, Optional[AttackResult], bool]:
    enc = session.encounters[session.current_encounter]
    enc.turn += 1

    enemy = _Enemy(enc.enemy_template, enc.enemy_hp)

    # TODO: получать narrative-описание от нейросети
    # narrative = await call_nn(session, character, enc)

    player_attack, enc.active_effects = resolve_turn(
        attacker=character,
        defender=enemy,
        is_player_attacker=True,
        turn_number=enc.turn,
        active_effects=enc.active_effects,
        nn_modifiers=nn_modifiers,
    )

    enc.enemy_hp = enemy.hp
    if enemy.hp <= 0:
        enc.finished = True
        return player_attack, None, True

    companion_attack = None
    if character.companion and character.companion.alive:
        companion = character.companion
        companion_attack, _ = resolve_turn(
            attacker=companion,
            defender=enemy,
            is_player_attacker=True,
            turn_number=enc.turn,
            active_effects={},
            nn_modifiers=None,
        )
        enc.enemy_hp = enemy.hp
        if enemy.hp <= 0:
            enc.finished = True
            return player_attack, companion_attack, True

    atk_min_saved, atk_max_saved = enemy.attack_min, enemy.attack_max
    atk_min_pick, atk_max_pick, _, atk_damage_type = enemy.pick_attack()
    enemy.attack_min, enemy.attack_max = atk_min_pick, atk_max_pick
    enemy_attack, enc.active_effects = resolve_turn(
        attacker=enemy,
        defender=character,
        is_player_attacker=False,
        turn_number=enc.turn,
        active_effects=enc.active_effects,
        nn_modifiers=None,
    )

    enemy.attack_min, enemy.attack_max = atk_min_saved, atk_max_saved

    if enemy_attack.final_damage > 0 and not enemy_attack.is_dodged:
        enc.active_effects = apply_mob_status_effects(
            enc.active_effects, atk_damage_type, enemy_attack.final_damage
        )

    player_died = character.hp <= 0
    if player_died:
        enc.finished = True

    return player_attack, enemy_attack, enemy.hp <= 0 or player_died


def generate_loot(
    location: LocationData,
    enemies_defeated: int,
    character_level: int,
    allowed_classes: list[str],
) -> list[Item]:
    items: list[Item] = []
    uid_counter = 0
    rarities: list[str] = []
    for r, w in location.drop_rates.rarity_weights.items():
        rarities.extend([r] * w)

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
    base_exp = location.exp_reward
    base_gold = secure_randint(location.gold_min, location.gold_max)
    per_participant_exp = max(1, base_exp // len(participants))
    per_participant_gold = max(1, base_gold // len(participants))
    for char in participants:
        if char.alive and char.hp > 0:
            char.add_experience(per_participant_exp)
            char.gold += per_participant_gold
