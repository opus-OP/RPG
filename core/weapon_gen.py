from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

from core.items import Item, ItemTemplate, ItemEffect, ItemType, Rarity
from utils.rng import secure_randint
from config import WEAPON_MIN_LEVEL_OFFSET, WEAPON_MAX_LEVEL_OFFSET

WEAPON_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "weapons.json"

_weapon_patterns: list[dict] = []


def load_weapon_patterns(path: str | Path = WEAPON_DATA_PATH):
    global _weapon_patterns
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    _weapon_patterns = data["weapon_patterns"]


def get_weapon_patterns() -> list[dict]:
    if not _weapon_patterns:
        load_weapon_patterns()
    return _weapon_patterns


def find_patterns(
    min_level: int = 1,
    max_level: int = 99,
    min_rarity: str = "COMMON",
    max_rarity: str = "LEGENDARY",
    allowed_classes: Optional[list[str]] = None,
) -> list[dict]:
    patterns = get_weapon_patterns()
    rarity_rank = {"COMMON": 0, "RARE": 1, "EPIC": 2, "LEGENDARY": 3}
    min_rank = rarity_rank.get(min_rarity, 0)
    max_rank = rarity_rank.get(max_rarity, 3)
    matched = []
    for p in patterns:
        if not (min_level <= p["required_level"] <= max_level):
            continue
        rank = rarity_rank.get(p["rarity"], 0)
        if rank < min_rank or rank > max_rank:
            continue
        if allowed_classes:
            if not any(c in p["allowed_classes"] for c in allowed_classes):
                continue
        matched.append(p)
    return matched


def roll_weapon_from_pattern(
    pattern: dict,
    uid: str,
) -> Optional[Item]:
    rarity_map = {
        "COMMON": Rarity.COMMON,
        "RARE": Rarity.RARE,
        "EPIC": Rarity.EPIC,
        "LEGENDARY": Rarity.LEGENDARY,
    }
    rarity = rarity_map.get(pattern["rarity"], Rarity.COMMON)

    base_names = pattern["base_names"]
    adjectives = pattern["adjectives"]
    selected_name = base_names[secure_randint(0, len(base_names) - 1)]
    selected_adj = adjectives[secure_randint(0, len(adjectives) - 1)]
    name = f"{selected_adj} {selected_name}"

    # TODO: AI генерирует название и описание на основе паттерна + локация + моб
    # ai_name = call_nn_for_name(pattern, location, mob)
    # ai_description = call_nn_for_description(pattern, location, mob)
    # Если AI не отвечает — используется шаблонное имя выше

    dmg_min = pattern["damage_min"]
    dmg_max = pattern["damage_max"]
    atk_bonus = secure_randint(dmg_min, dmg_max)

    crit_bonus = pattern.get("crit_bonus", 0.0)
    dodge_bonus = pattern.get("dodge_bonus", 0.0)

    effect = ItemEffect(atk_bonus=atk_bonus, crit_chance_bonus=crit_bonus, dodge_bonus=dodge_bonus)

    dur_min = pattern.get("durability_min", 50)
    dur_max = pattern.get("durability_max", 100)
    durability = secure_randint(dur_min, dur_max)

    tpl = ItemTemplate(
        name=name,
        item_type=ItemType.WEAPON,
        rarity=rarity,
        base_effect=effect,
        required_level=pattern["required_level"],
        durability_max=durability,
    )
    return Item(template=tpl, uid=uid, durability=durability, durability_max=durability)


def generate_loot_weapons(
    character_level: int = 0,
    allowed_classes: list[str] | None = None,
    num_rolls: int = 1,
    min_rarity: str = "COMMON",
    max_rarity: str = "LEGENDARY",
    min_level: int = 0,
    max_level: int = 0,
) -> list[Item]:
    if min_level <= 0 or max_level <= 0:
        min_level = max(1, character_level + WEAPON_MIN_LEVEL_OFFSET)
        max_level = character_level + WEAPON_MAX_LEVEL_OFFSET
    patterns = find_patterns(
        min_level=min_level,
        max_level=max_level,
        min_rarity=min_rarity,
        max_rarity=max_rarity,
        allowed_classes=allowed_classes,
    )
    if not patterns:
        return []

    items: list[Item] = []
    uid_counter = 0
    for _ in range(num_rolls):
        pattern = patterns[secure_randint(0, len(patterns) - 1)]
        item = roll_weapon_from_pattern(pattern, f"wpn_{uid_counter}")
        if item:
            items.append(item)
            uid_counter += 1
    return items


def get_attributes_descriptions(pattern: dict) -> list[str]:
    attr_descriptions = {
        "one_handed": "Одноручное",
        "two_handed": "Двуручное",
        "dual_wield": "Парное",
        "ranged": "Дальний бой",
        "melee": "Ближний бой",
        "polearm": "Древковое",
        "fast": "Быстрое",
        "heavy": "Тяжёлое",
        "versatile": "Универсальное",
        "fire": "Огненный урон",
        "ice": "Ледяной урон",
        "poison": "Ядовитый урон",
        "lightning": "Урон молнией",
        "holy": "Святой урон",
        "bleed": "Кровотечение",
        "vampiric": "Вампиризм",
        "scaling_strength": "Зависит от Силы",
        "scaling_agility": "Зависит от Ловкости",
        "scaling_intelligence": "Зависит от Интеллекта",
    }
    return [attr_descriptions.get(a, a) for a in pattern.get("attributes", [])]
