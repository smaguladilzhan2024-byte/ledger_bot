"""Поиск клиента по набранному имени с кнопкой «Создать клиента» (заказ, оплата, список клиентов)."""
from ..services import clients as client_svc


def build(s, query: str, *, pick_prefix: str, create_cb: str, archived_ok: bool = False,
          label=lambda c: c.name) -> tuple[str, list]:
    """(текст, строки кнопок). Если точного клиента нет, внизу кнопка «Создать клиента «имя»»."""
    q = (query or "").strip()
    found, fuzzy = client_svc.suggest(s, q)
    if not archived_ok:
        found = [c for c in found if not c.is_archived]
    exact = any(c.name.casefold() == q.casefold() for c in found)
    if not found:
        text = f"Клиента «{q}» нет в списке."
    elif fuzzy:
        text = f"Точного совпадения с «{q}» нет. Возможно, вы имели в виду:"
    else:
        text = "Нашёл:"
    rows = [[(label(c), f"{pick_prefix}{c.id}")] for c in found]
    looks_like_phone = q.replace("+", "").replace(" ", "").replace("-", "").isdigit()
    if q and len(q) <= 80 and not exact and not looks_like_phone:
        rows.append([(f"➕ Создать клиента «{q[:40]}»", create_cb)])
    return text, rows
