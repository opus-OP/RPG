"""Модель персонажа: характеристики, экипировка, компаньон.

Character — центральная сущность, объединяющая класс, уровень,
экипировку, инвентарь и боевое состояние.
"""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Optional

from core.classes import ClassTemplate, StatBlock, CLASSES
from core.items import Item, ItemEffect, ItemType
from config import (
    LEVEL_CURVE, MAX_LEVEL, HP_PER_LEVEL_MULT, STAT_PER_LEVEL_MULT,
    CRIT_CHANCE_MAX, DODGE_MAX, CRIT_MULTIPLIER_BASE,
)


@dataclass
class Companion:
    """Компаньон класса Лидер — самостоятельная боевая единица.

    Атрибуты обновляются при призыве в зависимости от уровня персонажа.
    """
    name: str = "Призванный страж"
    description: str = ""
    hp: int = 60
    max_hp: int = 60
    attack_min: int = 5
    attack_max: int = 10
    defense: int = 3
    dodge_chance: float = 0.03
    crit_chance: float = 0.03
    crit_multiplier: float = 2.0
    alive: bool = True


@dataclass
class Equipment:
    """Три слота экипировки: оружие, броня, аксессуар."""
    weapon: Optional[Item] = None
    armor: Optional[Item] = None
    accessory: Optional[Item] = None

    def equipped_items(self) -> list[Item]:
        """Все надетые предметы (не None)."""
        return [i for i in (self.weapon, self.armor, self.accessory) if i is not None]

    def total_effect(self) -> ItemEffect:
        """Суммарный эффект всех надетых предметов."""
        total = ItemEffect()
        for item in self.equipped_items():
            total = total + item.effect
        return total


@dataclass
class Character:
    """Игровой персонаж.

    Атрибуты:
        owner_tg_id: Telegram ID владельца.
        name: Уникальное имя персонажа (в рамках владельца).
        class_key: Ключ класса (warrior/rogue/mage/leader).
        level/experience: Уровень и опыт.
        hp/max_hp: Текущее и максимальное здоровье.
        gold: Валюта.
        equipment: Экипированные предметы.
        inventory: Инвентарь (не надетые предметы).
        in_raid: Флаг активности в рейде.
        companion: Компаньон (только для Лидера).
        last_raid_time: Таймстамп окончания последнего рейда.
        count_raid: Счётчик рейдов (для кулдауна каждые 3).
    """
    owner_tg_id: int
    name: str
    class_key: str
    description: str = ""
    level: int = 1
    experience: int = 0
    hp: int = 0
    max_hp: int = 0
    gold: int = 0
    equipment: Equipment = field(default_factory=Equipment)
    inventory: list[Item] = field(default_factory=list)
    in_raid: bool = False
    alive: bool = True
    companion: Optional[Companion] = None
    companion_name: str = "Призванный страж"
    companion_description: str = ""
    last_raid_time: float = 0.0
    count_raid: int = 0

    def __post_init__(self):
        self._buff_atk = 0
        self._buff_def = 0
        if self.hp == 0 and self.max_hp == 0:
            self._recalc_stats()

    def set_buffs(self, atk: int = 0, def_: int = 0):
        self._buff_atk = atk
        self._buff_def = def_

    def clear_buffs(self):
        self._buff_atk = 0
        self._buff_def = 0

    @property
    def template(self) -> ClassTemplate:
        return CLASSES[self.class_key]

    def _recalc_stats(self):
        """Пересчитывает max_hp на основе уровня персонажа."""
        t = self.template
        mult = 1.0 + (self.level - 1) * HP_PER_LEVEL_MULT
        self.max_hp = int(t.base_hp * mult)
        if self.hp <= 0 or self.hp > self.max_hp:
            self.hp = self.max_hp

    @property
    def attack_min(self) -> int:
        t = self.template
        base = t.base_atk_min + t.atk_per_level * (self.level - 1)
        item_bonus = self._item_stat("atk_bonus")
        return base + item_bonus + self._buff_atk

    @property
    def attack_max(self) -> int:
        t = self.template
        base = t.base_atk_max + t.atk_per_level * (self.level - 1)
        item_bonus = self._item_stat("atk_bonus")
        return base + item_bonus + self._buff_atk

    @property
    def crit_chance(self) -> float:
        t = self.template
        base = t.base_crit_chance + t.crit_per_level * (self.level - 1)
        item_bonus = self._item_stat("crit_chance_bonus")
        return min(base + item_bonus, CRIT_CHANCE_MAX)

    @property
    def crit_multiplier(self) -> float:
        item_bonus = self._item_stat("crit_multiplier_bonus")
        return CRIT_MULTIPLIER_BASE + item_bonus

    @property
    def defense(self) -> int:
        return self._item_stat("defense_bonus") + self._buff_def

    @property
    def dodge_chance(self) -> float:
        t = self.template
        base = t.base_dodge + t.dodge_per_level * (self.level - 1)
        item_bonus = self._item_stat("dodge_bonus")
        return min(base + item_bonus, DODGE_MAX)

    @property
    def stats(self) -> StatBlock:
        """Характеристики (Сила/Лов/Инт) с учётом уровня и экипировки."""
        t = self.template
        stat_mult = 1.0 + (self.level - 1) * STAT_PER_LEVEL_MULT
        base = StatBlock(
            strength=int(t.base_stats.strength * stat_mult),
            agility=int(t.base_stats.agility * stat_mult),
            intelligence=int(t.base_stats.intelligence * stat_mult),
        )
        item = self._item_stats()
        return base + item

    def _item_stat(self, attr: str) -> int | float:
        """Суммирует указанный атрибут эффекта со всех надетых предметов."""
        total = 0.0
        for item in self.equipment.equipped_items():
            val = getattr(item.effect, attr, 0.0)
            total += val
        if isinstance(total, float) and attr != "atk_bonus" and attr != "defense_bonus":
            return total
        return int(total) if attr in ("atk_bonus", "defense_bonus", "hp_bonus") else total

    def _item_stats(self) -> StatBlock:
        """Характеристики от экипировки."""
        total = StatBlock()
        for item in self.equipment.equipped_items():
            e = item.effect
            total.strength += e.strength_bonus
            total.agility += e.agility_bonus
            total.intelligence += e.intelligence_bonus
        return total

    @property
    def exp_to_next(self) -> int:
        """Опыт, необходимый для следующего уровня."""
        if self.level <= len(LEVEL_CURVE) - 1:
            return LEVEL_CURVE[self.level]
        excess = self.level - len(LEVEL_CURVE) + 1
        return int(LEVEL_CURVE[-1] * (1 + excess * 0.3))

    def add_experience(self, amount: int) -> bool:
        """Добавляет опыт, повышает уровень при необходимости. True, если уровень вырос."""
        self.experience += amount
        leveled = False
        while self.experience >= self.exp_to_next and self.level < MAX_LEVEL:
            self.experience -= self.exp_to_next
            self.level += 1
            self._recalc_stats()
            leveled = True
        self._recalc_stats()
        return leveled

    def can_equip(self, item: Item) -> bool:
        """Проверяет возможность экипировать предмет (уровень, класс, не сломан)."""
        if item.broken:
            return False
        if item.template.required_level > self.level:
            return False
        if item.template.required_class and item.template.required_class != self.class_key:
            return False
        return True

    def equip(self, item: Item) -> bool:
        """Надевает предмет. Снимает старый в тот же слот."""
        if not self.can_equip(item):
            return False
        if item not in self.inventory:
            return False
        self.inventory.remove(item)
        slot_map = {
            ItemType.WEAPON: "weapon",
            ItemType.ARMOR: "armor",
            ItemType.ACCESSORY: "accessory",
        }
        slot = slot_map.get(item.item_type)
        if slot is None:
            return False
        old = getattr(self.equipment, slot)
        if old is not None and not old.broken:
            self.inventory.append(old)
        setattr(self.equipment, slot, item)
        self._recalc_stats()
        return True

    def unequip(self, item: Item) -> bool:
        """Снимает предмет в инвентарь."""
        for slot in ("weapon", "armor", "accessory"):
            if getattr(self.equipment, slot) is item:
                setattr(self.equipment, slot, None)
                self.inventory.append(item)
                self._recalc_stats()
                return True
        return False

    def take_damage(self, raw_damage: int) -> int:
        """Наносит урон, не опуская HP ниже 0."""
        self.hp = max(0, self.hp - raw_damage)
        return raw_damage

    def heal(self, amount: int) -> int:
        """Восстанавливает HP до max_hp. Возвращает реально восстановленное."""
        before = self.hp
        self.hp = min(self.max_hp, self.hp + amount)
        return self.hp - before

    def revive(self):
        """Воскрешает с 1 HP."""
        self.hp = 1
        self.alive = True

    def can_raid(self) -> bool:
        """Проверяет, можно ли начать рейд (кулдаун каждые 3 рейда)."""
        if self.last_raid_time == 0 or self.count_raid % 3 != 0:
            return True
        from config import RAID_COOLDOWN_HOURS
        elapsed = time.time() - self.last_raid_time
        return elapsed >= RAID_COOLDOWN_HOURS * 3600

    def raid_cooldown_remaining(self) -> float:
        """Оставшееся время кулдауна в секундах."""
        if self.last_raid_time == 0 or self.count_raid % 3 != 0:
            return 0.0
        from config import RAID_COOLDOWN_HOURS
        elapsed = time.time() - self.last_raid_time
        remaining = RAID_COOLDOWN_HOURS * 3600 - elapsed
        return max(0.0, remaining)

    def mark_raid_done(self):
        """Фиксирует завершение рейда (таймстамп для кулдауна)."""
        self.last_raid_time = time.time()

    def summon_companion(self):
        """Создаёт компаньона для класса Лидер с параметрами, зависящими от уровня."""
        if self.class_key != "leader":
            self.companion = None
            return
        level = self.level
        self.companion = Companion(
            name=self.companion_name,
            description=self.companion_description,
            hp=50 + level * 10,
            max_hp=50 + level * 10,
            attack_min=5 + level * 2,
            attack_max=10 + level * 3,
            defense=3 + level,
            dodge_chance=min(0.03 + level * 0.002, 0.3),
            crit_chance=min(0.03 + level * 0.003, 0.3),
            crit_multiplier=2.0,
            alive=True,
        )

    def release_companion(self):
        """Удаляет компаньона."""
        self.companion = None

    def durability_damage_all(self, amount: int = 1, percent: float = 0.0):
        """Наносит износ всей экипировке. Сломанные предметы автоматически снимаются."""
        for item in self.equipment.equipped_items()[:]:
            dmg = max(1, int(item.durability_max * percent)) if percent > 0 else amount
            item.wear(dmg)
            if item.broken:
                slot = None
                for s in ("weapon", "armor", "accessory"):
                    if getattr(self.equipment, s) is item:
                        slot = s
                        break
                if slot:
                    setattr(self.equipment, slot, None)
