from __future__ import annotations
import json
import logging
from typing import Any, Optional

import httpx

import config
from utils.validators import validate_nn_response, ALLOWED_MODIFIERS

log = logging.getLogger("rpg.nn")

SYSTEM_PROMPT = (
    "Ты — нарратор RPG-боя. Описывай происходящее ярко, живо, на русском языке.\n\n"
    "Правила:\n"
    "1. Верни ТОЛЬКО JSON-объект, без лишнего текста.\n"
    "2. Поле \"narrative\" (str): описание ВСЕГО хода — и действия игрока, и ответной атаки врага.\n"
    "3. Поле \"actions\" (list): модификаторы для атаки игрока. "
    "Можешь вернуть пустой список, если ничего не применяешь.\n"
    "4. Поле \"enemy_actions\" (list): модификаторы для ответной атаки врага. "
    "Можешь вернуть пустой список, если ничего не применяешь.\n\n"
    "Доступные модификаторы (actions / enemy_actions):\n"
)

MODIFIER_DESCRIPTIONS = {
    "WEAK_SPOT_FOUND": '{"modifier": "WEAK_SPOT_FOUND", "value": 1.5, "target": "player"} — урон х1.5 (value 1.2–2.0)',
    "DODGE_BONUS": '{"modifier": "DODGE_BONUS", "value": 0.15, "target": "player"} — уклонение +15% (value 0.0–0.3)',
    "STUN": '{"modifier": "STUN", "value": 1.0, "target": "enemy"} — враг пропускает ход',
    "CRIT_BOOST": '{"modifier": "CRIT_BOOST", "value": 1.0, "target": "player"} — гарантированный крит',
    "TAUNT": '{"modifier": "TAUNT", "value": 0.5, "target": "player"} — щит (value × 30 ед.)',
}

for line in MODIFIER_DESCRIPTIONS.values():
    SYSTEM_PROMPT += f"   - {line}\n"

SYSTEM_PROMPT += (
    "\nПример ответа:\n"
    '{"narrative": "Вы замечаете брешь в защите врага и наносите точный удар! '
    'Враг в ярости отвечает мощным выпадом.", '
    '"actions": [{"modifier": "WEAK_SPOT_FOUND", "value": 1.5, "target": "player"}], '
    '"enemy_actions": [{"modifier": "TAUNT", "value": 0.5, "target": "enemy"}]}'
)


def _build_messages(
    location: str,
    turn: int,
    player: dict,
    enemies: list[dict],
    action_history: list[str],
    player_action: str = "",
) -> list[dict[str, str]]:
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
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
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
    player_action: str = "",
) -> Optional[dict[str, Any]]:
    if action_history is None:
        action_history = []

    if not config.NN_API_URL:
        log.warning("NN_API_URL not set, using fallback narrative")
        return fallback_response()

    url = config.NN_API_URL.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    headers = {"Authorization": f"Bearer {config.NN_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": config.NN_MODEL,
        "messages": _build_messages(location, turn, player, enemies, action_history, player_action),
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
            data["narrative"] = parsed.get("narrative", "")
            data["actions"] = parsed.get("actions", [])
            data["enemy_actions"] = parsed.get("enemy_actions", [])
            validated_actions = validate_nn_response(data, key="actions")
            validated_enemy = validate_nn_response(data, key="enemy_actions", check_narrative=False)
            return {
                "narrative": data["narrative"],
                "actions": validated_actions,
                "enemy_actions": validated_enemy,
            }
        except httpx.TimeoutException:
            log.warning("NN API timeout (attempt %d/%d)", attempt, config.NN_MAX_RETRIES)
        except httpx.RequestError as e:
            log.warning("NN API error: %s (attempt %d/%d)", e, attempt, config.NN_MAX_RETRIES)

    log.error("NN API failed after %d attempts, using fallback", config.NN_MAX_RETRIES)
    return fallback_response()


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


def fallback_response() -> dict[str, Any]:
    return {
        "narrative": "Бой продолжается. Звук стали и крики разносятся по полю.",
        "actions": [],
        "enemy_actions": [],
    }
