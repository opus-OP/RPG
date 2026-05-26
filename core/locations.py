from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LOCATIONS_JSON = Path(__file__).resolve().parent.parent / "data" / "locations.json"
MOBS_JSON = Path(__file__).resolve().parent.parent / "data" / "mobs.json"


@dataclass
class MobAttack:
    type: str
    damage_type: str
    damage_min: int
    damage_max: int
    description: str
    chance: float = 1.0


@dataclass
class MobTemplate:
    id: str
    name: str
    description: str
    hp: int
    defense: int
    dodge_chance: float
    crit_chance: float
    crit_multiplier: float
    attack: MobAttack
    attack_secondary: Optional[MobAttack] = None


@dataclass
class DropRates:
    weapon_chance: float
    rarity_weights: dict[str, int]


@dataclass
class Location:
    key: str
    name: str
    description: str
    recommended_level: int
    danger: int
    min_enemies: int
    max_enemies: int
    mob_ids: list[str]
    gold_min: int
    gold_max: int
    exp_reward: int
    drop_rates: DropRates
    enemies: list[MobTemplate] = field(default_factory=list)


_mob_db: dict[str, MobTemplate] = {}
_loc_db: dict[str, Location] = {}
_loaded = False


def _load_all():
    global _loaded, _mob_db, _loc_db
    if _loaded:
        return
    with open(MOBS_JSON, encoding="utf-8") as f:
        mobs_data = json.load(f)["mobs"]
    for m in mobs_data:
        atk = MobAttack(**m["attack"])
        atk2 = MobAttack(**m["attack_secondary"]) if m.get("attack_secondary") else None
        _mob_db[m["id"]] = MobTemplate(
            id=m["id"], name=m["name"], description=m["description"],
            hp=m["hp"], defense=m["defense"],
            dodge_chance=m["dodge_chance"], crit_chance=m["crit_chance"],
            crit_multiplier=m["crit_multiplier"],
            attack=atk, attack_secondary=atk2,
        )
    with open(LOCATIONS_JSON, encoding="utf-8") as f:
        locs_data = json.load(f)["locations"]
    for key, loc in locs_data.items():
        dr = loc["drop_rates"]
        drop_rates = DropRates(weapon_chance=dr["weapon_chance"], rarity_weights=dr["rarity_weights"])
        location = Location(
            key=key, name=loc["name"], description=loc["description"],
            recommended_level=loc["recommended_level"], danger=loc["danger"],
            min_enemies=loc["min_enemies"], max_enemies=loc["max_enemies"],
            mob_ids=loc["mobs"],
            gold_min=loc["gold_min"], gold_max=loc["gold_max"],
            exp_reward=loc["exp_reward"], drop_rates=drop_rates,
        )
        for mid in loc["mobs"]:
            if mid in _mob_db:
                location.enemies.append(_mob_db[mid])
        _loc_db[key] = location
    _loaded = True


def get_mob(mob_id: str) -> Optional[MobTemplate]:
    _load_all()
    return _mob_db.get(mob_id)


def get_mobs() -> dict[str, MobTemplate]:
    _load_all()
    return dict(_mob_db)


def get_location(key: str) -> Optional[Location]:
    _load_all()
    return _loc_db.get(key)


def get_locations() -> dict[str, Location]:
    _load_all()
    return dict(_loc_db)


_load_all()
LOCATIONS: dict[str, Location] = _loc_db
