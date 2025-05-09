import os
import logging
import uuid
import asyncio
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
from config import DATABASE_PATH, MAX_MESSAGE_LENGTH, DEFAULT_MODEL
from keyboards import get_main_keyboard, get_cancel_keyboard, get_settings_keyboard, get_text_models_keyboard
from database import Database
from instructions import INSTRUCTION_TEXT
from g4f.errors import ResponseError

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
    logger.critical("TELEGRAM_BOT_TOKEN is not set in environment variables.")
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
        if hasattr(g4f_client, 'close'):
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

# Вспомогательные функции
async def ensure_profile(user: types.User):
    cursor = await db.connection.execute("SELECT 1 FROM profiles WHERE user_id = ?", (user.id,))
    if not await cursor.fetchone():
        await db.connection.execute(
            "INSERT INTO profiles (user_id, name) VALUES (?, ?)",
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
    media_dir = Path("generated_media")
    media_dir.mkdir(exist_ok=True)
    unique_id = uuid.uuid4().hex
    safe_text = "".join(c if c.isalnum() else "_" for c in text)[:50]
    audio_filename = f"audio udio_{user_id}_{safe_text}_{unique_id}.mp3"
    audio_path = media_dir / audio_filename
    try:
        yield audio_path
    finally:
        try:
            if audio_path.exists():
                audio_path.unlink()
        except Exception as e:
            logger.error(f"Failed to delete temporary audio file {audio_path}: {e}")

# Обработчики команд
@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    user = message.from_user
    await ensure_profile(user)
    await state.update_data(session_id=await get_current_session(user.id))
    
    await message.answer(
        f"""Привет, <b>{user.first_name}</b>! 👋\n\n
        Я — умный бот на основе GPT-4, готовый помочь с различными задачами.  

<b>Что я умею?</b>  
✨ Отвечать на вопросы по разным темам
🎙 Генерировать голосовые ответы
💻 Помогать с программированием и кодом
🎨 Генерировать изображения по описанию
🖼 Обрабатывать изображения
🎭 Поддерживать интерактивные сценарии 

<b>Как мной пользоваться?</b>  
Просто напиши мне сообщение — я постараюсь помочь!  
Для генерации изображения нажми кнопку <b>🎨 Генерация изображения</b>.
Для генерации аудиоответа нажми кнопку <b>"🎙 Ответ голосом"</b>.  
Для поиска в интернете нажми кнопку <b>"🌐 Поиск в интернете"</b>.
Для настройки параметров нажми кнопку <b>"⚙️ Настройки"</b>.
Для выхода из чата нажми кнопку <b>"👉 Выход"</b>.
Для просмотра истории сообщений нажми кнопку <b>"🕓 История"</b>.
Для просмотра своего профиля нажми кнопку <b>"👤 Профиль"</b>. 

Начнём? 😊""",
        reply_markup=get_main_keyboard(user.id),
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
async def exit_from_instructions(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state in [UserStates.awaiting_audio.state, 
                         UserStates.awaiting_image.state,
                         UserStates.awaiting_search_query.state,
                         UserStates.awaiting_prompt.state]:
        await state.clear()
        await message.answer(
            "Вы вернулись в главное меню",
            reply_markup=get_main_keyboard(message.from_user.id)
        )
    else:
        await state.clear()
        await message.answer(
            "Вы уже находитесь в главном меню",
            reply_markup=get_main_keyboard(message.from_user.id)
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
    cursor = await db.connection.execute("SELECT id, name FROM models ORDER BY name")
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
                reply_markup=get_main_keyboard(user_id))
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
            reply_markup=get_main_keyboard(user_id))
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

    await bot.send_message(
        chat_id=chat_id,
        text="🗑 История очищена!",
        reply_markup=get_main_keyboard(user_id))
    
    await state.update_data(history_message_ids=[])
    await callback.answer()

@dp.callback_query(F.data == "cancel_clear")
async def cancel_clear_history(callback: types.CallbackQuery, state: FSMContext):
    try:
        await callback.message.edit_text("❌ Удаление отменено.")
        await bot.send_message(
            chat_id=callback.message.chat.id,
            text="Вы можете продолжить общение.",
            reply_markup=get_main_keyboard(callback.from_user.id))
    except Exception as e:
        logger.error(f"Error cancelling history clear for user_id={callback.from_user.id}: {e}", exc_info=True)
        await bot.send_message(
            chat_id=callback.message.chat.id,
            text="❌ Удаление отменено.",
            reply_markup=get_main_keyboard(callback.from_user.id))
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
    await state.update_data(session_id=session_id)  # Сохраняем session_id
    await state.set_state(None)  # Выходим из состояния, не удаляя данные
    await message.answer(
        "Вы вышли из режима генерации аудиоответов.",
        reply_markup=get_main_keyboard(message.from_user.id)
    )

@dp.message(UserStates.awaiting_audio, F.text)
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
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
            
            await state.update_data(session_id=session_id)  # Сохраняем session_id
            await msg.delete()
            
        except aiohttp.ClientError as e:
            logger.error(f"Network error generating audio for user_id={user.id}: {e}", exc_info=True)
            await msg.edit_text(
                "⚠️ Ошибка сети. Попробуйте позже.",
                reply_markup=cancel_keyboard
            )
        except g4f.Provider.ProviderError as e:
            logger.error(f"Provider error generating audio for user_id={user.id}: {e}", exc_info=True)
            await msg.edit_text(
                f"⚠️ Ошибка провайдера: {str(e)}",
                reply_markup=cancel_keyboard
            )
        except Exception as e:
            logger.error(f"Unexpected error generating audio for user_id={user.id}: {e}", exc_info=True)
            await msg.edit_text(
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
                model="realistic_stock_xl",
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
        await msg.edit_text(
            "⚠️ Ошибка сети. Попробуйте позже.",
            reply_markup=cancel_keyboard
        )
    except ResponseError as e:
        logger.error(f"ResponseError generating image for user_id={user_id}: {e}", exc_info=True)
        error_message = str(e)
        if "Invalid prompts detected" in error_message or "error_code\":769" in error_message:
            await msg.edit_text(
                "⚠️ Извините, мне запрещено генерировать изображения по такому запросу.\n\n"
                "Пожалуйста, измените ваш запрос, чтобы он не содержал запрещённых тем или формулировок.\n\n"
                "Примеры допустимых запросов:\n"
                "• 'Красивый закат на пляже'\n"
                "• 'Кот в шляфе, цифровая живопись'\n"
                "• 'Футуристический город ночью'",
                reply_markup=cancel_keyboard
            )
        else:
            await msg.edit_text(
                f"⚠️ Ошибка провайдера: {error_message}",
                reply_markup=cancel_keyboard
            )
        raise
    except g4f.Provider.ProviderError as e:
        logger.error(f"Provider error generating image for user_id={user_id}: {e}", exc_info=True)
        error_message = str(e)
        if "Invalid prompts detected" in error_message or "error_code\":769" in error_message:
            await msg.edit_text(
                "⚠️ Извините, мне запрещено генерировать изображения по такому запросу.\n\n"
                "Пожалуйста, измените ваш запрос, чтобы он не содержал запрещённых тем или формулировок.\n\n"
                "Примеры допустимых запросов:\n"
                "• 'Красивый закат на пляже'\n"
                "• 'Кот в шляфе, цифровая живопись'\n"
                "• 'Футуристический город ночью'",
                reply_markup=cancel_keyboard
            )
        else:
            await msg.edit_text(
                f"⚠️ Ошибка провайдера: {error_message}",
                reply_markup=cancel_keyboard
            )
        raise
    except ValueError as e:
        logger.error(f"ValueError in image generation for user_id={user_id}: {e}", exc_info=True)
        await msg.edit_text(
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
        await msg.edit_text(
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
        await msg.edit_text(
            "⚠️ Неизвестная ошибка. Попробуйте позже.",
            reply_markup=cancel_keyboard
        )

@dp.message(F.text == "👤 Профиль")
async def show_profile(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    cursor = await db.connection.execute("""
        SELECT name, gpt_requests, image_requests, created_at
        FROM profiles WHERE user_id = ?
    """, (user_id,))
    row = await cursor.fetchone()
    
    if not row:
        await ensure_profile(message.from_user)
        cursor = await db.connection.execute("""
            SELECT name, gpt_requests, image_requests, created_at
            FROM profiles WHERE user_id = ?
        """, (user_id,))
        row = await cursor.fetchone()
    
    name, gpt_count, img_count, created_at = row
    current_model = await get_user_model(user_id)
    await message.answer(
        f"<b>👤 Профиль</b>\n\n"
        f"<b>Имя:</b> {name}\n"
        f"<b>ID:</b> <code>{user_id}</code>\n"
        f"<b>🧠 GPT-запросов:</b> {gpt_count}\n"
        f"<b>🖼 Изображений:</b> {img_count}\n\n"
        f"<b>Текущая модель:</b> {current_model}\n"
        f"<b>С ботом с:</b> {created_at[:19]}",
        parse_mode="HTML",
        reply_markup=get_main_keyboard(user_id))

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
        reply_markup=get_main_keyboard(user_id)
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
    await state.update_data(session_id=session_id)  # Сохраняем session_id
    await state.set_state(None)  # Выходим из состояния, не удаляя данные
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
            
            await state.update_data(session_id=session_id)  # Сохраняем session_id
            await msg.delete()
        else:
            await msg.edit_text("Не удалось получить результаты поиска. Попробуйте другой запрос.")

    except aiohttp.ClientError as e:
        logger.error(f"Network error performing search for user_id={user_id}: {e}", exc_info=True)
        await msg.edit_text("⚠️ Ошибка сети. Попробуйте позже.", reply_markup=cancel_keyboard)
    except Exception as e:
        logger.error(f"Error performing search for user_id={user_id}: {e}", exc_info=True)
        await msg.edit_text(f"⚠️ Ошибка поиска: {str(e)}", reply_markup=cancel_keyboard)

@dp.message(F.photo)
async def handle_uploaded_photo(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    logger.info(f"Photo uploaded by user_id={user_id}")

    if (await state.get_data()).get("processing_photo"):
        return

    await state.update_data(processing_photo=True)

    photo = max(message.photo, key=lambda p: p.width * p.height)
    await state.update_data(photo_file_id=photo.file_id)
    
    await state.set_state(UserStates.awaiting_image_prompt)
    await message.answer(
        "📷 Вы отправили фото. Что вы хотите с ним сделать?\n"
        "Например: 'Опиши, что на фото', 'Проанализируй изображение', 'Сделай описание в стиле фэнтези'.",
        reply_markup=get_main_keyboard(user_id)
    )

@dp.message(UserStates.awaiting_image_prompt, F.text)
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=10))
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

        # Получаем историю сообщений для текущей сессии
        session_id = data.get("session_id", await get_current_session(user_id))
        prev_msgs = await fetch_user_history(user_id, session_id)
        
        # Формируем контекст из истории
        context = ""
        for msg, reply in prev_msgs[-5:]:  # Ограничиваем 5 сообщениями для экономии токенов
            context += f"User: {msg}\nAssistant: {reply}\n"
        if context:
            full_prompt = f"Previous conversation:\n{context}\nCurrent request: {prompt}"
        else:
            full_prompt = prompt

        async with aiohttp.ClientSession() as session:
            g4f_client.session = session
            response = await g4f_client.chat.completions.create(
                model="gpt-4",
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
            reply_markup=get_main_keyboard(user_id),
            parse_mode="HTML"
        )
        await msg.delete()

    except aiohttp.ClientError as e:
        logger.error(f"Network error in image processing for user_id={user_id}: {e}", exc_info=True)
        await msg.edit_text(
            "⚠️ Ошибка сети. Попробуйте позже.",
            reply_markup=get_main_keyboard(user_id)
        )
    except g4f.Provider.ProviderError as e:
        logger.error(f"Provider error in image processing for user_id={user_id}: {e}", exc_info=True)
        await msg.edit_text(
            f"⚠️ Ошибка провайдера: {str(e)}",
            reply_markup=get_main_keyboard(user_id)
        )
    except Exception as e:
        logger.error(f"Unexpected error in image processing for user_id={user_id}: {e}", exc_info=True)
        await msg.edit_text(
            "⚠️ Неизвестная ошибка. Попробуйте позже.",
            reply_markup=get_main_keyboard(user_id)
        )
    finally:
        await state.update_data(processing_photo=False, session_id=session_id)
        await state.set_state(None)

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
            
        for i in range(0, len(full_reply), MAX_MESSAGE_LENGTH):
            part = full_reply[i:i + MAX_MESSAGE_LENGTH]
            if len(full_reply) > MAX_MESSAGE_LENGTH:
                part = f"({i//MAX_MESSAGE_LENGTH + 1}/{len(full_reply)//MAX_MESSAGE_LENGTH + 1})\n\n{part}"
            await message.answer(
                part,
                reply_markup=get_main_keyboard(user_id),
                parse_mode="HTML"
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
        # Обновляем модель в базе данных
        await db.connection.execute(
            "UPDATE user_settings SET model_id = ? WHERE user_id = ?",
            (model_id, user_id)
        )
        await db.connection.commit()

        # Получаем текущую сессию из состояния
        data = await state.get_data()
        session_id = data.get("session_id", await get_current_session(user_id))

        # Сохраняем сессию обратно в состояние
        await state.update_data(session_id=session_id)

        # Уведомляем пользователя об успешной смене модели
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
    await state.clear()
    await callback.message.answer(
        "Вы находитесь в главном меню.",
        reply_markup=get_main_keyboard(callback.from_user.id))
    await callback.message.delete()
    await callback.answer()

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