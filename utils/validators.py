from __future__ import annotations
from typing import Any

ALLOWED_MODIFIERS: dict[str, tuple[float, float]] = {
    "WEAK_SPOT_FOUND": (1.2, 2.0),
    "DODGE_BONUS": (0.0, 0.3),
    "TAUNT": (0.0, 1.0),
    "STUN": (0.0, 1.0),
    "CRIT_BOOST": (0.0, 1.0),
}

RESERVED_ACTIONS = set(ALLOWED_MODIFIERS.keys())


def validate_nn_response(data: dict) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("NN response must be a dict")
    narrative = data.get("narrative", "")
    if not isinstance(narrative, str) or not narrative.strip():
        raise ValueError("NN response missing narrative")
    actions = data.get("actions", [])
    if not isinstance(actions, list):
        raise ValueError("NN actions must be a list")
    validated = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        modifier = action.get("modifier", "")
        value = action.get("value", 1.0)
        if modifier not in ALLOWED_MODIFIERS:
            continue
        lo, hi = ALLOWED_MODIFIERS[modifier]
        if not (lo <= value <= hi):
            value = max(lo, min(hi, value))
        target = str(action.get("target", "enemy"))
        validated.append({
            "target": target,
            "modifier": modifier,
            "value": value,
        })
    return validated


def validate_character_name(name: str) -> bool:
    if not name or len(name) < 2 or len(name) > 24:
        return False
    return all(c.isalnum() or c in " _- " for c in name)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
