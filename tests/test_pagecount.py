"""Подсчёт страниц присланного файла и заказ в один шаг."""
import io
import zipfile

import pytest
from pypdf import PdfWriter
from sqlalchemy import select

from ledger_bot.bot.handlers import files
from ledger_bot.db.models import Attachment, Order
from ledger_bot.db.session import session_scope
from ledger_bot.services import clients as client_svc, pagecount
from .conftest import svc
from .test_bot_flow import Harness, run


def make_pdf(pages: int) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def make_zip(member: str, xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, xml)
    return buf.getvalue()


# ---------- сам подсчёт ----------
def test_pdf_pages_exact():
    assert pagecount.count_pages(make_pdf(13), "lecture.pdf", "document") == 13
    assert pagecount.count_pages(make_pdf(1), "A.PDF", "document") == 1


def test_docx_and_odt_use_page_count_saved_by_the_editor():
    docx = make_zip("docProps/app.xml", "<Properties><Pages>7</Pages><Words>100</Words></Properties>")
    assert pagecount.count_pages(docx, "work.docx", "document") == 7
    no_pages = make_zip("docProps/app.xml", "<Properties><Words>100</Words></Properties>")
    assert pagecount.count_pages(no_pages, "work.docx", "document") is None
    odt = make_zip("meta.xml", '<office:meta><meta:document-statistic meta:page-count="5"/></office:meta>')
    assert pagecount.count_pages(odt, "work.odt", "document") == 5


def test_images_count_as_one_and_unsupported_or_broken_files_are_none():
    assert pagecount.count_pages(None, None, "photo") == 1
    assert pagecount.count_pages(None, "scan.png", "document") == 1
    assert pagecount.count_pages(b"x", "slides.pptx", "document") is None
    assert pagecount.count_pages(b"x", "old.doc", "document") is None
    assert pagecount.count_pages(b"not a pdf", "bad.pdf", "document") is None
    assert pagecount.count_pages(b"not a zip", "bad.docx", "document") is None
    assert pagecount.count_pages(None, "file.pdf", "document") is None
    assert not pagecount.needs_download("slides.pptx", "document") and pagecount.needs_download("a.pdf", "document")


# ---------- в боте ----------
@pytest.fixture
def pdf_downloads(monkeypatch):
    """Подменяет скачивание из Telegram: файлы по очереди отдают PDF с заданным числом страниц."""
    queue = []

    async def fake(bot, file_id, size):
        return queue.pop(0) if queue else None
    monkeypatch.setattr(files, "fetch_bytes", fake)
    return queue


def test_send_pdf_shows_pages_and_prices_then_one_step_order(db, pdf_downloads):
    pdf_downloads.append(make_pdf(13))
    with session_scope() as s:
        cid = client_svc.create_client(s, "Азамат").id
        single, duplex = svc(s, "Односторонняя печать").id, svc(s, "Двухсторонняя печать").id

    async def scenario():
        h = Harness()
        await h.send_file("lecture.pdf")
        offer, btns = h.last_text(), h.last_buttons()
        await h.tap(f"fl:q:{duplex}")
        ask_client = h.last_text()
        await h.tap(f"oc:{cid}")
        confirm = h.last_text()
        await h.tap("od:ok")
        return offer, btns, ask_client, confirm
    offer, btns, ask_client, confirm = run(scenario())
    assert "«lecture.pdf»: 13 стр." in offer
    assert "Односторонняя печать: 455 ₸ (13 л. × 35 ₸)" in offer
    assert "Двухсторонняя печать: 365 ₸ (6 л. × 55 ₸ + 1 стр. × 35 ₸)" in offer
    assert (f"Двухсторонняя печать — 365 ₸", f"fl:q:{duplex}") in btns and (f"Односторонняя печать — 455 ₸", f"fl:q:{single}") in btns
    assert "Для кого заказ" in ask_client
    assert "(13 стр.)" in confirm and "Итого: 365 ₸" in confirm and "Файлов к заказу: 1" in confirm
    with session_scope() as s:
        order = s.scalar(select(Order))
        att = s.scalar(select(Attachment))
        assert order.total_minor == 36500 and order.client_id == cid
        assert att.order_id == order.id and att.file_name == "lecture.pdf"


def test_two_files_add_up_and_manual_number_overrides(db, pdf_downloads):
    pdf_downloads += [make_pdf(10), make_pdf(5)]

    async def scenario():
        h = Harness()
        await h.send_file("a.pdf")
        first = h.last_text()
        await h.send_file("b.pdf")
        both = h.last_text()
        await h.say("20")                     # ввели число страниц вручную
        manual = h.last_text()
        return first, both, manual
    first, both, manual = run(scenario())
    assert ": 10 стр." in first and "«a.pdf, b.pdf»: 15 стр." in both and ": 20 стр." in manual


def test_unknown_format_asks_for_pages_then_prices(db):
    async def scenario():
        h = Harness()
        await h.send_file("slides.pptx")
        ask = h.last_text()
        await h.say("10")
        return ask, h.last_text()
    ask, priced = run(scenario())
    assert "Не смог посчитать страницы: «slides.pptx»" in ask
    assert "Односторонняя печать: 350 ₸" in priced and "Двухсторонняя печать: 275 ₸" in priced


def test_photo_counts_as_one_page_without_download(db):
    async def scenario():
        h = Harness()
        await h.send_file(photo=True)
        return h.last_text()
    text = run(scenario())
    assert "«фото»: 1 стр." in text and "Односторонняя печать: 35 ₸" in text and "Двухсторонняя печать: 35 ₸" in text


def test_file_too_big_is_not_downloaded(db, monkeypatch):
    import asyncio

    class Bot:
        async def download(self, *a, **k):
            raise AssertionError("не должен скачивать")
    assert asyncio.run(files.fetch_bytes(Bot(), "id", files.MAX_DOWNLOAD + 1)) is None
