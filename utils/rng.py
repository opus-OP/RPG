import secrets
import random


def secure_randint(a: int, b: int) -> int:
    return secrets.randbelow(b - a + 1) + a


def secure_randfloat() -> float:
    return secrets.SystemRandom().random()


def roll_chance(probability: float) -> bool:
    if probability <= 0.0:
        return False
    if probability >= 1.0:
        return True
    return secure_randfloat() < probability


def roll_dice(sides: int = 100) -> int:
    return secure_randint(1, sides)


def rand_range(min_val: float, max_val: float) -> float:
    return min_val + (max_val - min_val) * secure_randfloat()
