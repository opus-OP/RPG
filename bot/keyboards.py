from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.classes import CLASSES, CLASS_NAMES_RU
from core.locations import LOCATIONS


def _back(text: str = "Назад", data: str = "main_menu") -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    b.button(text=text, callback_data=data)
    return b


def main_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="👤 Профиль", callback_data="profile")
    b.button(text="🎒 Инвентарь", callback_data="inventory")
    b.button(text="🗺 Локации", callback_data="location")
    b.button(text="🏪 Рынок", callback_data="market")
    b.button(text="📜 Мои персонажи", callback_data="char_list")
    b.adjust(2)
    return b.as_markup()


def class_selection() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, cls in CLASSES.items():
        b.button(text=cls.name, callback_data=f"class_{key}")
    b.adjust(2)
    return b.as_markup()


def location_list() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, loc in LOCATIONS.items():
        label = f"{loc.name} (lvl {loc.recommended_level})"
        b.button(text=label, callback_data=f"loc_{key}")
    b.adjust(1)
    b.button(text="Назад", callback_data="main_menu")
    return b.as_markup()


def confirm_raid(location_key: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Вперёд!", callback_data=f"raid_start_{location_key}")
    b.button(text="❌ Отмена", callback_data="main_menu")
    return b.as_markup()


def raid_actions() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⚔️ Атаковать", callback_data="raid_attack")
    b.button(text="🔮 Спросить NN", callback_data="raid_nn")
    b.button(text="🏃 Сбежать", callback_data="raid_leave")
    b.adjust(2)
    return b.as_markup()


def raid_next() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="➡️ Следующий", callback_data="raid_next")
    return b.as_markup()


def raid_done() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Завершить", callback_data="main_menu")
    return b.as_markup()


def raid_failed() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💀 Вернуться", callback_data="main_menu")
    return b.as_markup()


def inventory_pages(items: list, page: int = 0, per_page: int = 6) -> InlineKeyboardMarkup:
    if not items:
        return _back("В меню").as_markup()
    start = page * per_page
    batch = items[start:start + per_page]
    b = InlineKeyboardBuilder()
    for item in batch:
        label = f"{item.name} [{item.rarity.value}]"
        b.button(text=label, callback_data=f"inv_item_{item.uid}")
    if page > 0:
        b.button(text="◀️", callback_data=f"inv_page_{page - 1}")
    if start + per_page < len(items):
        b.button(text="▶️", callback_data=f"inv_page_{page + 1}")
    b.button(text="Назад", callback_data="main_menu")
    b.adjust(1)
    return b.as_markup()


def item_actions(item_uid: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔧 Надеть", callback_data=f"inv_equip_{item_uid}")
    b.button(text="📦 Снять", callback_data=f"inv_unequip_{item_uid}")
    b.button(text="🗑 Выбросить", callback_data=f"inv_drop_{item_uid}")
    b.button(text="← Назад", callback_data="inventory")
    b.adjust(1)
    return b.as_markup()


def char_list(characters: list, current_name: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in characters:
        mark = "✅" if c.name == current_name else ""
        b.button(text=f"{mark} {c.name} — {CLASS_NAMES_RU.get(c.class_key, c.class_key)}", callback_data=f"char_switch_{c.name}")
    b.button(text="➕ Создать", callback_data="char_create")
    b.button(text="Назад", callback_data="main_menu")
    b.adjust(1)
    return b.as_markup()


def market_listings(listings: list, page: int = 0, per_page: int = 5) -> InlineKeyboardMarkup:
    if not listings:
        return _back("Назад").as_markup()
    start = page * per_page
    batch = listings[start:start + per_page]
    b = InlineKeyboardBuilder()
    for listing in batch:
        price = listing.price
        item = listing.item
        label = f"{item.name} — {price}💰"
        b.button(text=label, callback_data=f"market_buy_{listing.listing_id}")
    if page > 0:
        b.button(text="◀️", callback_data=f"market_page_{page - 1}")
    if start + per_page < len(listings):
        b.button(text="▶️", callback_data=f"market_page_{page + 1}")
    b.button(text="🔄 Обновить", callback_data="market_refresh")
    b.button(text="Назад", callback_data="main_menu")
    b.adjust(1)
    return b.as_markup()


def market_confirm(listing_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ Купить", callback_data=f"market_confirm_{listing_id}")
    b.button(text="❌ Отмена", callback_data="market")
    return b.as_markup()
