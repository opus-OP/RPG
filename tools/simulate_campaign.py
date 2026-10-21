"""Балансная симуляция кампаний без Telegram и нейросети.

Запуск:  python tools/simulate_campaign.py [число_прогонов] [ключи_локаций_через_запятую]

Игрок бьёт обычными атаками по сильнейшей характеристике (без бонусов от нейросети — на деле
игроки с хорошим описанием действий проходят легче), берёт случайные артефакты, не пропускает
сундуки с ловушками. Выводит долю побед соло по классам и отрядом из 2 и 4 героев.
Цель по умолчанию (соло, среднее по классам): чаща/заповедник ≈85%, руины ≈75%,
дворец ≈65%, подземелья ≈50%. Подгоняйте hp_mult/atk_mult локаций в data/campaign.json.
"""
import sys, random
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from core import campaign as C
from core.raid import (create_enemy, resolve_player_turn, resolve_companion_turn, resolve_enemy_turn,
                       build_initiative_order, get_current_turn, advance_turn_core, player_key)
from core.combat import StatusEffect
from utils.rng import roll_chance
import config

def best_attr(c):
    st = c.stats
    return max(("strength","agility","intelligence"), key=lambda a: getattr(st, a))

ATTR = {"warrior": "strength", "rogue": "agility", "mage": "intelligence", "leader": "intelligence"}

def fight(heroes, enc, mode="normal"):
    """heroes: dict uid->Character. Возвращает True если победили."""
    alive = {u: c for u, c in heroes.items() if c.alive and c.hp > 0}
    enc.initiative_order = build_initiative_order(alive, enc, mode)
    enc.current_turn_index = 0; enc.round_number = 0
    enemy = create_enemy(enc)
    for _ in range(400):
        if enc.enemy_hp <= 0: return True
        turn = get_current_turn(enc)
        if turn is None: return False
        t = turn["type"]
        if t == "player":
            c = heroes[turn["uid"]]
            if not c.alive or c.hp <= 0: advance_turn_core(enc); continue
            enc.turn += 1
            stunned = any(e.kind == StatusEffect.STUNNED for e in enc.active_effects.get(player_key(c), []))
            resolve_player_turn(c, enemy, enc, None, attribute=best_attr(c)) if not stunned else resolve_player_turn(c, enemy, enc, None)
            if c.hp <= 0: c.alive = False
        elif t == "companion":
            c = heroes[turn["uid"]]
            if c.alive and c.hp > 0 and c.companion and c.companion.alive:
                resolve_companion_turn(c, enemy, enc)
        else:
            al = [(u, c) for u, c in heroes.items() if c.alive and c.hp > 0]
            if not al: return False
            marked = [x for x in al if x[1].priority_target]
            tgt = random.choice(marked) if marked and roll_chance(config.CAMPAIGN_PRIORITY_TARGET_CHANCE) else random.choice(al)
            c = tgt[1]
            resolve_enemy_turn(c, enemy, enc, None)
            if c.hp <= 0: c.alive = False
        if not any(c.alive and c.hp > 0 for c in heroes.values()): return False
        if enc.enemy_hp <= 0: return True
        advance_turn_core(enc)
    return False

def run(loc_key, cls, party=1, art=True):
    loc = C.get_campaign_location(loc_key)
    state, encs = C.generate_campaign(loc, party)
    classes = [cls] if party == 1 else (["warrior", "rogue", "mage", "leader"] * 2)[:party]
    heroes = {i + 1: C.create_hero(i + 1, f"h{i}", classes[i], loc) for i in range(party)}
    for h in heroes.values(): h.summon_companion()
    fights = 0
    for room in state["rooms"]:
        t = room["type"]
        if t in ("fight", "boss"):
            enc = encs[room["enc"]]
            alive = {u: c for u, c in heroes.items() if c.alive and c.hp > 0}
            mode = "ambush" if room.get("ambush") else "normal"
            if mode == "normal" and any(roll_chance(c.surprise_chance) for c in alive.values()): mode = "surprise"
            ok = fight(heroes, enc, mode)
            if not ok: return False, fights
            fights += 1
            for c in heroes.values():
                if c.alive and c.hp > 0:
                    c.clear_buffs()
                    lv = C.award_fight_exp(c, loc)
                    if t != "boss" and not lv: C.heal_fraction(c, loc.heal_after_fight)
                    c.summon_companion() if c.class_key == "leader" else None
        elif t == "chest" and room["kind"] == "trap":
            h = random.choice([c for c in heroes.values() if c.alive])
            C.nonlethal_damage(h, 0.15 + 0.02 * loc.tier)
        elif t == "trap":
            for c in heroes.values():
                if c.alive and c.hp > 0 and not roll_chance(min(0.9, c.dodge_chance + 0.3)):
                    C.nonlethal_damage(c, 0.12 + 0.02 * loc.tier)
        elif t == "rest":
            for c in heroes.values():
                if c.alive: C.heal_fraction(c, 0.40)
        elif t == "find":
            f = loc.finds[room["flavor"]] if loc.finds else {"kind": "nothing"}
            c = random.choice([c for c in heroes.values() if c.alive])
            C.apply_find(f["kind"], c, {}) if False else None
            if f["kind"] == "heal": C.heal_fraction(c, 0.25)
            elif f["kind"] == "damage": C.nonlethal_damage(c, 0.12)
        elif t == "artifact" and art:
            for c in heroes.values():
                if c.alive and len(c.artifacts) < 2:
                    k = C.roll_artifact_offer(c)
                    if k: c.add_artifact(k)
    return True, fights

if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    locs = sys.argv[2].split(",") if len(sys.argv) > 2 else list(C.get_campaign_locations())
    for lk in locs:
        row = []
        for cls in ("warrior", "rogue", "mage", "leader"):
            w = sum(run(lk, cls, 1)[0] for _ in range(n))
            row.append(f"{cls[:3]} {100*w/n:3.0f}%")
        w4 = sum(run(lk, "warrior", 4)[0] for _ in range(n))
        w2 = sum(run(lk, "warrior", 2)[0] for _ in range(n))
        print(f"{lk:15s} solo: {' | '.join(row)}   party2 {100*w2/n:3.0f}%  party4 {100*w4/n:3.0f}%")
