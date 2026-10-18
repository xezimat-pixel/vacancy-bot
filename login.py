"""Один раз: вход в Telegram и сохранение сессии в session.txt (только на этом компьютере)."""
import os
from pathlib import Path
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / "keys.env"); load_dotenv(BASE / ".env")
api_id, api_hash = os.getenv("TG_API_ID", "").strip(), os.getenv("TG_API_HASH", "").strip()
if not api_id or not api_hash:
    raise SystemExit("Сначала заполни TG_API_ID и TG_API_HASH в файле keys.env")

print("Вход в Telegram. Номер вводи в формате +7XXXXXXXXXX.")
print("Код придёт в приложение Telegram. Если включён облачный пароль, его тоже спросят.\n")
with TelegramClient(StringSession(), int(api_id), api_hash,
                    device_model="Vacancy Filter", app_version="1.0") as client:
    me = client.get_me()
    out = BASE / "session.txt"
    out.write_text(client.session.save())
    os.chmod(out, 0o600)
    print(f"\nГотово, вошёл как {me.first_name}. Сессия сохранена в session.txt.")
    print("Никому не отправляй этот файл.")
