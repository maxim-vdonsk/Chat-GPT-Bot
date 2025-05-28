# config.py
import os

DATABASE_PATH = "chat_history.db"
MAX_MESSAGE_LENGTH = 4096
MEDIA_DIR = "generated_media"
VOICES_DIR = os.path.join(MEDIA_DIR, "voices")
VARIATIONS_DIR = os.path.join(MEDIA_DIR, "variations")
IMAGES_DIR = os.path.join(MEDIA_DIR, "images")

# Создание директорий, если они не существуют
for directory in [MEDIA_DIR, IMAGES_DIR, VOICES_DIR, VARIATIONS_DIR]:
    os.makedirs(directory, exist_ok=True)

AVAILABLE_MODELS = [
    ("gpt-4", "g4f"),
    ("gpt-4o", "g4f"),
    ("gpt-4o-mini", "g4f"),
    ("gemini-1.5-pro", "g4f"),
    ("deepseek-v3", "g4f"),
    ("deepseek-r1", "g4f"),
    ("sonar-pro", "g4f"),
    ("sonar-reasoning-pro", "g4f"),
]
DEFAULT_MODEL = "gpt-4o"
DEFAULT_VOICE = "ru-RU-SvetlanaNeural"  # Голос по умолчанию для русского языка
ENGLISH_VOICE = "en-US-AriaNeural"     # Голос для английского языка
