"""Private Telegram tennis tracker. Run: python bot.py."""
import argparse
import json
import logging
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from reports import chart, money, summary, overview_chart
from store import Store
from date_picker import DATE_KEYS, picker
from passes import PassFlow
from linking import LinkFlow
from navigation import Navigation, HOME
from pass_ai import YandexRecognizer, RecognitionError, MAX_IMAGE

MENU = HOME
HELP = (
    "Записывай дату, длительность, стоимость, число участников и тренера.\n"
    "Стоимость занятия и пополнения абонемента учитываются отдельно. Валюта — рубли.\n\n"
    "/menu — главное меню\n"
    "/settings — настройки\n"
    "/add — записать тренировку\n"
    "/pass — абонемент вручную\n"
    "/pass_ai — абонемент по двум фото\n"
    "/passes — абонементы и остатки\n"
    "/link — привязать существующие тренировки к абонементу\n"
    "/month — сводка и график за текущий месяц\n"
    "/month 2026-09 — выбрать месяц\n"
    "/history — последние 10 тренировок\n"
    "/delete 12 — удалить запись №12 с подтверждением\n"
    "/cancel — отменить ввод\n\n"
    "Быстрый ввод: /add 19.09.2026 90 2500\n"
    "Это 90 минут за 2500 ₽. Затем спрошу участников и тренера.\n"
    "Изменить запись: Мой теннис → История → запись → Изменить."
)


def parse_date(value, today):
    value = value.strip().casefold()
    if value == "сегодня":
        result = today
    elif value == "вчера":
        result = today - timedelta(days=1)
    else:
        try:
            result = datetime.strptime(value, "%d.%m.%Y").date()
        except ValueError:
            try:
                result = date.fromisoformat(value)
            except ValueError:
                raise ValueError("Дата: ДД.ММ.ГГГГ, например 19.09.2026, или «Сегодня».") from None
    if result > today:
        raise ValueError("Можно записывать только уже состоявшиеся тренировки.")
    if result.year < 2000:
        raise ValueError("Укажи дату не раньше 01.01.2000.")
    return result.isoformat()


def parse_minutes(value):
    if not re.fullmatch(r"[0-9]{1,4}", value.strip()):
        raise ValueError("Длительность — целое число минут, например 90.")
    result = int(value)
    if not 1 <= result <= 1440:
        raise ValueError("Длительность должна быть от 1 до 1440 минут.")
    return result


def parse_money(value):
    value = value.strip().replace(",", ".")
    if not re.fullmatch(r"[0-9]{1,7}(?:\.[0-9]{1,2})?", value):
        raise ValueError("Стоимость — сумма в рублях, например 2500 или 2500,50.")
    amount = Decimal(value)
    if amount > 1000000:
        raise ValueError("Максимальная стоимость одной тренировки — 1 000 000 ₽.")
    return int(amount * 100)


def parse_participants(value):
    if value.casefold() == "не помню":
        return None
    if not re.fullmatch(r"[0-9]{1,3}", value) or not 1 <= int(value) <= 100:
        raise ValueError("Укажи от 1 до 100 участников, включая себя, без тренера, или «Не помню».")
    return int(value)


def session_text(record):
    played_on = record.get("played_on", record.get("date"))
    cost = record.get("cost_kopecks", record.get("cost"))
    return (
        f"{date.fromisoformat(played_on):%d.%m.%Y} · "
        f"{str(record['minutes']) + ' мин' if record['minutes'] is not None else 'время не указано'} · {money(cost)}\n"
        f"Участников (включая тебя, без тренера): {record.get('participants') or 'не указано'}\n"
        f"Тренер: {('без тренера' if record.get('coach') == '' else record.get('coach')) or 'не указано'}"
    )


class Tracker:
    def __init__(self, store, owner_id, today=None, recognizer=None, telegram=None):
        self.store = store
        self.owner_id = owner_id
        self.base_today = today or date.today
        self.today = self.local_today
        self.passes = PassFlow(self, (parse_date, parse_money, parse_minutes, parse_participants), recognizer, telegram)
        self.links = LinkFlow(self, session_text)
        self.navigation = Navigation(self, (parse_date, parse_money, parse_minutes, parse_participants))

    def local_today(self):
        offset = self.store.preferences(self.owner_id).get("utc_offset")
        if offset is None:
            return self.base_today()
        return datetime.now(timezone(timedelta(hours=offset))).date()

    def reply(self, chat_id, text, keyboard=None):
        # Keep large pass previews below Telegram's UTF-16 message limit.
        if len(text.encode("utf-16-le")) > 7000:
            chunk = ""
            for line in text.splitlines(keepends=True):
                if len((chunk + line).encode("utf-16-le")) > 7000 and chunk:
                    self.reply(chat_id, chunk, keyboard)
                    chunk = ""
                while len(line.encode("utf-16-le")) > 7000:
                    self.reply(chat_id, line[:1500], keyboard)
                    line = line[1500:]
                chunk += line
            if chunk:
                self.reply(chat_id, chunk, keyboard)
            return
        self.store.enqueue(chat_id, "text", {
            "text": text, "keyboard": MENU if keyboard is None else keyboard,
        })

    def ask_participants(self, user_id, chat_id, draft):
        draft["step"] = "participants"
        self.store.set_draft(user_id, draft)
        self.reply(chat_id, "Сколько было участников, включая тебя, без тренера?",
                   [["1", "2", "3"], ["4", "5", "6"], ["Не помню"], ["Отменить ввод"]])

    def show_picker(self, user_id, chat_id, draft, first=None, recent=False, message_id=None):
        draft.setdefault("calendar_token", uuid.uuid4().hex[:12])
        self.store.set_draft(user_id, draft)
        payload = picker(draft["calendar_token"], first or self.today().replace(day=1), self.today(), recent)
        if message_id is not None:
            payload["message_id"] = message_id
        self.store.enqueue(chat_id, "calendar", payload)

    def handle_callback(self, query):
        chat = query.get("message", {}).get("chat", {})
        user_id = query.get("from", {}).get("id")
        if user_id != self.owner_id or chat.get("type") != "private":
            return
        chat_id = chat["id"]
        answer = {"callback_query_id": query["id"]}
        self.store.enqueue(chat_id, "callback", answer)
        draft = self.store.draft(user_id)
        parts = query.get("data", "").split(":")
        if (len(parts) != 4 or parts[0] != "cal" or not draft or draft["step"] != "date"
                or parts[1] != draft.get("calendar_token")):
            self.reply(chat_id, "Этот календарь уже неактивен. Начни новую запись: /add")
            return
        action, value = parts[2:]
        if action == "noop":
            return
        try:
            selected = date.fromisoformat(parse_date(value, self.today()))
            if action == "month" and selected.day == 1:
                self.show_picker(user_id, chat_id, draft, selected,
                                 message_id=query["message"]["message_id"])
            elif action == "day":
                self.handle_text(user_id, chat_id, selected.isoformat())
        except ValueError as error:
            self.reply(chat_id, str(error), DATE_KEYS)

    def confirm(self, user_id, chat_id, draft):
        draft["step"] = "confirm"
        self.store.set_draft(user_id, draft)
        self.reply(
            chat_id, "Сохранить тренировку?\n" + session_text(draft),
            [["Сохранить"], ["Отменить ввод"]],
        )

    def report(self, user_id, chat_id, month):
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
            raise ValueError("Период указывается как ГГГГ-ММ, например /month 2026-09.")
        try:
            first = date.fromisoformat(month + "-01")
        except ValueError:
            raise ValueError("Такого месяца нет. Пример: /month 2026-09.") from None
        if first.year < 2000 or first > self.today().replace(day=1):
            raise ValueError("Выбери прошедший или текущий месяц, начиная с 2000 года.")
        rows = self.store.month(user_id, month)
        self.reply(chat_id, summary(rows, month))
        if rows:
            self.store.enqueue(chat_id, "chart", {
                "rows": rows, "month": month, "today": self.today().isoformat(),
            })

    def handle(self, update):
        update_id = update["update_id"]
        # The state change, replies and deduplication marker commit together.
        with self.store.db:
            if self.store.processed(update_id):
                return
            if "callback_query" in update:
                self.handle_callback(update["callback_query"])
                self.store.mark_processed(update_id)
                return
            message = update.get("message", {})
            sender_id = message.get("from", {}).get("id")
            chat = message.get("chat", {})
            if sender_id == self.owner_id and chat.get("type") == "private":
                logging.info("Получено личное сообщение владельца, update_id=%s.", update_id)
                chat_id = chat["id"]
                text = message.get("text", "").strip()
                try:
                    if message.get("photo"):
                        self.passes.photo(sender_id, chat_id, message)
                    else:
                        self.handle_text(sender_id, chat_id, text)
                except ValueError as error:
                    draft = self.store.draft(sender_id)
                    keyboard = [["Отменить ввод"]] if draft else MENU
                    if draft and draft.get("flow") in {"pass", "link", "hub"}:
                        keyboard = draft.get("keyboard", keyboard)
                    elif draft and draft["step"] == "confirm":
                        keyboard = [["Сохранить"], ["Отменить ввод"]]
                    elif draft and draft["step"] == "duplicate_confirm":
                        keyboard = [["Да, это другое занятие"], ["Нет, не добавлять"]]
                    elif draft and draft["step"] == "date":
                        keyboard = DATE_KEYS
                    elif draft and draft["step"] == "delete":
                        keyboard = [["Удалить"], ["Отменить ввод"]]
                    self.reply(chat_id, str(error), keyboard)
            elif message:
                if chat.get("type") != "private":
                    logging.warning(
                        "Сообщение пропущено: тип чата %s. Напиши боту в личном чате.",
                        chat.get("type"),
                    )
                else:
                    logging.warning(
                        "Сообщение пропущено: sender_id=%s, TELEGRAM_USER_ID=%s. "
                        "Если это твой аккаунт, проверь ID через identify.py, "
                        "исправь .env и перезапусти бота.",
                        sender_id, self.owner_id,
                    )
            self.store.mark_processed(update_id)

    def handle_text(self, user_id, chat_id, text):
        if self.navigation.route(user_id, chat_id, text):
            return
        value = text.casefold()
        parts = text.split()
        command = parts[0].split("@", 1)[0].casefold() if parts else ""
        if command in {"/cancel", "/start", "/help"} or value == "отменить ввод":
            self.store.set_draft(user_id, None)
            self.reply(chat_id, "Ввод отменён." if command == "/cancel" or value == "отменить ввод" else HELP)
            return
        if command in {"/pass", "/pass_ai"} or value in {"абонемент вручную", "абонемент с ии"}:
            self.passes.start(user_id, chat_id, command == "/pass_ai" or value == "абонемент с ии")
            return
        if command == "/link" or value == "привязать тренировки":
            if command == "/link" and len(parts) > 1:
                if len(parts) != 2 or not re.fullmatch(r"[0-9]{1,18}", parts[1]):
                    raise ValueError("Пример: /link 1 — номер абонемента в боте из /passes.")
                self.links.start(user_id, chat_id, int(parts[1]))
            else:
                self.links.start(user_id, chat_id)
            return
        if command == "/passes" or value == "мои абонементы":
            rows = self.store.passes(user_id)
            for row in rows:
                self.reply(chat_id, f"№{row['id']} · {row['number']}\nЗанятий: {row['visits']}\n"
                           f"Пополнения: {money(row['paid'])}\nСписания: {money(row['used'])}\n"
                           f"Остаток: {money(row['paid'] - row['used'])}\n"
                           f"Привязать существующие занятия: /link {row['id']}")
            if not rows:
                self.reply(chat_id, "Абонементов пока нет.")
            return
        draft = self.store.draft(user_id)
        if draft and draft.get("flow") == "link" and not command.startswith("/"):
            self.links.handle(user_id, chat_id, text, draft)
            return
        if draft and draft.get("flow") == "pass" and not command.startswith("/"):
            self.passes.handle(user_id, chat_id, text, draft)
            return
        if command == "/add" or value == "записать тренировку":
            if command == "/add" and len(parts) > 1:
                if len(parts) != 4:
                    raise ValueError("Пример: /add 19.09.2026 90 2500")
                draft = {
                    "date": parse_date(parts[1], self.today()),
                    "minutes": parse_minutes(parts[2]), "cost": parse_money(parts[3]),
                }
                self.ask_participants(user_id, chat_id, draft)
            else:
                self.store.set_draft(user_id, {"step": "date"})
                self.reply(chat_id, "Когда была тренировка? Введи ДД.ММ.ГГГГ или выбери день.",
                           DATE_KEYS)
            return
        if command == "/month" or value in {"этот месяц", "прошлый месяц"}:
            if command == "/month" and len(parts) > 2:
                raise ValueError("Пример: /month 2026-09")
            target = self.today()
            if value == "прошлый месяц":
                target = target.replace(day=1) - timedelta(days=1)
            month = parts[1] if command == "/month" and len(parts) == 2 else target.strftime("%Y-%m")
            self.report(user_id, chat_id, month)
            return
        if command == "/history" or value == "последние тренировки":
            rows = self.store.history(user_id)
            text = "\n".join(f"№{row['id']} · {session_text(row)}" for row in rows)
            self.reply(chat_id, text + "\n\nУдалить запись: /delete НОМЕР" if rows else "Тренировок пока нет.")
            return
        if command == "/delete":
            if len(parts) != 2 or not re.fullmatch(r"[0-9]{1,18}", parts[1]):
                raise ValueError("Укажи номер из /history. Пример: /delete 12")
            record = self.store.get_session(user_id, int(parts[1]))
            if not record:
                raise ValueError("Такой записи нет. Проверь номер через /history.")
            self.store.set_draft(user_id, {"step": "delete", "id": record["id"]})
            self.reply(chat_id, "Удалить тренировку?\n" + session_text(record),
                       [["Удалить"], ["Отменить ввод"]])
            return
        if command.startswith("/"):
            self.reply(chat_id, "Неизвестная команда. Список команд: /help")
            return

        draft = self.store.draft(user_id)
        if not draft:
            self.reply(chat_id, "Чтобы записать занятие, нажми «➕ Добавить запись» или используй /add.")
            return
        step = draft["step"]
        if step == "date":
            if value in {"календарь", "последние 7 дней"}:
                self.show_picker(user_id, chat_id, draft, recent=value == "последние 7 дней")
                return
            draft.update(date=parse_date(text, self.today()), step="minutes")
            self.store.set_draft(user_id, draft)
            self.reply(chat_id, "Сколько минут длилась тренировка?", [["60", "90", "120"], ["Отменить ввод"]])
        elif step == "minutes":
            draft.update(minutes=parse_minutes(text), step="cost")
            self.store.set_draft(user_id, draft)
            self.reply(chat_id, "Сколько заплатила за эту тренировку, в рублях? Для бесплатной введи 0.",
                       [["Отменить ввод"]])
        elif step == "cost":
            draft["cost"] = parse_money(text)
            self.ask_participants(user_id, chat_id, draft)
        elif step == "participants":
            draft.update(participants=parse_participants(text), step="coach")
            self.store.set_draft(user_id, draft)
            coaches = [["Тренер: " + name] for name in self.store.coaches(user_id)]
            self.reply(chat_id, "Кто был тренером? Введи имя или выбери кнопку.",
                       coaches + [["Без тренера", "Не помню"], ["Отменить ввод"]])
        elif step == "coach":
            coach = text.removeprefix("Тренер: ").strip()
            if not coach or len(coach) > 80 or any(ord(c) < 32 for c in coach):
                raise ValueError("Введи имя тренера одной строкой, до 80 символов.")
            draft["coach"] = None if value == "не помню" else "" if value == "без тренера" else coach
            self.confirm(user_id, chat_id, draft)
        elif step in {"confirm", "duplicate_confirm"}:
            if step == "duplicate_confirm":
                if value == "нет, не добавлять":
                    self.store.set_draft(user_id, None)
                    self.reply(chat_id, "Повторная запись не добавлена.")
                    return
                if value != "да, это другое занятие":
                    raise ValueError("В этот день уже есть занятие. Подтверди отдельное занятие или нажми «Нет, не добавлять».")
            else:
                if value != "сохранить":
                    raise ValueError("Нажми «Сохранить» или «Отменить ввод».")
                existing = self.store.on_date(user_id, draft["date"])
                if existing:
                    draft["step"] = "duplicate_confirm"
                    self.store.set_draft(user_id, draft)
                    previous = "\n".join(f"№{row['id']} · {session_text(row)}" for row in existing[:5])
                    self.reply(chat_id,
                               f"На {date.fromisoformat(draft['date']):%d.%m.%Y} уже записано занятий: {len(existing)}.\n"
                               + previous + "\n\nТы действительно занималась ещё раз в этот день?",
                               [["Да, это другое занятие"], ["Нет, не добавлять"]])
                    return
            session_id = self.store.add(user_id, draft)
            self.store.set_draft(user_id, None)
            self.reply(chat_id, f"Сохранено, №{session_id}.\n" + session_text(draft))
        elif step == "delete":
            if value != "удалить":
                raise ValueError("Нажми «Удалить» или «Отменить ввод».")
            self.store.delete(user_id, draft["id"])
            self.store.set_draft(user_id, None)
            self.reply(chat_id, "Запись удалена. Статистика обновлена.")


class APIError(Exception):
    def __init__(self, code=0, retry_after=5):
        self.code = code
        self.retry_after = min(max(int(retry_after), 1), 300)
        super().__init__(f"Telegram API: код {code}")


class Telegram:
    def __init__(self, token):
        self.base_url = "https://api.telegram.org/bot" + token + "/"

    def call(self, method, data, photo=None):
        if photo is None:
            body = json.dumps(data).encode("utf-8")
            content_type = "application/json"
        else:
            boundary = uuid.uuid4().hex
            chunks = []
            for key, value in data.items():
                chunks.append(
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
                )
            chunks.extend([
                f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="report.png"\r\n'
                'Content-Type: image/png\r\n\r\n'.encode(),
                photo, f"\r\n--{boundary}--\r\n".encode(),
            ])
            body = b"".join(chunks)
            content_type = "multipart/form-data; boundary=" + boundary
        request = Request(self.base_url + method, data=body, headers={"Content-Type": content_type})
        try:
            with urlopen(request, timeout=45) as response:
                result = json.load(response)
        except HTTPError as error:
            try:
                result = json.loads(error.read())
                delay = result.get("parameters", {}).get("retry_after", 5)
            except (ValueError, TypeError):
                delay = 5
            raise APIError(error.code, delay) from None
        except (URLError, TimeoutError, OSError, ValueError):
            # Avoid leaking the bot token via urllib exception URLs.
            raise APIError() from None
        if not result.get("ok"):
            raise APIError(result.get("error_code", 0), result.get("parameters", {}).get("retry_after", 5))
        return result["result"]

    def download_photo(self, file_id):
        info = self.call("getFile", {"file_id": file_id})
        if info.get("file_size", 0) > MAX_IMAGE:
            raise RecognitionError("Фото больше 8 МБ. Начни импорт с уменьшенными фото.")
        path = info.get("file_path", "")
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", path) or ".." in path or path.startswith("/"):
            raise RecognitionError("Telegram не вернул корректный путь фотографии.")
        url = self.base_url.replace("/bot", "/file/bot", 1) + path
        try:
            with urlopen(url, timeout=45) as response:
                data = response.read(MAX_IMAGE + 1)
            if len(data) > MAX_IMAGE:
                raise RecognitionError("Фото больше 8 МБ.")
            return data
        except (URLError, OSError):
            raise RecognitionError("Не удалось скачать фото из Telegram. Повтори позже.") from None

    def deliver(self, item):
        payload = item["payload"]
        if item["kind"] == "callback":
            self.call("answerCallbackQuery", payload)
        elif item["kind"] == "calendar":
            self.call("editMessageText" if "message_id" in payload else "sendMessage",
                      {"chat_id": item["chat_id"], **payload})
        elif item["kind"] == "text":
            self.call("sendMessage", {
                "chat_id": item["chat_id"], "text": payload["text"],
                "reply_markup": {"keyboard": payload["keyboard"], "resize_keyboard": True},
            })
        elif item["kind"] == "overview_chart":
            self.call("sendPhoto", {"chat_id": item["chat_id"], "caption": payload["title"]},
                      photo=overview_chart(payload))
        else:
            image = chart(payload["rows"], payload["month"], date.fromisoformat(payload["today"]))
            self.call("sendPhoto", {
                "chat_id": item["chat_id"], "caption": "Тренировки по дням · " + payload["month"],
            }, photo=image)


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError("В .env ожидаются строки ИМЯ=значение.")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def main():
    parser = argparse.ArgumentParser(description="Личный бот для учёта тренировок.")
    parser.add_argument("--backup", metavar="PATH", help="Сделать копию базы и выйти.")
    args = parser.parse_args()
    load_env(Path(__file__).with_name(".env"))
    database = os.environ.get("DATABASE_PATH", "data/tracker.sqlite3")
    if args.backup:
        if not Path(database).is_file():
            parser.error("Исходной базы ещё нет.")
        store = Store(database)
        try:
            store.backup(args.backup)
        finally:
            store.close()
        print("Резервная копия сохранена.")
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    owner = os.environ.get("TELEGRAM_USER_ID", "")
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token) or not re.fullmatch(r"[1-9][0-9]*", owner):
        parser.error("Заполни TELEGRAM_BOT_TOKEN и TELEGRAM_USER_ID в .env.")
    try:
        utc_offset = int(os.environ.get("UTC_OFFSET_HOURS", "3"))
        if not -12 <= utc_offset <= 14:
            raise ValueError
    except ValueError:
        parser.error("UTC_OFFSET_HOURS должен быть целым числом от -12 до 14.")
    local_timezone = timezone(timedelta(hours=utc_offset))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    telegram = Telegram(token)
    store = Store(database)
    ai_key = os.environ.get("YANDEX_API_KEY", "")
    ai_folder = os.environ.get("YANDEX_FOLDER_ID", "")
    recognizer = YandexRecognizer(ai_key, ai_folder, os.environ.get("YANDEX_MODEL", "yandexgpt/latest")) if ai_key and ai_folder else None
    tracker = Tracker(store, int(owner), today=lambda: datetime.now(local_timezone).date(),
                      recognizer=recognizer, telegram=telegram)
    try:
        # A webhook would prevent polling. Never change an existing deployment silently.
        webhook = telegram.call("getWebhookInfo", {})
        if webhook.get("url"):
            raise SystemExit("У бота настроен webhook. Используй отдельного бота или отключи webhook.")
        identity = telegram.call("getMe", {})
        logging.info(
            "Бот @%s запущен. Ожидаю личные сообщения от TELEGRAM_USER_ID=%s.",
            identity.get("username"), owner,
        )
        while True:
            try:
                while item := store.pending():
                    if item["kind"] == "pass_ai":
                        try:
                            tracker.passes.process_job(item)
                        except APIError:
                            draft = store.draft(int(owner))
                            if draft and draft.get("token") == item["payload"]["token"]:
                                tracker.passes.fail_job(int(owner), item["chat_id"], draft,
                                                       "Не удалось получить фото из Telegram.")
                        store.delivered(item["id"])
                        continue
                    try:
                        telegram.deliver(item)
                    except APIError as error:
                        if error.code not in {400, 403}:
                            raise
                        logging.error("Ответ не доставлен (код %s). Запись в базе сохранена.", error.code)
                    else:
                        logging.info("Ответ отправлен, outbox_id=%s.", item["id"])
                    store.delivered(item["id"])
                updates = telegram.call("getUpdates", {
                    "offset": store.offset(), "timeout": 25, "allowed_updates": ["message", "callback_query"],
                })
                for update in updates:
                    tracker.handle(update)
            except APIError as error:
                if error.code in {401, 404, 409}:
                    raise SystemExit(
                        "Проверь токен и убедись, что запущен только один экземпляр бота."
                    ) from None
                logging.warning("Telegram временно недоступен (код %s). Повтор запроса.", error.code)
                time.sleep(error.retry_after)
    except KeyboardInterrupt:
        logging.info("Бот остановлен.")
    except APIError as error:
        raise SystemExit(f"Не удалось подключиться к Telegram (код {error.code}).") from None
    finally:
        store.close()


if __name__ == "__main__":
    main()
