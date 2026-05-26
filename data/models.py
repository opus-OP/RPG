import json
from dataclasses import dataclass, field, asdict
from typing import Optional

from core.character import Character, Equipment, Companion
from core.items import Item, ItemTemplate, ItemEffect, Rarity, ItemType


def item_to_dict(item: Item) -> dict:
    d = {
        "uid": item.uid,
        "template_name": item.template.name,
        "durability": item.durability,
        "durability_max": item.durability_max,
    }
    td = item.template
    d["template_data"] = {
        "name": td.name,
        "item_type": td.item_type.value,
        "rarity": td.rarity.value,
        "base_effect": {
            "hp_bonus": td.base_effect.hp_bonus,
            "atk_bonus": td.base_effect.atk_bonus,
            "crit_chance_bonus": td.base_effect.crit_chance_bonus,
            "crit_multiplier_bonus": td.base_effect.crit_multiplier_bonus,
            "defense_bonus": td.base_effect.defense_bonus,
            "dodge_bonus": td.base_effect.dodge_bonus,
            "strength_bonus": td.base_effect.strength_bonus,
            "agility_bonus": td.base_effect.agility_bonus,
            "intelligence_bonus": td.base_effect.intelligence_bonus,
        },
        "required_level": td.required_level,
        "required_class": td.required_class,
        "durability_max": td.durability_max,
    }
    return d


def item_from_dict(data: dict, templates: dict[str, ItemTemplate]) -> Optional[Item]:
    tpl = templates.get(data.get("template_name", ""))
    if not tpl and data.get("template_data"):
        td = data["template_data"]
        tpl = ItemTemplate(
            name=td["name"],
            item_type=ItemType(td["item_type"]),
            rarity=Rarity(td["rarity"]),
            base_effect=ItemEffect(
                hp_bonus=td["base_effect"].get("hp_bonus", 0),
                atk_bonus=td["base_effect"].get("atk_bonus", 0),
                crit_chance_bonus=td["base_effect"].get("crit_chance_bonus", 0.0),
                crit_multiplier_bonus=td["base_effect"].get("crit_multiplier_bonus", 0.0),
                defense_bonus=td["base_effect"].get("defense_bonus", 0),
                dodge_bonus=td["base_effect"].get("dodge_bonus", 0.0),
                strength_bonus=td["base_effect"].get("strength_bonus", 0),
                agility_bonus=td["base_effect"].get("agility_bonus", 0),
                intelligence_bonus=td["base_effect"].get("intelligence_bonus", 0),
            ),
            required_level=td.get("required_level", 1),
            required_class=td.get("required_class"),
            durability_max=td.get("durability_max", 100),
        )
    if not tpl:
        return None
    return Item(
        template=tpl,
        uid=data["uid"],
        durability=data.get("durability", tpl.durability_max),
        durability_max=data.get("durability_max", tpl.durability_max),
    )


def equipment_to_dict(eq: Equipment) -> dict:
    return {
        "weapon": item_to_dict(eq.weapon) if eq.weapon else None,
        "armor": item_to_dict(eq.armor) if eq.armor else None,
        "accessory": item_to_dict(eq.accessory) if eq.accessory else None,
    }


def equipment_from_dict(data: dict, templates: dict[str, ItemTemplate]) -> Equipment:
    return Equipment(
        weapon=item_from_dict(data["weapon"], templates) if data.get("weapon") else None,
        armor=item_from_dict(data["armor"], templates) if data.get("armor") else None,
        accessory=item_from_dict(data["accessory"], templates) if data.get("accessory") else None,
    )


def companion_to_dict(c: Companion) -> dict:
    return {
        "name": c.name,
        "description": c.description,
        "hp": c.hp,
        "max_hp": c.max_hp,
        "attack_min": c.attack_min,
        "attack_max": c.attack_max,
        "defense": c.defense,
        "dodge_chance": c.dodge_chance,
        "crit_chance": c.crit_chance,
        "crit_multiplier": c.crit_multiplier,
        "alive": c.alive,
    }


def companion_from_dict(data: dict) -> Optional[Companion]:
    if not data:
        return None
    return Companion(
        name=data.get("name", "Призванный страж"),
        description=data.get("description", ""),
        hp=data.get("hp", 60),
        max_hp=data.get("max_hp", 60),
        attack_min=data.get("attack_min", 5),
        attack_max=data.get("attack_max", 10),
        defense=data.get("defense", 3),
        dodge_chance=data.get("dodge_chance", 0.03),
        crit_chance=data.get("crit_chance", 0.03),
        crit_multiplier=data.get("crit_multiplier", 2.0),
        alive=data.get("alive", True),
    )


def character_to_dict(char: Character) -> dict:
    return {
        "owner_tg_id": char.owner_tg_id,
        "name": char.name,
        "class_key": char.class_key,
        "description": char.description,
        "level": char.level,
        "experience": char.experience,
        "hp": char.hp,
        "max_hp": char.max_hp,
        "gold": char.gold,
        "equipment": equipment_to_dict(char.equipment),
        "inventory": [item_to_dict(i) for i in char.inventory],
        "in_raid": char.in_raid,
        "alive": char.alive,
        "companion": companion_to_dict(char.companion) if char.companion else None,
        "companion_name": char.companion_name,
        "companion_description": char.companion_description,
        "last_raid_time": char.last_raid_time,
    }


def character_from_dict(data: dict, templates: dict[str, ItemTemplate]) -> Character:
    char = Character(
        owner_tg_id=data["owner_tg_id"],
        name=data["name"],
        class_key=data["class_key"],
        description=data.get("description", ""),
        level=data.get("level", 1),
        experience=data.get("experience", 0),
        hp=data.get("hp", 0),
        max_hp=data.get("max_hp", 0),
        gold=data.get("gold", 0),
        equipment=equipment_from_dict(data.get("equipment", {}), templates),
        inventory=[item_from_dict(i, templates) for i in data.get("inventory", []) if item_from_dict(i, templates) is not None],
        in_raid=data.get("in_raid", False),
        alive=data.get("alive", True),
        companion=companion_from_dict(data.get("companion")),
        companion_name=data.get("companion_name", "Призванный страж"),
        companion_description=data.get("companion_description", ""),
        last_raid_time=data.get("last_raid_time", 0.0),
    )
    return char
