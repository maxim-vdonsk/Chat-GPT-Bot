# keyboards.py
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder, InlineKeyboardButton
from aiogram.types import KeyboardButton

def get_main_keyboard(user_id: int, is_admin: bool = False):
    builder = ReplyKeyboardBuilder()
    buttons = [
        "🔄 Новый чат", "🎙 Ответ голосом",
        "🌐 Поиск в интернете", "🎨 Генерация изображения",
        "🔊 Перевести в голос", "🖌 Вариации изображения",  # Новые кнопки
        "📖 Инструкция", "👤 Профиль",
        "🕓 История", "⚙️ Настройки",
    ]
    for button_text in buttons:
        builder.add(KeyboardButton(text=button_text))
    if is_admin:
        builder.add(KeyboardButton(text="📊 Статистика"))
        builder.add(KeyboardButton(text="🛠 Управление моделями"))
        builder.add(KeyboardButton(text="📢 Рассылка"))
        builder.add(KeyboardButton(text="👥 Активность пользователей"))
        builder.add(KeyboardButton(text="🖼️ Сгенерировать (админ)"))
    builder.adjust(2)  # Устанавливаем 2 кнопки в строке
    return builder.as_markup(resize_keyboard=True)

def get_cancel_keyboard():
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="👉 Выход"))
    return builder.as_markup(resize_keyboard=True)

def get_settings_keyboard(current_model: str):
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📝 Текстовые модели", callback_data="text_models"))
    builder.add(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    builder.adjust(1)
    return builder.as_markup()

def get_text_models_keyboard(models: list, current_model: str):
    builder = InlineKeyboardBuilder()
    for model_id, model_name in models:
        text = f"✅ {model_name}" if model_name == current_model else model_name
        builder.add(InlineKeyboardButton(text=text, callback_data=f"set_model_{model_id}"))
    builder.add(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_settings"))
    builder.adjust(1)
    return builder.as_markup()

def get_manage_models_keyboard(models: list):
    builder = InlineKeyboardBuilder()
    for model_id, model_name, provider, is_active in models:
        status = "✅ Активна" if is_active else "❌ Неактивна"
        builder.add(InlineKeyboardButton(
            text=f"{model_name} ({provider}) - {status}",
            callback_data=f"toggle_model_{model_id}"
        ))
    builder.add(InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main"))
    builder.adjust(1)
    return builder.as_markup()
