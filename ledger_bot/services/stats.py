import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db.models import (Adjustment, Client, Order, OrderItem, Payment, PaymentAllocation,
                         Service, Transaction)
from ..money import fmt_money
from . import balance

PERIODS = {"today": "Сегодня", "week": "Неделя", "month": "Месяц",
           "prev": "Прошлый месяц", "all": "Всё время"}


def period_range(name: str, today: date):
    if name == "today":
        return today, today
    if name == "week":
        return today - timedelta(days=today.weekday()), today
    if name == "month":
        return today.replace(day=1), today
    if name == "prev":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    return None, None


@dataclass
class Stats:
    start: date | None
    end: date | None
    orders_count: int
    orders_value: int
    payments: int
    refunds: int
    other_income: int
    expenses: int
    total_debt: int
    total_credit: int

    @property
    def net_received(self) -> int:
        return self.payments - self.refunds

    @property
    def money_movement(self) -> int:
        return self.net_received + self.other_income - self.expenses


def compute(s: Session, start: date | None, end: date | None) -> Stats:
    sdt = datetime.combine(start, datetime.min.time()) if start else None
    edt = datetime.combine(end + timedelta(days=1), datetime.min.time()) if end else None

    def dt_between(col, q):
        if sdt:
            q = q.where(col >= sdt)
        if edt:
            q = q.where(col < edt)
        return q

    cnt, val = s.execute(dt_between(Order.created_at, select(
        func.count(Order.id), func.coalesce(func.sum(Order.total_minor), 0)
    ).where(Order.status == "active"))).one()

    def pay_sum(kind):
        return s.execute(dt_between(Payment.created_at, select(
            func.coalesce(func.sum(Payment.amount_minor), 0)).where(Payment.kind == kind))).scalar()

    def tr_sum(kind):
        q = select(func.coalesce(func.sum(Transaction.amount_minor), 0)).where(
            Transaction.kind == kind, Transaction.voided_at.is_(None))
        if start:
            q = q.where(Transaction.date >= start)
        if end:
            q = q.where(Transaction.date <= end)
        return s.execute(q).scalar()

    active_total = s.execute(select(func.coalesce(func.sum(Order.total_minor), 0)).where(
        Order.status == "active")).scalar()
    alloc = s.execute(select(func.coalesce(func.sum(PaymentAllocation.amount_minor), 0)).join(
        Order, Order.id == PaymentAllocation.order_id).where(
        Order.status == "active", PaymentAllocation.released_at.is_(None))).scalar()
    adj = s.execute(select(func.coalesce(func.sum(Adjustment.amount_minor), 0)).join(
        Order, Order.id == Adjustment.order_id).where(Order.status == "active")).scalar()
    credit = sum(max(balance.client_credit(s, cid), 0) for cid in s.scalars(select(Client.id)))
    return Stats(start, end, cnt, val, pay_sum("payment"), pay_sum("refund"),
                 tr_sum("income"), tr_sum("expense"), active_total - alloc - adj, credit)


def format_stats(st: Stats, title: str) -> str:
    rng = f" ({st.start:%d.%m.%Y}–{st.end:%d.%m.%Y})" if st.start and st.end else ""
    mv = st.money_movement
    sign = "+" if mv > 0 else ""
    return (
        f"Статистика: {title}{rng}\n\n"
        f"Создано заказов: {st.orders_count} на {fmt_money(st.orders_value)}\n"
        f"Получено от клиентов: {fmt_money(st.net_received)}"
        + (f" (возвраты: {fmt_money(st.refunds)})" if st.refunds else "") + "\n"
        f"Другие доходы: {fmt_money(st.other_income)}\n"
        f"Расходы: {fmt_money(st.expenses)}\n\n"
        f"Движение денег: {sign}{fmt_money(mv)}\n"
        "(полученное + другие доходы − расходы. Это не чистая прибыль: "
        "в расходах только то, что вы внесли сами.)\n\n"
        f"Долг клиентов сейчас: {fmt_money(st.total_debt)} (это ещё не полученные деньги)\n"
        f"Предоплаты клиентов: {fmt_money(st.total_credit)}"
    )


WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
BAR_WIDTH = 12


def daily_flows(s: Session, start: date | None, end: date | None) -> dict[date, list[int]]:
    """По дням: [получено, расходы]. Получено = оплаты − возвраты + прочие доходы."""
    flows: dict[date, list[int]] = defaultdict(lambda: [0, 0])
    pq = select(Payment.created_at, Payment.kind, Payment.amount_minor)
    if start:
        pq = pq.where(Payment.created_at >= datetime.combine(start, datetime.min.time()))
    if end:
        pq = pq.where(Payment.created_at < datetime.combine(end + timedelta(days=1), datetime.min.time()))
    for dt, kind, amount in s.execute(pq):
        if kind == "payment":
            flows[dt.date()][0] += amount
        elif kind == "refund":
            flows[dt.date()][0] -= amount
    tq = select(Transaction.date, Transaction.kind, Transaction.amount_minor).where(Transaction.voided_at.is_(None))
    if start:
        tq = tq.where(Transaction.date >= start)
    if end:
        tq = tq.where(Transaction.date <= end)
    for d, kind, amount in s.execute(tq):
        flows[d][0 if kind == "income" else 1] += amount
    return flows


def chart_rows(key: str, start: date | None, end: date | None, flows) -> list[tuple[str, int, int]]:
    """Столбики: день (неделя), неделя (месяц), месяц (всё время)."""
    def total(days):
        return sum(flows[d][0] for d in days if d in flows), sum(flows[d][1] for d in days if d in flows)

    if key == "today":
        return [("Сегодня", *total([end]))]
    if key == "week":
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        return [(f"{WEEKDAYS[d.weekday()]} {d:%d.%m}", *total([d])) for d in days]
    if key in ("month", "prev"):
        rows, a = [], start
        while a <= end:
            b = min(a + timedelta(days=6), end)
            days = [a + timedelta(days=i) for i in range((b - a).days + 1)]
            rows.append((f"{a:%d}–{b:%d.%m}", *total(days)))
            a = b + timedelta(days=1)
        return rows
    if not flows:  # всё время: по месяцам, последние 12
        return []
    first, last = min(flows), max(flows)
    y, m, rows = first.year, first.month, []
    while (y, m) <= (last.year, last.month):
        days = [d for d in flows if (d.year, d.month) == (y, m)]
        rows.append((f"{m:02d}.{y}", *total(days)))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return rows[-12:]


def _bar(value: int, top: int, ch: str) -> str:
    if value <= 0:
        return "·"
    return ch * max(1, round(value / top * BAR_WIDTH))


def format_chart(rows: list[tuple[str, int, int]]) -> str:
    """Текстовая гистограмма: █ получено, ▒ расходы (длина считается от самого большого значения)."""
    top = max((max(i, e) for _, i, e in rows), default=0)
    if top <= 0:
        return ""
    out = ["Гистограмма: █ получено, ▒ расходы"]
    for label, inc, exp in rows:
        out.append("")
        out.append(label)
        out.append(f"{_bar(inc, top, '█')} {fmt_money(inc)}")
        if exp:
            out.append(f"{_bar(exp, top, '▒')} {fmt_money(exp)}")
    return "\n".join(out)


def chart_text(s: Session, key: str, start: date | None, end: date | None) -> str:
    return format_chart(chart_rows(key, start, end, daily_flows(s, start, end)))


# ---------- расход бумаги ----------
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь",
          "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]


def pack_sheets() -> int:
    """Листов в пачке (по умолчанию 500; можно поменять переменной PAPER_PACK_SHEETS)."""
    try:
        return max(1, int(os.getenv("PAPER_PACK_SHEETS", "500")))
    except ValueError:
        return 500


def _month_back(d: date, n: int) -> date:
    y, m = d.year, d.month - n
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def paper_by_month(s: Session, today: date, months: int = 3) -> list[tuple[date, int]]:
    """Листов бумаги по месяцам (от старых к новым, последний — текущий месяц).

    Считаются позиции услуг «по листам» в неотменённых заказах: у двусторонней печати в заказе уже
    листы, а лишняя нечётная страница занимает свой лист. Конспекты и услуги не по листам не входят.
    """
    first = _month_back(today, months - 1)
    rows = s.execute(
        select(Order.created_at, OrderItem.qty_milli)
        .join(OrderItem, OrderItem.order_id == Order.id)
        .join(Service, Service.id == OrderItem.service_id)
        .where(Order.status != "cancelled", Service.calc_type == "sheet",
               Order.created_at >= datetime.combine(first, datetime.min.time()))).all()
    milli: dict[tuple[int, int], int] = defaultdict(int)
    for created, qty in rows:
        milli[(created.year, created.month)] += qty
    out = []
    for i in range(months - 1, -1, -1):
        m = _month_back(today, i)
        out.append((m, -(-milli[(m.year, m.month)] // 1000)))
    return out


def _packs(sheets: int, pack: int) -> str:
    return f"{sheets / pack:.1f}".replace(".", ",").replace(",0", "")


def format_paper(rows: list[tuple[date, int]], today: date, pack: int | None = None) -> str:
    pack = pack or pack_sheets()
    lines = [f"Расход бумаги (пачка = {pack} л.)", ""]
    for m, sheets in reversed(rows):
        mark = f" (идёт, на {today:%d.%m})" if (m.year, m.month) == (today.year, today.month) else ""
        lines.append(f"{MONTHS[m.month - 1].capitalize()} {m.year}{mark}: {sheets} л. ≈ {_packs(sheets, pack)} пач.")
    full = [sheets for m, sheets in rows if (m.year, m.month) != (today.year, today.month)]
    basis = max(full) if full else (rows[-1][1] if rows else 0)
    if basis:
        lines += ["", f"Чтобы хватило на месяц, как самый большой из прошлых: {basis} л. = "
                      f"{-(-basis // pack)} пач. (с запасом до целой пачки)."]
    else:
        lines += ["", "Пока нет заказов с печатью по листам."]
    lines.append("Считаются заказы с печатью по листам; конспекты и отменённые заказы не входят.")
    return "\n".join(lines)
