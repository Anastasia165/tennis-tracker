"""Telegram menus, record forms and browsing. All work is performed in the update transaction."""
import re
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from date_picker import DATE_KEYS
from reports import money

HOME = [["➕ Добавить запись"], ["🎾 Мой теннис", "💳 Финансы"], ["⚙️ Настройки"]]
BACK = "⬅️ Назад"
CANCEL = "Отменить ввод"
KINDS = {"training": "Тренировка", "game": "Игра", "match": "Матч турнира"}
LABELS = {
    "date": "Дата", "club": "Клуб / место", "coach": "Тренер", "minutes": "Длительность",
    "participants": "Участники", "cost": "Сумма", "pass_id": "Оплата", "format": "Формат",
    "opponent": "Соперник / участники", "score": "Счёт", "result": "Результат",
    "note": "Комментарий", "name": "Название", "place": "Итоговое место",
    "category": "Категория", "relation": "Связать с событием",
}
CATEGORIES = ["Инвентарь", "Струны и перетяжка", "Одежда", "Аренда корта", "Прочее"]
RESULTS = ["Победа", "Поражение", "Ничья", "Не указан"]


def short(value, length=55):
    return str(value or "")[:length]


def day(value):
    return date.fromisoformat(value).strftime("%d.%m.%Y") if value else "Дата неизвестна"


def duration(rows):
    known = [r for r in rows if r["minutes"] is not None]
    minutes = sum(r["minutes"] for r in known)
    return f"{minutes // 60} ч {minutes % 60} мин; длительность известна для {len(known)} из {len(rows)}"


class Navigation:
    def __init__(self, bot, parsers):
        self.bot, self.store = bot, bot.store
        self.parse_date, self.parse_money, self.parse_minutes, self.parse_people = parsers

    def put(self, uid, chat, draft, text, keys):
        draft["flow"] = "hub"
        draft["keyboard"] = keys
        self.store.set_draft(uid, draft)
        self.bot.reply(chat, text, keys)

    def screen(self, uid, chat, area, text, actions, back="home", **extra):
        # Each visible button is bound to this screen, avoiding stale record actions.
        keys = [[label] for label, _ in actions]
        keys += [[BACK]] if area != "home" else []
        self.put(uid, chat, dict(step="screen", area=area, choices=dict(actions), back=back, **extra), text, keys)

    def period(self, uid, area):
        pref = self.store.preferences(uid).get(area + "_period", "month")
        today = self.bot.today()
        if isinstance(pref, list):
            return pref[0], pref[1]
        if pref == "previous":
            end = today.replace(day=1) - timedelta(days=1)
            return end.replace(day=1).isoformat(), end.isoformat()
        if pref == "year":
            return today.replace(month=1, day=1).isoformat(), today.isoformat()
        return today.replace(day=1).isoformat(), today.isoformat()

    def period_title(self, uid, area):
        start, end = self.period(uid, area)
        return day(start) + " — " + day(end)

    def show(self, uid, chat, area):
        if isinstance(area, dict):
            if area["action"] == "card":
                return self.card(uid, chat, area["entity"], area["id"])
            if area["action"] == "list":
                return self.listing(uid, chat, **area["args"])
        if area == "home":
            self.store.set_draft(uid, None)
            self.bot.reply(chat, "Твой теннис: запиши занятие или посмотри статистику и финансы.\n"
                           "Быстрая запись: /add · Все команды: /help", HOME)
        elif area == "add":
            self.screen(uid, chat, area, "Что добавить?", [
                ("🏫 Тренировка с тренером", {"action": "new", "entity": "session", "kind": "training"}),
                ("🎾 Игра без тренера", {"action": "new", "entity": "session", "kind": "game"}),
                ("🏆 Турнир", "tournament_add"), ("💳 Абонемент / пополнение", "pass_add"),
                ("🧾 Другой расход", {"action": "new", "entity": "expense"})])
        elif area == "sport":
            start, end = self.period(uid, area)
            rows = self.store.records(uid, start, end)
            counts = Counter(r["kind"] for r in rows)
            tournaments = self.store.tournaments_in(uid, start, end)
            self.screen(uid, chat, area,
                "🎾 Мой теннис · " + self.period_title(uid, area) +
                f"\nТренировок: {counts['training']}\nИгр: {counts['game']}\n"
                f"Турниров: {len(tournaments)}\nМатчей турниров: {counts['match']}\n"
                "На корте: " + duration(rows), [
                ("🏫 Тренировки", "training_stats"), ("🎾 Игры", "game_stats"),
                ("🏆 Турниры", {"action": "list", "args": {"view": "tournaments"}}),
                ("📅 История", {"action": "list", "args": {"view": "history"}}),
                ("📊 Динамика", "dynamics"), ("🗓 Выбрать период", "sport_period")])
        elif area == "finance":
            start, end = self.period(uid, area)
            f = self.store.finances(uid, start, end)
            text = ("💳 Финансы · " + self.period_title(uid, area) +
                    "\nОплачено за период: " + money(f["paid"]) +
                    "\nСтоимость состоявшихся занятий: " + money(f["consumed"]) +
                    "\nТекущий остаток на всех абонементах: " + money(f["balance"]) +
                    "\n\nПополнение — оплата. Списание за занятие не считается повторной оплатой.")
            if f["unknown_count"]:
                text += f"\nПополнения без даты: {f['unknown_count']} на {money(f['unknown'])}. "                         "Они включены в остаток, но не в оплату за период. Дату можно указать в карточке абонемента."
            self.screen(uid, chat, area, text, [
                ("💳 Мои абонементы", {"action": "list", "args": {"view": "passes"}}),
                ("🧾 История операций", {"action": "list", "args": {"view": "operations"}}),
                ("📊 Расходы по категориям", "categories"), ("🗓 Выбрать период", "finance_period")])
        elif area in {"sport_period", "finance_period"}:
            parent = area.split("_")[0]
            self.screen(uid, chat, area, "За какой период показать статистику?", [
                (label, {"action": "period", "area": parent, "value": value})
                for label, value in [("Этот месяц", "month"), ("Прошлый месяц", "previous"),
                                      ("Этот год", "year"), ("Другой период", "custom")]], back=parent)
        elif area in {"training_stats", "game_stats"}:
            kind = "training" if area == "training_stats" else "game"
            rows = self.store.records(uid, *self.period(uid, "sport"), kind)
            text = (("Тренировки" if kind == "training" else "Игры") + " · " +
                    self.period_title(uid, "sport") + f"\nЗанятий: {len(rows)}\nНа корте: " + duration(rows))
            for field, title in ([("coach", "По тренерам"), ("club", "По клубам"), ("participants", "Размер группы")]
                                 if kind == "training" else [("opponent", "По соперникам / участникам"), ("result", "Результаты")]):
                counts = Counter(str(r[field] or "Не указано") for r in rows)
                if field == "opponent":
                    details = []
                    for name, n in counts.most_common():
                        games = [r for r in rows if str(r[field] or "Не указано") == name]
                        wins = sum(r["result"] == "Победа" for r in games)
                        losses = sum(r["result"] == "Поражение" for r in games)
                        details.append(f"{name}: игр {n}, побед {wins}, поражений {losses}")
                else:
                    details = [f"{name}: {n}" for name, n in counts.most_common()]
                text += "\n\n" + title + ":\n" + ("\n".join(details) or "Нет данных")
            self.screen(uid, chat, area, text, [
                ("Список тренировок" if kind == "training" else "Список игр",
                 {"action": "list", "args": {"view": "sessions", "kind": kind}})], back="sport")
        elif area == "tournament_add":
            self.screen(uid, chat, area, "Создать турнир или записать матч?", [
                ("Новый турнир", {"action": "new", "entity": "tournament"}),
                ("Добавить матч в турнир", {"action": "list", "args": {"view": "tournaments", "all_time": True}})], back="add")
        elif area == "pass_add":
            self.screen(uid, chat, area, "Абонемент", [
                ("Новый абонемент", "pass_method"),
                ("Пополнить существующий", {"action": "list", "args": {"view": "passes", "select": "deposit"}})], back="add")
        elif area == "pass_method":
            self.screen(uid, chat, area, "Как добавить абонемент?", [
                ("📷 По фото с ИИ", {"action": "import", "ai": True}),
                ("✍️ Вручную", {"action": "import", "ai": False})], back="pass_add")
        elif area == "settings":
            p = self.store.preferences(uid)
            self.screen(uid, chat, area,
                "⚙️ Настройки\nОсновной клуб: " + str(p.get("club") or "не выбран") +
                "\nТренер: " + str(p.get("coach") or "не выбран") +
                "\nОбычная длительность: " + str(p.get("minutes") or "не выбрана") +
                "\nЧасовой пояс: " + (f"UTC{p['utc_offset']:+d}" if "utc_offset" in p else "из настройки запуска бота"), [
                ("🏫 Мои клубы", {"action": "presets", "field": "club"}),
                ("👤 Мои тренеры", {"action": "presets", "field": "coach"}),
                ("⏱ Обычная длительность", {"action": "setting", "field": "minutes"}),
                ("🌍 Часовой пояс", {"action": "setting", "field": "utc_offset"})])
        elif area == "categories":
            start, end = self.period(uid, "finance")
            f = self.store.finances(uid, start, end)
            rows = [r for r in self.store.records(uid, start, end) if r["kind"] != "match"]
            known = [r for r in rows if r["minutes"] is not None]
            minutes = sum(r["minutes"] for r in known)
            text = "Оплаты по категориям · " + self.period_title(uid, "finance") + "\n"
            text += "\n".join(k + ": " + money(v) for k, v in f["categories"].items()) or "Нет оплат."
            text += "\n\nСредняя стоимость тренировки / игры: " + (
                money(Decimal(sum(r["cost_kopecks"] for r in rows)) / len(rows)) if rows else "нет данных")
            text += "\nСтоимость часа (занятия с известным временем, без турнирных взносов): " + (
                money(Decimal(sum(r["cost_kopecks"] for r in known)) * 60 / minutes) if minutes else "нет данных")
            self.screen(uid, chat, area, text, [], back="finance")
            if f["categories"]:
                self.store.enqueue(chat, "overview_chart", dict(mode="finance", values=f["categories"],
                                   title="Оплаты · " + self.period_title(uid, "finance")))
        elif area == "dynamics":
            self.screen(uid, chat, area, "Выбери группировку графика. Время без указанной длительности не включается.", [
                ("По неделям", {"action": "chart", "group": "week"}),
                ("По месяцам", {"action": "chart", "group": "month"})], back="sport")

    def listing(self, uid, chat, view, page=0, kind=None, all_time=False, select=None,
                pass_id=None, cash_filter="all"):
        start, end = self.period(uid, "finance" if view == "operations" else "sport")
        if all_time:
            start, end = "2000-01-01", self.bot.today().isoformat()
        parent = "finance" if view in {"operations", "passes", "deposits"} else "sport"
        if pass_id:
            parent = {"action": "card", "entity": "pass", "id": pass_id}
        items = []
        if view == "passes":
            for r in self.store.all_passes(uid):
                target = ({"action": "new", "entity": "deposit", "pass_id": r["id"]} if select == "deposit"
                          else {"action": "card", "entity": "pass", "id": r["id"]})
                items.append((f"Абонемент №{r['id']} · {short(r['number'], 20)} · {money(r['paid'] - r['used'])}", target))
        elif view == "deposits":
            for r in self.store.pass_deposits(uid, pass_id):
                items.append((f"Пополнение №{r['id']} · {day(r['paid_on'])} · {money(r['amount'])}",
                              {"action": "card", "entity": "deposit", "id": r["id"]}))
        elif view == "operations":
            for r in self.store.operations(uid, start, end):
                if cash_filter == "cash" and not r["cash"] or cash_filter == "debit" and r["cash"]:
                    continue
                items.append((f"{day(r['date'])} · {short(r['title'], 24)} · {money(r['amount'])} [{r['entity'][0]}{r['id']}]",
                              {"action": "card", "entity": r["entity"], "id": r["id"]}))
        else:
            entries = []
            if view in {"history", "sessions"}:
                rows = self.store.pass_sessions(uid, pass_id) if pass_id else self.store.records(uid, start, end, kind)
                entries += [(r["played_on"], r["id"], f"{KINDS[r['kind']]} №{r['id']} · {day(r['played_on'])}",
                             {"action": "card", "entity": "session", "id": r["id"]}) for r in rows]
            if view in {"history", "tournaments"}:
                entries += [(r["played_on"], r["id"], f"Турнир №{r['id']} · {day(r['played_on'])} · {short(r['name'], 24)}",
                             {"action": "card", "entity": "tournament", "id": r["id"]})
                            for r in self.store.tournaments_in(uid, start, end)]
            items = [(r[2], r[3]) for r in sorted(entries, key=lambda r: (r[0], r[1]), reverse=True)]
        page = max(0, min(page, max(0, (len(items) - 1) // 8)))
        args = dict(view=view, page=page, kind=kind, all_time=all_time, select=select,
                    pass_id=pass_id, cash_filter=cash_filter)
        actions = items[page * 8:page * 8 + 8]
        if page:
            actions.append(("◀️ Предыдущие", {"action": "list", "args": {**args, "page": page - 1}}))
        if (page + 1) * 8 < len(items):
            actions.append(("Следующие ▶️", {"action": "list", "args": {**args, "page": page + 1}}))
        if view == "passes":
            actions.append(("Добавить абонемент", "pass_method"))
        if view == "operations":
            actions += [(label, {"action": "list", "args": {**args, "page": 0, "cash_filter": value}})
                        for label, value in [("Все операции", "all"), ("Только оплаты", "cash"), ("Только списания", "debit")]]
        title = {"passes": "Абонементы · текущие остатки", "deposits": "Пополнения (включая неизвестные даты)",
                 "operations": "Операции", "history": "История", "sessions": "Занятия", "tournaments": "Турниры"}[view]
        if view not in {"passes", "deposits"} and not pass_id:
            title += " · " + day(start) + " — " + day(end)
        self.screen(uid, chat, "list", title + (f"\nВсего: {len(items)} · страница {page + 1}" if items else "\nЗаписей пока нет."),
                    actions, back=parent, list_args=args)

    def describe(self, entity, data):
        lines = []
        if entity == "session":
            lines.append(KINDS[data["kind"]])
        elif entity == "tournament":
            lines.append("Турнир")
        elif entity == "expense":
            lines.append("Расход")
        else:
            lines.append("Пополнение абонемента №" + str(data["pass_id"]))
        for key in ("name", "date", "club", "coach", "minutes", "participants", "format",
                    "opponent", "score", "result", "cost", "place", "category", "note"):
            if key not in data or (entity == "session" and key == "coach" and data["kind"] != "training") or (entity == "session" and key == "cost" and data["kind"] == "match"):
                continue
            value = data[key]
            if key == "date":
                value = day(value)
            elif key == "cost":
                value = money(value)
            elif value is None or value == "":
                value = "не указано"
            lines.append(LABELS[key] + ": " + str(value))
        if entity == "session" and data["kind"] != "match":
            lines.append("Оплата: " + (f"абонемент №{data['pass_id']}" if data.get("pass_id") else "отдельно"))
        if data.get("session_id"):
            lines.append("Связано с занятием №" + str(data["session_id"]))
        if data.get("tournament_id"):
            lines.append("Турнир №" + str(data["tournament_id"]))
        return "\n".join(lines)

    def record_data(self, entity, row):
        data = dict(row)
        data["date"] = row.get("played_on", row.get("paid_on"))
        data["cost"] = row.get("cost_kopecks", row.get("fee", row.get("amount", 0)))
        return data

    def card(self, uid, chat, entity, record_id):
        row = self.store.owned_record(uid, entity, record_id)
        if not row:
            raise ValueError("Запись не найдена. Вернись в главное меню.")
        back = "finance" if entity in {"pass", "deposit", "expense"} else "sport"
        if entity == "pass":
            text = (f"Абонемент №{row['id']} · {row['number']}\nКлуб: {row['club'] or 'не указан'}\n"
                    f"Пополнения: {money(row['paid'])}\nСтоимость занятий: {money(row['used'])}\n"
                    f"Остаток: {money(row['paid'] - row['used'])}")
            actions = [
                ("Пополнить", {"action": "new", "entity": "deposit", "pass_id": record_id}),
                ("Занятия по абонементу", {"action": "list", "args": {"view": "sessions", "pass_id": record_id}}),
                ("Пополнения и их даты", {"action": "list", "args": {"view": "deposits", "pass_id": record_id}}),
                ("Привязать ранее записанные тренировки", {"action": "link", "id": record_id}),
                ("Указать клуб", {"action": "pass_club", "id": record_id})]
        else:
            text = f"Запись №{record_id}\n" + self.describe(entity, self.record_data(entity, row))
            actions = [("Изменить", {"action": "edit", "entity": entity, "id": record_id}),
                       ("Удалить", {"action": "delete", "entity": entity, "id": record_id})]
            if entity == "tournament":
                matches = self.store.matches(uid, record_id)
                counts = Counter(r["result"] for r in matches)
                text += f"\n\nМатчей: {len(matches)} · побед: {counts['Победа']} · поражений: {counts['Поражение']}"
                actions.insert(0, ("Добавить матч", {"action": "new", "entity": "session", "kind": "match", "tournament_id": record_id}))
                actions.insert(1, ("Матчи турнира", {"action": "matches", "id": record_id}))
            if entity == "deposit":
                back = {"action": "card", "entity": "pass", "id": row["pass_id"]}
        self.screen(uid, chat, "card", text, actions, back=back, entity=entity, record_id=record_id)

    def start_form(self, uid, chat, entity, kind=None, pass_id=None, tournament_id=None, record_id=None):
        pref = self.store.preferences(uid)
        if entity == "session" and not record_id:
            last = self.store.db.execute(
                "SELECT club,coach,minutes FROM sessions WHERE user_id=? AND kind=? ORDER BY played_on DESC,id DESC LIMIT 1",
                (uid, kind)).fetchone()
            if last:
                for field in ("club", "coach", "minutes"):
                    if field not in pref:
                        pref[field] = last[field]
        data = {}
        original = self.store.owned_record(uid, entity, record_id) if record_id else None
        if record_id and not original:
            raise ValueError("Запись не найдена.")
        if entity == "session":
            if original:
                kind = original["kind"]
            data = dict(kind=kind, club=pref.get("club"), minutes=pref.get("minutes"),
                        participants=None, cost=0, pass_id=pass_id, coach=pref.get("coach") if kind == "training" else "",
                        tournament_id=tournament_id)
            if kind != "match":
                data.pop("cost")
            fields = ["date", "club"]
            if kind == "training":
                fields += ["coach", "minutes", "participants", "cost", "pass_id", "note"]
                last = self.store.db.execute("""SELECT pass_id FROM sessions WHERE user_id=? AND kind='training'
                    AND pass_id IS NOT NULL ORDER BY played_on DESC,id DESC LIMIT 1""", (uid,)).fetchone()
                if last:
                    data["pass_id"] = last[0]
            else:
                fields += ["minutes", "format", "participants", "opponent", "score", "result"]
                if kind == "game":
                    fields += ["cost"]
                fields += ["note"]
            if tournament_id:
                tournament = self.store.tournament(uid, tournament_id)
                if not tournament:
                    raise ValueError("Турнир не найден.")
                data.update(date=tournament["played_on"], club=tournament["club"], format=tournament["format"])
        elif entity == "tournament":
            data = dict(club=pref.get("club"))
            fields = ["name", "date", "club", "format", "cost", "place", "note"]
        elif entity == "expense":
            fields = ["date", "category", "cost", "relation", "note"]
        elif entity == "deposit":
            if not original and not self.store.get_pass(uid, pass_id):
                raise ValueError("Сначала выбери абонемент.")
            data = dict(pass_id=pass_id)
            fields = ["date", "cost"]
        else:
            raise ValueError("Неизвестная запись.")
        if original:
            data = self.record_data(entity, original)
        draft = dict(mode="form", step="form", entity=entity, data=data, fields=fields, index=0,
                     token=uuid.uuid4().hex, id=record_id, original=original)
        if original:
            self.choose_field(uid, chat, draft)
        else:
            self.ask(uid, chat, draft)

    def ask(self, uid, chat, draft):
        field = draft["fields"][draft["index"]]
        data = draft["data"]
        draft["step"] = "date" if field == "date" else "form"
        draft["mode"] = "form"
        prompts = {
            "date": "Когда это было? ДД.ММ.ГГГГ или выбери день.",
            "club": "Где играли? Название клуба / корта.",
            "coach": "Кто был тренером?",
            "minutes": "Сколько минут играли?",
            "participants": "Сколько было участников, включая тебя, без тренера?",
            "cost": "Твоя сумма в рублях (0 — бесплатно).",
            "pass_id": "Как оплачена тренировка?",
            "format": "Одиночная или парная игра?",
            "opponent": "Соперник / участники: введи имена одной строкой.",
            "score": "Счёт, например 6:4 3:6 10:8. Можно пропустить.",
            "result": "Какой результат?",
            "note": "Комментарий — необязательно.",
            "name": "Название турнира?",
            "place": "Итоговое место — можно указать позже.",
            "category": "Категория расхода?",
            "relation": "Связать расход с событием? Можно ввести «занятие 12» или «турнир 3» (номера из истории).",
        }
        text = prompts[field]
        if field == "cost":
            text = {"tournament": "Твой взнос за весь турнир, в рублях. Он учитывается один раз.",
                    "deposit": "Сколько денег внесла на абонемент, в рублях?",
                    "expense": "Сумма отдельного расхода, в рублях. Не повторяй сумму, уже включённую в стоимость занятия."}.get(draft["entity"], "Стоимость занятия для тебя, в рублях (0 — бесплатно).")
        keys = []
        options = {}
        pref = self.store.preferences(uid)
        if field == "date":
            keys = [r[:] for r in DATE_KEYS[:-1]]
            # A new token per date field prevents a prior calendar from editing another date.
            draft.setdefault("calendar_token", uuid.uuid4().hex[:12])
            if draft["entity"] == "deposit":
                keys.append(["Не помню"])
        elif field in {"club", "coach"}:
            values = list(pref.get(field + "s", []))
            if field == "coach":
                values += self.store.coaches(uid)
            values = list(dict.fromkeys(values))[:12]
            keys = [[v] for v in values]
            keys.append(["Не помню"])
        elif field == "minutes":
            keys = [["60", "90", "120"], ["Не помню"]]
        elif field == "participants":
            keys = [["1", "2", "3"], ["4", "5", "6"], ["Не помню"]]
        elif field == "format":
            keys = [["Одиночная", "Парная"], ["Не помню"]]
        elif field == "result":
            keys = [[v] for v in RESULTS]
        elif field == "pass_id":
            passes = self.store.all_passes(uid)
            keys = [["Отдельная оплата"]]
            for p in passes:
                label = f"Абонемент №{p['id']} · {short(p['number'], 24)}"
                options[label] = p["id"]
                keys.append([label])
        elif field == "category":
            keys = [[v] for v in CATEGORIES]
        elif field == "relation":
            keys = [["Без привязки"]]
            for r in self.store.history(uid)[:4]:
                label = f"Занятие №{r['id']} · {day(r['played_on'])}"
                options[label] = ["session_id", r["id"]]
                keys.append([label])
            for r in self.store.tournaments_in(uid, "2000-01-01", self.bot.today().isoformat())[:4]:
                label = f"Турнир №{r['id']} · {short(r['name'], 24)}"
                options[label] = ["tournament_id", r["id"]]
                keys.append([label])
        elif field in {"note", "opponent", "score", "place"}:
            keys = [["Пропустить"]]
        if field in data and data[field] is not None and data[field] != "":
            current = data[field]
            if field == "cost":
                current = money(current)
            elif field == "pass_id":
                current = "абонемент №" + str(current)
            text += "\nПредложено: " + str(current)
            keys.insert(0, ["Оставить"])
        draft["options"] = options
        self.put(uid, chat, draft, text, keys + [[BACK], [CANCEL]])

    def choose_field(self, uid, chat, draft):
        draft["mode"], draft["step"] = "choose_field", "choose_field"
        self.put(uid, chat, draft, "Что изменить?",
                 [[LABELS[f]] for f in draft["fields"]] + [[BACK], [CANCEL]])

    def review(self, uid, chat, draft):
        draft["mode"], draft["step"] = "review", "review"
        text = self.describe(draft["entity"], draft["data"])
        p = draft["data"].get("pass_id")
        if p and draft["entity"] == "session":
            row = self.store.get_pass(uid, p)
            if row:
                restored = draft["original"]["cost_kopecks"] if draft.get("original") and draft["original"]["pass_id"] == p else 0
                balance = row["paid"] - row["used"] + restored - draft["data"]["cost"]
                text += "\nОстаток после сохранения: " + money(balance)
                if balance < 0:
                    text += "\n⚠️ Получится отрицательный остаток. Проверь сумму и пополнения."
        if draft["entity"] == "deposit" and not draft["data"].get("date"):
            text += "\nБез даты пополнение войдёт в баланс, но не в оплату за конкретный период."
        self.put(uid, chat, draft, text + "\n\nСохранить?", [["Сохранить", "Изменить"], [CANCEL]])

    def form_input(self, uid, chat, text, draft):
        field = draft["fields"][draft["index"]]
        data = draft["data"]
        if text == BACK:
            if draft.get("editing_field"):
                return self.review(uid, chat, draft)
            draft["index"] = max(0, draft["index"] - 1)
            draft.pop("calendar_token", None)
            return self.ask(uid, chat, draft)
        if field == "date" and text in {"Календарь", "Последние 7 дней"}:
            return self.bot.show_picker(uid, chat, draft, recent=text == "Последние 7 дней")
        if text == "Оставить":
            if field not in data or data[field] is None:
                raise ValueError("Нет предложенного значения. Введи значение.")
            value = data[field]
        elif field == "date":
            value = None if text == "Не помню" and draft["entity"] == "deposit" else self.parse_date(text, self.bot.today())
        elif field == "minutes":
            value = None if text == "Не помню" else self.parse_minutes(text)
        elif field == "participants":
            value = self.parse_people(text)
        elif field == "cost":
            value = self.parse_money(text)
        elif field == "pass_id":
            if text != "Отдельная оплата" and text not in draft["options"]:
                raise ValueError("Выбери абонемент кнопкой или отдельную оплату.")
            value = draft["options"].get(text)
        elif field == "format":
            if text not in {"Одиночная", "Парная", "Не помню"}:
                raise ValueError("Выбери формат кнопкой.")
            value = None if text == "Не помню" else text
        elif field == "result":
            if text not in RESULTS:
                raise ValueError("Выбери результат кнопкой.")
            value = None if text == "Не указан" else text
        elif field == "category":
            if text not in CATEGORIES:
                raise ValueError("Выбери категорию кнопкой.")
            value = text
        elif field == "relation":
            target = draft["options"].get(text)
            match = re.fullmatch(r"(занятие|турнир)\s+([1-9][0-9]{0,17})", text.casefold())
            if match:
                target = ["session_id" if match[1] == "занятие" else "tournament_id", int(match[2])]
            if text != "Без привязки" and not target:
                raise ValueError("Выбери запись кнопкой, введи «занятие 12» / «турнир 3» или «Без привязки».")
            if target:
                reader = self.store.get_session if target[0] == "session_id" else self.store.tournament
                if not reader(uid, target[1]):
                    raise ValueError("Запись не найдена.")
            data.update(session_id=None, tournament_id=None)
            if target:
                data[target[0]] = target[1]
            value = text
        else:
            optional = field in {"club", "coach", "note", "opponent", "score", "place"}
            value = None if optional and text in {"Не помню", "Пропустить"} else self.clean(text, 400 if field == "note" else 80)
        data[field] = value
        draft.pop("calendar_token", None)
        if draft.pop("editing_field", False) or draft["index"] + 1 == len(draft["fields"]):
            self.review(uid, chat, draft)
        else:
            draft["index"] += 1
            self.ask(uid, chat, draft)

    @staticmethod
    def clean(text, limit=80):
        if not text.strip() or len(text) > limit or any(ord(c) < 32 for c in text):
            raise ValueError(f"Введи текст одной строкой, до {limit} символов.")
        if text.startswith("/"):
            raise ValueError("Для названия введи текст без команды.")
        return text.strip()

    def save(self, uid, chat, draft, duplicate_ok=False):
        if draft["entity"] == "session" and not duplicate_ok:
            rows = [r for r in self.store.on_date(uid, draft["data"]["date"]) if r["id"] != draft.get("id")]
            # Editing fields without moving the event to another date is not a new duplicate.
            changed_date = not draft.get("original") or draft["original"]["played_on"] != draft["data"]["date"]
            if rows and changed_date:
                draft["mode"], draft["step"] = "duplicate", "duplicate"
                preview = "\n".join(f"{KINDS[r['kind']]} №{r['id']} · {money(r['cost_kopecks'])}" for r in rows[:5])
                self.put(uid, chat, draft, f"На {day(draft['data']['date'])} уже есть занятий: {len(rows)}.\n" +
                         preview + "\nЭто действительно другое занятие?",
                         [["Да, это другое занятие"], ["Нет, не добавлять"], ["Изменить"]])
                return
        if draft["entity"] == "tournament" and not draft.get("id") and not duplicate_ok:
            rows = self.store.tournaments_in(uid, draft["data"]["date"], draft["data"]["date"])
            if rows:
                draft["mode"], draft["step"] = "duplicate", "duplicate"
                self.put(uid, chat, draft, "На эту дату уже есть турнир. Добавить отдельный турнир?",
                         [["Да, это другое занятие"], ["Нет, не добавлять"], ["Изменить"]])
                return
        record_id = self.store.save_record(uid, draft)
        self.store.set_draft(uid, None)
        self.bot.reply(chat, "Сохранено.\n" + self.describe(draft["entity"], draft["data"]))
        self.card(uid, chat, draft["entity"], record_id)

    def presets(self, uid, chat, field):
        pref = self.store.preferences(uid)
        values = list(pref.get(field + "s", []))
        if field == "coach":
            values += self.store.coaches(uid)
        if pref.get(field):
            values.insert(0, pref[field])
        values = list(dict.fromkeys(values))
        self.screen(uid, chat, "presets", "Выбери значение, чтобы сделать его основным или переименовать.\n"
                    "Старые записи при переименовании справочника не меняются.", [
            (v, {"action": "preset_item", "field": field, "value": v}) for v in values] +
            [("Добавить", {"action": "setting", "field": field}),
             ("Не предлагать по умолчанию", {"action": "clear_default", "field": field})], back="settings")

    def setting_input(self, uid, chat, text, draft):
        field = draft["field"]
        pref = self.store.preferences(uid)
        if field == "minutes":
            pref[field] = self.parse_minutes(text)
        elif field == "utc_offset":
            if not re.fullmatch(r"[+-]?[0-9]{1,2}", text) or not -12 <= int(text) <= 14:
                raise ValueError("Введи целое смещение UTC от -12 до 14, например 3 для Москвы.")
            pref[field] = int(text)
        elif field == "pass_club":
            name = self.clean(text)
            if not self.store.get_pass(uid, draft["pass_id"]):
                raise ValueError("Абонемент не найден.")
            self.store.db.execute("UPDATE passes SET club=? WHERE id=? AND user_id=?", (name, draft["pass_id"], uid))
            return self.card(uid, chat, "pass", draft["pass_id"])
        else:
            name = self.clean(text)
            values = list(pref.get(field + "s", []))
            old = draft.get("old")
            if old:
                values = [name if v == old else v for v in values]
                if pref.get(field) == old:
                    pref[field] = name
            else:
                pref[field] = name
            values.append(name)
            if len(set(values)) > 40:
                raise ValueError("В справочнике уже 40 значений. Переименуй существующее.")
            pref[field + "s"] = list(dict.fromkeys(values))
        self.store.set_preferences(uid, pref)
        self.show(uid, chat, "settings")

    def execute(self, uid, chat, target):
        if isinstance(target, str):
            return self.show(uid, chat, target)
        action = target["action"]
        if action == "new":
            return self.start_form(uid, chat, target["entity"], target.get("kind"), target.get("pass_id"), target.get("tournament_id"))
        if action == "edit":
            return self.start_form(uid, chat, target["entity"], record_id=target["id"])
        if action == "card":
            return self.card(uid, chat, target["entity"], target["id"])
        if action == "list":
            return self.listing(uid, chat, **target["args"])
        if action == "import":
            return self.bot.passes.start(uid, chat, target["ai"])
        if action == "link":
            return self.bot.links.start(uid, chat, target["id"])
        if action == "matches":
            rows = self.store.matches(uid, target["id"])
            draft_args = dict(view="matches", tournament_id=target["id"], page=0)
            return self.match_page(uid, chat, draft_args)
        if action == "match_page":
            return self.match_page(uid, chat, target["args"])
        if action == "delete":
            original = self.store.owned_record(uid, target["entity"], target["id"])
            if not original:
                raise ValueError("Запись не найдена.")
            warning = "Удалить эту запись?"
            if target["entity"] == "tournament":
                warning += "\nВсе матчи этого турнира тоже будут удалены. Отдельные расходы останутся."
            if target["entity"] in {"session", "deposit"}:
                warning += "\nБаланс абонемента и статистика будут пересчитаны."
            return self.put(uid, chat, dict(step="delete_record", mode="delete_record", entity=target["entity"], original=original),
                            warning, [["Да, удалить"], [BACK]])
        if action == "period":
            if target["value"] != "custom":
                pref = self.store.preferences(uid)
                pref[target["area"] + "_period"] = target["value"]
                self.store.set_preferences(uid, pref)
                return self.show(uid, chat, target["area"])
            return self.put(uid, chat, dict(step="date", mode="period_start", area=target["area"]),
                            "Начало периода: выбери день или введи ДД.ММ.ГГГГ.", DATE_KEYS)
        if action == "chart":
            rows = self.store.records(uid, *self.period(uid, "sport"))
            if not rows:
                return self.bot.reply(chat, "За выбранный период нет занятий.", [["⬅️ Главное меню"]])
            self.store.enqueue(chat, "overview_chart", dict(mode="sport", rows=rows, group=target["group"],
                               title="Теннис · " + self.period_title(uid, "sport"),
                               start=self.period(uid, "sport")[0], end=self.period(uid, "sport")[1]))
            return
        if action == "presets":
            return self.presets(uid, chat, target["field"])
        if action == "preset_item":
            return self.screen(uid, chat, "preset_item", target["value"], [
                ("Сделать основным", {**target, "action": "set_default"}),
                ("Переименовать", {"action": "setting", "field": target["field"], "old": target["value"]})], back="settings")
        if action in {"set_default", "clear_default"}:
            pref = self.store.preferences(uid)
            pref[target["field"]] = target.get("value")
            self.store.set_preferences(uid, pref)
            return self.show(uid, chat, "settings")
        if action in {"setting", "pass_club"}:
            field = target.get("field", "pass_club")
            prompt = {"minutes": "Обычная длительность в минутах?",
                      "utc_offset": "Смещение UTC: например 3 для Москвы (от -12 до 14).",
                      "coach": "Имя тренера?", "club": "Название клуба?", "pass_club": "Название клуба абонемента?"}[field]
            return self.put(uid, chat, dict(step="setting", mode="setting", field=field, old=target.get("old"),
                                           pass_id=target.get("id")), prompt, [[BACK]])

    def match_page(self, uid, chat, args):
        tid, page = args["tournament_id"], args["page"]
        if not self.store.tournament(uid, tid):
            raise ValueError("Турнир не найден.")
        rows = self.store.matches(uid, tid)
        page = max(0, min(page, max(0, (len(rows) - 1) // 8)))
        actions = [(f"Матч №{r['id']} · {short(r['opponent'] or 'соперник не указан', 25)} · {r['result'] or 'без результата'}",
                    {"action": "card", "entity": "session", "id": r["id"]}) for r in rows[page*8:page*8+8]]
        if page:
            actions.append(("◀️ Предыдущие", {"action": "match_page", "args": {**args, "page": page-1}}))
        if (page+1)*8 < len(rows):
            actions.append(("Следующие ▶️", {"action": "match_page", "args": {**args, "page": page+1}}))
        self.screen(uid, chat, "matches", f"Матчи турнира · всего {len(rows)}", actions,
                    back={"action": "card", "entity": "tournament", "id": tid})

    def route(self, uid, chat, text):
        command = text.split()[0].split("@")[0].casefold() if text else ""
        top = {"➕ Добавить запись": "add", "🎾 Мой теннис": "sport", "💳 Финансы": "finance",
               "⚙️ Настройки": "settings", "⬅️ Главное меню": "home", "/start": "home",
               "/settings": "settings", "/menu": "home"}
        if text in top or command in {"/start", "/settings", "/menu"}:
            self.show(uid, chat, top.get(text, top.get(command, "home")))
            return True
        draft = self.store.draft(uid)
        if command == "/passes":
            self.listing(uid, chat, "passes")
            return True
        if not draft or draft.get("flow") != "hub" or command.startswith("/"):
            return False
        mode = draft.get("mode", "screen")
        if text == CANCEL:
            self.show(uid, chat, "home")
            return True
        if mode == "screen":
            if text == BACK:
                self.show(uid, chat, draft.get("back", "home"))
            elif text in draft["choices"]:
                self.execute(uid, chat, draft["choices"][text])
            else:
                raise ValueError("Выбери действие кнопкой. Главное меню: /menu")
        elif mode == "form":
            self.form_input(uid, chat, text, draft)
        elif mode in {"review", "duplicate"}:
            if text == "Изменить":
                self.choose_field(uid, chat, draft)
            elif text == "Сохранить" and mode == "review":
                self.save(uid, chat, draft)
            elif text == "Да, это другое занятие" and mode == "duplicate":
                self.save(uid, chat, draft, duplicate_ok=True)
            elif text == "Нет, не добавлять" and mode == "duplicate":
                self.show(uid, chat, "home")
            else:
                raise ValueError("Подтверди действие кнопкой или отмени ввод.")
        elif mode == "choose_field":
            if text == BACK:
                self.review(uid, chat, draft)
            else:
                fields = {LABELS[f]: i for i, f in enumerate(draft["fields"])}
                if text not in fields:
                    raise ValueError("Выбери поле кнопкой.")
                draft.update(index=fields[text], editing_field=True)
                draft.pop("calendar_token", None)
                self.ask(uid, chat, draft)
        elif mode == "setting":
            if text == BACK:
                self.show(uid, chat, "settings")
            else:
                self.setting_input(uid, chat, text, draft)
        elif mode == "delete_record":
            if text == BACK:
                self.card(uid, chat, draft["entity"], draft["original"]["id"])
            elif text == "Да, удалить":
                self.store.remove_record(uid, draft["entity"], draft["original"])
                self.bot.reply(chat, "Запись удалена. Статистика и остатки обновлены.")
                self.show(uid, chat, "home")
            else:
                raise ValueError("Подтверди удаление кнопкой или вернись назад.")
        elif mode in {"period_start", "period_end"}:
            if text in {"Календарь", "Последние 7 дней"}:
                self.bot.show_picker(uid, chat, draft, recent=text == "Последние 7 дней")
            else:
                value = self.parse_date(text, self.bot.today())
                if mode == "period_start":
                    draft.update(start=value, mode="period_end")
                    draft.pop("calendar_token", None)
                    self.put(uid, chat, draft, "Конец периода: выбери день или введи ДД.ММ.ГГГГ.", DATE_KEYS)
                else:
                    if value < draft["start"]:
                        raise ValueError("Конец периода не может быть раньше начала.")
                    pref = self.store.preferences(uid)
                    pref[draft["area"] + "_period"] = [draft["start"], value]
                    self.store.set_preferences(uid, pref)
                    self.show(uid, chat, draft["area"])
        return True
