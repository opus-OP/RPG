"""Система предметов: шаблоны, редкость, эффекты и экземпляры.

Предмет создаётся из ItemTemplate с учётом множителя редкости.
Item — конкретный экземпляр с UID, износом и состоянием.
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Rarity(Enum):
    """Редкость предмета. Влияет на множитель эффектов."""
    COMMON = "обычный"
    RARE = "редкий"
    EPIC = "эпический"
    LEGENDARY = "легендарный"


class ItemType(Enum):
    """Тип предмета, определяющий слот экипировки."""
    WEAPON = "оружие"
    ARMOR = "броня"
    ACCESSORY = "аксессуар"


@dataclass
class ItemEffect:
    """Суммарный бонус предмета ко всем характеристикам.

    Каждое поле суммируется с базовыми параметрами персонажа.
    Для предметов COMMON значения — базовые; редкость умножает их.
    """
    hp_bonus: int = 0
    atk_bonus: int = 0
    crit_chance_bonus: float = 0.0
    crit_multiplier_bonus: float = 0.0
    defense_bonus: int = 0
    dodge_bonus: float = 0.0
    strength_bonus: int = 0
    agility_bonus: int = 0
    intelligence_bonus: int = 0

    def __add__(self, other: ItemEffect) -> ItemEffect:
        """Суммирует два набора эффектов (для нескольких экипированных предметов)."""
        return ItemEffect(
            hp_bonus=self.hp_bonus + other.hp_bonus,
            atk_bonus=self.atk_bonus + other.atk_bonus,
            crit_chance_bonus=self.crit_chance_bonus + other.crit_chance_bonus,
            crit_multiplier_bonus=self.crit_multiplier_bonus + other.crit_multiplier_bonus,
            defense_bonus=self.defense_bonus + other.defense_bonus,
            dodge_bonus=self.dodge_bonus + other.dodge_bonus,
            strength_bonus=self.strength_bonus + other.strength_bonus,
            agility_bonus=self.agility_bonus + other.agility_bonus,
            intelligence_bonus=self.intelligence_bonus + other.intelligence_bonus,
        )


RARITY_EFFECT_MULTIPLIER = {
    Rarity.COMMON: 1.0,
    Rarity.RARE: 1.5,
    Rarity.EPIC: 2.5,
    Rarity.LEGENDARY: 4.5,
}


@dataclass
class ItemTemplate:
    """Шаблон предмета — неизменяемая часть, общая для всех экземпляров.

    Атрибуты:
        name: Название (может содержать префикс для сгенерированного оружия).
        base_effect: Базовый эффект (до умножения на редкость).
        required_level/class: Ограничения на экипировку.
        durability_max: Максимальная прочность.
    """
    name: str
    item_type: ItemType
    rarity: Rarity
    base_effect: ItemEffect
    required_level: int = 1
    required_class: Optional[str] = None
    durability_max: int = 100

    def final_effect(self) -> ItemEffect:
        """Возвращает эффект с учётом множителя редкости."""
        mult = RARITY_EFFECT_MULTIPLIER[self.rarity]
        return ItemEffect(
            hp_bonus=int(self.base_effect.hp_bonus * mult),
            atk_bonus=int(self.base_effect.atk_bonus * mult),
            crit_chance_bonus=self.base_effect.crit_chance_bonus * mult,
            crit_multiplier_bonus=self.base_effect.crit_multiplier_bonus * mult,
            defense_bonus=int(self.base_effect.defense_bonus * mult),
            dodge_bonus=self.base_effect.dodge_bonus * mult,
            strength_bonus=int(self.base_effect.strength_bonus * mult),
            agility_bonus=int(self.base_effect.agility_bonus * mult),
            intelligence_bonus=int(self.base_effect.intelligence_bonus * mult),
        )


@dataclass
class Item:
    """Конкретный экземпляр предмета.

    Атрибуты:
        template: Ссылка на шаблон.
        uid: Уникальный идентификатор экземпляра.
        durability: Текущая прочность (0 = сломан).
    """
    template: ItemTemplate
    uid: str
    durability: int
    durability_max: int

    @property
    def name(self) -> str:
        return self.template.name

    @property
    def rarity(self) -> Rarity:
        return self.template.rarity

    @property
    def item_type(self) -> ItemType:
        return self.template.item_type

    @property
    def broken(self) -> bool:
        return self.durability <= 0

    @property
    def effect(self) -> ItemEffect:
        return self.template.final_effect()

    def wear(self, amount: int = 1) -> None:
        """Снижает прочность, но не ниже 0."""
        self.durability = max(0, self.durability - amount)
