"""Напоминания о долгах: через N дней после заказа бот сам напоминает владельцу, кто не заплатил."""
from datetime import date, datetime, time, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db.models import Client, Order
from ..money import fmt_money
from . import attachments, balance

MAX_CLIENTS = 12
MAX_ORDERS_PER_CLIENT = 5


def due(s: Session, today: date, days: int, force: bool = False) -> list[dict]:
    """Должники, у которых есть неоплаченные заказы старше N дней.

    Заказ повторно попадает в список не раньше чем через N дней после прошлого напоминания
    (force=True игнорирует это, нужно для команды /remind).
    """
    cutoff = today - timedelta(days=days)
    limit = datetime.combine(cutoff + timedelta(days=1), time.min)
    client_ids = s.scalars(select(Order.client_id).where(
        Order.status == "active", Order.client_id.is_not(None), Order.created_at < limit).distinct())
    entries = []
    for cid in client_ids:
        orders = []
        for o, debt in balance.open_orders(s, cid):
            if o.created_at.date() > cutoff:
                continue
            if not force and o.last_reminded_on and o.last_reminded_on > cutoff:
                continue
            files = [a.file_name or ("фото" if a.media == "photo" else "файл")
                     for a in attachments.for_order(s, o.id)]
            orders.append(dict(id=o.id, day=o.created_at.date(), debt=debt, files=files,
                               age=(today - o.created_at.date()).days))
        if orders:
            orders.sort(key=lambda x: (x["day"], x["id"]))
            entries.append(dict(client_id=cid, name=s.get(Client, cid).name,
                                debt=sum(x["debt"] for x in orders), orders=orders))
    entries.sort(key=lambda e: e["orders"][0]["day"])  # самые давние долги сверху
    return entries


def _client_text(e: dict) -> str:
    parts = []
    for o in e["orders"][:MAX_ORDERS_PER_CLIENT]:
        what = f"заказ от {o['day']:%d.%m}" + (f" ({', '.join(o['files'])})" if o["files"] else "")
        parts.append(f"{what} — {fmt_money(o['debt'])}")
    return (f"{e['name']}, здравствуйте! Напоминаю про оплату: " + "; ".join(parts)
            + (f". Всего {fmt_money(e['debt'])}." if len(parts) > 1 else "."))


def digest(entries: list[dict], days: int) -> tuple[str, list[int], list[int]]:
    """(текст сообщения, id клиентов для кнопок, id показанных заказов)."""
    shown = entries[:MAX_CLIENTS]
    lines = [f"Напоминание: не оплачено больше {days} дн.", ""]
    order_ids, client_ids = [], []
    for e in shown:
        client_ids.append(e["client_id"])
        lines.append(f"{e['name']} — долг {fmt_money(e['debt'])}")
        for o in e["orders"][:MAX_ORDERS_PER_CLIENT]:
            order_ids.append(o["id"])
            files = f", файл: {', '.join(o['files'])}" if o["files"] else ""
            lines.append(f"  заказ №{o['id']} от {o['day']:%d.%m} ({o['age']} дн.): {fmt_money(o['debt'])}{files}")
        lines.append(f"  Текст клиенту: «{_client_text(e)}»")
        lines.append("")
    if len(entries) > len(shown):
        lines.append(f"…и ещё должников: {len(entries) - len(shown)} (придут в следующем напоминании)")
    return "\n".join(lines).rstrip(), client_ids, order_ids


def mark_reminded(s: Session, order_ids: list[int], today: date) -> None:
    """Запоминает дату напоминания. Обычный UPDATE: не вызывает лишную отправку копии базы."""
    if order_ids:
        s.execute(update(Order).where(Order.id.in_(order_ids)).values(last_reminded_on=today))
