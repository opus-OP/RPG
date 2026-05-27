from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

from core.character import Character
from utils.rng import secure_randint, roll_chance


@dataclass
class EventReward:
    gold: int = 0
    damage: int = 0
    heal: int = 0
    item_template: str = ""
    buff_atk: int = 0
    buff_def: int = 0


@dataclass
class RaidEvent:
    id: str
    text: str
    attribute: Optional[str]  # "strength" / "agility" / "intelligence" / None = pure chance
    dc: int  # difficulty: stat + d20 >= dc; if attribute=None, dc = success chance %
    success: EventReward
    fail: Optional[EventReward] = None


def resolve_event(event: RaidEvent, char: Character) -> tuple[bool, EventReward]:
    success: bool
    if event.attribute:
        stat_val = getattr(char.stats, event.attribute, 5)
        roll = secure_randint(1, 20)
        total = stat_val + roll
        success = total >= event.dc
    else:
        success = roll_chance(event.dc / 100.0)

    reward = event.success if success else event.fail
    if reward is None:
        reward = EventReward()
    return success, reward
