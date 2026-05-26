import sys; sys.path.insert(0, ".")

import json
import pytest
from typing import AsyncGenerator

from data.storage import Storage
from data.storage import register_templates
from core.character import Character, Companion
from core.raid import RaidEncounter, RaidSession, RaidStatus
from core.locations import Location, DropRates, MobTemplate, MobAttack


MOCK_GOBLIN = MobTemplate(
    id="goblin",
    name="Гоблин",
    description="Злобный зелёный гоблин",
    hp=50, defense=2, dodge_chance=0.05,
    crit_chance=0.05, crit_multiplier=2.0,
    attack=MobAttack(type="melee", damage_type="physical",
                     damage_min=5, damage_max=10,
                     description="удар кинжалом"),
)

MOCK_LOCATION = Location(
    key="forest", name="Лес", description="Тёмный лес",
    recommended_level=3, danger=2,
    min_enemies=2, max_enemies=4,
    mob_ids=["goblin"],
    gold_min=10, gold_max=30,
    exp_reward=50,
    drop_rates=DropRates(weapon_chance=0.8, rarity_weights={"common": 5, "rare": 3}),
    enemies=[MOCK_GOBLIN],
)


@pytest.fixture
async def storage() -> AsyncGenerator[Storage, None]:
    s = Storage(db_path=":memory:")
    await s.connect()
    register_templates()
    yield s
    await s.close()


@pytest.fixture
def warrior():
    return Character(owner_tg_id=1, name="Воин", class_key="warrior")


@pytest.fixture
def leader():
    c = Character(owner_tg_id=2, name="Лидер", class_key="leader")
    c.companion = Companion(name="Страж")
    return c


@pytest.fixture
def encounter():
    return RaidEncounter(enemy_hp=50, enemy_max_hp=50, enemy_template={
        "name": "Goblin", "hp": 50, "atk_min": 5, "atk_max": 10,
        "defense": 2, "dodge_chance": 0.05, "crit_chance": 0.05,
        "crit_multiplier": 2.0, "atk_damage_type": "physical", "attack_secondary": None,
    })


@pytest.fixture
def session(warrior, encounter):
    s = RaidSession(
        raid_id="test", location_key="forest",
        status=RaidStatus.IN_PROGRESS,
        participants=[warrior],
        encounters=[encounter],
    )
    return s


@pytest.fixture
def location():
    return MOCK_LOCATION
