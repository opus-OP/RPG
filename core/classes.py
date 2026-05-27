from __future__ import annotations
from dataclasses import dataclass


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
    base_stats: StatBlock
    description: str


WARRIOR = ClassTemplate(
    name="Воин",
    base_stats=StatBlock(strength=5, agility=2, intelligence=1),
    description="Мастер ближнего боя. Высокое HP, средний урон, низкий крит.",
)

ROGUE = ClassTemplate(
    name="Плут",
    base_stats=StatBlock(strength=2, agility=5, intelligence=1),
    description="Скрытный убийца. Низкое HP, высокий крит и уклонение.",
)

MAGE = ClassTemplate(
    name="Маг",
    base_stats=StatBlock(strength=1, agility=2, intelligence=5),
    description="Стеклянная пушка. Низкое HP, высокий урон, магия игнорирует часть защиты.",
)

LEADER = ClassTemplate(
    name="Лидер",
    base_stats=StatBlock(strength=2, agility=2, intelligence=4),
    description="Предводитель отряда. Призывает союзника в помощь, усиливает команду.",
)

CLASSES: dict[str, ClassTemplate] = {
    "warrior": WARRIOR,
    "rogue": ROGUE,
    "mage": MAGE,
    "leader": LEADER,
}

CLASS_NAMES_RU = {k: v.name for k, v in CLASSES.items()}
