from __future__ import annotations
from dataclasses import dataclass
from typing import Callable


@dataclass
class StatBlock:
    strength: int = 0
    agility: int = 0
    intelligence: int = 0

    def __add__(self, other: StatBlock) -> StatBlock:
        return StatBlock(
            strength=self.strength + other.strength,
            agility=self.agility + other.agility,
            intelligence=self.intelligence + other.intelligence,
        )


@dataclass
class ClassTemplate:
    name: str
    base_hp: int
    base_atk_min: int
    base_atk_max: int
    base_crit_chance: float
    base_dodge: float
    base_stats: StatBlock
    atk_per_level: int
    crit_per_level: float
    dodge_per_level: float
    description: str


WARRIOR = ClassTemplate(
    name="Воин",
    base_hp=120,
    base_atk_min=8,
    base_atk_max=14,
    base_crit_chance=0.05,
    base_dodge=0.02,
    base_stats=StatBlock(strength=5, agility=2, intelligence=1),
    atk_per_level=2,
    crit_per_level=0.005,
    dodge_per_level=0.003,
    description="Мастер ближнего боя. Высокое HP, средний урон, низкий крит.",
)

ROGUE = ClassTemplate(
    name="Плут",
    base_hp=80,
    base_atk_min=6,
    base_atk_max=12,
    base_crit_chance=0.15,
    base_dodge=0.08,
    base_stats=StatBlock(strength=2, agility=5, intelligence=1),
    atk_per_level=3,
    crit_per_level=0.01,
    dodge_per_level=0.005,
    description="Скрытный убийца. Низкое HP, высокий крит и уклонение.",
)

MAGE = ClassTemplate(
    name="Маг",
    base_hp=70,
    base_atk_min=10,
    base_atk_max=18,
    base_crit_chance=0.08,
    base_dodge=0.03,
    base_stats=StatBlock(strength=1, agility=2, intelligence=5),
    atk_per_level=4,
    crit_per_level=0.008,
    dodge_per_level=0.002,
    description="Стеклянная пушка. Низкое HP, высокий урон, магия игнорирует часть защиты.",
)

LEADER = ClassTemplate(
    name="Лидер",
    base_hp=95,
    base_atk_min=7,
    base_atk_max=13,
    base_crit_chance=0.05,
    base_dodge=0.04,
    base_stats=StatBlock(strength=2, agility=2, intelligence=4),
    atk_per_level=2,
    crit_per_level=0.005,
    dodge_per_level=0.004,
    description="Предводитель отряда. Призывает союзника в помощь, усиливает команду.",
)

CLASSES: dict[str, ClassTemplate] = {
    "warrior": WARRIOR,
    "rogue": ROGUE,
    "mage": MAGE,
    "leader": LEADER,
}

CLASS_NAMES_RU = {k: v.name for k, v in CLASSES.items()}
