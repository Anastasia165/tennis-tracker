"""Shared, persisted conversation for manual and AI pass imports."""
import re
import logging
import json
import uuid
from datetime import date
from reports import money
from linking import snapshot
from pass_ai import RecognitionError, normalize_amount

CANCEL = [["Отменить ввод"]]
UNKNOWN = [["Не помню"], ["Отменить ввод"]]
REVIEW = [["Сохранить абонемент"], ["Добавить занятие", "Изменить занятие"],
          ["Убрать занятие", "Изменить пополнения"], ["Использовать существующее"], ["Отменить ввод"]]


class PassFlow:
    def __init__(self, tracker, parsers, recognizer=None, telegram=None):
        self.t = tracker
        self.store = tracker.store
        self.parse_date, self.parse_money, self.parse_minutes, self.parse_people = parsers
        self.recognizer, self.telegram = recognizer, telegram

    def ask(self, uid, chat, d, step, text, keyboard=None):
        d["step"] = "pass_" + step
        d["keyboard"] = keyboard or CANCEL
        self.store.set_draft(uid, d)
        self.t.reply(chat, text, d["keyboard"])

    def start(self, uid, chat, ai=False):
        if ai and (not self.recognizer or not self.telegram):
            self.t.reply(chat, "ИИ ещё не подключён. Настрой YANDEX_API_KEY и YANDEX_FOLDER_ID "
                         "по README. Пока можно выбрать «Абонемент вручную».")
            return
        d = {"flow": "pass", "token": uuid.uuid4().hex, "source": "ai" if ai else "manual",
             "sessions": [], "deposits": [], "photos": [], "ocr": []}
        if ai:
            self.ask(uid, chat, d, "photos", "Пришли фото лицевой стороны, затем оборота — "
                     "двумя фотографиями (можно альбомом). Перед отправкой в Яндекс будет кнопка подтверждения.")
        else:
            self.ask(uid, chat, d, "number", "Какой номер или название у абонемента? Можно написать «Без номера».")

    def photo(self, uid, chat, message):
        d = self.store.draft(uid)
        if not d or d.get("step") != "pass_photos":
            self.t.reply(chat, "Для импорта фото сначала выбери «Абонемент с ИИ».")
            return
        photo = message["photo"][-1]
        if photo.get("file_size", 0) > 8 * 1024 * 1024:
            raise ValueError("Фото слишком большое. Отправь до 8 МБ.")
        identity = photo.get("file_unique_id", photo["file_id"])
        if any(p["unique"] == identity for p in d["photos"]):
            raise ValueError("Это фото уже получено. Пришли другую сторону.")
        d["photos"].append({"file_id": photo["file_id"], "unique": identity})
        if len(d["photos"]) == 1:
            self.ask(uid, chat, d, "photos", "Лицевая сторона получена. Теперь пришли оборот.")
        else:
            self.ask(uid, chat, d, "ready", "Обе стороны получены. «Распознать» отправит фото в Яндекс OCR, "
                     "а распознанный текст — в YandexGPT. Оба сервиса платные. "
                     "Результат останется черновиком до твоего подтверждения.",
                     [["Распознать"], ["Абонемент вручную"], ["Отменить ввод"]])

    def process_job(self, item):
        uid, chat = item["payload"]["user_id"], item["chat_id"]
        d = self.store.draft(uid)
        if not d or d.get("token") != item["payload"]["token"] or d["step"] != "pass_processing":
            return
        if uid != self.t.owner_id:
            return
        if not self.recognizer or not self.telegram:
            self.fail_job(uid, chat, d, "ИИ не подключён. Настрой ключ и каталог по README или выбери ручной ввод.")
            return
        # Persist each successful OCR stage. A manual retry reuses completed stages.
        try:
            while len(d["ocr"]) < 2:
                photo = d["photos"][len(d["ocr"])]
                data = self.telegram.download_photo(photo["file_id"])
                page = self.recognizer.ocr(data)
                d["ocr"].append(page)
                visible_text = "".join(c for c in page.get("text", "")
                                       if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))
                logging.info("OCR — сторона %s (%s):\n%s\nКонец OCR стороны %s.",
                             len(d["ocr"]), "лицевая" if len(d["ocr"]) == 1 else "оборот",
                             visible_text or "[полный текст отсутствует]", len(d["ocr"]))
                with self.store.db:
                    self.store.set_draft(uid, d)
            result = self.recognizer.extract(d["ocr"])
            logging.info("Результат разбора занятий (дата и сумма в рублях): %s",
                         json.dumps(result["sessions"], ensure_ascii=False))
            d["suggestions"] = result["sessions"]
            d["suggestion_index"] = 0
            d["suggested_number"] = result["number"]
            d["suggested_deposits"] = result["deposits"]
            d["notes"] = result["notes"]
            with self.store.db:
                for note in d["notes"]:
                    if note:
                        self.t.reply(chat, "Пометка ИИ для проверки: " + note, CANCEL)
                self.ask(uid, chat, d, "number", "Проверь номер/название абонемента: "
                         + (result["number"] or "не распознан") + ".\nВведи правильное значение или нажми «Верно».",
                         [["Верно"], ["Отменить ввод"]] if result["number"] else CANCEL)
        except (RecognitionError, OSError, ValueError) as error:
            # APIError from Telegram is handled by the caller as a safe failure.
            self.fail_job(uid, chat, d, str(error) if isinstance(error, RecognitionError)
                          else "Распознавание не завершено.")

    def fail_job(self, uid, chat, d, reason):
        with self.store.db:
            self.ask(uid, chat, d, "ready", reason + "\nЧерновик сохранён. Повтор может потребовать "
                     "дополнительной оплаты; автоматически не повторяю.",
                     [["Распознать"], ["Абонемент вручную"], ["Отменить ввод"]])

    def deposits(self, text):
        parts = text.replace(";", " ").split()
        if not parts or len(parts) > 100:
            raise ValueError("Введи от 1 до 100 сумм через пробел, например: 8000 5000 60.")
        return [self.parse_money(p) for p in parts]

    def date_value(self, text, year):
        if re.fullmatch(r"\d{1,2}\.\d{1,2}", text):
            text += "." + str(year)
        return self.parse_date(text, self.t.today())

    def next_row(self, uid, chat, d):
        index = d.get("suggestion_index", 0)
        suggestions = d.get("suggestions", [])
        if index < len(suggestions):
            d["candidate"] = suggestions[index]
            d["suggestion_index"] = index + 1
            self.ask_date(uid, chat, d)
        else:
            d.pop("candidate", None)
            if d["sessions"]:
                self.review(uid, chat, d)
            else:
                self.ask_date(uid, chat, d)

    def ask_date(self, uid, chat, d):
        candidate = d.get("candidate", {}).get("date")
        text = "Дата занятия: ДД.ММ (год " + str(d["year"]) + ") или ДД.ММ.ГГГГ."
        keys = [["Пропустить строку"], ["Отменить ввод"]]
        if candidate:
            text = "Проверь дату с фото: " + candidate + ".\n" + text
            keys.insert(0, ["Верно"])
        self.ask(uid, chat, d, "date", text, keys)

    def ask_coach(self, uid, chat, d, row=False):
        self.ask(uid, chat, d, "row_coach" if row else "coach",
                 "Имя тренера этого занятия?" if row else "Обычный тренер? Применю к занятиям, исключения можно исправить.",
                 [["Тренер: " + name] for name in self.store.coaches(uid)]
                 + [["Без тренера"], *UNKNOWN])

    def ask_people(self, uid, chat, d, step="entry_people"):
        self.ask(uid, chat, d, step,
                 "Сколько участников было " + d["current"]["date"] + ", включая тебя, без тренера?",
                 [["1", "2", "3", "4", "5", "6"], *UNKNOWN])

    def row_done(self, uid, chat, d):
        row = d.pop("current")
        index = d.pop("edit_index", None)
        if index is None:
            d["sessions"].append(row)
            self.next_row(uid, chat, d)
        else:
            d["sessions"][index] = row
            self.review(uid, chat, d)

    def review(self, uid, chat, d):
        d.pop("duplicates_ok", None)
        lines = []
        for i, row in enumerate(d["sessions"], 1):
            duration = str(row["minutes"]) + " мин" if row["minutes"] is not None else "время не указано"
            coach = "без тренера" if row["coach"] == "" else row["coach"] or "не указан"
            lines.append(f"{i}. {date.fromisoformat(row['date']):%d.%m.%Y} · {money(row['cost'])} · "
                         f"{duration} · участников: {row['participants'] or '?'} · тренер: {coach}"
                         + (f" · привяжу №{row['existing']['id']}" if row.get("existing") else ""))
        self.t.reply(chat, "Абонемент " + d["number"] + "\n" +
                     ("\n".join(lines) if lines else "Занятий пока нет."), REVIEW)
        paid = sum(d["deposits"])
        used = sum(row["cost"] for row in d["sessions"])
        balance = paid - used
        label = "Остаток" if balance >= 0 else "Списания превышают пополнения на"
        self.ask(uid, chat, d, "review",
                 "Пополнения: " + " + ".join(money(x) for x in d["deposits"]) +
                 f" = {money(paid)}\nСтоимость занятий: {money(used)}\n{label}: {money(abs(balance))}\n"
                 "Проверь все строки и пометки. Исправления можно внести по номеру строки. "
                 "Пополнения не прибавляются к стоимости занятий в статистике.", REVIEW)

    def handle(self, uid, chat, text, d):
        step = d["step"].removeprefix("pass_")
        value = text.casefold()
        if step in {"photos", "processing"}:
            raise ValueError("Пришли две фотографии." if step == "photos" else "Распознавание ожидает обработки.")
        if step == "ready":
            if value != "распознать":
                raise ValueError("Выбери «Распознать» или ручной ввод.")
            self.ask(uid, chat, d, "processing", "Распознаю обе стороны. Это может занять несколько минут.")
            self.store.enqueue(chat, "pass_ai", {"user_id": uid, "token": d["token"]})
        elif step == "number":
            number = d.get("suggested_number") if value == "верно" else text
            if not number or len(number) > 80 or any(ord(c) < 32 for c in number):
                raise ValueError("Укажи номер или название одной строкой, до 80 символов.")
            d["number"] = number
            self.ask(uid, chat, d, "year", "В каком году были занятия? Введи четыре цифры. "
                     "Если абонемент захватывает два года, для отдельных занятий укажи полную дату.")
        elif step == "year":
            if not re.fullmatch(r"[0-9]{4}", text) or not 2000 <= int(text) <= self.t.today().year:
                raise ValueError("Укажи год от 2000 до текущего.")
            d["year"] = int(text)
            suggestion = d.get("suggested_deposits", [])
            shown = " ".join(x or "?" for x in suggestion)
            self.ask(uid, chat, d, "deposits", "Какие суммы положены на абонемент? "
                     "Введи через пробел, без разделителей тысяч: 8000 5000 60."
                     + ("\nИИ прочитал: " + shown + ". Проверь или введи заново." if shown else ""),
                     [["Верно"], ["Отменить ввод"]] if shown else CANCEL)
        elif step in {"deposits", "edit_deposits"}:
            raw = " ".join(x or "?" for x in d.get("suggested_deposits", [])) if value == "верно" else text
            d["deposits"] = self.deposits(raw)
            if step == "edit_deposits":
                self.review(uid, chat, d)
            else:
                self.ask(uid, chat, d, "minutes", "Обычная длительность занятия в минутах? "
                         "Применю ко всем строкам; исключения можно исправить перед сохранением.",
                         [["60", "90", "120"], *UNKNOWN])
        elif step in {"minutes", "row_minutes"}:
            minutes = None if value == "не помню" else self.parse_minutes(text)
            target = d["current"] if step.startswith("row_") else d.setdefault("defaults", {})
            target["minutes"] = minutes
            if step == "row_minutes":
                self.ask_people(uid, chat, d, "row_people")
            else:
                d["defaults"]["participants"] = None
                self.ask_coach(uid, chat, d)
        elif step in {"people", "row_people"}:
            target = d["current"] if step.startswith("row_") else d["defaults"]
            # Legacy drafts at pass_people can still advance; new entries ask per session.
            target["participants"] = self.parse_people(text) if step == "row_people" else None
            self.ask_coach(uid, chat, d, row=step == "row_people")
        elif step == "entry_people":
            d["current"]["participants"] = self.parse_people(text)
            self.row_done(uid, chat, d)
        elif step in {"coach", "row_coach"}:
            name = text.removeprefix("Тренер: ").strip()
            if not name or len(name) > 80 or any(ord(c) < 32 for c in name):
                raise ValueError("Имя тренера — одна строка до 80 символов.")
            target = d["current"] if step.startswith("row_") else d["defaults"]
            target["coach"] = None if value == "не помню" else "" if value == "без тренера" else name
            if step.startswith("row_"):
                self.row_done(uid, chat, d)
            else:
                self.next_row(uid, chat, d)
        elif step == "date":
            if value == "пропустить строку":
                if "edit_index" in d:
                    d.pop("edit_index")
                    d.pop("current", None)
                    self.review(uid, chat, d)
                elif d.get("candidate"):
                    self.next_row(uid, chat, d)
                else:
                    self.review(uid, chat, d)
                return
            raw = d.get("candidate", {}).get("date") if value == "верно" else text
            if not raw:
                raise ValueError("Дата не распознана. Введи её вручную.")
            day = self.date_value(raw, d["year"])
            d["current"] = {**d["defaults"], "date": day, "participants": None}
            candidate = normalize_amount(d.get("candidate", {}).get("cost"))
            if "candidate" in d:
                d["candidate"]["cost"] = candidate
            if candidate is not None:
                self.ask(uid, chat, d, "cost",
                         "Сумма за " + day + ": " + money(self.parse_money(candidate)) +
                         ".\nНажми «Верно» или введи исправленную сумму.",
                         [["Верно"], ["Отменить ввод"]])
            else:
                prefix = "ИИ не смог связать сумму с этой датой. " if d.get("source") == "ai" else ""
                self.ask(uid, chat, d, "cost", prefix + "Сколько списали за это занятие, в рублях?")
        elif step == "cost":
            raw = d.get("candidate", {}).get("cost") if value == "верно" else text
            if not raw:
                raise ValueError("Сумма не распознана. Введи её вручную.")
            d["current"]["cost"] = self.parse_money(raw)
            if "edit_index" in d:
                self.ask(uid, chat, d, "row_minutes", "Длительность именно этого занятия, в минутах?", UNKNOWN)
            else:
                self.ask_people(uid, chat, d)
        elif step == "reuse_index":
            if not re.fullmatch(r"[0-9]{1,3}", text) or not 1 <= int(text) <= len(d["sessions"]):
                raise ValueError("Укажи номер строки абонемента.")
            index = int(text) - 1
            selected_ids = {r["existing"]["id"] for r in d["sessions"] if r.get("existing")}
            candidates = [r for r in self.store.on_date(uid, d["sessions"][index]["date"])
                          if r["pass_id"] is None and r["id"] not in selected_ids]
            if not candidates:
                raise ValueError("В эту дату нет доступных занятий без абонемента. Выбери другую строку или /cancel.")
            d["reuse_index"] = index
            d["reuse_candidates"] = [snapshot(r) for r in candidates]
            self.ask(uid, chat, d, "reuse_select",
                     "Выбери номер существующего занятия. Его сохранённые данные заменят строку черновика:\n" +
                     "\n".join(f"№{r['id']} · {r['played_on']} · {money(r['cost_kopecks'])} · "
                               f"{r['minutes'] if r['minutes'] is not None else '?'} мин · "
                               f"тренер: {r['coach'] or 'не указан'} · участников: {r['participants'] or '?'}"
                               for r in candidates),
                     [[str(r["id"])] for r in candidates[:20]] + [["К проверке"], ["Отменить ввод"]])
        elif step == "reuse_select":
            if text == "К проверке":
                self.review(uid, chat, d)
                return
            if not re.fullmatch(r"[0-9]{1,18}", text):
                raise ValueError("Укажи номер существующего занятия.")
            expected = next((r for r in d["reuse_candidates"] if r["id"] == int(text)), None)
            if expected is None:
                raise ValueError("Выбери занятие из показанного списка.")
            self.store.validate_links(uid, [expected])
            d["reuse_selected"] = expected
            old = d["sessions"][d["reuse_index"]]
            self.ask(uid, chat, d, "reuse_confirm",
                     f"Использовать занятие №{expected['id']} вместо строки {d['reuse_index'] + 1}?\n"
                     f"Сумма строки: {money(old['cost'])} → {money(expected['cost_kopecks'])}.\n"
                     "Дата, длительность, участники и тренер останутся из существующей записи. "
                     "Привязка произойдёт только при сохранении всего абонемента.",
                     [["Да, использовать"], ["К проверке"], ["Отменить ввод"]])
        elif step == "reuse_confirm":
            if text == "К проверке":
                self.review(uid, chat, d)
                return
            if text != "Да, использовать":
                raise ValueError("Подтверди кнопкой «Да, использовать» или вернись к проверке.")
            row = d["reuse_selected"]
            self.store.validate_links(uid, [row])
            d["sessions"][d["reuse_index"]] = {
                "date": row["played_on"], "cost": row["cost_kopecks"], "minutes": row["minutes"],
                "participants": row["participants"], "coach": row["coach"], "existing": row}
            self.review(uid, chat, d)
        elif step in {"edit_index", "remove_index"}:
            if not re.fullmatch(r"[0-9]{1,3}", text) or not 1 <= int(text) <= len(d["sessions"]):
                raise ValueError("Укажи номер строки из списка.")
            index = int(text) - 1
            if step == "remove_index":
                d["sessions"].pop(index)
                self.review(uid, chat, d)
            else:
                row = d["sessions"][index]
                if row.get("existing"):
                    raise ValueError("Эта строка использует существующее занятие. Чтобы заменить её, убери строку "
                                     "и добавь нужную заново. Исходная тренировка не удалится.")
                d["edit_index"] = index
                d["candidate"] = {"date": row["date"], "cost": str(row["cost"] // 100) + "." + f"{row['cost'] % 100:02d}"}
                self.ask_date(uid, chat, d)
        elif step in {"review", "duplicates"}:
            if value == "добавить занятие":
                if len(d["sessions"]) >= 100:
                    raise ValueError("В одном абонементе максимум 100 занятий.")
                d.pop("candidate", None)
                self.ask_date(uid, chat, d)
            elif value == "использовать существующее":
                self.ask(uid, chat, d, "reuse_index",
                         "Какую строку абонемента заменить существующей тренировкой? Введи номер строки из итога.")
            elif value in {"изменить занятие", "убрать занятие"}:
                self.ask(uid, chat, d, "edit_index" if value == "изменить занятие" else "remove_index",
                         "Введи номер строки из списка.")
            elif value == "изменить пополнения":
                self.ask(uid, chat, d, "edit_deposits", "Введи все пополнения заново через пробел.")
            elif value in {"сохранить абонемент", "это разные занятия"}:
                if value == "это разные занятия" and step != "duplicates":
                    raise ValueError("Сначала проверь итог и нажми «Сохранить абонемент».")
                conflicts = self.store.pass_conflicts(uid, d)
                if conflicts and value != "это разные занятия":
                    self.ask(uid, chat, d, "duplicates", "Возможные дубли: " + ", ".join(conflicts)
                             + ".\nВ указанные даты действительно было несколько отдельных занятий? "
                             "Если это повторный ввод, убери лишние строки. Для одинакового номера абонемента "
                             "также проверь, что вводишь другой абонемент.",
                             [["Использовать существующее"], ["Это разные занятия"], ["Убрать занятие"], ["Отменить ввод"]])
                    return
                pass_id = self.store.save_pass(uid, d)
                self.store.set_draft(uid, None)
                self.t.reply(chat, f"Абонемент №{pass_id} сохранён. Занятий: {len(d['sessions'])}. "
                             "Посмотреть остаток: /passes")
            else:
                raise ValueError("Выбери действие на кнопках под итогом.")
