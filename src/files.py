"""Присланный файл: решить, как коучу его прочесть.

Глаза у бота не свои, а движка: инструмент Read открывает текст, PDF и картинки.
Работа этого модуля — разложить присланное по трём кучкам:

- Read откроет сам (текст любого вида, PDF) — кладём на диск как есть;
- Read не откроет, но текст внутри есть (Word, Excel, PowerPoint, OpenOffice) —
  вынимаем текст здесь и кладём рядом обычным файлом;
- текста не достать (архив, видео, старый двоичный .doc) — честно говорим.

Офисные форматы — это zip с XML внутри, поэтому разбираются стандартной
библиотекой, без новых зависимостей: каждая зависимость — ещё одна версия,
которую надо пинить и поднимать (правило 6).

Модуль ничего не знает про Telegram: на входе имя и байты, на выходе путь.
Поэтому проверяется тестами без бота.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

# Больше этого Telegram боту файл не отдаёт — ограничение Bot API, не наше.
# Проверяем заранее, чтобы ответить по-человечески, а не ошибкой скачивания.
ЛИМИТ_TELEGRAM = 20 * 1024 * 1024

# Текст, который пришёл не в UTF-8. Без расширения из этого списка байты
# в cp1251 не перекодируем: cp1251 «читает» что угодно, и двоичный мусор
# превратился бы в текст из кракозябр.
ТЕКСТОВЫЕ = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".log", ".ini", ".toml", ".rtf", ".srt",
    ".py", ".js", ".ts", ".sh", ".sql", ".ipynb",
}
# Двоичное старого образца: текст внутри есть, но без отдельной программы
# его не вынуть. Подсказываем, во что пересохранить.
СТАРЫЕ_ОФИСНЫЕ = {".doc", ".xls", ".ppt"}


@dataclass(frozen=True)
class Разбор:
    """Что делать с файлом. `путь` пуст — читать нечего, `отказ` объясняет почему."""

    путь: Path | None
    вид: str = ""
    отказ: str = ""
    # Текст вынут из офисного файла: оформление и картинки остались за бортом,
    # и коуч должен это знать, чтобы не судить о вёрстке, которой не видел.
    вынуто: bool = False


def разобрать(папка: Path, имя: str, данные: bytes) -> Разбор:
    """Положить файл в `папку` так, чтобы коуч его прочёл инструментом Read."""
    папка.mkdir(parents=True, exist_ok=True)
    имя = Path(имя).name or "файл"
    суффикс = Path(имя).suffix.lower()

    if not данные.strip():
        return Разбор(None, отказ="он пустой")

    if суффикс == ".pdf" or данные.startswith(b"%PDF"):
        путь = папка / (имя if суффикс == ".pdf" else f"{имя}.pdf")
        путь.write_bytes(данные)
        return Разбор(путь, "PDF")

    if zipfile.is_zipfile(io.BytesIO(данные)):
        try:
            вынуто = _из_офисного(данные)
        except (zipfile.BadZipFile, ElementTree.ParseError, KeyError):
            вынуто = None
        if вынуто is not None:
            вид, текст = вынуто
            путь = папка / f"{имя}.txt"
            путь.write_text(текст, encoding="utf-8")
            return Разбор(путь, вид, вынуто=True)
        return Разбор(None, отказ="это архив — распакуй и пришли файлы по одному")

    текст = _как_текст(данные, суффикс)
    if текст is not None:
        # Пишем заново в UTF-8: Read ждёт именно её, а файл из Windows
        # мог прийти в cp1251.
        путь = папка / имя
        путь.write_text(текст, encoding="utf-8")
        return Разбор(путь, "текст")

    if суффикс in СТАРЫЕ_ОФИСНЫЕ:
        return Разбор(
            None,
            отказ=f"это старый формат {суффикс} — пересохрани в "
            f"{суффикс}x или PDF и пришли ещё раз",
        )
    return Разбор(None, отказ="текста в нём я не нахожу")


def _как_текст(данные: bytes, суффикс: str) -> str | None:
    """Байты как текст — или None, если это не текст."""
    if b"\x00" in данные[:8192]:
        return None
    try:
        return данные.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    if суффикс in ТЕКСТОВЫЕ:
        return данные.decode("cp1251", errors="replace")
    return None


# ── офисные форматы ───────────────────────────────────────────────────────────


def _из_офисного(данные: bytes) -> tuple[str, str] | None:
    """(вид, текст) для Word, Excel, PowerPoint и OpenOffice. Чужой zip — None."""
    with zipfile.ZipFile(io.BytesIO(данные)) as архив:
        имена = set(архив.namelist())
        if "word/document.xml" in имена:
            return "документ Word", _абзацы(архив.read("word/document.xml"), "p", "t")
        if "xl/workbook.xml" in имена:
            return "таблица Excel", _excel(архив)
        if any(имя.startswith("ppt/slides/slide") for имя in имена):
            return "презентация PowerPoint", _powerpoint(архив, имена)
        if "content.xml" in имена and "mimetype" in имена:
            # OpenOffice: текст, таблица и презентация — один content.xml,
            # абзацы в нём одинаково зовутся text:p и text:h.
            return "документ OpenOffice", _абзацы(архив.read("content.xml"), ("p", "h"), None)
    return None


def _местное(тег: str) -> str:
    """Имя тега без пространства имён: `{http://…}p` → `p`."""
    return тег.rsplit("}", 1)[-1]


def _абзацы(xml: bytes, абзац: str | tuple[str, ...], кусок: str | None) -> str:
    """Текст по абзацам. `кусок` — тег, в котором лежат буквы; None — весь текст абзаца.

    Две ловушки Word, пойманные на настоящем файле 18.10.2026 — одна надпись
    в рамке выходила четырежды:
    - надпись-рамка — это абзацы ВНУТРИ абзаца; их буквы не приклеиваем
      к внешнему, а выводим отдельными строками;
    - mc:Fallback — копия той же рамки для старых программ; пропускаем целиком.
    """
    абзацы = (абзац,) if isinstance(абзац, str) else абзац
    строки: list[str] = []

    def абзац_целиком(элемент) -> None:
        буквы: list[str] = []

        def собрать(узел) -> None:
            if кусок is None:
                буквы.append(узел.text or "")
            for ребёнок in узел:
                имя = _местное(ребёнок.tag)
                if имя == "Fallback":
                    pass
                elif имя in абзацы:
                    абзац_целиком(ребёнок)
                elif кусок is not None and имя == кусок:
                    буквы.append(ребёнок.text or "")
                elif кусок is not None and имя == "tab":
                    буквы.append("\t")
                elif кусок is not None and имя in ("br", "cr"):
                    буквы.append("\n")
                else:
                    собрать(ребёнок)
                if кусок is None:
                    буквы.append(ребёнок.tail or "")

        собрать(элемент)
        строки.append("".join(буквы))

    def обойти(узел) -> None:
        for ребёнок in узел:
            имя = _местное(ребёнок.tag)
            if имя == "Fallback":
                continue
            if имя in абзацы:
                абзац_целиком(ребёнок)
            else:
                обойти(ребёнок)

    обойти(ElementTree.fromstring(xml))
    # Пустые абзацы подряд схлопываем в один: в документах ими делают отступы.
    return re.sub(r"\n{3,}", "\n\n", "\n".join(строки)).strip() + "\n"


def _powerpoint(архив: zipfile.ZipFile, имена: set[str]) -> str:
    слайды = sorted(
        (имя for имя in имена if re.fullmatch(r"ppt/slides/slide\d+\.xml", имя)),
        key=lambda имя: int(re.search(r"\d+", имя).group()),
    )
    части = []
    for номер, имя in enumerate(слайды, 1):
        части.append(f"── Слайд {номер} ──\n{_абзацы(архив.read(имя), 'p', 't')}")
    return "\n".join(части)


def _excel(архив: zipfile.ZipFile) -> str:
    """Каждый лист — строки, ячейки через табуляцию."""
    общие: list[str] = []
    if "xl/sharedStrings.xml" in архив.namelist():
        for элемент in ElementTree.fromstring(архив.read("xl/sharedStrings.xml")):
            if _местное(элемент.tag) == "si":
                общие.append("".join(
                    часть.text or "" for часть in элемент.iter() if _местное(часть.tag) == "t"
                ))

    # Имя листа и его файл связаны через rels: порядок файлов sheet1, sheet2…
    # с порядком листов в книге совпадать не обязан.
    ссылки = {}
    for связь in ElementTree.fromstring(архив.read("xl/_rels/workbook.xml.rels")):
        ссылки[связь.get("Id")] = связь.get("Target", "").lstrip("/").removeprefix("xl/")
    части = []
    for лист in ElementTree.fromstring(архив.read("xl/workbook.xml")).iter():
        if _местное(лист.tag) != "sheet":
            continue
        rid = next((знач for ключ, знач in лист.attrib.items() if _местное(ключ) == "id"), None)
        файл = f"xl/{ссылки.get(rid, '')}"
        if файл not in архив.namelist():
            continue
        строки = []
        for ряд in ElementTree.fromstring(архив.read(файл)).iter():
            if _местное(ряд.tag) != "row":
                continue
            ячейки = [_ячейка(я, общие) for я in ряд if _местное(я.tag) == "c"]
            if any(ячейки):
                строки.append("\t".join(ячейки))
        части.append(f"── Лист «{лист.get('name', '')}» ──\n" + "\n".join(строки))
    return "\n\n".join(части) + "\n"


def _ячейка(ячейка: ElementTree.Element, общие: list[str]) -> str:
    тип = ячейка.get("t")
    if тип == "inlineStr":
        return "".join(ч.text or "" for ч in ячейка.iter() if _местное(ч.tag) == "t")
    значение = next((ч.text or "" for ч in ячейка if _местное(ч.tag) == "v"), "")
    if тип == "s" and значение.isdigit() and int(значение) < len(общие):
        return общие[int(значение)]
    return значение
