"""Валидаторы для ответов нейросети и пользовательского ввода.

Белый список модификаторов — единственный способ для NN повлиять на бой.
"""

from typing import Any

ALLOWED_MODIFIERS: dict[str, tuple[float, float]] = {
    "WEAK_SPOT_FOUND": (1.2, 2.0),
    "DODGE_BONUS": (0.0, 0.3),
    "TAUNT": (0.0, 1.0),
    "STUN": (0.0, 1.0),
    "CRIT_BOOST": (0.0, 1.0),
}

RESERVED_ACTIONS = set(ALLOWED_MODIFIERS.keys())


def validate_nn_response(
    data: dict, key: str = "actions", check_narrative: bool = True
) -> list[dict[str, Any]]:
    """Валидирует JSON-ответ нейросети.

    Args:
        data: Сырой распарсенный JSON от NN.
        key: Ключ модификаторов ("actions" или "enemy_actions").
        check_narrative: Проверять ли наличие поля narrative.

    Returns:
        Список отфильтрованных модификаторов, прошедших белый список.

    Raises:
        ValueError: Если структура ответа невалидна.
    """
    if not isinstance(data, dict):
        raise ValueError("NN response must be a dict")
    if check_narrative:
        narrative = data.get("narrative", "")
        if not isinstance(narrative, str) or not narrative.strip():
            raise ValueError("NN response missing narrative")
    actions = data.get(key, [])
    if not isinstance(actions, list):
        raise ValueError(f"NN {key} must be a list")
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
    """Проверяет, что имя персонажа 2-24 символа, только буквы/цифры/пробел/_-."""
    if not name or len(name) < 2 or len(name) > 24:
        return False
    return all(c.isalnum() or c in " _- " for c in name)


def clamp(value: float, lo: float, hi: float) -> float:
    """Ограничивает число диапазоном [lo, hi]."""
    return max(lo, min(hi, value))
