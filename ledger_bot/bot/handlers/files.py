"""Файлы заказов и чеки оплат.

Бот не скачивает файлы: он запоминает ссылку Telegram (file_id) и имя. Диск сервера не растёт.
Через FILE_RETENTION_DAYS дней ссылка стирается, запись об оплате и файле остаётся.
"""
import asyncio
import io
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from ...db.models import Client, Order, Payment
from ...db.session import session_scope
from ...money import fmt_money
from ...services import attachments, catalog, orders as order_svc, pagecount
from ..common import show
from ..keyboards import CANCEL, ikb
from . import orders

router = Router()
log = logging.getLogger("ledger_bot")
MAX_DOWNLOAD = 20 * 1024 * 1024  # Telegram отдаёт боту файлы только до 20 МБ
ANY_FILE = F.document | F.photo


class FileFlow(StatesGroup):
    choose = State()  # файл прислали «просто так» — спрашиваем, куда его сохранить
    draft = State()   # файлы к заказу, который ещё оформляется
    wait = State()    # файлы к уже существующему заказу или чек к платежу


def extract(message: Message) -> dict | None:
    """Достаёт из сообщения всё нужное для повторной отправки (без скачивания)."""
    if message.document:
        d = message.document
        return dict(media="document", file_id=d.file_id, unique_id=d.file_unique_id,
                    file_name=d.file_name, size=d.file_size)
    if message.photo:
        p = message.photo[-1]  # самый большой размер
        return dict(media="photo", file_id=p.file_id, unique_id=p.file_unique_id,
                    file_name=None, size=p.file_size)
    return None


def _label(f: dict) -> str:
    return f.get("file_name") or ("фото" if f["media"] == "photo" else "файл")


def _keep_note() -> str:
    return f"Хранится {attachments.RETENTION_DAYS} дн., потом ссылка удаляется (запись о файле остаётся)."


# ---------- файл прислали без контекста: считаем страницы и сразу показываем цену ----------
async def fetch_bytes(bot, file_id: str, size: int | None):
    """Скачивает файл в память (на диск не пишется). None, если файл слишком большой или не скачался."""
    if size and size > MAX_DOWNLOAD:
        return None
    buf = io.BytesIO()
    try:
        await bot.download(file_id, destination=buf)
    except Exception:
        log.warning("Не удалось скачать файл для подсчёта страниц")
        return None
    return buf.getvalue()


async def _count(message: Message, f: dict) -> int | None:
    if pagecount.is_image(f.get("file_name"), f["media"]):
        return 1
    if not pagecount.needs_download(f.get("file_name"), f["media"]):
        return None
    data = await fetch_bytes(message.bot, f["file_id"], f.get("size"))
    if data is None:
        return None
    try:  # разбор в отдельном потоке и с лимитом времени, чтобы бот не «замирал»
        return await asyncio.wait_for(
            asyncio.to_thread(pagecount.count_pages, data, f.get("file_name"), f["media"]), 20)
    except Exception:
        return None


def _total_pages(data: dict) -> int | None:
    """Всего страниц: введённое вручную число или сумма по всем файлам (если известны все)."""
    if data.get("manual"):
        return data["manual"]
    files = data.get("files") or []
    if files and all(x.get("pages") for x in files):
        return sum(x["pages"] for x in files)
    return None


def print_options(s) -> list:
    """Услуги для быстрого заказа: односторонняя печать и двусторонняя (2 стр. на лист)."""
    svcs = [sv for sv in catalog.list_services(s)
            if sv.is_active and not sv.is_archived and sv.calc_type == "sheet" and not sv.variants]
    single = next((sv for sv in svcs if sv.pages_per_sheet == 1 and "односторон" in sv.name.casefold()), None)
    duplex = next((sv for sv in svcs if sv.pages_per_sheet > 1), None)
    return [sv for sv in (single, duplex) if sv]


def _breakdown(lines) -> str:
    parts = []
    for l in lines:
        q = l.qty_milli // 1000 if l.qty_milli % 1000 == 0 else l.qty_milli / 1000
        parts.append(f"{q} {'л.' if l.unit == 'лист' else l.unit} × {fmt_money(l.price_minor)}")
    return " + ".join(parts)


async def _offer(message: Message, state: FSMContext):
    data = await state.get_data()
    files, total = data["files"], _total_pages(data)
    names = ", ".join(_label(x) for x in files[:3]) + (f" и ещё {len(files) - 3}" if len(files) > 3 else "")
    rows, text = [], ""
    if total:
        text = f"{'Файлы' if len(files) > 1 else 'Файл'} «{names}»: {total} стр.\n"
        with session_scope() as s:
            for sv in print_options(s):
                lines = order_svc.build_lines(s, [{"sid": sv.id, "vid": None, "qty": total * 1000}])
                price = sum(l.total_minor for l in lines)
                text += f"\n{sv.name}: {fmt_money(price)} ({_breakdown(lines)})"
                rows.append([(f"{sv.name} — {fmt_money(price)}", f"fl:q:{sv.id}")])
        text += "\n\nВыберите, как печатать, и я оформлю заказ."
    else:
        unknown = ", ".join(_label(x) for x in files if not x.get("pages")) or names
        text = (f"Не смог посчитать страницы: «{unknown}». Напишите число страниц сообщением, "
                "и я сразу посчитаю цену. Или выберите, куда сохранить файл.")
    rows += [[("Новый заказ (услугу выберу сам)", "fl:new")],
             [("К существующему заказу", "fl:lo")], [("Это чек об оплате", "fl:lp")], CANCEL]
    await message.answer(text, reply_markup=ikb(rows))


@router.message(StateFilter(None), ANY_FILE)
async def stray_file(message: Message, state: FSMContext):
    f = extract(message)
    f["pages"] = await _count(message, f)
    await state.set_state(FileFlow.choose)
    await state.update_data(files=[f], manual=None)
    await _offer(message, state)


@router.message(FileFlow.choose, ANY_FILE)
async def more_stray(message: Message, state: FSMContext):
    data = await state.get_data()
    f = extract(message)
    f["pages"] = await _count(message, f)
    await state.update_data(files=data["files"] + [f])
    await _offer(message, state)


@router.message(FileFlow.choose, F.text.regexp(r"^\s*\d{1,4}\s*$"))
async def manual_pages(message: Message, state: FSMContext):
    n = int(message.text)
    if n < 1:
        await message.answer("Страниц должно быть больше нуля.")
        return
    await state.update_data(manual=n)
    await _offer(message, state)


@router.callback_query(F.data.regexp(r"^fl:q:\d+$"), FileFlow.choose)
async def quick_order(cb: CallbackQuery, state: FSMContext):
    """Заказ в один шаг: страницы уже посчитаны, осталось выбрать клиента и подтвердить."""
    data = await state.get_data()
    total = _total_pages(data)
    if not total:
        await cb.answer("Сначала напишите число страниц.", show_alert=True)
        return
    sid, files = int(cb.data.split(":")[2]), data["files"]
    await orders.start_order(cb.message, state)
    await state.update_data(files=files, items=[{"sid": sid, "vid": None, "qty": total * 1000}])
    await cb.answer()


@router.callback_query(F.data == "fl:new", FileFlow.choose)
async def stray_new_order(cb: CallbackQuery, state: FSMContext):
    files = (await state.get_data())["files"]
    await orders.start_order(cb.message, state)
    await state.update_data(files=files)
    await cb.answer()


@router.callback_query(F.data == "fl:lo", FileFlow.choose)
async def stray_pick_order(cb: CallbackQuery, state: FSMContext):
    with session_scope() as s:
        rows = []
        for o in s.scalars(select(Order).where(Order.status == "active").order_by(Order.id.desc()).limit(8)):
            who = s.get(Client, o.client_id).name if o.client_id else "без клиента"
            rows.append([(f"№{o.id} {o.created_at:%d.%m} {who} — {fmt_money(o.total_minor)}", f"fl:to:o:{o.id}")])
    await show(cb, "Выберите заказ (последние 8):" if rows else "Заказов пока нет.", ikb(rows + [CANCEL]))


@router.callback_query(F.data == "fl:lp", FileFlow.choose)
async def stray_pick_payment(cb: CallbackQuery, state: FSMContext):
    with session_scope() as s:
        rows = []
        for p in s.scalars(select(Payment).where(Payment.kind == "payment", Payment.client_id.is_not(None))
                           .order_by(Payment.id.desc()).limit(8)):
            who = s.get(Client, p.client_id).name
            rows.append([(f"{p.created_at:%d.%m %H:%M} {who} — {fmt_money(p.amount_minor)}", f"fl:to:p:{p.id}")])
    await show(cb, "К какой оплате относится чек (последние 8):" if rows else "Оплат пока нет.",
               ikb(rows + [CANCEL]))


@router.callback_query(F.data.regexp(r"^fl:to:[op]:\d+$"), FileFlow.choose)
async def stray_save(cb: CallbackQuery, state: FSMContext):
    _, _, kind, tid = cb.data.split(":")
    files = (await state.get_data()).get("files") or []
    n = _save(files, kind, int(tid))
    await state.clear()
    await show(cb, _saved_text(n, kind, int(tid)))


# ---------- файлы к заказу, который оформляется ----------
@router.callback_query(F.data == "fl:draft")
async def draft_ask(cb: CallbackQuery, state: FSMContext):
    if (await state.get_data()).get("mode") != "order":
        await cb.answer("Начните заново: «Создать заказ»", show_alert=True)
        return
    await state.set_state(FileFlow.draft)
    await show(cb, "Пришлите файл клиента (документом или фото). Можно несколько.\n" + _keep_note(),
               ikb([[("Готово", "fl:done")], CANCEL]))


@router.message(FileFlow.draft, ANY_FILE)
async def draft_file(message: Message, state: FSMContext):
    data = await state.get_data()
    f = extract(message)
    files = [x for x in data.get("files") or [] if x["unique_id"] != f["unique_id"]] + [f]
    await state.update_data(files=files)
    await message.answer(f"Добавлено: {_label(f)}. Файлов: {len(files)}.",
                         reply_markup=ikb([[("Готово", "fl:done")], CANCEL]))


@router.callback_query(F.data == "fl:done", FileFlow.draft)
async def draft_done(cb: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await orders.show_confirm(cb, state)


# ---------- файл к существующему заказу / чек к платежу ----------
@router.callback_query(F.data.regexp(r"^fl:(ao|rc):\d+$"))
async def attach_ask(cb: CallbackQuery, state: FSMContext):
    _, kind, tid = cb.data.split(":")
    await state.clear()
    await state.set_state(FileFlow.wait)
    await state.update_data(kind="o" if kind == "ao" else "p", tid=int(tid), n=0)
    what = f"файл для заказа №{tid}" if kind == "ao" else "чек об оплате (скриншот, фото или PDF)"
    await show(cb, f"Пришлите {what}.\n" + _keep_note(), ikb([[("Готово", "fl:fin")], CANCEL]))


@router.message(FileFlow.wait, ANY_FILE)
async def wait_file(message: Message, state: FSMContext):
    data = await state.get_data()
    f = extract(message)
    added = _save([f], data["kind"], data["tid"])
    n = data.get("n", 0) + added
    await state.update_data(n=n)
    await message.answer(("Сохранено: " if added else "Такой файл уже есть: ") + _label(f)
                         + ". Можно прислать ещё или нажать «Готово».",
                         reply_markup=ikb([[("Готово", "fl:fin")], CANCEL]))


@router.callback_query(F.data == "fl:fin", FileFlow.wait)
async def wait_done(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    rows = [[("Открыть заказ", f"cl:od:{data['tid']}")]] if data["kind"] == "o" else []
    await show(cb, f"Готово. Сохранено файлов: {data.get('n', 0)}.", ikb(rows) if rows else None)


def _save(files: list[dict], kind: str, tid: int) -> int:
    n = 0
    with session_scope() as s:
        for f in files:
            n += attachments.add(s, order_id=tid if kind == "o" else None,
                                 payment_id=tid if kind == "p" else None, **attachments.clean(f))[1]
    return n


def _saved_text(n: int, kind: str, tid: int) -> str:
    where = f"к заказу №{tid}" if kind == "o" else "как чек к оплате"
    return f"Сохранено файлов: {n} ({where}).\n" + _keep_note()


# ---------- показать всё, что есть по заказу ----------
@router.callback_query(F.data.startswith("fl:show:"))
async def show_proof(cb: CallbackQuery, state: FSMContext):
    oid = int(cb.data.split(":")[2])
    items, gone = [], 0
    with session_scope() as s:
        for a in attachments.for_order(s, oid):
            items.append((a.media, a.file_id, f"Файл клиента, заказ №{oid}: {a.file_name or ''}".strip(": ")))
        for p, amount, receipts in attachments.order_payments(s, oid):
            for a in receipts:
                items.append((a.media, a.file_id,
                              f"Чек: оплата {p.created_at:%d.%m.%Y %H:%M}, {fmt_money(p.amount_minor)}"))
    await cb.answer()
    if not items:
        await cb.message.answer("К этому заказу файлов и чеков не прикреплено.")
        return
    for media, file_id, caption in items:
        if not file_id:
            gone += 1
            continue
        try:
            if media == "photo":
                await cb.message.answer_photo(file_id, caption=caption)
            else:
                await cb.message.answer_document(file_id, caption=caption)
        except TelegramBadRequest:
            gone += 1
    if gone:
        await cb.message.answer(f"Не удалось показать файлов: {gone} (срок хранения вышел или Telegram "
                                "больше не отдаёт файл). Запись о них в карточке заказа осталась.")
