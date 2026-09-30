"""Print private-message sender IDs to help configure your own bot."""
import os
import re
from pathlib import Path
from bot import APIError, Telegram, load_env


def main():
    load_env(Path(__file__).with_name(".env"))
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise SystemExit("Сначала заполни TELEGRAM_BOT_TOKEN в .env.")
    telegram = Telegram(token)
    offset = 0
    print("Останови основной бот. Отправь /start своему боту в Telegram. Выход: Ctrl+C.")
    try:
        while True:
            updates = telegram.call("getUpdates", {
                "offset": offset, "timeout": 25, "allowed_updates": ["message"],
            })
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message", {})
                if message.get("chat", {}).get("type") == "private":
                    sender = message.get("from", {})
                    print(f"ID: {sender.get('id')}, username: {sender.get('username', '(нет)')}", flush=True)
    except KeyboardInterrupt:
        pass
    except APIError as error:
        raise SystemExit(f"Не удалось получить сообщения (код {error.code}).") from None


if __name__ == "__main__":
    main()
