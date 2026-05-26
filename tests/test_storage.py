"""Test storage: character save/load, item serialization roundtrip, raid sessions."""
import sys; sys.path.insert(0, ".")
import pytest
from data.models import item_to_dict, item_from_dict, character_to_dict, character_from_dict
from data.storage import register_templates, _ITEM_TEMPLATES
from core.raid import session_to_dict, session_from_dict
from core.raid import RaidSession, RaidStatus
from core.items import Item, ItemTemplate, ItemEffect, ItemType, Rarity


@pytest.fixture(autouse=True)
def ensure_templates():
    register_templates()


def test_item_to_dict_roundtrip():
    tpl = ItemTemplate("Test Sword", ItemType.WEAPON, Rarity.RARE,
                        ItemEffect(atk_bonus=5, crit_chance_bonus=0.02))
    item = Item(template=tpl, uid="test_001", durability=80, durability_max=100)
    d = item_to_dict(item)
    restored = item_from_dict(d, _ITEM_TEMPLATES)
    assert restored is not None
    assert restored.uid == "test_001"
    assert restored.template.name == "Test Sword"
    assert restored.durability == 80


def test_item_roundtrip_with_unknown_template():
    """Items with template not in _ITEM_TEMPLATES rebuild from template_data."""
    tpl = ItemTemplate("Dynamic Axe", ItemType.WEAPON, Rarity.EPIC,
                        ItemEffect(atk_bonus=12, defense_bonus=3),
                        required_level=10)
    item = Item(template=tpl, uid="dyn_001")
    d = item_to_dict(item)
    restored = item_from_dict(d, {})
    assert restored is not None
    assert restored.template.name == "Dynamic Axe"
    assert restored.template.base_effect.atk_bonus == 12
    assert restored.template.required_level == 10


def test_static_template_resolved_from_registry():
    """Items with known template names use the registered template."""
    tpl = _ITEM_TEMPLATES.get("Деревянный меч")
    assert tpl is not None
    item = Item(template=tpl, uid="static_001")
    d = item_to_dict(item)
    restored = item_from_dict(d, _ITEM_TEMPLATES)
    assert restored is not None
    assert restored.template.name == "Деревянный меч"


@pytest.mark.asyncio
async def test_save_load_character(storage):
    from core.character import Character
    char = Character(owner_tg_id=1, name="TestChar", class_key="warrior",
                     level=5, gold=100, hp=80, max_hp=100)
    await storage.save_character(char)
    loaded = await storage.load_character_by_name(1, "TestChar")
    assert loaded is not None
    assert loaded.name == "TestChar"
    assert loaded.level == 5
    assert loaded.gold == 100
    assert loaded.hp == 80


@pytest.mark.asyncio
async def test_load_all_characters(storage):
    from core.character import Character
    for i in range(3):
        await storage.save_character(Character(owner_tg_id=1, name=f"Char{i}", class_key="warrior"))
    chars = await storage.load_characters(1)
    assert len(chars) == 3


@pytest.mark.asyncio
async def test_delete_character(storage):
    from core.character import Character
    await storage.save_character(Character(owner_tg_id=1, name="DeleteMe", class_key="warrior"))
    await storage.delete_character(1, "DeleteMe")
    loaded = await storage.load_character_by_name(1, "DeleteMe")
    assert loaded is None


@pytest.mark.asyncio
async def test_can_create_character(storage):
    from core.character import Character
    from config import MAX_CHARACTERS_PER_PLAYER
    for i in range(MAX_CHARACTERS_PER_PLAYER - 1):
        await storage.save_character(Character(owner_tg_id=1, name=f"Ch{i}", class_key="warrior"))
    assert await storage.can_create_character(1) is True
    await storage.save_character(Character(owner_tg_id=1, name="Last", class_key="warrior"))
    assert await storage.can_create_character(1) is False


@pytest.mark.asyncio
async def test_raid_session_crud(storage):
    session = RaidSession(
        raid_id="raid_001", location_key="forest",
        status=RaidStatus.IN_PROGRESS,
        participants=[], encounters=[],
        participant_names={1: "Player1"},
        current_encounter=0,
    )
    data = session_to_dict(session)
    await storage.save_raid_session("raid_001", data)
    loaded_data = await storage.load_raid_session("raid_001")
    assert loaded_data is not None
    assert loaded_data["raid_id"] == "raid_001"
    assert loaded_data["participant_names"]["1"] == "Player1"


@pytest.mark.asyncio
async def test_delete_raid_session(storage):
    await storage.save_raid_session("del_me", {"raid_id": "del_me"})
    await storage.delete_raid_session("del_me")
    assert await storage.load_raid_session("del_me") is None


@pytest.mark.asyncio
async def test_market_listing_crud(storage):
    from core.items import ItemTemplate, ItemEffect, ItemType, Rarity, Item
    tpl = ItemTemplate("Test Ring", ItemType.ACCESSORY, Rarity.COMMON, ItemEffect(hp_bonus=5))
    item = Item(template=tpl, uid="market_001")
    await storage.save_market_listing("list_001", 1, "TestChar", item, 100)
    listings = await storage.load_active_market_listings()
    assert len(listings) >= 1
    listing = [l for l in listings if l["listing_id"] == "list_001"]
    assert len(listing) == 1
    assert listing[0]["price"] == 100
    await storage.deactivate_market_listing("list_001")
    listings = await storage.load_active_market_listings()
    assert len([l for l in listings if l["listing_id"] == "list_001"]) == 0
