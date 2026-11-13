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
    assert "Текст клиенту: «Айгерим, привет! Напомню про оплату: заказ от" in text and "250 ₸" in text


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


# ---------- сообщение клиенту с копированием и постоянные кнопки ----------
def test_main_menu_is_persistent_for_desktop():
    from ledger_bot.bot.keyboards import main_menu
    m = main_menu()
    assert m.is_persistent is True and m.resize_keyboard is True and m.keyboard


def test_client_card_has_message_button_only_with_debt_and_text_is_copyable(db):
    with session_scope() as s:
        o, cid = _order(s, "Азамат", 2)
        attachments.add(s, order_id=o.id, media="document", file_id="F", unique_id="u", file_name="a<b>.pdf")
        _order(s, "Азамат", 1, qty=5000, key="second")
        free = client_svc.create_client(s, "Без долга").id

    async def scenario():
        h = Harness()
        await h.tap(f"cl:c:{cid}")
        assert ("Сообщение клиенту о долге", f"cl:m:{cid}") in h.last_buttons()
        await h.tap(f"cl:c:{free}")
        no_btn = [b for b in h.last_buttons() if b[1].startswith("cl:m:")]
        await h.tap(f"cl:m:{free}")
        none_text = h.last_text()
        await h.tap(f"cl:m:{cid}")
        msg = [c for c in h.session.calls if getattr(c, "parse_mode", None) == "HTML"][-1]
        return no_btn, none_text, msg.text
    no_btn, none_text, text = run(scenario())
    assert no_btn == [] and "нет долга" in none_text
    assert text.count("<code>") == 2 and text.count("</code>") == 2                         # русский и казахский блоки
    assert "Азамат, привет! Напомню про оплату:" in text and "Всего 525 ₸. Скинь, когда сможешь." in text   # 350 + 175
    assert "Азамат, сәлем!" in text and "бір бет 10*35 тг = 350 тг" in text and "Жалпы қалғаны: 525 тг" in text
    assert "a&lt;b&gt;.pdf" in text and "a<b>" not in text                                    # экранирование


def test_digest_html_wraps_client_text_and_escapes_names(db):
    with session_scope() as s:
        _order(s, "<Тест>&", 4)
        entries = reminders.due(s, today(), 3)
        html_text, _, _ = reminders.digest(entries, 3, as_html=True)
        plain, _, _ = reminders.digest(entries, 3)
    assert "<code>" in html_text and "&lt;Тест&gt;&amp;" in html_text and "<Тест>" not in html_text
    assert "<code>" not in plain and "Текст клиенту: «" in plain


def test_reminder_is_sent_with_html_parse_mode(db):
    with session_scope() as s:
        _order(s, "Айгерим", 4)
    h = Harness()
    assert asyncio.run(remind.send_due(h.bot, [OWNER], 3)) is True
    sent = [c for c in h.session.calls if getattr(c, "text", None) and "Напоминание" in c.text]
    assert sent[0].parse_mode == "HTML" and "<code>" in sent[0].text


def test_kazakh_text_format_duplex_partial_payment_and_unknown_service(db):
    with session_scope() as s:
        two = svc(s, "Двухсторонняя печать")
        cid = client_svc.create_client(s, "Азамат").id
        o, _ = orders.create_order(s, items=[{"sid": two.id, "vid": None, "qty": 13000}], client_id=cid, key="d1")
        o.created_at = now() - timedelta(days=4)
        attachments.add(s, order_id=o.id, media="document", file_id="F", unique_id="u", file_name="lecture.pdf")
        payments.accept_payment(s, client_id=cid, amount_minor=10000, key="p1")
        cat = catalog_first_category(s)
        custom = catalog.create_service(s, "Сканирование", cat, "sheet", "лист", 2750)
        o2, _ = orders.create_order(s, items=[{"sid": custom.id, "vid": None, "qty": 2000}], client_id=cid, key="d2")
        o2.created_at = now() - timedelta(days=5)
        entry = reminders.client_entry(s, cid, today())
        kz = reminders.client_text_kz(entry)
    lines = kz.split("\n")
    assert lines[0] == "Азамат, сәлем! Төлем туралы еске салып қояйын."
    assert "Сканирование 2*27,5 тг = 55 тг" in kz                                    # своё название не трогаем
    assert "екі бет (13 бет) 6*55 тг = 330 тг" in kz and "екі бет — соңғы бет, бір жақты 1*35 тг = 35 тг" in kz
    assert "(lecture.pdf):" in kz and "Төленді: 100 тг" in kz
    assert lines[-2:] == ["Жалпы қалғаны: 320 тг", "Уақытың болғанда салып жібер."]       # (365 - 100) + 55


def catalog_first_category(s):
    return catalog.list_categories(s)[0].id


def test_digest_stays_within_telegram_limit_with_many_debtors(db):
    with session_scope() as s:
        for i in range(12):
            for k in range(5):
                _order(s, f"Клиент{i}", 4 + k, qty=10000 + k * 1000, key=f"m{i}{k}")
        entries = reminders.due(s, today(), 3)
        text, client_ids, _ = reminders.digest(entries, 3, as_html=True)
    assert len(text) <= 4096 and 0 < len(client_ids) <= 12
    if len(client_ids) < 12:
        assert "…и ещё должников" in text
