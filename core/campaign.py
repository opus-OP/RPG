"""Ивент-кампании: линейная цепочка комнат с боями, сундуками, находками и боссом.

Чистая логика без Telegram. Состояние кампании хранится в RaidSession.campaign (JSON):
    loc, rooms, room_idx, in_fight, pending, candies{uid: n}, heroes{uid: имя},
    scouted, party_size, finalized.

Герои кампании — временные персонажи в таблице characters с owner_tg_id = -uid.
Они не пересекаются с обычными персонажами игрока и удаляются в конце кампании.
"""

from __future__ import annotations
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import config
from core.artifacts import ARTIFACTS
from core.character import Character
from core.raid import RaidEncounter, generate_enemy_hp
from utils.rng import secure_randint, roll_chance

CAMPAIGN_JSON = Path(__file__).resolve().parent.parent / "data" / "campaign.json"

# Приоритет автораспределения очков (по кругу). Сила даёт и HP, и урон, и защиту, поэтому
# она в приоритете у всех классов; ловкость/интеллект — «лицо» класса.
_ALLOC_PATTERN = {
    "warrior": ["strength", "strength", "agility"],
    "rogue": ["strength", "strength", "agility"],
    "mage": ["strength", "strength", "intelligence"],
    "leader": ["strength", "strength", "intelligence"],
}

# Стартовый «походный набор»: добавочные очки характеристик. Подобраны симуляцией так,
# чтобы без снаряжения все классы проходили кампанию примерно с одинаковым шансом
# (у плута и мага при тех же очках слишком мало HP и урона).
CLASS_KIT_POINTS = {"warrior": 0, "rogue": 3, "mage": 5, "leader": 0}

CHEST_KINDS = ("treasure", "trap", "empty")


@dataclass
class CampaignLocation:
    key: str
    name: str
    icon: str
    difficulty: str
    tier: int
    price: int                 # награда за босса, 🍬
    description: str
    intro: str
    mob_ids: list[str]
    boss_id: str
    start_level: int
    hp_mult: float
    atk_mult: float
    chest_candy: tuple[int, int]
    fights: tuple[int, int]
    heal_after_fight: float
    exp_frac: float
    finds: list[dict]


_mobs: dict[str, dict] = {}
_locs: dict[str, CampaignLocation] = {}


def _load():
    if _locs:
        return
    with open(CAMPAIGN_JSON, encoding="utf-8") as f:
        data = json.load(f)
    _mobs.update(data["mobs"])
    for key, d in data["locations"].items():
        _locs[key] = CampaignLocation(
            key=key, name=d["name"], icon=d.get("icon", "🗺"), difficulty=d["difficulty"],
            tier=d["tier"], price=d["price"], description=d["description"],
            intro=d.get("intro", ""), mob_ids=d["mobs"], boss_id=d["boss"],
            start_level=d.get("start_level", 1), hp_mult=d.get("hp_mult", 1.0),
            atk_mult=d.get("atk_mult", 1.0), chest_candy=tuple(d["chest_candy"]),
            fights=tuple(d.get("fights", (5, 7))), heal_after_fight=d.get("heal_after_fight", 0.3),
            exp_frac=d.get("exp_frac", 0.5), finds=d.get("finds", []),
        )


def get_campaign_locations() -> dict[str, CampaignLocation]:
    _load()
    return dict(_locs)


def get_campaign_location(key: str) -> Optional[CampaignLocation]:
    _load()
    return _locs.get(key)


def get_campaign_mob(mob_id: str) -> Optional[dict]:
    _load()
    return _mobs.get(mob_id)


# ─── Герои ──────────────────────────────────────────────────

def auto_allocate(char: Character) -> None:
    """Раздаёт свободные очки характеристик по приоритету класса."""
    pattern = _ALLOC_PATTERN.get(char.class_key, ["strength", "agility", "intelligence"])
    i = 0
    while char.stat_points > 0:
        char.allocate_stat(pattern[i % len(pattern)])
        i += 1


def hero_owner(uid: int) -> int:
    """Владелец временного героя в таблице characters."""
    return -abs(uid)


def create_hero(uid: int, name: str, class_key: str, loc: CampaignLocation,
                description: str = "") -> Character:
    """Новый герой кампании на стартовом уровне локации, очки уже распределены."""
    hero = Character(owner_tg_id=hero_owner(uid), name=name, class_key=class_key,
                     description=description)
    extra = loc.start_level - 1
    if extra > 0:
        hero.level = loc.start_level
        hero.stat_points += 3 * extra
    hero.stat_points += CLASS_KIT_POINTS.get(class_key, 0)
    auto_allocate(hero)
    hero.max_hp = hero._calc_max_hp()
    hero.hp = hero.max_hp
    hero.in_raid = True
    hero.alive = True
    return hero


def award_fight_exp(char: Character, loc: CampaignLocation) -> bool:
    """Опыт за победу в бою: доля от нужного до следующего уровня (темп один на всех).

    Возвращает True, если герой поднял уровень. Очки сразу распределяются.
    """
    hp_before = char.hp
    leveled = char.add_experience(max(1, int(char.exp_to_next * loc.exp_frac)))
    if leveled:
        auto_allocate(char)
        char.hp = char.max_hp  # повышение уровня лечит (как в обычной игре)
    else:
        char.hp = min(hp_before, char.max_hp)
    return leveled


def heal_fraction(char: Character, fraction: float) -> int:
    """Лечение в долях от макс. HP (с учётом артефактов)."""
    if not char.alive or char.hp <= 0:
        return 0
    return char.heal(max(1, int(char.max_hp * fraction)))


def nonlethal_damage(char: Character, fraction: float) -> int:
    """Урон от ловушек и порчи: никогда не убивает (оставляет минимум 1 HP)."""
    dmg = max(1, int(char.max_hp * fraction))
    dmg = min(dmg, max(0, char.hp - 1))
    char.hp -= dmg
    return dmg


# ─── Враги ──────────────────────────────────────────────────

def build_enemy_template(mob_id: str, loc: CampaignLocation, party_size: int = 1) -> tuple[dict, int]:
    """Шаблон врага для RaidEncounter и его HP (с учётом множителей локации и размера отряда)."""
    base = get_campaign_mob(mob_id)
    if base is None:
        raise KeyError(f"Неизвестный моб кампании: {mob_id}")
    hp0 = max(10, int(base["hp"] * loc.hp_mult))
    template = {
        "name": base["name"],
        "mob_id": mob_id,
        "description": base.get("description", ""),
        "hp": hp0,
        "atk_min": max(1, int(base["atk_min"] * loc.atk_mult)),
        "atk_max": max(1, int(base["atk_max"] * loc.atk_mult)),
        "defense": base["defense"],
        "dodge_chance": base["dodge_chance"],
        "crit_chance": base["crit_chance"],
        "crit_multiplier": base["crit_multiplier"],
        "atk_damage_type": base["atk_damage_type"],
        "madness_chance": base.get("madness_chance", 0.0),
        "attack_secondary": None,
    }
    sec = base.get("attack_secondary")
    if sec:
        s = dict(sec)
        s["damage_min"] = max(1, int(s["damage_min"] * loc.atk_mult))
        s["damage_max"] = max(1, int(s["damage_max"] * loc.atk_mult))
        template["attack_secondary"] = s
    is_boss = mob_id == loc.boss_id
    hp = hp0 if is_boss else generate_enemy_hp({"hp": hp0})
    if party_size > 1:
        extra = party_size - 1
        hp = int(hp * (1 + config.CAMPAIGN_PARTY_HP_SCALE * extra))
        k = 1 + config.CAMPAIGN_PARTY_ATK_SCALE * extra
        template["atk_min"] = max(1, int(template["atk_min"] * k))
        template["atk_max"] = max(1, int(template["atk_max"] * k))
        if template["attack_secondary"]:
            template["attack_secondary"]["damage_min"] = max(1, int(template["attack_secondary"]["damage_min"] * k))
            template["attack_secondary"]["damage_max"] = max(1, int(template["attack_secondary"]["damage_max"] * k))
    template["hp"] = hp
    return template, hp


def _encounter(mob_id: str, loc: CampaignLocation, party_size: int) -> RaidEncounter:
    template, hp = build_enemy_template(mob_id, loc, party_size)
    return RaidEncounter(enemy_hp=hp, enemy_max_hp=hp, enemy_template=template)


# ─── Генерация кампании ─────────────────────────────────────

def _shuffle(items: list) -> list:
    items = list(items)
    for i in range(len(items) - 1, 0, -1):
        j = secure_randint(0, i)
        items[i], items[j] = items[j], items[i]
    return items


def _pick(seq):
    return seq[secure_randint(0, len(seq) - 1)]


def _chest_kinds(n: int) -> list[str]:
    """6–8 сундуков: большинство с сокровищем, часть с ловушкой или пустые."""
    kinds = []
    for _ in range(n):
        r = secure_randint(1, 100)
        kinds.append("treasure" if r <= 60 else "trap" if r <= 80 else "empty")
    while kinds.count("treasure") < max(3, n // 2):
        idx = [i for i, k in enumerate(kinds) if k != "treasure"][0]
        kinds[idx] = "treasure"
    return kinds


def generate_campaign(loc: CampaignLocation, party_size: int = 1) -> tuple[dict, list[RaidEncounter]]:
    """Строит цепочку комнат и список боёв (по одному на боевую комнату).

    Returns: (состояние кампании без участников, encounters).
    """
    encounters: list[RaidEncounter] = []
    n_fights = secure_randint(*loc.fights)
    middle: list[dict] = []

    for _ in range(n_fights):
        enc = _encounter(_pick(loc.mob_ids), loc, party_size)
        encounters.append(enc)
        middle.append({"type": "fight", "enc": len(encounters) - 1,
                       "ambush": roll_chance(config.CAMPAIGN_AMBUSH_CHANCE)})
    for kind in _chest_kinds(secure_randint(6, 8)):
        middle.append({"type": "chest", "kind": kind})
    for _ in range(2):
        middle.append({"type": "find", "flavor": secure_randint(0, max(0, len(loc.finds) - 1))})
    for _ in range(2):
        middle.append({"type": "artifact"})
    middle.append({"type": "trap"})

    middle = _shuffle(middle)
    # первая комната — без засады и без артефакта (игроки ещё не освоились)
    first_ok = next(i for i, r in enumerate(middle) if r["type"] != "artifact")
    middle[0], middle[first_ok] = middle[first_ok], middle[0]
    if middle[0]["type"] == "fight":
        middle[0]["ambush"] = False

    boss_enc = _encounter(loc.boss_id, loc, party_size)
    encounters.append(boss_enc)
    rooms = middle + [{"type": "rest"}, {"type": "boss", "enc": len(encounters) - 1, "ambush": False}]

    state = {
        "loc": loc.key, "rooms": rooms, "room_idx": -1, "in_fight": False,
        "pending": None, "candies": {}, "heroes": {}, "scouted": -1,
        "party_size": party_size, "finalized": False, "chests_opened": 0,
    }
    return state, encounters


# ─── Описания и подсказки ───────────────────────────────────

ROOM_HINTS = {
    "fight": "⚔️ впереди враги",
    "boss": "👑 покои босса",
    "chest": "📦 сундук",
    "find": "🔍 какая-то находка",
    "artifact": "✨ реликвия",
    "trap": "🪤 ловушка",
    "rest": "🔥 место для отдыха",
}


def room_hint(room: dict) -> str:
    """Что видит «Глаз пророка»: тип комнаты и её подвох."""
    t = room["type"]
    if t == "fight" and room.get("ambush"):
        return "⚔️ впереди **засада**!"
    if t == "chest":
        return {"treasure": "📦 сундук с **сокровищем**", "trap": "📦 сундук с **ловушкой**",
                "empty": "📦 **пустой** сундук"}[room["kind"]]
    return ROOM_HINTS.get(t, "❓")


def artifact_text(key: str) -> str:
    a = ARTIFACTS[key]
    return f"{a.icon} {a.name}\n   ➕ {a.plus}\n   ➖ {a.minus}"


def artifact_line(char: Character) -> str:
    if not char.artifacts:
        return "—"
    return ", ".join(f"{ARTIFACTS[k].icon} {ARTIFACTS[k].name}" for k in char.artifacts if k in ARTIFACTS)


def roll_artifact_offer(char: Character) -> Optional[str]:
    """Случайный артефакт, которого у героя ещё нет."""
    pool = [k for k in ARTIFACTS if k not in char.artifacts]
    return _pick(pool) if pool else None


def chest_candies(loc: CampaignLocation) -> int:
    return secure_randint(*loc.chest_candy)


# ─── Находки (труп, фляга, ягоды…) ──────────────────────────

FIND_OUTCOME_TEXT = {
    "heal": "💚 Это оказалось целебным!",
    "damage": "🤢 Зря вы это сделали…",
    "buff_atk": "⚔️ Вы чувствуете прилив сил! (+2 к атаке отряда до конца кампании)",
    "buff_def": "🛡 Вы чувствуете себя защищённее! (+2 к защите отряда до конца кампании)",
    "debuff_atk": "😵 Вам стало хуже: рука дрожит. (−2 к атаке отряда до конца кампании)",
    "debuff_def": "😵 Вас пробирает холод. (−2 к защите отряда до конца кампании)",
    "nothing": "🤷 Ничего не произошло.",
}


def apply_find(kind: str, char: Character, buffs: dict) -> str:
    """Применяет исход находки к герою (баффы — на весь отряд). Возвращает текст."""
    text = FIND_OUTCOME_TEXT.get(kind, FIND_OUTCOME_TEXT["nothing"])
    if kind == "heal":
        got = heal_fraction(char, 0.25)
        text += f" +{got}❤️"
    elif kind == "damage":
        text += f" −{nonlethal_damage(char, 0.12)}❤️"
    elif kind == "buff_atk":
        buffs["atk"] = buffs.get("atk", 0) + 2
    elif kind == "buff_def":
        buffs["def"] = buffs.get("def", 0) + 2
    elif kind == "debuff_atk":
        buffs["atk"] = buffs.get("atk", 0) - 2
    elif kind == "debuff_def":
        buffs["def"] = buffs.get("def", 0) - 2
    return text


# ─── Конфеты: итоговая команда для основного бота ────────────

def boss_reward(loc: CampaignLocation, fallen: bool) -> int:
    if not fallen:
        return loc.price
    return int(loc.price * config.CAMPAIGN_FALLEN_BOSS_SHARE)


def build_reward_command(candies: dict, command: str = "", bot_username: str = "") -> Optional[str]:
    """Одна строка со всеми участниками: /cmd[@bot] 111:250 222:300.

    Участники с нулевой суммой не включаются. Если платить некому — None.
    """
    pairs = [(int(uid), int(n)) for uid, n in candies.items() if int(n) > 0]
    if not pairs:
        return None
    command = (command or config.CANDY_COMMAND).lstrip("/")
    bot_username = (bot_username if bot_username != "" else config.MAIN_BOT_USERNAME).lstrip("@")
    head = f"/{command}@{bot_username}" if bot_username else f"/{command}"
    return head + " " + " ".join(f"{uid}:{n}" for uid, n in sorted(pairs))
