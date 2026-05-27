"""Система предметов: шаблоны, редкость, эффекты и экземпляры.

Предмет создаётся из ItemTemplate с учётом множителя редкости.
Item — конкретный экземпляр с UID, износом и состоянием.
"""

from __future__ import annotations
from dataclasses import dataclass, field
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
    crit_chance_bonus: float = 0.0
    crit_multiplier_bonus: float = 0.0
    defense_bonus: int = 0
    dodge_bonus: float = 0.0
    strength_bonus: int = 0
    agility_bonus: int = 0
    intelligence_bonus: int = 0

    def __add__(self, other: ItemEffect) -> ItemEffect:
        total = ItemEffect()
        for field in self.__dataclass_fields__:
            setattr(total, field, getattr(self, field) + getattr(other, field))
        return total


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
        mult = RARITY_EFFECT_MULTIPLIER[self.rarity]
        result = ItemEffect()
        for field in ItemEffect.__dataclass_fields__:
            base_val = getattr(self.base_effect, field, 0)
            if isinstance(base_val, int):
                setattr(result, field, int(base_val * mult))
            else:
                setattr(result, field, base_val * mult)
        return result


@dataclass
class Item:
    template: ItemTemplate
    uid: str
    durability: int
    durability_max: int
    attributes: list[str] = field(default_factory=list)

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
        self.durability = max(0, self.durability - amount)
