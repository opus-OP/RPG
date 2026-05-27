from __future__ import annotations
import json
import logging
from typing import Any, Optional

import httpx

import config
from utils.validators import validate_nn_response

log = logging.getLogger("rpg.nn")

MODIFIER_LIST = (
    "- WEAK_SPOT_FOUND: множитель урона (0.3–2.0). >1 = усиление атаки (нашёл брешь), <1 = ослабление (споткнулся, скользко, неудачный удар).\n"
    "- DODGE_BONUS: бонус к уклонению (0.0–0.3). Игрок уворачивается от ответной атаки врага.\n"
    "- STUN: враг пропускает ход (0 или 1). Оглушение, замешательство врага.\n"
    "- CRIT_BOOST: гарантированный критический удар (0 или 1). Идеальный момент для мощной атаки.\n"
    "- TAUNT: щит (value × 30 ед. поглощения). Игрок ставит блок, провоцирует врага на себя.\n"
)

MODIFIER_TARGET_HINT = (
    '\nКаждый модификатор обязательно содержит "target": "player" (эффект на игрока) или "target": "enemy" (эффект на врага).\n'
)

MODIFIER_EXAMPLE = (
    '\nПример для player_modifiers:\n'
    '{"actions": [{"modifier": "WEAK_SPOT_FOUND", "value": 1.5, "target": "player"}]}'
)

ENEMY_MODIFIER_EXAMPLE = (
    '\nПример для enemy_modifiers:\n'
    '{"enemy_actions": [{"modifier": "WEAK_SPOT_FOUND", "value": 0.7, "target": "enemy"}]}'
)

SYSTEM_PROMPTS: dict[str, str] = {
    "player_modifiers": (
        "Ты — нарратор RPG-боя.\n\n"
        "Верни ТОЛЬКО JSON с полем \"actions\" — список модификаторов для атаки игрока.\n"
        "Пустой список, если модификаторы не нужны.\n\n"
        "Доступные модификаторы:\n" + MODIFIER_LIST + MODIFIER_TARGET_HINT
        + 'WEAK_SPOT_FOUND, CRIT_BOOST, DODGE_BONUS, TAUNT — обычно target: "player" (накладываются на игрока).\n'
        + 'STUN — всегда target: "enemy".\n'
        + MODIFIER_EXAMPLE
    ),
    "player_narrative": (
        "Ты — нарратор RPG-боя. Опиши действие игрока ярко и живо.\n\n"
        "Верни ТОЛЬКО JSON с полем \"player_narrative\" — описание атаки игрока.\n"
        "Учитывай нанесённый урон: если урон 0 — опиши промах/блок.\n"
    ),
    "enemy_modifiers": (
        "Ты — нарратор RPG-боя.\n\n"
        "Верни ТОЛЬКО JSON с полем \"enemy_actions\" — список модификаторов для атаки врага.\n"
        "Пустой список, если модификаторы не нужны.\n\n"
        "Доступные модификаторы:\n" + MODIFIER_LIST + MODIFIER_TARGET_HINT
        + 'WEAK_SPOT_FOUND — target: "enemy" (враг наносит повышенный/пониженный урон игроку).\n'
        + 'CRIT_BOOST — target: "enemy" (враг критует).\n'
        + 'DODGE_BONUS — target: "enemy" (враг уворачивается от атаки игрока).\n'
        + 'TAUNT — target: "enemy" (щит врага).\n'
        + 'STUN — target: "player" (игрок пропускает ход).\n'
        + ENEMY_MODIFIER_EXAMPLE
    ),
    "enemy_narrative": (
        "Ты — нарратор RPG-боя. Опиши ответное действие врага ярко и живо.\n\n"
        "Верни ТОЛЬКО JSON с полем \"enemy_narrative\" — описание атаки врага.\n"
        "Учитывай нанесённый урон: если урон 0 — опиши промах/блок.\n"
    ),
}

MODE_KEY_MAP: dict[str, str] = {
    "player_modifiers": "actions",
    "player_narrative": "player_narrative",
    "enemy_modifiers": "enemy_actions",
    "enemy_narrative": "enemy_narrative",
}


def _build_context(
    location: str,
    turn: int,
    player: dict,
    enemies: list[dict],
    action_history: list[str],
    player_action: str = "",
    damage: Optional[int] = None,
) -> str:
    ctx = (
        f"Локация: {location}\n"
        f"Ход: {turn}\n\n"
        f"Игрок: {player.get('name', '?')} ({player.get('class', '?')}) "
        f"— HP {player.get('hp', '?')}/{player.get('max_hp', '?')}\n"
        f"Враги:\n"
    )
    for i, e in enumerate(enemies, 1):
        ctx += f"  {i}. {e.get('name', '?')} — HP {e.get('hp', '?')}/{e.get('max_hp', '?')}\n"
    ctx += f"\nИстория действий: {', '.join(action_history) or 'начало боя'}\n"
    if player_action:
        ctx += f"\nДействие игрока: {player_action}\n"
    if damage is not None:
        ctx += f"Нанесённый урон: {damage}\n"
    return ctx


def _build_messages(
    mode: str,
    location: str,
    turn: int,
    player: dict,
    enemies: list[dict],
    action_history: list[str],
    player_action: str = "",
    damage: Optional[int] = None,
) -> list[dict[str, str]]:
    sys_prompt = SYSTEM_PROMPTS.get(mode)
    if not sys_prompt:
        sys_prompt = SYSTEM_PROMPTS["player_modifiers"]
    ctx = _build_context(location, turn, player, enemies, action_history, player_action, damage)
    return [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": ctx},
    ]


def _parse_json_response(text: str) -> Optional[dict]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


async def call_narrative_api(
    location: str,
    turn: int,
    player: dict,
    enemies: list[dict],
    action_history: Optional[list[str]] = None,
    *,
    player_action: str = "",
    damage: Optional[int] = None,
    mode: str = "player_modifiers",
) -> Optional[dict[str, Any]]:
    if action_history is None:
        action_history = []

    if not config.NN_API_URL:
        log.warning("NN_API_URL not set")
        return None

    url = config.NN_API_URL.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    key = MODE_KEY_MAP.get(mode, "actions")
    headers = {"Authorization": f"Bearer {config.NN_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": config.NN_MODEL,
        "messages": _build_messages(mode, location, turn, player, enemies, action_history, player_action, damage),
        "temperature": 0.8,
        "max_tokens": 512,
    }

    for attempt in range(1, config.NN_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=config.NN_TIMEOUT) as client:
                resp = await client.post(url, json=body, headers=headers)
            if resp.status_code != 200:
                detail = _extract_error(resp)
                log.warning("NN API %d (attempt %d/%d): %s", resp.status_code, attempt, config.NN_MAX_RETRIES, detail)
                continue
            data = resp.json()
            content = _extract_content(data)
            if content is None:
                log.warning("NN response missing content (attempt %d/%d)", attempt, config.NN_MAX_RETRIES)
                continue
            parsed = _parse_json_response(content)
            if parsed is None:
                log.warning("NN response not JSON (attempt %d/%d): %.200s", attempt, config.NN_MAX_RETRIES, content)
                continue

            if key in ("actions", "enemy_actions"):
                raw_list = parsed.get(key, [])
                validated = validate_nn_response({"actions": raw_list}, key=key, check_narrative=False)
                return {key: validated}
            else:
                text_val = parsed.get(key, "")
                return {key: text_val}

        except httpx.TimeoutException:
            log.warning("NN API timeout (attempt %d/%d)", attempt, config.NN_MAX_RETRIES)
        except httpx.RequestError as e:
            log.warning("NN API error: %s (attempt %d/%d)", e, attempt, config.NN_MAX_RETRIES)

    log.error("NN API failed after %d attempts", config.NN_MAX_RETRIES)
    return None


def _extract_error(resp) -> str:
    try:
        return str(resp.json())
    except Exception:
        return resp.text[:200]


def _extract_content(data: dict) -> Optional[str]:
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
