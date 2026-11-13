"""Расход бумаги по месяцам: сколько листов и пачек купить."""
from datetime import date, timedelta

from ledger_bot.db.models import Order
from ledger_bot.db.session import session_scope
from ledger_bot.services import clients as client_svc, orders, stats
from ledger_bot.timeutil import now, today
from .conftest import svc
from .test_bot_flow import Harness, run


def _order(s, key, items, back_days=0):
    cid = client_svc.create_client(s, f"К{key}").id
    o, _ = orders.create_order(s, items=items, client_id=cid, key=key)
    if back_days:
        o.created_at = now() - timedelta(days=back_days)
    return o


def test_paper_counts_sheets_not_pages_and_skips_cancelled_and_copies(db):
    with session_scope() as s:
        one, two, copy = svc(s, "Односторонняя печать"), svc(s, "Двухсторонняя печать"), svc(s, "Конспект")
        _order(s, "a", [{"sid": one.id, "vid": None, "qty": 10000}])                    # 10 листов
        _order(s, "b", [{"sid": two.id, "vid": None, "qty": 13000}])                    # 6 листов + 1 лист для нечётной
        _order(s, "c", [{"sid": copy.id, "vid": None, "qty": 5000}])                    # конспекты не считаем
        bad = _order(s, "d", [{"sid": one.id, "vid": None, "qty": 100000}])             # отменён
        orders.cancel_order(s, bad.id, "ошибка")
        rows = stats.paper_by_month(s, today(), 3)
    assert len(rows) == 3 and rows[-1][0] == today().replace(day=1)
    assert rows[-1][1] == 10 + 6 + 1 and rows[0][1] == 0


def test_paper_previous_month_and_forecast_text(db):
    with session_scope() as s:
        one = svc(s, "Односторонняя печать")
        _order(s, "now", [{"sid": one.id, "vid": None, "qty": 200000}])                 # 200 л. сейчас
        _order(s, "old", [{"sid": one.id, "vid": None, "qty": 1200000}], back_days=35)  # 1200 л. в прошлом месяце
        rows = stats.paper_by_month(s, today(), 3)
    assert rows[-1][1] == 200 and rows[-2][1] == 1200 or rows[-3][1] == 1200
    text = stats.format_paper(rows, today(), pack=500)
    assert "пачка = 500 л." in text and "1200 л. ≈ 2,4 пач." in text and "200 л. ≈ 0,4 пач." in text
    assert "1200 л. = 3 пач." in text                                                   # ориентир: 3 пачки


def test_paper_empty_and_pack_env(db, monkeypatch):
    with session_scope() as s:
        rows = stats.paper_by_month(s, today())
    assert "Пока нет заказов" in stats.format_paper(rows, today())
    monkeypatch.setenv("PAPER_PACK_SHEETS", "250")
    assert stats.pack_sheets() == 250
    monkeypatch.setenv("PAPER_PACK_SHEETS", "abc")
    assert stats.pack_sheets() == 500


def test_paper_button_in_stats_menu(db):
    with session_scope() as s:
        _order(s, "x", [{"sid": svc(s, "Односторонняя печать").id, "vid": None, "qty": 7000}])

    async def scenario():
        h = Harness()
        await h.say("Финансовая статистика")
        assert ("Расход бумаги", "st:paper") in h.last_buttons()
        await h.tap("st:paper")
        return h.last_text()
    text = run(scenario())
    assert "Расход бумаги" in text and "7 л." in text


def test_stats_message_shows_paper_for_the_period(db):
    with session_scope() as s:
        _order(s, "a", [{"sid": svc(s, "Односторонняя печать").id, "vid": None, "qty": 7000}])
        _order(s, "b", [{"sid": svc(s, "Конспект").id, "vid": None, "qty": 3000}])            # конспекты не считаются
        old = _order(s, "c", [{"sid": svc(s, "Односторонняя печать").id, "vid": None, "qty": 40000}], back_days=400)
        assert stats.paper_sheets(s, today(), today()) == 7
        assert stats.paper_sheets(s, None, None) == 47

    async def scenario():
        h = Harness()
        await h.say("Финансовая статистика")
        await h.tap("st:today")
        return h.last_text()
    text = run(scenario())
    assert "Бумага за период: 7 л. ≈ <0,1 пач. (пачка = 500 л.)" in text     # денег за сегодня нет, поэтому гистограммы нет
