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
    attribute: Optional[str]
    dc: int
    success: EventReward
    fail: Optional[EventReward] = None


def event_from_dict(data: dict) -> RaidEvent:
    ev_data = dict(data)
    ev_data["success"] = EventReward(**ev_data["success"])
    if ev_data.get("fail"):
        ev_data["fail"] = EventReward(**ev_data["fail"])
    return RaidEvent(**ev_data)


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


def apply_event_reward(
    reward: EventReward,
    char: Character,
    buffs: dict[str, int] | None = None,
) -> list[str]:
    parts = []
    if reward.gold:
        char.gold += reward.gold
        parts.append(f"{reward.gold}💰")
    if reward.heal:
        old_hp = char.hp
        char.hp = min(char.max_hp, char.hp + reward.heal)
        parts.append(f"+{char.hp - old_hp}❤️")
    if reward.damage:
        char.hp = max(0, char.hp - reward.damage)
        parts.append(f"-{reward.damage}❤️")
    if reward.item_template:
        parts.append(f"item:{reward.item_template}")
    if (reward.buff_atk or reward.buff_def) and buffs is not None:
        if reward.buff_atk:
            buffs["atk"] = buffs.get("atk", 0) + reward.buff_atk
        if reward.buff_def:
            buffs["def"] = buffs.get("def", 0) + reward.buff_def
        b = []
        if reward.buff_atk:
            b.append(f"+{reward.buff_atk}⚔️")
        if reward.buff_def:
            b.append(f"+{reward.buff_def}🛡")
        parts.append(f"({' | '.join(b)} до конца рейда)")
    return parts
