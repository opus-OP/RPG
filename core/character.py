from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Optional

from core.classes import ClassTemplate, StatBlock, CLASSES
from core.items import Item, ItemEffect, ItemType
from config import LEVEL_CURVE, MAX_LEVEL, HP_PER_LEVEL_MULT, STAT_PER_LEVEL_MULT, CRIT_CHANCE_MAX, DODGE_MAX, CRIT_MULTIPLIER_BASE


@dataclass
class Companion:
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
    weapon: Optional[Item] = None
    armor: Optional[Item] = None
    accessory: Optional[Item] = None

    def equipped_items(self) -> list[Item]:
        return [i for i in (self.weapon, self.armor, self.accessory) if i is not None]

    def total_effect(self) -> ItemEffect:
        total = ItemEffect()
        for item in self.equipped_items():
            total = total + item.effect
        return total


@dataclass
class Character:
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
        if self.hp == 0 and self.max_hp == 0:
            self._recalc_stats()

    @property
    def template(self) -> ClassTemplate:
        return CLASSES[self.class_key]

    def _recalc_stats(self):
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
        return base + item_bonus

    @property
    def attack_max(self) -> int:
        t = self.template
        base = t.base_atk_max + t.atk_per_level * (self.level - 1)
        item_bonus = self._item_stat("atk_bonus")
        return base + item_bonus

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
        return self._item_stat("defense_bonus")

    @property
    def dodge_chance(self) -> float:
        t = self.template
        base = t.base_dodge + t.dodge_per_level * (self.level - 1)
        item_bonus = self._item_stat("dodge_bonus")
        return min(base + item_bonus, DODGE_MAX)

    @property
    def stats(self) -> StatBlock:
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
        total = 0.0
        for item in self.equipment.equipped_items():
            val = getattr(item.effect, attr, 0.0)
            total += val
        if isinstance(total, float) and attr != "atk_bonus" and attr != "defense_bonus":
            return total
        return int(total) if attr in ("atk_bonus", "defense_bonus", "hp_bonus") else total

    def _item_stats(self) -> StatBlock:
        total = StatBlock()
        for item in self.equipment.equipped_items():
            e = item.effect
            total.strength += e.strength_bonus
            total.agility += e.agility_bonus
            total.intelligence += e.intelligence_bonus
        return total

    @property
    def exp_to_next(self) -> int:
        if self.level <= len(LEVEL_CURVE) - 1:
            return LEVEL_CURVE[self.level]
        excess = self.level - len(LEVEL_CURVE) + 1
        return int(LEVEL_CURVE[-1] * (1 + excess * 0.3))

    def add_experience(self, amount: int) -> bool:
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
        if item.broken:
            return False
        if item.template.required_level > self.level:
            return False
        if item.template.required_class and item.template.required_class != self.class_key:
            return False
        return True

    def equip(self, item: Item) -> bool:
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
        for slot in ("weapon", "armor", "accessory"):
            if getattr(self.equipment, slot) is item:
                setattr(self.equipment, slot, None)
                self.inventory.append(item)
                self._recalc_stats()
                return True
        return False

    def take_damage(self, raw_damage: int) -> int:
        self.hp = max(0, self.hp - raw_damage)
        return raw_damage

    def heal(self, amount: int) -> int:
        before = self.hp
        self.hp = min(self.max_hp, self.hp + amount)
        return self.hp - before

    def revive(self):
        self.hp = 1
        self.alive = True

    def can_raid(self) -> bool:
        if self.last_raid_time == 0 or self.count_raid % 3 != 0:
            return True
        from config import RAID_COOLDOWN_HOURS
        elapsed = time.time() - self.last_raid_time
        return elapsed >= RAID_COOLDOWN_HOURS * 3600

    def raid_cooldown_remaining(self) -> float:
        if self.last_raid_time == 0 or self.count_raid % 3 != 0:
            return 0.0
        from config import RAID_COOLDOWN_HOURS
        elapsed = time.time() - self.last_raid_time
        remaining = RAID_COOLDOWN_HOURS * 3600 - elapsed
        return max(0.0, remaining)

    def mark_raid_done(self):
        self.last_raid_time = time.time()

    def summon_companion(self):
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
        self.companion = None

    def durability_damage_all(self, amount: int = 1, percent: float = 0.0):
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
