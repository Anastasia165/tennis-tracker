"""Explicit selection and confirmation of existing sessions."""
import re
from reports import money


def snapshot(row):
    return {key: row[key] for key in ("id", "played_on", "minutes", "cost_kopecks", "participants", "coach")}


class LinkFlow:
    def __init__(self, tracker, session_text):
        self.t, self.store, self.format = tracker, tracker.store, session_text

    def ask(self, uid, chat, draft, step, text, keys):
        draft.update(flow="link", step=step, keyboard=keys)
        self.store.set_draft(uid, draft)
        self.t.reply(chat, text, keys)

    def start(self, uid, chat, pass_id=None):
        if pass_id is not None:
            self.choose_pass(uid, chat, {}, pass_id)
            return
        cards = self.store.passes(uid)
        if not cards:
            self.t.reply(chat, "Сначала создай абонемент через /pass. Можно сохранить его без новых занятий: "
                         "на вопрос о дате нажми «Пропустить строку». Затем привяжи уже записанные.")
            return
        self.ask(uid, chat, {}, "link_pass", "К какому абонементу привязать занятия? Выбери его номер в боте "
                 "(слева от названия) или введи его вручную.\n" +
                 "\n".join(f"№{p['id']} · {p['number']}" for p in cards),
                 [[str(p["id"])] for p in cards] + [["Отменить ввод"]])

    def choose_pass(self, uid, chat, d, pass_id):
        card = self.store.get_pass(uid, pass_id)
        if not card:
            raise ValueError("Абонемент не найден. Проверь номер в /passes.")
        d.update(pass_id=pass_id, page=0)
        self.show(uid, chat, d)

    def show(self, uid, chat, d):
        rows = self.store.unlinked(uid, d["page"] * 20)
        if not rows and d["page"]:
            d["page"] -= 1
            rows = self.store.unlinked(uid, d["page"] * 20)
        if not rows:
            self.store.set_draft(uid, None)
            self.t.reply(chat, "Нет тренировок без абонемента.")
            return
        self.ask(uid, chat, d, "link_select",
                 f"Занятия без абонемента, страница {d['page'] + 1}:\n" +
                 "\n".join(f"№{r['id']} · {self.format(r)}" for r in rows) +
                 "\n\nВведи номера нужных занятий через пробел (например: 12 15 18). "
                 "Можно указать номера с разных страниц. Сначала покажу итог.",
                 [["Дальше", "Назад"], ["Отменить ввод"]])

    def handle(self, uid, chat, text, d):
        if d["step"] == "link_pass":
            if not re.fullmatch(r"[0-9]{1,18}", text):
                raise ValueError("Введи номер абонемента в боте.")
            self.choose_pass(uid, chat, d, int(text))
        elif d["step"] == "link_select":
            if text in {"Дальше", "Назад"}:
                d["page"] = max(0, d["page"] + (1 if text == "Дальше" else -1))
                self.show(uid, chat, d)
                return
            parts = text.replace(",", " ").split()
            if not 1 <= len(parts) <= 100 or any(not re.fullmatch(r"[0-9]{1,18}", p) for p in parts):
                raise ValueError("Введи от 1 до 100 номеров занятий через пробел.")
            ids = list(dict.fromkeys(map(int, parts)))
            rows = []
            for session_id in ids:
                row = self.store.get_session(uid, session_id)
                if not row or row["pass_id"] is not None:
                    raise ValueError(f"Занятие №{session_id} недоступно или уже связано с абонементом.")
                rows.append(row)
            card = self.store.get_pass(uid, d["pass_id"])
            if not card:
                raise ValueError("Абонемент больше не доступен.")
            total = sum(r["cost_kopecks"] for r in rows)
            d["selection"] = [snapshot(r) for r in rows]
            self.ask(uid, chat, d, "link_confirm",
                     f"Привязать к абонементу №{card['id']} · {card['number']}?\n" +
                     "\n".join(f"№{r['id']} · {self.format(r)}" for r in rows) +
                     f"\nСписания увеличатся на {money(total)}.\n"
                     f"Остаток: {money(card['paid'] - card['used'])} → {money(card['paid'] - card['used'] - total)}.\n"
                     "Новых тренировок не будет; даты, стоимость, тренер и длительность сохранятся.",
                     [["Привязать"], ["Выбрать другие"], ["Отменить ввод"]])
        elif d["step"] == "link_confirm":
            if text == "Выбрать другие":
                self.show(uid, chat, d)
            elif text == "Привязать":
                self.store.link_sessions(uid, d["pass_id"], d["selection"])
                card = self.store.get_pass(uid, d["pass_id"])
                self.store.set_draft(uid, None)
                self.t.reply(chat, f"Привязано занятий: {len(d['selection'])}. "
                             f"Остаток абонемента: {money(card['paid'] - card['used'])}.")
            else:
                raise ValueError("Нажми «Привязать», «Выбрать другие» или «Отменить ввод».")
