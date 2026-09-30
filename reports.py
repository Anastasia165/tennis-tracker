"""Monthly text summaries and PNG charts, generated in memory."""
import calendar
import io
from datetime import date
from decimal import Decimal, ROUND_HALF_UP


def money(kopecks):
    rubles = (Decimal(kopecks) / 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{rubles:,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def summary(rows, month):
    count = len(rows)
    known = [row for row in rows if row["minutes"] is not None]
    minutes = sum(row["minutes"] for row in known)
    cost = sum(row["cost_kopecks"] for row in rows)
    if not count:
        return f"Теннис · {month}\nЗа этот месяц тренировок пока нет."
    average_cost = Decimal(cost) / count
    per_hour = Decimal(sum(row["cost_kopecks"] for row in known)) * 60 / minutes if minutes else None
    average_duration = f"{minutes / len(known):.0f} мин" if known else "не указана"
    return (
        f"Теннис · {month}\n\n"
        f"Тренировок: {count}\n"
        f"Время на корте: {minutes // 60} ч {minutes % 60} мин\n"
        f"Средняя длительность: {average_duration}\n"
        f"Длительность известна для {len(known)} из {count} занятий.\n\n"
        f"Стоимость состоявшихся занятий: {money(cost)}\n"
        f"Средняя стоимость занятия: {money(average_cost)}\n"
        f"Стоимость часа (занятия с известной длительностью): {money(per_hour) if per_hour is not None else 'неизвестна'}\n\n"
        "Стоимость отнесена к дате занятия. Пополнения абонементов показаны отдельно в /passes."
    )


def chart(rows, month, today=None):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.ticker import MaxNLocator

    year, month_number = map(int, month.split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    today = today or date.today()
    if month == today.strftime("%Y-%m"):
        last_day = today.day
    days = list(range(1, last_day + 1))
    counts = [0] * last_day
    hours = [0.0] * last_day
    rubles = [0.0] * last_day
    for row in rows:
        index = date.fromisoformat(row["played_on"]).day - 1
        if 0 <= index < last_day:
            counts[index] += 1
            hours[index] += (row["minutes"] or 0) / 60
            rubles[index] += row["cost_kopecks"] / 100

    fig = Figure(figsize=(9, 8), dpi=130, layout="constrained", facecolor="#f8fafc")
    FigureCanvasAgg(fig)
    axes = fig.subplots(3, 1, sharex=True)
    for ax, values, title in zip(
        axes, [counts, hours, rubles],
        ["Посещения, шт.", "Известное время, ч", "Стоимость занятий, ₽"],
    ):
        ax.set_facecolor("#f8fafc")
        ax.bar(days, values, width=0.7, color="#237b68", zorder=3)
        ax.set_ylabel(title, fontsize=10)
        ax.grid(axis="y", color="#dce2e8", linewidth=0.7, zorder=0)
        ax.spines[["top", "right"]].set_visible(False)
        ax.set_ylim(bottom=0, top=max(values, default=0) * 1.25 or 1)
        ax.tick_params(labelsize=9)
        ax.set_xlim(0.25, last_day + 0.75)
    axes[0].yaxis.set_major_locator(MaxNLocator(integer=True))
    ticks = list(range(1, last_day + 1, max(1, last_day // 8)))
    if ticks[-1] != last_day:
        ticks.append(last_day)
    axes[-1].set_xticks(ticks)
    axes[-1].set_xlabel("День месяца")
    fig.suptitle(f"Теннис · {month}", fontsize=17, fontweight="bold")
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png")
    return buffer.getvalue()


def overview_chart(payload):
    """Separate sports attendance/time from cash payments; generate an in-memory PNG."""
    from collections import defaultdict
    from datetime import timedelta
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.ticker import MaxNLocator

    fig = Figure(figsize=(10, 7), dpi=130, layout="constrained", facecolor="#f8fafc")
    FigureCanvasAgg(fig)
    if payload["mode"] == "finance":
        values = payload["values"]
        ax = fig.subplots()
        names = list(values)
        ax.barh(names, [values[n] / 100 for n in names], color="#237b68")
        ax.set_xlabel("Оплачено, ₽")
        ax.invert_yaxis()
    else:
        buckets = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        for row in payload["rows"]:
            played = date.fromisoformat(row["played_on"])
            key = (played.replace(day=1) if payload["group"] == "month"
                   else played - timedelta(days=played.weekday())).isoformat()
            values = buckets[key][row["kind"]]
            values[0] += 1
            values[1] += (row["minutes"] or 0) / 60
        if payload.get("start") and payload.get("end"):
            first, last = date.fromisoformat(payload["start"]), date.fromisoformat(payload["end"])
            cursor = first.replace(day=1) if payload["group"] == "month" else first - timedelta(days=first.weekday())
            while cursor <= last:
                buckets[cursor.isoformat()]
                cursor = ((cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
                          if payload["group"] == "month" else cursor + timedelta(days=7))
        labels = sorted(buckets)
        axes = fig.subplots(2, 1, sharex=True)
        for i, (kind, title, color) in enumerate([
            ("training", "Тренировки", "#237b68"), ("game", "Игры", "#478bd1"), ("match", "Матчи турниров", "#c48836")
        ]):
            for metric, ax in enumerate(axes):
                ax.bar([n + (i-1)*0.25 for n in range(len(labels))],
                       [buckets[key][kind][metric] for key in labels], width=0.24, label=title, color=color)
        axes[0].set_ylabel("Занятий")
        axes[0].yaxis.set_major_locator(MaxNLocator(integer=True))
        axes[0].legend()
        axes[1].set_ylabel("Известное время, ч")
        stride = max(1, (len(labels) + 11) // 12)
        positions = list(range(0, len(labels), stride))
        axes[1].set_xticks(positions, [labels[i] for i in positions], rotation=35, ha="right")
        axes[1].set_xlabel("Начало месяца" if payload["group"] == "month" else "Начало недели")
    for ax in fig.axes:
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=0.2)
    fig.suptitle(payload["title"], fontsize=14)
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png")
    return buffer.getvalue()
