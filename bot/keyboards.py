"""Инлайн-клавиатуры для Telegram-интерфейса.

Все клавиатуры используют callback_data с префиксами для маршрутизации
в соответствующие хендлеры (см. register_handlers в handlers.py).
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from secrets import token_hex

from core.classes import CLASSES, CLASS_NAMES_RU
from core.locations import LOCATIONS


def main_menu() -> InlineKeyboardMarkup:
    """Главное меню: профиль, инвентарь, локации, рынок, персонажи."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Профиль", callback_data="profile"),
         InlineKeyboardButton("🎒 Инвентарь", callback_data="inventory")],
        [InlineKeyboardButton("🗺 Локации", callback_data="location"),
         InlineKeyboardButton("🏪 Рынок", callback_data="market")],
        [InlineKeyboardButton("📜 Мои персонажи", callback_data="char_list")],
    ])


def class_selection() -> InlineKeyboardMarkup:
    """Выбор класса при создании персонажа (2 колонки)."""
    buttons = []
    for key, cls in CLASSES.items():
        buttons.append(InlineKeyboardButton(cls.name, callback_data=f"class_{key}"))
    return InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


def location_list() -> InlineKeyboardMarkup:
    """Список доступных локаций для рейда."""
    keyboard = []
    for key, loc in LOCATIONS.items():
        keyboard.append([InlineKeyboardButton(
            f"{loc.name} (lvl {loc.recommended_level})",
            callback_data=f"loc_{key}",
        )])
    keyboard.append([InlineKeyboardButton("Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


def confirm_raid(location_key: str) -> InlineKeyboardMarkup:
    """Подтверждение рейда: соло / онлайн / отмена."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚔️ Соло", callback_data=f"raid_start_{location_key}")],
        [InlineKeyboardButton("👥 Онлайн", callback_data=f"raid_online_{location_key}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="main_menu")],
    ])


def raid_lobby(location_name: str, code: str, participants: list[tuple[int, str]], is_owner: bool) -> InlineKeyboardMarkup:
    """Лобби группового рейда: список участников, старт (только лидер), выход."""
    keyboard = []
    for uid, name in participants:
        keyboard.append([InlineKeyboardButton(f"👤 {name}", callback_data="raid_lobby_noop")])
    if is_owner:
        keyboard.append([InlineKeyboardButton("⚔️ Начать рейд", callback_data="raid_lobby_start")])
    keyboard.append([InlineKeyboardButton("🏃 Выйти", callback_data="raid_lobby_leave")])
    return InlineKeyboardMarkup(keyboard)


def raid_lobby_text(location_name: str, code: str, participants: list[tuple[int, str]]) -> str:
    """Текст лобби: название локации, код, участники."""
    lines = [f"🎮 **{location_name}** — ожидание игроков"]
    lines.append(f"🔑 Код: `{code}`")
    lines.append("")
    lines.append("**Участники:**")
    for uid, name in participants:
        lines.append(f"• {name}")
    lines.append("")
    lines.append("Отправь код другому игроку, чтобы он присоединился через `/raidjoin`.")
    return "\n".join(lines)


def raid_actions() -> InlineKeyboardMarkup:
    """Кнопки во время рейда: действие / сбежать."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔮 Действие", callback_data="raid_action")],
        [InlineKeyboardButton("🏃 Сбежать", callback_data="raid_leave")],
    ])


def raid_next() -> InlineKeyboardMarkup:
    """Переход к следующему врагу."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️ Следующий", callback_data="raid_next")],
    ])


def raid_done() -> InlineKeyboardMarkup:
    """Завершение рейда — возврат в меню."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Завершить", callback_data="main_menu")],
    ])


def raid_failed() -> InlineKeyboardMarkup:
    """Поражение в рейде."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💀 Вернуться", callback_data="main_menu")],
    ])


def inventory_pages(items: list, page: int = 0, per_page: int = 6) -> InlineKeyboardMarkup:
    """Инвентарь с постраничной навигацией (6 предметов на странице)."""
    if not items:
        return InlineKeyboardMarkup([[InlineKeyboardButton("В меню", callback_data="main_menu")]])
    start = page * per_page
    batch = items[start:start + per_page]
    keyboard = []
    for item in batch:
        keyboard.append([InlineKeyboardButton(
            f"{item.name} [{item.rarity.value}]",
            callback_data=f"inv_item_{item.uid}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"inv_page_{page - 1}"))
    if start + per_page < len(items):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"inv_page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


def item_actions(item_uid: str) -> InlineKeyboardMarkup:
    """Действия с предметом: надеть, снять, продать, выбросить."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔧 Надеть", callback_data=f"inv_equip_{item_uid}")],
        [InlineKeyboardButton("📦 Снять", callback_data=f"inv_unequip_{item_uid}")],
        [InlineKeyboardButton("💰 Продать", callback_data=f"inv_sell_{item_uid}")],
        [InlineKeyboardButton("🗑 Выбросить", callback_data=f"inv_drop_{item_uid}")],
        [InlineKeyboardButton("← Назад", callback_data="inventory")],
    ])


def char_list(characters: list, current_name: str) -> InlineKeyboardMarkup:
    """Список персонажей с переключением и удалением."""
    keyboard = []
    for c in characters:
        mark = "✅ " if c.name == current_name else ""
        keyboard.append([
            InlineKeyboardButton(
                f"{mark}{c.name} — {CLASS_NAMES_RU.get(c.class_key, c.class_key)}",
                callback_data=f"char_switch_{c.name}",
            ),
            InlineKeyboardButton("❌", callback_data=f"char_del_{c.name}"),
        ])
    keyboard.append([InlineKeyboardButton("➕ Создать", callback_data="char_create")])
    keyboard.append([InlineKeyboardButton("Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


def char_delete_confirm(name: str) -> InlineKeyboardMarkup:
    """Подтверждение удаления персонажа."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data=f"char_del_yes_{name}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="char_list")],
    ])


def market_listings(listings: list, page: int = 0, per_page: int = 5) -> InlineKeyboardMarkup:
    """Список объявлений рынка с пагинацией и обновлением."""
    if not listings:
        return InlineKeyboardMarkup([[InlineKeyboardButton("Назад", callback_data="main_menu")]])
    start = page * per_page
    batch = listings[start:start + per_page]
    keyboard = []
    for listing in batch:
        keyboard.append([InlineKeyboardButton(
            f"{listing.item.name} — {listing.price}💰",
            callback_data=f"market_buy_{listing.listing_id}",
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"market_page_{page - 1}"))
    if start + per_page < len(listings):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"market_page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="market_refresh")])
    keyboard.append([InlineKeyboardButton("Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


def market_confirm(listing_id: str) -> InlineKeyboardMarkup:
    """Подтверждение покупки."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Купить", callback_data=f"market_confirm_{listing_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data="market")],
    ])
