import os
import logging
import uuid
import asyncio
import aiosqlite
from pathlib import Path
from contextlib import asynccontextmanager
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton
import aiohttp
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_exponential
from g4f.client import AsyncClient
import g4f.Provider
from config import DATABASE_PATH, MAX_MESSAGE_LENGTH, DEFAULT_MODEL, DEFAULT_VOICE, ENGLISH_VOICE, VOICES_DIR, VARIATIONS_DIR, IMAGES_DIR
from keyboards import get_main_keyboard, get_cancel_keyboard, get_settings_keyboard, get_text_models_keyboard, get_manage_models_keyboard
from database import Database
from instructions import INSTRUCTION_TEXT
from g4f.errors import ResponseError
from datetime import datetime
import edge_tts
from PIL import Image, ImageEnhance, ImageFilter, UnidentifiedImageError
import io

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    logger.critical("TELEGRAM_BOT_TOKEN не установлен в переменных окружения.")
    raise ValueError("TELEGRAM_BOT_TOKEN is required.")
ADMIN = os.getenv("ADMIN", "")
ADMIN_IDS = [int(admin_id) for admin_id in ADMIN.split(",") if admin_id.strip().isdigit()] if ADMIN else []

# Инициализация бота и диспетчера
bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# Обработчики корректного завершения
async def on_shutdown():
    logger.info("Shutting down...")
    try:
        await bot.session.close()
        if hasattr(g4f_client, 'session') and g4f_client.session and hasattr(g4f_client.session, 'close'):
            await g4f_client.close()
        if hasattr(dp, 'storage'):
            await dp.storage.close()
    except Exception as e:
        logger.error(f"Error during shutdown: {e}", exc_info=True)
    logger.info("Bot resources released")

dp.shutdown.register(on_shutdown)

# Инициализация базы данных
db = Database(DATABASE_PATH)
g4f_client = AsyncClient(image_provider=g4f.Provider.ARTA)
cancel_keyboard = get_cancel_keyboard()

# Состояния FSM
class UserStates(StatesGroup):
    awaiting_prompt = State()
    awaiting_search_query = State()
    awaiting_audio = State()
    awaiting_image = State()
    awaiting_image_prompt = State()
    awaiting_admin_image_description = State()
    awaiting_broadcast_message = State()
    awaiting_text_to_voice = State()
    awaiting_image_variations = State()

# Вспомогательные функции
async def ensure_profile(user: types.User):
    cursor = await db.connection.execute("SELECT 1 FROM profiles WHERE user_id = ?", (user.id,))
    if not await cursor.fetchone():
        await db.connection.execute(
            # Добавляем created_at при создании нового профиля
            "INSERT INTO profiles (user_id, name, created_at) VALUES (?, ?, datetime('now', 'localtime'))",
            (user.id, user.full_name)
        )
        await db.connection.execute(
            "INSERT OR IGNORE INTO user_settings (user_id, model_id) VALUES (?, (SELECT id FROM models WHERE name = ? LIMIT 1))",
            (user.id, DEFAULT_MODEL)
        )
        await db.connection.commit()

async def get_current_session(user_id: int) -> int:
    cursor = await db.connection.execute(
        "SELECT COALESCE(MAX(session_id), 0) + 1 FROM history WHERE user_id = ?",
        (user_id,)
    )
    row = await cursor.fetchone()
    return row[0]

async def get_user_model(user_id: int) -> str:
    cursor = await db.connection.execute("""
        SELECT m.name FROM user_settings us
        JOIN models m ON us.model_id = m.id
        WHERE us.user_id = ?
    """, (user_id,))
    row = await cursor.fetchone()
    if not row:
        await ensure_profile(types.User(id=user_id, first_name="User", is_bot=False))
        return DEFAULT_MODEL
    return row[0]

async def fetch_user_history(user_id: int, session_id: int) -> list:
    cursor = await db.connection.execute("""
        SELECT message, reply FROM history
        WHERE user_id = ? AND session_id = ?
        ORDER BY timestamp ASC
        LIMIT 20
    """, (user_id, session_id))
    return await cursor.fetchall()

async def save_message_to_history(user_id: int, text: str, reply: str, session_id: int):
    await db.connection.execute(
        "INSERT INTO history (user_id, message, reply, session_id) VALUES (?, ?, ?, ?)",
        (user_id, text, reply, session_id)
    )
    await db.connection.execute(
        "UPDATE profiles SET gpt_requests = gpt_requests + 1 WHERE user_id = ?",
        (user_id,)
    )
    await db.connection.commit()

@asynccontextmanager
async def temp_audio_file(user_id: int, text: str):
    media_dir = Path(VOICES_DIR)
    unique_id = uuid.uuid4().hex
    safe_text = "".join(c if c.isalnum() else "_" for c in text)[:50]
    audio_filename = f"audio_{user_id}_{safe_text}_{unique_id}.mp3"
    audio_path = media_dir / audio_filename
    try:
        yield audio_path
    finally:
        try:
            if audio_path.exists():
                audio_path.unlink()
        except Exception as e:
            logger.error(f"Failed to delete temporary audio file {audio_path}: {e}")

@asynccontextmanager
async def temp_image_file(user_id: int, suffix: str):
    media_dir = Path(VARIATIONS_DIR)
    media_dir.mkdir(parents=True, exist_ok=True)  # Ensure directory exists
    unique_id = uuid.uuid4().hex
    image_filename = f"variation_{user_id}_{unique_id}_{suffix}.png"
    image_path = media_dir / image_filename
    try:
        logger.debug(f"Creating temporary image file: {image_path}")
        yield image_path
    except Exception as e:
        logger.error(f"Error in temp_image_file for {image_path}: {e}")
        raise

async def generate_voice(text: str, language: str = "ru") -> str:
    """Генерирует голосовой ответ из текста с использованием edge-tts."""
    voice = DEFAULT_VOICE if language == "ru" else ENGLISH_VOICE
    unique_id = uuid.uuid4().hex
    audio_path = Path(VOICES_DIR) / f"voice_{unique_id}.mp3"
    
    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(audio_path))
        return audio_path
    except Exception as e:
        logger.error(f"Error generating voice: {e}")
        raise

async def create_image_variations(image_path: Path, user_id: int, num_variations: int = 4) -> list:
    """Создает вариации изображения с применением различных фильтров и эффектов."""
    try:
        logger.info(f"Opening image for user_id={user_id}")
        image = Image.open(image_path).convert("RGB")
        variations = []
        
        logger.info(f"Generating brightness variation for user_id={user_id}")
        async with temp_image_file(user_id, "bright") as bright_path:
            enhancer = ImageEnhance.Brightness(image)
            bright_image = enhancer.enhance(1.5)
            bright_image.save(bright_path, "PNG")
            variations.append(bright_path)
        
        logger.info(f"Generating contrast variation for user_id={user_id}")
        async with temp_image_file(user_id, "contrast") as contrast_path:
            enhancer = ImageEnhance.Contrast(image)
            contrast_image = enhancer.enhance(1.5)
            contrast_image.save(contrast_path, "PNG")
            variations.append(contrast_path)
        
        logger.info(f"Generating blur variation for user_id={user_id}")
        async with temp_image_file(user_id, "blur") as blur_path:
            blur_image = image.filter(ImageFilter.GaussianBlur(radius=2))
            blur_image.save(blur_path, "PNG")
            variations.append(blur_path)
        
        logger.info(f"Generating black-and-white variation for user_id={user_id}")
        async with temp_image_file(user_id, "bw") as bw_path:
            bw_image = image.convert("L").convert("RGB")
            bw_image.save(bw_path, "PNG")
            variations.append(bw_path)
        
        return variations
    except UnidentifiedImageError:
        logger.error(f"Invalid image file for user_id={user_id}")
        raise ValueError("Невозможно открыть изображение. Убедитесь, что файл является действительным изображением.")
    except Exception as e:
        logger.error(f"Error creating image variations for user_id={user_id}: {e}")
        raise

# Обработчики команд
@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    user = message.from_user
    await ensure_profile(user)
    await state.update_data(session_id=await get_current_session(user.id))
    is_admin = user.id in ADMIN_IDS
    await message.answer(
        f"""Привет, <b>{user.first_name}</b>! 👋\n\n
        Я — умный бот на основе GPT-4, готовый помочь с различными задачами.  

<b>Что я умею?</b>  
✨ Отвечать на вопросы по разным темам
🎙 Генерировать голосовые ответы
🔊 Переводить текстовые ответы в голос
💻 Помогать с программированием и кодом
🎨 Генерировать изображения по описанию
🖌 Создавать вариации изображений
🖼 Обрабатывать изображения
🎭 Поддерживать интерактивные сценарии 

<b>Как мной пользоваться?</b>  
Просто напиши мне сообщение — я постараюсь помочь!  
Для генерации изображения нажми кнопку <b>🎨 Генерация изображения</b>.
Для создания вариаций изображения нажми <b>🖌 Вариации изображения</b>.
Для генерации аудиоответа нажми кнопку <b>"🎙 Ответ голосом"</b>.  
Для перевода текста в голос нажми <b>"🔊 Перевести в голос"</b>.
Для поиска в интернете нажми кнопку <b>"🌐 Поиск в интернете"</b>.
Для настройки параметров нажми кнопку <b>"⚙️ Настройки"</b>.
Для выхода из чата нажми кнопку <b>"👉 Выход"</b>.
Для просмотра истории сообщений нажми кнопку <b>"🕓 История"</b>.
Для просмотра своего профиля нажми кнопку <b>"👤 Профиль"</b>. 

Начнём? 😊""",
        reply_markup=get_main_keyboard(user.id, is_admin),
        parse_mode="HTML"
    )

@dp.message(Command("cancel"))
async def cancel_command(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Все действия отменены. Вы в главном меню.",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.message(F.text == "📖 Инструкция")
async def show_instruction(message: types.Message):
    await message.answer(INSTRUCTION_TEXT, parse_mode="HTML")

@dp.message(F.text == "👉 Выход")
async def handle_exit(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Вы вышли в главное меню.",
        reply_markup=get_main_keyboard(message.from_user.id, is_admin=message.from_user.id in ADMIN_IDS)
    )

@dp.message(F.text == "⚙️ Настройки")
async def show_settings(message: types.Message, state: FSMContext):
    await state.clear()
    current_model = await get_user_model(message.from_user.id)
    await message.answer(
        f"⚙️ <b>Настройки</b>\n\nТекущая модель: <b>{current_model}</b>",
        parse_mode="HTML",
        reply_markup=get_settings_keyboard(current_model)
    )

@dp.callback_query(F.data == "text_models")
async def show_text_models(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    cursor = await db.connection.execute("SELECT id, name FROM models WHERE is_active = 1 ORDER BY name")
    models = await cursor.fetchall()
    
    current_model = await get_user_model(user_id)
    await callback.message.edit_text(
        "Выберите модель для текстовых ответов:",
        parse_mode="HTML",
        reply_markup=get_text_models_keyboard(models, current_model)
    )
    await callback.answer()

async def show_settings_from_query(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    current_model = await get_user_model(user_id)
    await callback.message.edit_text(
        f"⚙️ <b>Настройки</b>\n\nТекущая модель: <b>{current_model}</b>",
        parse_mode="HTML",
        reply_markup=get_settings_keyboard(current_model)
    )
    await callback.answer()

@dp.message(F.text == "🕓 История")
async def show_history(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    await state.update_data(history_message_ids=[])
    data = await state.get_data()
    
    try:
        cursor = await db.connection.execute("""
            SELECT message, reply, timestamp
            FROM history
            WHERE user_id = ?
            ORDER BY timestamp DESC
            LIMIT 5
        """, (user_id,))
        rows = await cursor.fetchall()

        if not rows:
            msg = await message.answer(
                "История сообщений пуста.",
                reply_markup=get_main_keyboard(user_id, is_admin=user_id in ADMIN_IDS))
            data['history_message_ids'].append(msg.message_id)
            await state.set_data(data)
            return

        for i, (msg_text, reply_text, ts) in enumerate(rows[::-1], 1):
            message_text = (
                f"<b>Сообщение #{i} ({ts[:19]})</b>\n"
                f"👤 <b>Вы:</b> {msg_text[:500]}{'...' if len(msg_text) > 500 else ''}\n"
                f"🤖 <b>Бот:</b> {reply_text[:500]}{'...' if len(reply_text) > 500 else ''}"
            )
            msg = await message.answer(message_text, parse_mode="HTML")
            data['history_message_ids'].append(msg.message_id)
            await state.set_data(data)

        builder = InlineKeyboardBuilder()
        builder.add(InlineKeyboardButton(text="🗑 Очистить историю", callback_data="confirm_clear"))
        msg = await message.answer("Вы можете очистить историю:", reply_markup=builder.as_markup())
        data['history_message_ids'].append(msg.message_id)
        await state.set_data(data)
        
    except Exception as e:
        logger.error(f"Error in history handler for user_id={user_id}: {e}", exc_info=True)
        msg = await message.answer(
            "Произошла ошибка при получении истории.",
            reply_markup=get_main_keyboard(user_id, is_admin=user_id in ADMIN_IDS))
        data['history_message_ids'].append(msg.message_id)
        await state.set_data(data)

@dp.callback_query(F.data == "confirm_clear")
async def clear_history_callback(callback: types.CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="✅ Да", callback_data="do_clear"))
    builder.add(InlineKeyboardButton(text="❌ Нет", callback_data="cancel_clear"))
    await callback.message.edit_text(
        "Вы уверены, что хотите удалить историю?",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(F.data == "do_clear")
async def do_clear_history(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    data = await state.get_data()
    
    await db.connection.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
    await db.connection.commit()
    
    if 'history_message_ids' in data:
        for msg_id in reversed(data['history_message_ids']):
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                await asyncio.sleep(0.3)
            except Exception as e:
                if "not found" not in str(e).lower():
                    logger.error(f"Failed to delete message {msg_id} for user_id={user_id}: {e}")
    
    try:
        await callback.message.delete()
    except Exception as e:
        if "not found" not in str(e).lower():
            logger.error(f"Error deleting button message for user_id={user_id}: {e}")

    is_admin = user_id in ADMIN_IDS
    await bot.send_message(
        chat_id=chat_id,
        text="🗑 История очищена!",
        reply_markup=get_main_keyboard(user_id, is_admin=is_admin))
    
    await state.update_data(history_message_ids=[])
    await callback.answer()

@dp.callback_query(F.data == "cancel_clear")
async def cancel_clear_history(callback: types.CallbackQuery, state: FSMContext):
    try:
        user_id = callback.from_user.id
        is_admin = user_id in ADMIN_IDS
        await callback.message.edit_text("❌ Удаление отменено.")
        await bot.send_message(
            chat_id=callback.message.chat.id,
            text="Вы можете продолжить общение.",
            reply_markup=get_main_keyboard(user_id, is_admin=is_admin))
    except Exception as e:
        logger.error(f"Error cancelling history clear for user_id={callback.from_user.id}: {e}", exc_info=True)
        await bot.send_message(
            chat_id=callback.message.chat.id,
            text="❌ Удаление отменено.",
            reply_markup=get_main_keyboard(callback.from_user.id, is_admin=(callback.from_user.id in ADMIN_IDS)))
    await callback.answer()

@dp.message(F.text == "🎙 Ответ голосом")
async def start_audio_response(message: types.Message, state: FSMContext):
    await state.set_state(UserStates.awaiting_audio)
    await message.answer(
        "Вы перешли в меню голосового ответа, напишите текстом свой вопрос и получите ответ голосом\n\n"
        "Нажмите 👉 Выход, когда закончите.",
        reply_markup=cancel_keyboard
    )

async def exit_audio_mode(message: types.Message, state: FSMContext):
    data = await state.get_data()
    session_id = data.get("session_id", await get_current_session(message.from_user.id))
    await state.update_data(session_id=session_id)
    await state.set_state(None)
    await message.answer(
        "Вы вышли из режима генерации аудиоответов.",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.message(F.text == "🔊 Перевести в голос")
async def start_text_to_voice(message: types.Message, state: FSMContext):
    await state.set_state(UserStates.awaiting_text_to_voice)
    await message.answer(
        "Отправьте текст, который нужно перевести в голосовой ответ.\n\n"
        "Нажмите 👉 Выход, когда закончите.",
        reply_markup=cancel_keyboard
    )

@dp.message(UserStates.awaiting_text_to_voice, F.text)
async def handle_text_to_voice(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    text = message.text.strip()
    
    if text == "👉 Выход":
        await state.clear()
        await message.answer(
            "Вы вышли из режима перевода текста в голос.",
            reply_markup=get_main_keyboard(user_id, is_admin=user_id in ADMIN_IDS)
        )
        return

    if not text:
        await message.answer(
            "Пожалуйста, введите текст для перевода в голос.",
            reply_markup=cancel_keyboard
        )
        return

    msg = await message.answer("🔊 Генерация голосового ответа...")

    try:
        # Определяем язык на основе текста
        language = "ru" if any(c in text.lower() for c in "абвгдеёжзийклмнопрстуфхцчшщъыьэюя") else "en"
        
        # Генерируем голосовой файл
        async with temp_audio_file(user_id, text) as audio_path:
            audio_path = await generate_voice(text, language)
            if not audio_path:
                logger.error(f"Failed to generate voice for user_id={user_id}: No audio path returned")
                await msg.delete()
                await message.answer(
                    "Не удалось сгенерировать голосовой ответ. Попробуйте другой текст.",
                    reply_markup=cancel_keyboard
                )
                return
                
            await bot.send_audio(
                chat_id=message.chat.id,
                audio=FSInputFile(audio_path),
                caption="🎧 Ваш голосовой ответ готов!",
                reply_markup=cancel_keyboard
            )
        
        # Сохраняем в историю
        try:
            await db.connection.execute(
                "INSERT INTO history (user_id, message, reply, session_id) VALUES (?, ?, ?, ?)",
                (user_id, text, "Голосовой ответ сгенерирован", (await state.get_data()).get("session_id", 1))
            )
            # Пытаемся обновить audio_requests, но не прерываем выполнение при ошибке
            try:
                await db.connection.execute(
                    "UPDATE profiles SET audio_requests = audio_requests + 1 WHERE user_id = ?",
                    (user_id,)
                )
            except aiosqlite.OperationalError as e:
                if "no such column: audio_requests" in str(e):
                    logger.warning(f"Column audio_requests not found for user_id={user_id}, skipping update")
                else:
                    logger.error(f"Database error for user_id={user_id}: {e}")
            await db.connection.commit()
        except Exception as e:
            logger.error(f"Error saving to history for user_id={user_id}: {e}")
            # Не отправляем сообщение об ошибке, так как аудио уже отправлено
        
        await msg.delete()
        
    except Exception as e:
        logger.error(f"Unexpected error generating voice for user_id={user_id}: {e}")
        await msg.delete()
        await message.answer(
            "Не удалось сгенерировать голосовой ответ. Попробуйте другой текст.",
            reply_markup=cancel_keyboard
        )

@dp.message(F.text == "🖌 Вариации изображения")
async def start_image_variations(message: types.Message, state: FSMContext):
    await state.set_state(UserStates.awaiting_image_variations)
    await message.answer(
        "📷 Пожалуйста, загрузите изображение для создания вариаций.\n\n"
        "Нажмите 👉 Выход, когда закончите.",
        reply_markup=cancel_keyboard
    )

@dp.message(UserStates.awaiting_image_variations, F.photo)
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def handle_image_variations(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    photo = max(message.photo, key=lambda p: p.width * p.height)
    
    msg = await message.answer("🖌 Создаю вариации изображения...")

    try:
        file = await bot.get_file(photo.file_id)
        file_path = file.file_path
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}") as resp:
                if resp.status != 200:
                    raise aiohttp.ClientError(f"Failed to download photo, status: {resp.status}")
                image_data = await resp.read()
        
        temp_image_path = Path(IMAGES_DIR) / f"original_{user_id}_{uuid.uuid4().hex}.png"
        with open(temp_image_path, "wb") as f:
            f.write(image_data)
        
        variations = await create_image_variations(temp_image_path, user_id)
        
        TELEGRAM_MAX_PHOTO_SIZE = 10 * 1024 * 1024  # 10MB
        media_group = []
        for i, variation_path in enumerate(variations):
            # Check file size
            file_size = variation_path.stat().st_size
            logger.info(f"Variation #{i} for user_id={user_id}, file={variation_path}, size={file_size} bytes")
            if file_size > TELEGRAM_MAX_PHOTO_SIZE:
                logger.error(f"Variation #{i} for user_id={user_id} is too large: {file_size} bytes")
                raise ValueError(f"Variation #{i} exceeds Telegram's 10MB limit: {file_size} bytes")
            media_group.append(types.InputMediaPhoto(media=FSInputFile(variation_path), caption=f"Вариация #{i+1}"))

        if media_group:
            try:
                await bot.send_media_group(
                    chat_id=message.chat.id,
                    media=media_group
                )
                logger.info(f"Successfully sent media group with {len(media_group)} variations for user_id={user_id}")
            except Exception as e:
                logger.error(f"Failed to send media group for user_id={user_id}: {e}")
                # Optionally, try sending one by one as a fallback or just raise the error
                raise # Re-raise to indicate failure
        
        await db.connection.execute(
            "INSERT INTO history (user_id, message, reply, session_id) VALUES (?, ?, ?, ?)",
            (user_id, "Создание вариаций изображения", f"Сгенерировано {len(variations)} вариаций", (await state.get_data()).get("session_id", 1))
        )
        await db.connection.execute(
            "UPDATE profiles SET image_requests = image_requests + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.connection.commit()
        await msg.delete()
        
        # Send the cancel keyboard separately if needed, as send_media_group doesn't take reply_markup for the whole group
        await message.answer(
            "Вариации готовы! Нажмите 👉 Выход, если закончили.",
            reply_markup=cancel_keyboard
        )
        # Clean up original image
        if temp_image_path.exists():
            temp_image_path.unlink()
    except Exception as e:
        logger.error(f"Error creating image variations for user_id={user_id}: {e}")
        await msg.delete()
        await message.answer(
            f"⚠️ Ошибка создания вариаций изображения: {str(e)}. Попробуйте позже.",
            reply_markup=cancel_keyboard
        )
        raise  # Re-raise for retry logic

@dp.message(F.text == "🖼️ Сгенерировать (админ)")
async def admin_generate_image(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("⚠️ У вас нет прав для использования этой функции.")
        return

    await state.set_state(UserStates.awaiting_admin_image_description)
    await message.answer(
        "Введите описание для генерации изображения (админский режим):",
        reply_markup=cancel_keyboard
    )

@dp.message(UserStates.awaiting_admin_image_description, F.text)
async def process_admin_image_prompt(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    prompt = message.text.strip()

    if not prompt:
        await message.answer("Описание не может быть пустым.")
        return

    msg = await message.answer("🖼️ Генерирую изображение (админ)...")

    try:
        async with aiohttp.ClientSession() as session:
            client = AsyncClient(provider=g4f.Provider.ImageLabs)
            client.session = session
            response = await client.images.generate(
                prompt=prompt,
                model="sdxl-turbo",
                response_format="url"
            )

        if not response.data or not response.data[0].url:
            raise ValueError("Не удалось получить URL изображения.")

        image_url = response.data[0].url
        await bot.send_photo(
            chat_id=message.chat.id,
            photo=image_url,
            reply_markup=cancel_keyboard
        )
        await msg.delete()
    except aiohttp.ClientError as e:
        logger.error(f"Ошибка сети при генерации изображения (админ): {e}")
        await msg.delete()
        await message.answer(
            f"⚠️ Ошибка сети: {e}",            
            reply_markup=cancel_keyboard
        )
    except g4f.errors.ResponseError as e:
        logger.error(f"Ошибка от провайдера при генерации изображения (админ): {e}")
        await msg.delete()
        await message.answer(
            f"⚠️ Ошибка от провайдера: {e}",
            reply_markup=cancel_keyboard
        )
    except Exception as e:
        logger.error(f"Неизвестная ошибка при генерации изображения (админ): {e}")
        await msg.delete()
        await message.answer(
            f"⚠️ Неизвестная ошибка: {e}",
            reply_markup=cancel_keyboard
        )

@dp.message(F.text == "📢 Рассылка")
async def broadcast_message(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("⚠️ У вас нет прав для рассылки сообщений.")
        return

    await message.answer(
        "Введите сообщение для рассылки:",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state("awaiting_broadcast_message")

@dp.message(UserStates.awaiting_broadcast_message)
async def process_broadcast_message(message: types.Message, state: FSMContext):
    broadcast_text = message.text
    if not broadcast_text:
        await message.answer("Сообщение не может быть пустым.")
        await state.clear()
        return

    cursor = await db.connection.execute("SELECT user_id FROM profiles")
    user_ids = [row[0] for row in await cursor.fetchall()]

    successful = 0
    failed = 0
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, broadcast_text)
            successful += 1
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Failed to send message to {user_id}: {e}")
            failed += 1

    await message.answer(f"Рассылка завершена. Успешно отправлено: {successful}, не удалось: {failed}.", reply_markup=get_main_keyboard(message.from_user.id))
    await state.clear()

@dp.message(F.text == "📊 Статистика")
async def show_admin_stats(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("⚠️ У вас нет прав для просмотра статистики.")
        return

    try:
        cursor = await db.connection.execute("""
            SELECT date, action_type, model_name, SUM(count) as total_count
            FROM user_stats
            GROUP BY date, action_type, model_name
            ORDER BY date DESC, action_type, model_name
            LIMIT 50
        """)
        rows = await cursor.fetchall()

        if not rows:
            await message.answer("Статистика пока пуста.")
            return

        stats_text = "📊 <b>Статистика использования</b>\n\n"
        for row in rows:
            date, action_type, model_name, total_count = row
            stats_text += (
                f"<b>Дата:</b> {date}\n"
                f"<b>Тип запроса:</b> {action_type}\n"
                f"<b>Модель:</b> {model_name}\n"
                f"<b>Количество:</b> {total_count}\n\n"
            )

        await message.answer(stats_text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error fetching admin stats: {e}", exc_info=True)
        await message.answer(
            "⚠️ Произошла ошибка при получении статистики."
        )

@dp.message(F.text == "👥 Активность пользователей")
async def show_user_activity(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("⚠️ У вас нет прав для просмотра активности пользователей.")
        return

    try:
        cursor = await db.connection.execute("""
            SELECT p.user_id, p.name, p.gpt_requests, p.image_requests, p.audio_requests, p.created_at, MAX(s.date) as last_activity
            FROM profiles p
            LEFT JOIN user_stats s ON p.user_id = s.user_id
            GROUP BY p.user_id
            ORDER BY last_activity DESC
            LIMIT 50
        """)
        rows = await cursor.fetchall()

        if not rows:
            await message.answer("Активность пользователей пока отсутствует.")
            return

        activity_text = "👥 <b>Активность пользователей</b>\n\n"
        for row in rows:
            user_id, name, gpt_requests, image_requests, audio_requests, created_at, last_activity = row
            activity_text += (
                f"<b>Пользователь:</b> {name} (ID: {user_id})\n"
                f"<b>GPT-запросов:</b> {gpt_requests}\n"
                f"<b>Изображений:</b> {image_requests}\n"
                f"<b>Аудио:</b> {audio_requests}\n"
                f"<b>Дата регистрации:</b> {created_at[:10]}\n"
                f"<b>Последняя активность:</b> {last_activity or 'Неизвестно'}\n\n"
            )

        await message.answer(activity_text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Error fetching user activity: {e}", exc_info=True)
        await message.answer(
            "⚠️ Произошла ошибка при получении активности пользователей."
        )

@dp.message(F.text == "🛠 Управление моделями")
async def manage_models(message: types.Message):
    user_id = message.from_user.id
    if user_id not in ADMIN_IDS:
        await message.answer("⚠️ У вас нет прав для управления моделями.", reply_markup=get_main_keyboard(user_id))
        return

    try:
        cursor = await db.connection.execute("SELECT id, name, provider, is_active FROM models ORDER BY name")
        models = await cursor.fetchall()

        if not models:
            await message.answer(
                "Модели отсутствуют в базе данных.",
                reply_markup=get_main_keyboard(user_id, is_admin=True)
            )
            return

        await message.answer(
            "🛠 <b>Управление моделями</b>\n\nВыберите модель для изменения статуса:",
            parse_mode="HTML",
            reply_markup=get_manage_models_keyboard(models),
            reply_to_message_id=message.message_id
        )

    except Exception as e:
        logger.error(f"Error fetching models for user_id={user_id}: {e}", exc_info=True)
        await message.answer(
            "⚠️ Произошла ошибка при получении списка моделей.",
            reply_markup=get_main_keyboard(user_id, is_admin=True)
        )

@dp.callback_query(F.data.startswith("toggle_model_"))
async def toggle_model_status(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    if user_id not in ADMIN_IDS:
        await callback.answer("⚠️ У вас нет прав для управления моделями.", show_alert=True)
        return

    try:
        model_id = int(callback.data.split("_")[-1])
        cursor = await db.connection.execute("SELECT is_active FROM models WHERE id = ?", (model_id,))
        row = await cursor.fetchone()
        if not row:
            await callback.answer("⚠️ Модель не найдена.", show_alert=True)
            return

        new_status = 0 if row[0] else 1
        await db.connection.execute(
            "UPDATE models SET is_active = ? WHERE id = ?",
            (new_status, model_id)
        )
        await db.connection.commit()

        cursor = await db.connection.execute("SELECT id, name, provider, is_active FROM models ORDER BY name")
        models = await cursor.fetchall()

        await callback.message.edit_text(
            "🛠 <b>Управление моделями</b>\n\nВыберите модель для изменения статуса:",
            parse_mode="HTML",
            reply_markup=get_manage_models_keyboard(models)
        )
        await callback.answer(f"Статус модели изменён на {'активна' if new_status else 'неактивна'}.", show_alert=True)
        
        await callback.message.answer("Выберите действие:", reply_markup=get_main_keyboard(user_id, is_admin=True))
    except Exception as e:
        logger.error(f"Error toggling model status for user_id={user_id}: {e}", exc_info=True)
        await callback.message.answer(
            "⚠️ Произошла ошибка при изменении статуса модели.",
            reply_markup=get_main_keyboard(user_id, is_admin=True)
        )

@dp.message(UserStates.awaiting_audio, F.text)
async def handle_audio_response(message: types.Message, state: FSMContext):
    try:
        user = message.from_user
        if not user:
            raise AttributeError("User object is None")
        text = message.text.strip()
        if not text:
            raise ValueError("Message text is empty")
        
        if text == "👉 Выход":
            await exit_audio_mode(message, state)
            return

        msg = await message.answer("🎙 Генерация аудиоответа...")

        try:
            data = await state.get_data()
            session_id = data.get("session_id", await get_current_session(user.id))
            
            prev_msgs = await fetch_user_history(user.id, session_id)
            
            messages = []
            for msg, reply in prev_msgs[-10:]:
                messages.extend([
                    {"role": "user", "content": msg},
                    {"role": "assistant", "content": reply}
                ])
            messages.append({"role": "user", "content": text})
            
            current_model = "gpt-4o"
            
            async with aiohttp.ClientSession() as session:
                g4f_client.session = session
                response = await g4f_client.chat.completions.create(
                    model=current_model,
                    messages=messages,
                    temperature=0.7,
                    max_tokens=2000
                )
            
            if not response.choices:
                raise ValueError("Empty response from GPT")
            
            text_response = response.choices[0].message.content
            
            async with temp_audio_file(user.id, text) as audio_path:
                client = AsyncClient(provider=g4f.Provider.PollinationsAI)
                async with aiohttp.ClientSession() as session:
                    client.session = session
                    audio_response = await client.chat.completions.create(
                        model="openai-audio",
                        messages=[{"role": "user", "content": text_response}],
                        audio={"voice": "alloy", "format": "mp3"},
                    )
                audio_response.choices[0].message.save(str(audio_path))
                await bot.send_audio(
                    chat_id=message.chat.id,
                    audio=FSInputFile(audio_path),
                    caption="🎧 Вот ваш аудиоответ!",
                    reply_markup=cancel_keyboard
                )
            
            await db.connection.execute(
                "INSERT INTO history (user_id, message, reply, session_id) VALUES (?, ?, ?, ?)",
                (user.id, text, text_response, session_id)
            )
            await db.connection.commit()
            
            await state.update_data(session_id=session_id)
            await msg.delete()
            
        except aiohttp.ClientError as e:
            logger.error(f"Network error generating audio for user_id={user.id}: {e}", exc_info=True)
            await msg.delete()
            await message.answer(
                "⚠️ Ошибка сети. Попробуйте позже.",
                reply_markup=cancel_keyboard
            )
        except g4f.Provider.ProviderError as e:
            logger.error(f"Provider error generating audio for user_id={user.id}: {e}", exc_info=True)
            await msg.delete()
            await message.answer(
                f"⚠️ Ошибка провайдера: {str(e)}",
                reply_markup=cancel_keyboard
            )
        except Exception as e:
            logger.error(f"Unexpected error generating audio for user_id={user.id}: {e}", exc_info=True)
            await msg.delete()
            await message.answer(
                "⚠️ Неизвестная ошибка. Попробуйте позже.",
                reply_markup=cancel_keyboard
            )
    except AttributeError as e:
        logger.error(f"AttributeError in handle_audio_response: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error in handle_audio_response: {e}")
        raise

@dp.message(F.text == "🎨 Генерация изображения")
async def start_image_generation(message: types.Message, state: FSMContext):
    await state.set_state(UserStates.awaiting_image)
    logger.info(f"Set state to awaiting_image for user_id={message.from_user.id}")
    await message.answer(
        "Отправь описание для генерации изображения.\n"
        "Постарайтесь максимально составить описание для наилучшего эффекта.\n\n"
        "Нажмите 👉 Выход, когда закончите.",
        reply_markup=cancel_keyboard
    )

@dp.message(UserStates.awaiting_image, F.text)
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def handle_image_generation(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    prompt = message.text.strip()
    
    logger.info(f"Generating image for user_id={user_id} with prompt: {prompt[:50]}...")

    if prompt == "👉 Выход":
        await state.clear()
        await message.answer(
            "Вы вышли из режима генерации изображений.",
            reply_markup=get_main_keyboard(user_id)
        )
        return

    if not prompt:
        await message.answer(
            "Пожалуйста, введите описание для изображения.",
            reply_markup=cancel_keyboard
        )
        return

    forbidden_keywords = ["обнажённая", "nude", "naked", "adult"]
    if any(keyword.lower() in prompt.lower() for keyword in forbidden_keywords):
        await message.answer(
            "⚠️ Запрос содержит недопустимый контент.\n\n"
            "Пожалуйста, измените ваш запрос, чтобы он соответствовал правилам.\n\n"
            "Примеры допустимых запросов:\n"
            "• 'Красивый закат на пляже'\n"
            "• 'Кот в шляфе, цифровая живопись'\n"
            "• 'Футуристический город ночью'",
            reply_markup=cancel_keyboard
        )
        return

    msg = await message.answer("🎨 Генерация изображения...\nМне нужно немного времени⌛.\nСкоро выведу результат👇")

    try:
        async with aiohttp.ClientSession() as session:
            client = AsyncClient(provider=g4f.Provider.ARTA)
            client.session = session
            response = await client.images.generate(
                model="yamers_realistic_xl" if "yamers_realistic_xl" in [m[1] for m in g4f.Provider.ARTA.models] else "realistic_stock_xl",
                prompt=prompt,
                response_format="url",
            )
        logger.info(f"Image generation response: {response}")
        
        if not hasattr(response, 'data') or not response.data:
            logger.error("Response missing 'data' attribute or data is empty")
            raise ValueError("Invalid response structure from provider")
        if not hasattr(response.data[0], 'url'):
            logger.error("Response data[0] missing 'url' attribute")
            raise ValueError("Invalid response structure from provider")
            
        await bot.send_photo(
            chat_id=message.chat.id,
            photo=response.data[0].url,
            reply_markup=cancel_keyboard
        )
        await db.connection.execute(
            "UPDATE profiles SET image_requests = image_requests + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.connection.execute(
            "INSERT INTO history (user_id, message, reply, session_id) VALUES (?, ?, ?, ?)",
            (user_id, prompt, "Изображение сгенерировано", (await state.get_data()).get("session_id", 1))
        )
        await db.connection.commit()
        await msg.delete()
    except aiohttp.ClientError as e:
        logger.error(f"Network error generating image for user_id={user_id}: {e}", exc_info=True)
        await msg.delete()
        await message.answer(
            "⚠️ Ошибка сети. Попробуйте позже.",
            reply_markup=cancel_keyboard
        )
    except ResponseError as e:
        logger.error(f"ResponseError generating image for user_id={user_id}: {e}", exc_info=True)
        error_message = str(e)
        if "Invalid prompts detected" in error_message or "error_code\":769" in error_message:
            await msg.delete()
            await message.answer(
                "⚠️ Извините, мне запрещено генерировать изображения по такому запросу.\n\n"
                "Пожалуйста, измените ваш запрос, чтобы он не содержал запрещённых тем или формулировок.\n\n"
                "Примеры допустимых запросов:\n"
                "• 'Красивый закат на пляже'\n"
                "• 'Кот в шляфе, цифровая живопись'\n"
                "• 'Футуристический город ночью'",
                reply_markup=cancel_keyboard
            )
        else:
            await msg.delete()
            await message.answer(
                f"⚠️ Ошибка провайдера: {error_message}",
                reply_markup=cancel_keyboard
            )
        raise
    except g4f.Provider.ProviderError as e:
        logger.error(f"Provider error generating image for user_id={user_id}: {e}", exc_info=True)
        error_message = str(e)
        if "Invalid prompts detected" in error_message or "error_code\":769" in error_message:
            await msg.delete()
            await message.answer(
                "⚠️ Извините, мне запрещено генерировать изображения по такому запросу.\n\n"
                "Пожалуйста, измените ваш запрос, чтобы он не содержал запрещённых тем или формулировок.\n\n"
                "Примеры допустимых запросов:\n"
                "• 'Красивый закат на пляже'\n"
                "• 'Кот в шляфе, цифровая живопись'\n"
                "• 'Футуристический город ночью'",
                reply_markup=cancel_keyboard
            )
        else:
            await msg.delete()
            await message.answer(
                f"⚠️ Ошибка провайдера: {error_message}",
                reply_markup=cancel_keyboard
            )
        raise
    except ValueError as e:
        logger.error(f"ValueError in image generation for user_id={user_id}: {e}", exc_info=True)
        await msg.delete()
        await message.answer(
            "⚠️ Ошибка обработки ответа от провайдера. Возможно, запрос содержит недопустимый контент.\n\n"
            "Пожалуйста, измените ваш запрос, чтобы он соответствовал правилам.\n\n"
            "Примеры допустимых запросов:\n"
            "• 'Красивый закат на пляже'\n"
            "• 'Кот в шляфе, цифровая живопись'\n"
            "• 'Футуристический город ночью'",
            reply_markup=cancel_keyboard
        )
    except AttributeError as e:
        logger.error(f"AttributeError in image generation for user_id={user_id}: {e}", exc_info=True)
        await msg.delete()
        await message.answer(
            "⚠️ Ошибка обработки ответа от провайдера. Возможно, запрос содержит недопустимый контент.\n\n"
            "Пожалуйста, измените ваш запрос, чтобы он соответствовал правилам.\n\n"
            "Примеры допустимых запросов:\n"
            "• 'Красивый закат на пляже'\n"
            "• 'Кот в шляфе, цифровая живопись'\n"
            "• 'Футуристический город ночью'",
            reply_markup=cancel_keyboard
        )
    except Exception as e:
        logger.error(f"Unexpected error generating image for user_id={user_id}: {e}", exc_info=True)
        await msg.delete()
        await message.answer(
            "⚠️ Неизвестная ошибка. Попробуйте позже.",
            reply_markup=cancel_keyboard
        )

@dp.message(F.text == "👤 Профиль")
async def show_profile(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    cursor = await db.connection.execute("""
        SELECT name, gpt_requests, image_requests, audio_requests, created_at
        FROM profiles WHERE user_id = ?
    """, (user_id,))
    row = await cursor.fetchone()
    
    if not row:
        await ensure_profile(message.from_user)
        cursor = await db.connection.execute("""
            SELECT name, gpt_requests, image_requests, audio_requests, created_at
            FROM profiles WHERE user_id = ?
        """, (user_id,))
        row = await cursor.fetchone()
    
    name, gpt_count, img_count, audio_count, created_at_val = row
    current_model = await get_user_model(user_id)
    
    # Более надежная проверка перед срезом
    created_at_str = created_at_val[:19] if created_at_val and isinstance(created_at_val, str) else "Неизвестно"
    await message.answer(
        f"<b>👤 Профиль</b>\n\n"
        f"<b>Имя:</b> {name}\n"
        f"<b>ID:</b> <code>{user_id}</code>\n"
        f"<b>🧠 GPT-запросов:</b> {gpt_count}\n"
        f"<b>🖼 Изображений:</b> {img_count}\n"
        f"<b>🎧 Аудио:</b> {audio_count}\n"
        f"<b>Текущая модель:</b> {current_model}\n"
        f"<b>С ботом с:</b> {created_at_str}",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(user_id, is_admin=user_id in ADMIN_IDS))

@dp.message(F.text == "🔄 Новый чат")
async def new_chat(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    await state.clear()
    
    new_session = await get_current_session(user_id)
    await state.update_data(session_id=new_session)
    
    await state.update_data(history_message_ids=[])
    
    await message.answer(
        "🔄 <b>Новый чат начат</b>\n"
        "Контекст предыдущего диалога полностью очищен.\n\n"
        "Можете задать новый вопрос.",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(user_id, is_admin=user_id in ADMIN_IDS)
    )

@dp.message(F.text == "🌐 Поиск в интернете")
async def start_web_search(message: types.Message, state: FSMContext):
    await state.set_state(UserStates.awaiting_search_query)
    await message.answer(
        "🔍 Введите запрос для поиска в интернете:",
        reply_markup=cancel_keyboard
    )

async def exit_search_mode(message: types.Message, state: FSMContext):
    data = await state.get_data()
    session_id = data.get("session_id", await get_current_session(message.from_user.id))
    await state.update_data(session_id=session_id)
    await state.set_state(None)
    await message.answer(
        "Вы вышли из режима поиска в интернете.",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.message(UserStates.awaiting_search_query, F.text)
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
async def handle_web_search(message: types.Message, state: FSMContext):
    if message.text == "👉 Выход":
        await exit_search_mode(message, state)
        return
        
    search_query = message.text.strip()
    user_id = message.from_user.id
    
    if not search_query:
        await message.answer(
            "Пожалуйста, введите запрос для поиска.",
            reply_markup=cancel_keyboard
        )
        return
    
    msg = await message.answer("🔍 Выполняю поиск...\nМне нужно немного времени⌛.\nСкоро выведу результат👇")

    try:
        async with aiohttp.ClientSession() as session:
            g4f_client.session = session
            response = await g4f_client.chat.completions.create(
                model="gpt-4",
                messages=[{"role": "user", "content": search_query}],
                tool_calls=[{
                    "function": {
                        "arguments": {
                            "query": search_query,
                            "max_results": 5,
                            "max_words": 2500,
                            "backend": "auto",
                            "add_text": True,
                            "timeout": 5
                        },
                        "name": "search_tool"
                    },
                    "type": "function"
                }]
            )

        if response.choices and response.choices[0].message.content:
            search_results = response.choices[0].message.content
            await message.answer(
                f"🌐 Результаты поиска:\n\n{search_results}",
                reply_markup=cancel_keyboard,
                parse_mode="HTML"
            )
            
            data = await state.get_data()
            session_id = data.get("session_id", await get_current_session(user_id))
            logger.debug(f"Saving search to history: user_id={user_id}, query={search_query}, results={search_results[:50]}..., session_id={session_id}")
            await db.connection.execute(
                "INSERT INTO history (user_id, message, reply, session_id) VALUES (?, ?, ?, ?)",
                (user_id, search_query, search_results, session_id)
            )
            await db.connection.commit()
            
            await state.update_data(session_id=session_id)
            await msg.delete()
        else:
            await msg.delete()
            await message.answer("Не удалось получить результаты поиска. Попробуйте другой запрос.")

    except aiohttp.ClientError as e:
        logger.error(f"Network error performing search for user_id={user_id}: {e}", exc_info=True)
        await msg.delete()
        await message.answer("⚠️ Ошибка сети. Попробуйте позже.", reply_markup=cancel_keyboard)
    except Exception as e:
        logger.error(f"Error performing search for user_id={user_id}: {e}", exc_info=True)
        await msg.delete()
        await message.answer(f"⚠️ Ошибка поиска: {str(e)}", reply_markup=cancel_keyboard)

@dp.message(F.photo)
async def handle_uploaded_photo(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    logger.info(f"[handle_uploaded_photo] Получено фото от user_id={user_id}")

    # Сбрасываем флаг "обрабатывается", если застрял
    data = await state.get_data()
    if data.get("processing_photo"):
        logger.warning(f"[handle_uploaded_photo] Обнаружено зависшее состояние обработки, сбрасываю.")
        await state.update_data(processing_photo=False)

    try:
        photo = max(message.photo, key=lambda p: p.width * p.height)
        photo_file_id = photo.file_id

        await state.update_data(photo_file_id=photo_file_id)
        await state.update_data(processing_photo=True)

        logger.info(f"[handle_uploaded_photo] Сохранён photo_file_id={photo_file_id}")

        await state.set_state(UserStates.awaiting_image_prompt)
        await message.answer(
            "📷 Вы отправили фото. Что вы хотите с ним сделать?\n"
            "Например: 'Опиши, что на фото', 'Проанализируй изображение', 'Сделай описание в стиле фэнтези'.",
            reply_markup=get_main_keyboard(user_id, is_admin=user_id in ADMIN_IDS)
        )
    except Exception as e:
        logger.error(f"[handle_uploaded_photo] Ошибка при обработке фото от user_id={user_id}: {e}")
        await message.answer(
            "⚠️ Не удалось обработать фото. Пожалуйста, попробуйте снова.",
            reply_markup=get_main_keyboard(user_id)
        )


@dp.message(UserStates.awaiting_image_prompt, F.text)
async def handle_image_prompt(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    prompt = message.text.strip()
    
    if not prompt:
        await state.clear()
        await state.update_data(processing_photo=False)
        await message.answer(
            "Пожалуйста, укажите, что нужно сделать с изображением.",            
            reply_markup=get_main_keyboard(user_id)
        )
        return

    data = await state.get_data()
    photo_file_id = data.get("photo_file_id")
    if not photo_file_id:
        logger.warning(f"photo_file_id not found for user_id={user_id}, prompt={prompt}")
        await state.clear()
        await state.update_data(processing_photo=False)
        await message.answer(
            "⚠️ Ошибка: изображение не найдено. Пожалуйста, загрузите фото заново.",            
            reply_markup=get_main_keyboard(user_id)
        )
        return

    msg = await message.answer("📷 Обрабатываю изображение...\nМне нужно немного времени⌛.\nСкоро выведу результат👇")

    try:
        file = await bot.get_file(photo_file_id)
        file_path = file.file_path
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}") as resp:
                if resp.status != 200:
                    raise aiohttp.ClientError(f"Failed to download photo, status: {resp.status}")
                image_data = await resp.read()

        session_id = data.get("session_id", await get_current_session(user_id))
        prev_msgs = await fetch_user_history(user_id, session_id)
        
        context = ""
        for msg, reply in prev_msgs[-5:]:
            context += f"User: {msg}\nAssistant: {reply}\n"
        if context:
            full_prompt = f"Previous conversation:\n{context}\nCurrent request: {prompt}"
        else:
            full_prompt = prompt

        async with aiohttp.ClientSession() as session:
            g4f_client.session = session
            response = await g4f_client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": full_prompt}],
                image=image_data
            )

        if not response.choices:
            raise ValueError("Empty response from GPT")

        description = response.choices[0].message.content

        await db.connection.execute(
            "INSERT INTO history (user_id, message, reply, session_id) VALUES (?, ?, ?, ?)",
            (user_id, f"Обработка изображения: {prompt}", description, session_id)
        )
        await db.connection.execute(
            "UPDATE profiles SET gpt_requests = gpt_requests + 1 WHERE user_id = ?",
            (user_id,)
        )
        await db.connection.commit()

        await message.answer(
            f"📷 Результат обработки:\n\n{description}",
            reply_markup=get_main_keyboard(user_id, is_admin=user_id in ADMIN_IDS),
            parse_mode="HTML"
        )
        await msg.delete()

    except aiohttp.ClientError as e:
        logger.error(f"Network error in image processing for user_id={user_id}: {e}", exc_info=True)
        await msg.delete()
        await message.answer(
            "⚠️ Ошибка сети. Попробуйте позже.",
            reply_markup=get_main_keyboard(user_id, is_admin=user_id in ADMIN_IDS)
        )
    except g4f.Provider.ProviderError as e:
        logger.error(f"Provider error in image processing for user_id={user_id}: {e}", exc_info=True)
        await msg.delete()
        await message.answer(
            f"⚠️ Ошибка провайдера: {str(e)}",
            reply_markup=get_main_keyboard(user_id)
        )
    except Exception as e:
        logger.error(f"Unexpected error in image processing for user_id={user_id}: {e}", exc_info=True)
        await msg.delete()
        await message.answer(
            "⚠️ Неизвестная ошибка. Попробуйте позже.",
            reply_markup=get_main_keyboard(user_id, is_admin=user_id in ADMIN_IDS)
        )
    finally:
        await state.update_data(processing_photo=False)
        await state.set_state(None)

@dp.message(F.video | F.document)
async def unsupported_media_handler(message: types.Message, state: FSMContext):
    await message.answer(
        "⚠️ Извините, я пока не умею работать с видео или файлами. "
        "Пожалуйста, отправьте текстовое сообщение или изображение.",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.callback_query(F.data.startswith("set_model_"))
async def set_model_callback(callback: types.CallbackQuery, state: FSMContext):
    model_id = int(callback.data.split("_")[-1])
    user_id = callback.from_user.id

    try:
        await db.connection.execute(
            "UPDATE user_settings SET model_id = ? WHERE user_id = ?",
            (model_id, user_id)
        )
        await db.connection.commit()

        data = await state.get_data()
        session_id = data.get("session_id", await get_current_session(user_id))
        await state.update_data(session_id=session_id)

        await callback.message.edit_text("✅ Модель успешно изменена!")
        await show_settings_from_query(callback)
        await callback.answer()

    except Exception as e:
        logger.error(f"Error updating model for user_id={user_id}: {e}", exc_info=True)
        await callback.message.edit_text("⚠️ Произошла ошибка при смене модели. Попробуйте позже.")
        await callback.answer()

@dp.callback_query(F.data == "back_to_settings")
async def back_to_settings_callback(callback: types.CallbackQuery):
    await show_settings_from_query(callback)

@dp.callback_query(F.data == "back_to_main")
async def back_to_main_callback(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    is_admin = user_id in ADMIN_IDS
    await state.clear()
    await callback.message.answer(
        "Вы находитесь в главном меню.",
        reply_markup=get_main_keyboard(user_id, is_admin=is_admin)
    )
    await callback.message.delete(reply_markup=cancel_keyboard)
    await callback.message.delete()
    await callback.answer()

@dp.callback_query(F.data.startswith("convert_to_voice_"))
async def handle_convert_to_voice(callback: types.CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    # callback.data is structured as "convert_to_voice_{USER_MESSAGE_ID}_{PART_INDEX}"
    # The text itself is stored in FSM context with a key like "{BOT_MESSAGE_ID}_{PART_INDEX}"
    # We need to reconstruct this key.

    try:
        # The last part of the callback_data string is the part_index
        part_index_str = callback.data.split("_")[-1]
    except IndexError:
        logger.error(f"Could not parse part_index from callback_data: {callback.data} for user_id={user_id}")
        await callback.message.edit_text("⚠️ Ошибка: неверный формат данных для кнопки.")
        await callback.answer()
        return

    # callback.message.message_id is the ID of the bot's message where the button was pressed.
    bot_message_id = callback.message.message_id
    
    # This is the key used when storing the text in handle_message
    fsm_storage_key = f"{bot_message_id}_{part_index_str}"

    # Retrieve the stored response from FSM context
    data = await state.get_data()
    responses = data.get("response_texts", {})
    text = responses.get(fsm_storage_key)

    if not text:
        logger.warning(f"Text not found for FSM key '{fsm_storage_key}'. Callback data: '{callback.data}'. Available keys in FSM: {list(responses.keys())} for user_id={user_id}")
        await callback.message.edit_text("⚠️ Не удалось найти текст для преобразования в голос.")
        await callback.answer()
        return

    msg = await callback.message.answer("🔊 Генерация голосового ответа...")

    try:
        # Determine language based on text content
        language = "ru" if any(c in text.lower() for c in "абвгдеёжзийклмнопрстуфхцчшщъыьэюя") else "en"

        # Generate voice file
        async with temp_audio_file(user_id, text) as audio_path:
            audio_path = await generate_voice(text, language)
            if not audio_path:
                logger.error(f"Failed to generate voice for user_id={user_id}: No audio path returned")
                await msg.delete()
                await callback.message.answer(
                    "Не удалось сгенерировать голосовой ответ. Попробуйте позже.",
                    reply_markup=get_main_keyboard(user_id, is_admin=user_id in ADMIN_IDS)
                )
                return

            await bot.send_audio(
                chat_id=callback.message.chat.id,
                audio=FSInputFile(audio_path),
                caption="🎧 Ваш голосовой ответ готов!",
                reply_markup=get_main_keyboard(user_id, is_admin=user_id in ADMIN_IDS)
            )

        # Save to history
        try:
            session_id = data.get("session_id", await get_current_session(user_id))
            await db.connection.execute(
                "INSERT INTO history (user_id, message, reply, session_id) VALUES (?, ?, ?, ?)",
                (user_id, "Перевод ответа в голос", "Голосовой ответ сгенерирован", session_id)
            )
            await db.connection.execute(
                "UPDATE profiles SET audio_requests = audio_requests + 1 WHERE user_id = ?",
                (user_id,)
            )
            await db.connection.commit()
        except Exception as e:
            logger.error(f"Error saving voice conversion to history for user_id={user_id}: {e}")

        await msg.delete()
        await callback.message.edit_reply_markup(reply_markup=None)  # Remove the inline button
        await callback.answer()

    except Exception as e:
        logger.error(f"Unexpected error generating voice for user_id={user_id}: {e}")
        await msg.delete()
        await callback.message.answer(
            "Не удалось сгенерировать голосовой ответ. Попробуйте позже.",
            reply_markup=get_main_keyboard(user_id, is_admin=user_id in ADMIN_IDS)
        )
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer()

@dp.message(F.text)
async def handle_message(message: types.Message, state: FSMContext):
    if await state.get_state() is not None:
        return
        
    user = message.from_user
    text = message.text.strip()
    user_id = user.id

    if text.startswith('/'):
        return
        
    logger.info(f"New message from {user_id}: {text[:100]}...")

    try:
        await ensure_profile(user)
        
        data = await state.get_data()
        session_id = data.get("session_id")
        if not session_id:
            session_id = await get_current_session(user_id)
            await state.update_data(session_id=session_id)
        
        await bot.send_chat_action(message.chat.id, "typing")
        
        prev_msgs = await fetch_user_history(user_id, session_id)
        
        messages = []
        for msg, reply in prev_msgs[-10:]:
            messages.extend([
                {"role": "user", "content": msg},
                {"role": "assistant", "content": reply}
            ])
        messages.append({"role": "user", "content": text})
        
        current_model = await get_user_model(user_id)
        
        msg = await message.answer("💬 Обрабатываю запрос...")
        
        async with aiohttp.ClientSession() as session:
            g4f_client.session = session
            response = await g4f_client.chat.completions.create(
                model=current_model,
                messages=messages,
                temperature=0.7,
                max_tokens=2000
            )
        
        if not response.choices:
            raise ValueError("Empty response from GPT")
            
        full_reply = response.choices[0].message.content
        
        await save_message_to_history(user_id, text, full_reply, session_id)
        
        try:
            await msg.delete()
        except:
            pass
            
        # Store responses for voice conversion
        response_texts = data.get("response_texts", {})
        
        for i in range(0, len(full_reply), MAX_MESSAGE_LENGTH):
            part = full_reply[i:i + MAX_MESSAGE_LENGTH]
            if len(full_reply) > MAX_MESSAGE_LENGTH:
                part = f"({i//MAX_MESSAGE_LENGTH + 1}/{len(full_reply)//MAX_MESSAGE_LENGTH + 1})\n\n{part}"
                
            # Create inline keyboard with "Перевести в голос" button
            builder = InlineKeyboardBuilder()
            builder.add(InlineKeyboardButton(text="🔊 Перевести в голос", callback_data=f"convert_to_voice_{message.message_id}_{i//MAX_MESSAGE_LENGTH}"))
            
            sent_message = await message.answer(
                part,
                reply_markup=builder.as_markup(),
                parse_mode="HTML"
            )
            
            # Store the part of the response with a unique key
            response_texts[f"{sent_message.message_id}_{i//MAX_MESSAGE_LENGTH}"] = part
            await state.update_data(response_texts=response_texts)
        
        await db.connection.execute(
            "INSERT INTO user_stats (user_id, date, action_type, model_name, count) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, date, action_type, model_name) DO UPDATE SET count = count + 1",
            (user_id, str(datetime.now().date()), "text", current_model, 1)
        )
            
    except aiohttp.ClientError as e:
        logger.error(f"Network error: {e}", exc_info=True)
        await message.answer(
            "⚠️ Проблемы с подключением. Попробуйте позже.",
            reply_markup=get_main_keyboard(user_id))
            
    except g4f.Provider.ProviderError as e:
        logger.error(f"Provider error: {e}", exc_info=True)
        await message.answer(
            f"⚠️ Ошибка провайдера: {str(e)}",
            reply_markup=get_main_keyboard(user_id))
            
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        await message.answer(
            "⚠️ Произошла непредвиденная ошибка. Разработчики уже уведомлены.",
            reply_markup=get_main_keyboard(user_id))

async def main():
    logger.info("Starting bot...")
    await db.connect()
    await db.init_db()
    logger.info("Database initialized")
    try:
        logger.info("Starting polling...")
        await dp.start_polling(bot)
    finally:
        await db.close()
        logger.info("Bot stopped")

if __name__ == "__main__":
    asyncio.run(main())
