"""Артефакты ивент-кампаний: сильный плюс и ощутимый минус у каждого.

Артефакт хранится в Character.artifacts как ключ. Модификаторы суммируются.
Единицы: доли (0.2 = 20%), уклонение и меткость — в долях шанса (0.05 = 5 п.п.),
инициатива — в очках броска.

Упрощения относительно исходного ТЗ:
  * «ближний бой» = атаки по Силе, «дальний бой» = атаки по Ловкости;
  * «лечащие навыки» = любое получаемое лечение (отдельных навыков лечения в игре нет);
  * «болезнь» приравнена к яду.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class Artifact:
    key: str
    name: str
    icon: str
    plus: str            # текст для игрока
    minus: str
    melee_dmg: float = 0.0
    ranged_dmg: float = 0.0
    bleed_res: float = 0.0
    poison_res: float = 0.0
    madness_res: float = 0.0
    heal_in: float = 0.0
    dmg_taken: float = 0.0
    max_hp_pct: float = 0.0
    dodge: float = 0.0
    initiative: int = 0
    accuracy: float = 0.0
    debuff_power: float = 0.0
    surprise_chance: float = 0.0
    scout_chance: float = 0.0
    priority_target: bool = False


ARTIFACTS: dict[str, Artifact] = {a.key: a for a in [
    Artifact("swiss_knife", "Швейцарский нож", "🔪",
             "+20% урон в ближнем бою (атаки Силой)", "−20% сопротивление кровотечению",
             melee_dmg=0.20, bleed_res=-0.20),
    Artifact("plague_pill", "Пилюля чумного доктора", "💊",
             "+30% к получаемому лечению", "−25% макс. HP",
             heal_in=0.30, max_hp_pct=-0.25),
    Artifact("vampire_tooth", "Зуб вампира", "🦷",
             "+50% сопротивление кровотечению", "−25% к получаемому лечению",
             bleed_res=0.50, heal_in=-0.25),
    Artifact("ghost_lamp", "Призрачная лампа", "🏮",
             "40% шанс застать врагов врасплох (первыми ходят герои)", "−30% сопротивление безумию",
             surprise_chance=0.40, madness_res=-0.30),
    Artifact("prophet_eye", "Глаз пророка", "👁",
             "30% шанс разведки: виден тип следующей комнаты, ловушки и засады", "−5% уклонение",
             scout_chance=0.30, dodge=-0.05),
    Artifact("grandpa_monocle", "Монокль деда", "🧐",
             "+5% меткость, +40% эффективность дебаффов", "—",
             accuracy=0.05, debuff_power=0.40),
    Artifact("paper_pipe", "Трубка-бумажкомёт", "🎺",
             "+20% урон в дальнем бою (атаки Ловкостью)", "−4% уклонение",
             ranged_dmg=0.20, dodge=-0.04),
    Artifact("sinner_armor", "Броня из кожи грешника", "🦺",
             "−20% получаемый урон", "−40% получаемое лечение",
             dmg_taken=-0.20, heal_in=-0.40),
    Artifact("mark_amulet", "Амулет-метка", "📿",
             "+30% макс. HP", "носитель — приоритетная цель для врагов",
             max_hp_pct=0.30, priority_target=True),
    Artifact("star_ointment", "Мазь «звёздочка»", "🧴",
             "+50% сопротивление яду и болезни", "−4 к инициативе",
             poison_res=0.50, initiative=-4),
]}

MAX_ARTIFACTS = 2
