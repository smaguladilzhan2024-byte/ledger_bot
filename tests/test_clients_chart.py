"""Поиск клиента с подсказкой «Создать клиента» и гистограмма в статистике."""
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select

from ledger_bot.db.models import Client, Order, Payment
from ledger_bot.db.session import session_scope
from ledger_bot.services import clients as client_svc, finance, payments, stats
from ledger_bot.timeutil import today
from .test_bot_flow import Harness, run


def _names():
    with session_scope() as s:
        return [c.name for c in s.scalars(select(Client).order_by(Client.id))]


def _create_btn(buttons):
    return [b for b in buttons if b[1].endswith(":mk")]


def test_order_unknown_name_offers_create_and_continues(db):
    async def scenario():
        h = Harness()
        await h.say("Создать заказ")
        assert "напишите имя" in h.last_text()
        await h.say("Данияр")                       # клиента ещё нет
        text, btns = h.last_text(), h.last_buttons()
        await h.tap("oc:mk")
        return text, btns, h.last_text()
    text, btns, after = run(scenario())
    assert "нет в списке" in text and _create_btn(btns) and "Данияр" in _create_btn(btns)[0][0]
    assert _names() == ["Данияр"] and "Новый заказ — Данияр" in after


def test_order_typo_suggests_similar_and_still_can_create(db):
    with session_scope() as s:
        client_svc.create_client(s, "Айгерим")
    async def scenario():
        h = Harness()
        await h.say("Создать заказ")
        await h.say("Айгирим")                      # опечатка
        return h.last_text(), h.last_buttons()
    text, btns = run(scenario())
    assert "Возможно" in text and any("Айгерим" in b[0] for b in btns) and _create_btn(btns)


def test_exact_name_has_no_create_button_and_picks_client(db):
    with session_scope() as s:
        cid = client_svc.create_client(s, "Мадина").id
    async def scenario():
        h = Harness()
        await h.say("Создать заказ")
        await h.say("мадина")                       # регистр не важен
        btns = h.last_buttons()
        await h.tap(f"oc:{cid}")
        return btns, h.last_text()
    btns, after = run(scenario())
    assert not _create_btn(btns) and "Новый заказ — Мадина" in after


def test_payment_and_clients_menu_offer_create(db):
    async def scenario():
        h = Harness()
        await h.say("Приём оплаты")
        await h.say("Нурлан")
        pay_btns = h.last_buttons()
        await h.tap("pc:mk")
        pay_text = h.last_text()
        await h.say("Клиенты")
        await h.say("Асель")
        cl_btns = h.last_buttons()
        await h.tap("cl:mk")
        ask = h.last_text()
        await h.tap("cl:skip")
        return pay_btns, pay_text, cl_btns, ask
    pay_btns, pay_text, cl_btns, ask = run(scenario())
    assert _create_btn(pay_btns) and "Нурлан" in pay_text
    assert _create_btn(cl_btns) and "Асель" in ask
    assert _names() == ["Нурлан", "Асель"]


def test_menu_button_text_is_not_taken_as_client_name(db):
    async def scenario():
        h = Harness()
        await h.say("Создать заказ")
        await h.say("Клиенты")                      # кнопка меню, а не имя
        return h.last_text()
    assert "Клиенты" in run(scenario()) and _names() == []


# ---------- гистограмма ----------
def _pay(s, day, amount, kind="payment"):
    s.add(Payment(kind=kind, amount_minor=amount, idempotency_key=uuid.uuid4().hex, created_at=datetime.combine(day, datetime.min.time()) + timedelta(hours=12)))


def test_chart_week_bars_scale_and_expenses(db):
    t = today()
    start = t - timedelta(days=t.weekday())
    with session_scope() as s:
        _pay(s, start, 100000)
        _pay(s, start, 20000, "refund")
        finance.add_transaction(s, kind="income", amount_minor=20000, day=start, category="Прочее", description="x", key="i1")
        finance.add_transaction(s, kind="expense", amount_minor=50000, day=start, category="Бумага", description="x", key="e1")
        rows = stats.chart_rows("week", start, t, stats.daily_flows(s, start, t))
    assert rows[0][1:] == (100000, 50000) and len(rows) == t.weekday() + 1
    text = stats.format_chart(rows)
    assert "🟩" * stats.BAR_WIDTH in text and "🟥" * (stats.BAR_WIDTH // 2) in text and "1 000 ₸" in text


def test_chart_month_weeks_and_all_time_months(db):
    t = today()
    with session_scope() as s:
        _pay(s, t, 30000)
        flows = stats.daily_flows(s, None, None)
        month_rows = stats.chart_rows("month", t.replace(day=1), t, stats.daily_flows(s, t.replace(day=1), t))
        all_rows = stats.chart_rows("all", None, None, flows)
    assert sum(r[1] for r in month_rows) == 30000 and "–" in month_rows[0][0]
    assert all_rows == [(f"{t.month:02d}.{t.year}", 30000, 0)]


def test_chart_empty_and_stats_message_includes_chart(db):
    with session_scope() as s:
        assert stats.chart_text(s, "all", None, None) == ""
        _pay(s, today(), 55000)

    async def scenario():
        h = Harness()
        await h.say("Финансовая статистика")
        await h.tap("st:today")
        return h.last_text()
    text = run(scenario())
    assert "Статистика: Сегодня" in text and "Гистограмма" in text and "550 ₸" in text
