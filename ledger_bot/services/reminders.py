"""Напоминания о долгах: через N дней после заказа бот сам напоминает владельцу, кто не заплатил."""
import html
from datetime import date, datetime, time, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..db.models import Client, Order
from ..money import fmt_money
from . import attachments, balance

MAX_CLIENTS = 12
MAX_ORDERS_PER_CLIENT = 5
TELEGRAM_SAFE_LEN = 3500  # лимит сообщения Telegram — 4096 знаков


def _entry(s: Session, cid: int, today: date, cutoff: date | None = None, force: bool = True) -> dict | None:
    """Неоплаченные заказы клиента. С cutoff берутся только старше этой даты (и не напоминавшиеся недавно)."""
    orders = []
    for o, debt in balance.open_orders(s, cid):
        if cutoff is not None:
            if o.created_at.date() > cutoff:
                continue
            if not force and o.last_reminded_on and o.last_reminded_on > cutoff:
                continue
        files = [a.file_name or ("фото" if a.media == "photo" else "файл")
                 for a in attachments.for_order(s, o.id)]
        orders.append(dict(id=o.id, day=o.created_at.date(), debt=debt, files=files,
                           age=(today - o.created_at.date()).days, total=o.total_minor,
                           items=[(i.name, i.qty_milli, i.price_minor, i.total_minor) for i in o.items]))
    if not orders:
        return None
    orders.sort(key=lambda x: (x["day"], x["id"]))
    return dict(client_id=cid, name=s.get(Client, cid).name,
                debt=sum(x["debt"] for x in orders), orders=orders)


def client_entry(s: Session, cid: int, today: date) -> dict | None:
    """Все неоплаченные заказы клиента (для кнопки «Сообщение клиенту»)."""
    return _entry(s, cid, today)


def due(s: Session, today: date, days: int, force: bool = False) -> list[dict]:
    """Должники, у которых есть неоплаченные заказы старше N дней.

    Заказ повторно попадает в список не раньше чем через N дней после прошлого напоминания
    (force=True игнорирует это, нужно для команды /remind).
    """
    cutoff = today - timedelta(days=days)
    limit = datetime.combine(cutoff + timedelta(days=1), time.min)
    client_ids = list(s.scalars(select(Order.client_id).where(
        Order.status == "active", Order.client_id.is_not(None), Order.created_at < limit).distinct()))
    entries = [e for cid in client_ids if (e := _entry(s, cid, today, cutoff, force))]
    entries.sort(key=lambda e: e["orders"][0]["day"])  # самые давние долги сверху
    return entries


def client_text(e: dict) -> str:
    """Готовое сообщение клиенту: сколько и за что он должен."""
    parts = []
    for o in e["orders"][:MAX_ORDERS_PER_CLIENT]:
        what = f"заказ от {o['day']:%d.%m}" + (f" ({', '.join(o['files'])})" if o["files"] else "")
        parts.append(f"{what} — {fmt_money(o['debt'])}")
    return (f"{e['name']}, привет! Напомню про оплату: " + "; ".join(parts)
            + (f". Всего {fmt_money(e['debt'])}." if len(parts) > 1 else ".") + " Скинь, когда сможешь.")


# Названия стандартных услуг по-казахски; своё название услуги остаётся как есть
KZ_WORDS = [
    ("Альбом по гистологии", "гистология альбомы"),
    ("Односторонняя печать", "бір бет"),
    ("Двухсторонняя печать", "екі бет"),
    ("Двусторонняя печать", "екі бет"),
    ("Конспект", "конспект"),
    ("последняя стр. с одной стороны", "соңғы бет, бір жақты"),
    ("1 стр. с одной стороны", "1 бет, бір жақты"),
    ("стр.", "бет"),
]


def kz_name(name: str) -> str:
    for ru, kz in KZ_WORDS:
        name = name.replace(ru, kz)
    return name


def _tg(minor: int) -> str:
    return fmt_money(minor).replace("₸", "тг")


def _num(minor: int) -> str:
    if minor % 100 == 0:
        return str(minor // 100)
    return f"{minor / 100:.2f}".rstrip("0").replace(".", ",")


def _qty(milli: int) -> str:
    return str(milli // 1000) if milli % 1000 == 0 else f"{milli / 1000:g}".replace(".", ",")


def client_text_kz(e: dict) -> str:
    """Сообщение клиенту по-казахски со строками вида «бір бет 10*35 тг = 350 тг»."""
    lines = [f"{e['name']}, сәлем! Төлем туралы еске салып қояйын."]
    for o in e["orders"][:MAX_ORDERS_PER_CLIENT]:
        files = f" ({', '.join(o['files'])})" if o["files"] else ""
        lines.append(f"{o['day']:%d.%m}{files}:")
        for name, qty, price, total in o["items"]:
            lines.append(f"{kz_name(name)} {_qty(qty)}*{_num(price)} тг = {_tg(total)}")
        paid = o["total"] - o["debt"]
        if paid > 0:
            lines.append(f"Төленді: {_tg(paid)}")
    hidden = len(e["orders"]) - MAX_ORDERS_PER_CLIENT
    if hidden > 0:
        lines.append(f"…және тағы {hidden} тапсырыс")
    lines.append(("Қалғаны: " if len(e["orders"]) == 1 else "Жалпы қалғаны: ") + _tg(e["debt"]))
    lines.append("Уақытың болғанда салып жібер.")
    return "\n".join(lines)


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def digest(entries: list[dict], days: int, as_html: bool = False) -> tuple[str, list[int], list[int]]:
    """(текст сообщения, id клиентов для кнопок, id показанных заказов).

    as_html=True: тексты для клиента в <code>: в Telegram по ним достаточно нажать, и они скопируются
    (по-русски и по-казахски). Сообщение не длиннее лимита Telegram: что не влезло, придёт в следующий раз.
    """
    e_ = esc if as_html else str
    head = f"Напоминание: не оплачено больше {days} дн.\n"
    body, order_ids, client_ids = [], [], []
    for e in entries[:MAX_CLIENTS]:
        block = [f"{e_(e['name'])} — долг {fmt_money(e['debt'])}"]
        for o in e["orders"][:MAX_ORDERS_PER_CLIENT]:
            files = f", файл: {e_(', '.join(o['files']))}" if o["files"] else ""
            block.append(f"  заказ №{o['id']} от {o['day']:%d.%m} ({o['age']} дн.): {fmt_money(o['debt'])}{files}")
        if as_html:
            block.append("  Сообщение клиенту (нажмите на него, чтобы скопировать):")
            block.append(f"<code>{esc(client_text(e))}</code>")
            block.append("  Қазақша:")
            block.append(f"<code>{esc(client_text_kz(e))}</code>")
        else:
            block.append(f"  Текст клиенту: «{client_text(e)}»")
        text_block = "\n".join(block)
        if body and len(head) + sum(len(b) + 2 for b in body) + len(text_block) > TELEGRAM_SAFE_LEN:
            break
        body.append(text_block)
        client_ids.append(e["client_id"])
        order_ids += [o["id"] for o in e["orders"][:MAX_ORDERS_PER_CLIENT]]
    text = head + "\n" + "\n\n".join(body)
    if len(entries) > len(body):
        text += f"\n\n…и ещё должников: {len(entries) - len(body)} (придут в следующем напоминании)"
    return text, client_ids, order_ids


def mark_reminded(s: Session, order_ids: list[int], today: date) -> None:
    """Запоминает дату напоминания. Обычный UPDATE: не вызывает лишную отправку копии базы."""
    if order_ids:
        s.execute(update(Order).where(Order.id.in_(order_ids)).values(last_reminded_on=today))
