"""Подсчёт страниц присланного файла (файл читается в памяти и нигде не сохраняется).

Точно: PDF и картинки. Для DOCX и ODT берётся число страниц, которое записала программа при сохранении
(Word и LibreOffice это делают; у файлов из других программ его может не быть).
Остальные форматы (DOC, PPTX и т.д.) не считаются: тогда число страниц вводят вручную.
"""
import io
import re
import zipfile

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".heic")
COUNTABLE_EXT = (".pdf", ".docx", ".odt") + IMAGE_EXT
MAX_META = 1_000_000  # метаданные в архиве не больше ~1 МБ


def needs_download(file_name: str | None, media: str) -> bool:
    """Нужно ли скачивать файл, чтобы посчитать страницы (картинки считаются без скачивания)."""
    name = (file_name or "").lower()
    return media == "document" and name.endswith((".pdf", ".docx", ".odt"))


def is_image(file_name: str | None, media: str) -> bool:
    return media == "photo" or (file_name or "").lower().endswith(IMAGE_EXT)


def _pdf(data: bytes) -> int | None:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        if not reader.decrypt(""):
            return None
    n = len(reader.pages)
    return n or None


def _zip_meta(data: bytes, member: str) -> str | None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info = zf.getinfo(member)
        if info.file_size > MAX_META:
            return None
        return zf.read(member).decode("utf-8", "ignore")


def _docx(data: bytes) -> int | None:
    xml = _zip_meta(data, "docProps/app.xml")
    m = re.search(r"<Pages>\s*(\d+)\s*</Pages>", xml or "")
    return int(m.group(1)) or None if m else None


def _odt(data: bytes) -> int | None:
    xml = _zip_meta(data, "meta.xml")
    m = re.search(r'page-count="(\d+)"', xml or "")
    return int(m.group(1)) or None if m else None


def count_pages(data: bytes | None, file_name: str | None, media: str) -> int | None:
    """Число страниц или None, если посчитать нельзя."""
    if is_image(file_name, media):
        return 1
    if not data:
        return None
    name = (file_name or "").lower()
    try:
        if name.endswith(".pdf"):
            return _pdf(data)
        if name.endswith(".docx"):
            return _docx(data)
        if name.endswith(".odt"):
            return _odt(data)
    except Exception:  # битый или защищённый файл
        return None
    return None
