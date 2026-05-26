import json
import os
import aiosqlite
from pathlib import Path
from typing import Optional

from config import DATABASE_URL, DATA_DIR, MAX_CHARACTERS_PER_PLAYER
from core.character import Character
from core.items import ItemTemplate, ItemEffect, Rarity, ItemType, Item
from data.models import character_to_dict, character_from_dict, item_to_dict, item_from_dict


_ITEM_TEMPLATES: dict[str, ItemTemplate] = {}


def register_templates():
    from core.items import ItemTemplate, ItemEffect, Rarity, ItemType
    tpls = [
        ItemTemplate("Деревянный меч", ItemType.WEAPON, Rarity.COMMON, ItemEffect(atk_bonus=2)),
        ItemTemplate("Кожаная куртка", ItemType.ARMOR, Rarity.COMMON, ItemEffect(defense_bonus=3)),
        ItemTemplate("Кольцо выносливости", ItemType.ACCESSORY, Rarity.RARE, ItemEffect(hp_bonus=10)),
        ItemTemplate("Волчий клык", ItemType.WEAPON, Rarity.RARE, ItemEffect(atk_bonus=5, crit_chance_bonus=0.02)),
        ItemTemplate("Стальной топор", ItemType.WEAPON, Rarity.COMMON, ItemEffect(atk_bonus=5)),
        ItemTemplate("Каменная броня", ItemType.ARMOR, Rarity.RARE, ItemEffect(defense_bonus=8, hp_bonus=15)),
        ItemTemplate("Амулет тролля", ItemType.ACCESSORY, Rarity.EPIC, ItemEffect(hp_bonus=30, defense_bonus=5)),
        ItemTemplate("Проклятый клинок", ItemType.WEAPON, Rarity.RARE, ItemEffect(atk_bonus=10, crit_chance_bonus=0.05)),
        ItemTemplate("Плащ теней", ItemType.ARMOR, Rarity.EPIC, ItemEffect(defense_bonus=12, dodge_bonus=0.05)),
        ItemTemplate("Корона смерти", ItemType.ACCESSORY, Rarity.LEGENDARY, ItemEffect(atk_bonus=15, crit_chance_bonus=0.08, hp_bonus=50)),
    ]
    for t in tpls:
        _ITEM_TEMPLATES[t.name] = t


def get_template(name: str) -> Optional[ItemTemplate]:
    return _ITEM_TEMPLATES.get(name)


class Storage:
    def __init__(self, db_path: str = ""):
        self.db_path = db_path or str(DATA_DIR / "rpg.db")
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._init_db()
        register_templates()

    async def _init_db(self):
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS characters (
                owner_tg_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (owner_tg_id, name)
            );
            CREATE TABLE IF NOT EXISTS market_listings (
                listing_id TEXT PRIMARY KEY,
                seller_id INTEGER NOT NULL,
                character_name TEXT NOT NULL,
                item_data TEXT NOT NULL,
                price INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS raids (
                raid_id TEXT PRIMARY KEY,
                data TEXT NOT NULL
            );
        """)
        await self._conn.commit()

    async def save_character(self, char: Character):
        data = json.dumps(character_to_dict(char), ensure_ascii=False)
        await self._conn.execute(
            "INSERT OR REPLACE INTO characters (owner_tg_id, name, data) VALUES (?, ?, ?)",
            (char.owner_tg_id, char.name, data),
        )
        await self._conn.commit()

    async def load_characters(self, owner_tg_id: int) -> list[Character]:
        cursor = await self._conn.execute(
            "SELECT data FROM characters WHERE owner_tg_id = ?", (owner_tg_id,)
        )
        rows = await cursor.fetchall()
        chars = []
        for row in rows:
            data = json.loads(row["data"])
            char = character_from_dict(data, _ITEM_TEMPLATES)
            chars.append(char)
        return chars

    async def load_character_by_name(self, owner_tg_id: int, name: str) -> Optional[Character]:
        cursor = await self._conn.execute(
            "SELECT data FROM characters WHERE owner_tg_id = ? AND name = ?",
            (owner_tg_id, name),
        )
        row = await cursor.fetchone()
        if row:
            data = json.loads(row["data"])
            return character_from_dict(data, _ITEM_TEMPLATES)
        return None

    async def delete_character(self, owner_tg_id: int, name: str):
        await self._conn.execute(
            "DELETE FROM characters WHERE owner_tg_id = ? AND name = ?",
            (owner_tg_id, name),
        )
        await self._conn.commit()

    async def save_market_listing(
        self, listing_id: str, seller_id: int, character_name: str,
        item: Item, price: int, active: bool = True,
    ):
        item_data = json.dumps(item_to_dict(item), ensure_ascii=False)
        await self._conn.execute(
            "INSERT OR REPLACE INTO market_listings "
            "(listing_id, seller_id, character_name, item_data, price, active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (listing_id, seller_id, character_name, item_data, price, int(active)),
        )
        await self._conn.commit()

    async def save_raid_session(self, raid_id: str, data: dict):
        await self._conn.execute(
            "INSERT OR REPLACE INTO raids (raid_id, data) VALUES (?, ?)",
            (raid_id, json.dumps(data, ensure_ascii=False)),
        )
        await self._conn.commit()

    async def load_raid_session(self, raid_id: str) -> Optional[dict]:
        cursor = await self._conn.execute(
            "SELECT data FROM raids WHERE raid_id = ?", (raid_id,)
        )
        row = await cursor.fetchone()
        if row:
            return json.loads(row["data"])
        return None

    async def delete_raid_session(self, raid_id: str):
        await self._conn.execute("DELETE FROM raids WHERE raid_id = ?", (raid_id,))
        await self._conn.commit()

    async def find_raid_by_participant(self, user_id: int, status: str = "") -> Optional[dict]:
        cursor = await self._conn.execute("SELECT raid_id, data FROM raids")
        rows = await cursor.fetchall()
        for row in rows:
            data = json.loads(row["data"])
            if data.get("status") == "pending" or data.get("status") == status or not status:
                for p in data.get("participants", []):
                    if p.get("owner_tg_id") == user_id:
                        return data
        return None

    async def deactivate_market_listing(self, listing_id: str):
        await self._conn.execute(
            "UPDATE market_listings SET active = 0 WHERE listing_id = ?",
            (listing_id,),
        )
        await self._conn.commit()

    async def load_active_market_listings(self) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM market_listings WHERE active = 1"
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["item"] = item_from_dict(json.loads(data["item_data"]), _ITEM_TEMPLATES)
            result.append(data)
        return result

    async def credit_gold(self, owner_tg_id: int, character_name: str, amount: int):
        char = await self.load_character_by_name(owner_tg_id, character_name)
        if char:
            char.gold += amount
            await self.save_character(char)

    async def can_create_character(self, owner_tg_id: int) -> bool:
        count = await self.character_count(owner_tg_id)
        return count < MAX_CHARACTERS_PER_PLAYER

    async def character_count(self, owner_tg_id: int) -> int:
        cursor = await self._conn.execute(
            "SELECT COUNT(*) as cnt FROM characters WHERE owner_tg_id = ?",
            (owner_tg_id,),
        )
        row = await cursor.fetchone()
        return row["cnt"] if row else 0

    async def close(self):
        if self._conn:
            await self._conn.close()


storage = Storage()
