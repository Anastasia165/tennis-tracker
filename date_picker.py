"""Inline date picker; callback data is bound to one draft."""
import calendar
from datetime import date, timedelta

MONTHS = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")
DATE_KEYS = [["Сегодня", "Вчера"], ["Последние 7 дней", "Календарь"], ["Отменить ввод"]]


def picker(token, first, today, recent=False):
    def button(text, action="noop", value=""):
        return {"text": text, "callback_data": f"cal:{token}:{action}:{value}"}
    if recent:
        days = [today - timedelta(days=i) for i in range(7)]
        rows = [[button(f"{day:%d.%m} · " + ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")[day.weekday()],
                        "day", day.isoformat())] for day in days if day.year >= 2000]
        return {"text": "Выбери день тренировки:", "reply_markup": {"inline_keyboard": rows}}
    rows = [[button(label) for label in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")]]
    for week in calendar.monthcalendar(first.year, first.month):
        rows.append([button(str(day), "day", first.replace(day=day).isoformat())
                     if day and first.replace(day=day) <= today else button("·") for day in week])
    nav = []
    for delta, label in ((-12, "« Год"), (-1, "‹ Месяц"), (1, "Месяц ›"), (12, "Год »")):
        index = first.year * 12 + first.month - 1 + delta
        target = date(index // 12, index % 12 + 1, 1)
        if date(2000, 1, 1) <= target <= today.replace(day=1):
            nav.append(button(label, "month", target.isoformat()))
    if nav:
        rows.append(nav)
    return {"text": f"{MONTHS[first.month - 1]} {first.year} — выбери день:",
            "reply_markup": {"inline_keyboard": rows}}
