"""Рыночная экономика: P2P-торговля предметами с комиссией.

Market — глобальный синглтон, хранящий активные объявления.
Комиссия системы (MARKET_COMMISSION) сжигается для борьбы с инфляцией.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from core.items import Item
from config import MARKET_COMMISSION

if TYPE_CHECKING:
    from data.storage import Storage


@dataclass
class MarketListing:
    """Объявление о продаже предмета.

    Атрибуты:
        listing_id: Уникальный ID объявления.
        seller_id: Telegram ID продавца.
        character_name: Имя персонажа-продавца.
        item: Продаваемый предмет.
        price: Цена в золоте.
        active: True, пока объявление активно (не куплено/отменено).
    """
    listing_id: str
    seller_id: int
    character_name: str
    item: Item
    price: int
    active: bool = True


@dataclass
class Market:
    """Глобальный рынок. Синглтон (экземпляр MARKET).

    listings: Словарь {listing_id: MarketListing}.
    _next_id: Счётчик для генерации ID.
    """
    listings: dict[str, MarketListing] = field(default_factory=dict)
    _next_id: int = 0

    def create_listing(
        self,
        seller_id: int,
        character_name: str,
        item: Item,
        price: int,
    ) -> Optional[MarketListing]:
        """Создаёт новое объявление."""
        if price <= 0:
            return None
        listing = MarketListing(
            listing_id=str(self._next_id),
            seller_id=seller_id,
            character_name=character_name,
            item=item,
            price=price,
        )
        self._next_id += 1
        self.listings[listing.listing_id] = listing
        return listing

    def buy_listing(
        self,
        listing_id: str,
        buyer,
    ) -> tuple[bool, Optional[Item], int, str, int]:
        """Покупает предмет. Комиссия вычитается из суммы продавца.

        Returns:
            (success, item, seller_id, seller_name, seller_earns)
        """
        listing = self.listings.get(listing_id)
        if not listing or not listing.active:
            return False, None, 0, "", 0
        if listing.seller_id == buyer.owner_tg_id:
            return False, None, 0, "", 0
        if buyer.gold < listing.price:
            return False, None, 0, "", 0
        commission = int(listing.price * MARKET_COMMISSION)
        seller_earns = listing.price - commission
        buyer.gold -= listing.price
        listing.active = False
        return True, listing.item, listing.seller_id, listing.character_name, seller_earns

    def cancel_listing(self, listing_id: str, owner_id: int) -> bool:
        """Отменяет объявление (только владелец)."""
        listing = self.listings.get(listing_id)
        if not listing or listing.seller_id != owner_id:
            return False
        listing.active = False
        return True

    def get_active_listings(self) -> list[MarketListing]:
        """Все активные объявления."""
        return [l for l in self.listings.values() if l.active]

    def get_player_listings(self, player_id: int) -> list[MarketListing]:
        """Активные объявления конкретного игрока."""
        return [l for l in self.listings.values() if l.seller_id == player_id and l.active]

    async def load_from_storage(self, storage: Storage):
        """Загружает активные объявления из БД при старте."""
        rows = await storage.load_active_market_listings()
        max_id = 0
        for row in rows:
            item = row["item"]
            if item is None:
                continue
            listing = MarketListing(
                listing_id=row["listing_id"],
                seller_id=row["seller_id"],
                character_name=row["character_name"],
                item=item,
                price=row["price"],
                active=bool(row["active"]),
            )
            self.listings[listing.listing_id] = listing
            lid = int(row["listing_id"])
            if lid >= max_id:
                max_id = lid + 1
        self._next_id = max_id

    async def persist_listing(self, storage: Storage, listing: MarketListing):
        """Сохраняет объявление в БД."""
        await storage.save_market_listing(
            listing_id=listing.listing_id,
            seller_id=listing.seller_id,
            character_name=listing.character_name,
            item=listing.item,
            price=listing.price,
            active=listing.active,
        )

    async def persist_deactivate(self, storage: Storage, listing_id: str):
        """Помечает объявление как неактивное в БД."""
        await storage.deactivate_market_listing(listing_id)


MARKET = Market()
