"""Ежедневная проверка долгов и отправка напоминания владельцу."""
import asyncio
import logging

from ..db.session import session_scope
from ..services import reminders
from ..timeutil import now, today
from .keyboards import ikb

log = logging.getLogger("ledger_bot")


def build_message(days: int, force: bool = False):
    """(текст, кнопки, id заказов) или None, если напоминать не о чем."""
    with session_scope() as s:
        entries = reminders.due(s, today(), days, force=force)
        if not entries:
            return None
        text, client_ids, order_ids = reminders.digest(entries, days)
        names = {e["client_id"]: e["name"] for e in entries}
    rows = [[(f"Открыть: {names[c]}", f"cl:c:{c}")] for c in client_ids[:8]]
    return text, rows, order_ids


async def send_due(bot, owner_ids, days: int) -> bool:
    msg = await asyncio.to_thread(build_message, days)
    if not msg:
        return False
    text, rows, order_ids = msg
    sent = False
    for oid in owner_ids:
        try:
            await bot.send_message(oid, text, reply_markup=ikb(rows) if rows else None)
            sent = True
        except Exception:
            log.warning("Не удалось отправить напоминание владельцу %s", oid)
    if sent:  # помечаем только если сообщение реально ушло
        def _mark():
            with session_scope() as s:
                reminders.mark_reminded(s, order_ids, today())
        await asyncio.to_thread(_mark)
    return sent


async def remind_loop(bot, owner_ids, days: int, hour: int) -> None:
    """Каждые 30 минут: если время напоминаний наступило, отправляет список (повторно за день не шлёт)."""
    while True:
        try:
            if now().hour >= hour:
                await send_due(bot, owner_ids, days)
        except Exception:
            log.exception("Напоминания о долгах не отправились")
        await asyncio.sleep(1800)
