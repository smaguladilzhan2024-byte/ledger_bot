"""Напоминания о долгах через N дней."""
import asyncio
import sqlite3
from datetime import timedelta

from ledger_bot.bot import remind
from ledger_bot.bot.handlers import settings
from ledger_bot.db import session as dbs
from ledger_bot.db.models import Order
from ledger_bot.db.session import init_db, session_scope
from ledger_bot.services import attachments, catalog, clients as client_svc, orders, payments, reminders
from ledger_bot.timeutil import now, today
from .conftest import svc
from .test_bot_flow import Harness, OWNER, run


def _order(s, name, days_ago, qty=10000, key=None, service="Односторонняя печать"):
    cid = next((c.id for c in client_svc.search(s, name) if c.name == name), None) or client_svc.create_client(s, name).id
    o, _ = orders.create_order(s, items=[{"sid": svc(s, service).id, "vid": None, "qty": qty}],
                               client_id=cid, key=key or f"{name}{days_ago}{qty}")
    o.created_at = now() - timedelta(days=days_ago)
    return o, cid


def test_due_after_three_days_only_unpaid_active_with_client(db):
    with session_scope() as s:
        old, cid = _order(s, "Айгерим", 3)                       # ровно 3 дня: уже пора
        _order(s, "Данияр", 2)                                   # 2 дня: рано
        paid, pcid = _order(s, "Мадина", 5)
        payments.accept_payment(s, client_id=pcid, amount_minor=paid.total_minor, key="pay1")
        bad, _ = _order(s, "Нурлан", 6)
        orders.cancel_order(s, bad.id, "ошибка")
        no_client, _ = orders.create_order(s, items=[{"sid": svc(s, "Конспект").id, "vid": None, "qty": 1000}],
                                           client_id=None, key="nc")
        no_client.created_at = now() - timedelta(days=9)
        got = reminders.due(s, today(), 3)
    assert [e["name"] for e in got] == ["Айгерим"] and got[0]["debt"] == 35000
    assert got[0]["orders"][0]["age"] == 3


def test_partial_payment_shows_remaining_debt_and_files_in_digest(db):
    with session_scope() as s:
        o, cid = _order(s, "Айгерим", 4)
        payments.accept_payment(s, client_id=cid, amount_minor=10000, key="p1")
        attachments.add(s, order_id=o.id, media="document", file_id="F", unique_id="u", file_name="gistology.pdf")
        entries = reminders.due(s, today(), 3)
        text, client_ids, order_ids = reminders.digest(entries, 3)
    assert entries[0]["debt"] == 25000 and client_ids == [cid] and order_ids == [o.id]
    assert "Айгерим — долг 250 ₸" in text and "gistology.pdf" in text and f"заказ №{o.id}" in text
    assert "Текст клиенту: «Айгерим, здравствуйте! Напоминаю про оплату: заказ от" in text and "250 ₸" in text


def test_reminded_order_returns_only_after_another_n_days(db):
    with session_scope() as s:
        o, _ = _order(s, "Айгерим", 4)
        oid = o.id
        reminders.mark_reminded(s, [oid], today())
    with session_scope() as s:
        assert reminders.due(s, today(), 3) == []                                     # сегодня уже напоминали
        assert reminders.due(s, today() + timedelta(days=2), 3) == []
        assert len(reminders.due(s, today() + timedelta(days=3), 3)) == 1             # через 3 дня снова
        assert len(reminders.due(s, today(), 3, force=True)) == 1                      # /remind показывает всё


def test_send_due_sends_once_and_marks_only_after_success(db):
    with session_scope() as s:
        _order(s, "Айгерим", 4)
    h = Harness()
    assert asyncio.run(remind.send_due(h.bot, [OWNER], 3)) is True
    sent = [c for c in h.session.calls if getattr(c, "text", None) and "Напоминание" in c.text]
    assert len(sent) == 1 and sent[0].reply_markup.inline_keyboard[0][0].text == "Открыть: Айгерим"
    assert asyncio.run(remind.send_due(h.bot, [OWNER], 3)) is False                    # повторно в тот же день — нет
    assert len([c for c in h.session.calls if getattr(c, "text", None) and "Напоминание" in c.text]) == 1


def test_failed_send_does_not_mark_orders(db):
    class Broken:
        async def send_message(self, *a, **k):
            raise RuntimeError("нет /start")
    with session_scope() as s:
        o, _ = _order(s, "Айгерим", 4)
        oid = o.id
    assert asyncio.run(remind.send_due(Broken(), [OWNER], 3)) is False
    with session_scope() as s:
        assert s.get(Order, oid).last_reminded_on is None                               # завтра попробует снова


def test_remind_command_and_empty_case(db):
    settings.CFG["remind_days"] = 3

    async def scenario():
        h = Harness()
        await h.say("/remind")
        empty = h.last_text()
        with session_scope() as s:
            _order(s, "Данияр", 5)
        await h.say("/remind")
        return empty, h.last_text()
    empty, full = run(scenario())
    assert empty == "Просроченных долгов нет." and "Данияр — долг 350 ₸" in full


def test_old_database_gets_reminder_column(tmp_path):
    path = str(tmp_path / "old.db")
    init_db(path)
    dbs.dispose_engine()
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE orders DROP COLUMN last_reminded_on")
    con.commit()
    con.close()
    init_db(path)                                                                       # запуск новой версии
    with session_scope() as s:
        catalog.seed_defaults(s)
        assert s.query(Order).count() == 0
        o, _ = _order(s, "Тест", 1)                                                     # запись в новую колонку работает
        o.last_reminded_on = today()
