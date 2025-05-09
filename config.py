# config.py
DATABASE_PATH = "chat_history.db"
MAX_MESSAGE_LENGTH = 4096
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