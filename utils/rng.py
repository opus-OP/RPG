"""Генераторы случайных чисел.

Все RNG-операции используют модуль `secrets` — криптостойкий ГСЧ.
Никакие случайные величины не должны генерироваться нейросетью.
"""

import secrets


def secure_randint(a: int, b: int) -> int:
    """Случайное целое в [a, b] (включительно)."""
    return secrets.randbelow(b - a + 1) + a


def secure_randfloat() -> float:
    """Случайное число с плавающей точкой в [0.0, 1.0)."""
    return secrets.SystemRandom().random()


def roll_chance(probability: float) -> bool:
    """Вероятностная проверка: True с шансом `probability`.

    Заменяет `random.random() < prob`, но через криптостойкий ГСЧ.
    """
    if probability <= 0.0:
        return False
    if probability >= 1.0:
        return True
    return secure_randfloat() < probability


def roll_dice(sides: int = 100) -> int:
    """Бросок N-гранной кости: [1, sides]."""
    return secure_randint(1, sides)


def rand_range(min_val: float, max_val: float) -> float:
    """Случайное вещественное в [min_val, max_val]."""
    return min_val + (max_val - min_val) * secure_randfloat()
