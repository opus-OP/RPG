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
class EventOption:
    text: str
    attribute: Optional[str]
    dc: int
    success: EventReward
    fail: Optional[EventReward] = None


@dataclass
class RaidEvent:
    id: str
    text: str
    options: list[EventOption]


def event_from_dict(data: dict) -> RaidEvent:
    ev_data = dict(data)
    ev_data["options"] = [
        EventOption(
            text=o["text"],
            attribute=o.get("attribute"),
            dc=o["dc"],
            success=EventReward(**o["success"]),
            fail=EventReward(**o["fail"]) if o.get("fail") else None,
        )
        for o in ev_data["options"]
    ]
    return RaidEvent(**ev_data)


def resolve_event_option(option: EventOption, char: Character) -> tuple[bool, EventReward]:
    if option.attribute:
        stat_val = getattr(char.stats, option.attribute, 5)
        roll = secure_randint(1, 20)
        total = stat_val + roll
        success = total >= option.dc
    else:
        success = roll_chance(option.dc / 100.0)

    reward = option.success if success else option.fail
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
