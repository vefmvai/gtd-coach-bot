"""Дашборд стратегии — один самодостаточный HTML-файл, который бот шлёт в телеграм.

Решение владельца 31.07 вместо дашборда на сервере: айфон открывает такой файл
во внутреннем браузере, ничего не надо хостить и не надо пароля.

**Ноль внешних запросов** — не про красоту. Из РФ половина CDN недоступна:
внешний шрифт не приедет, и вёрстка развалится ровно на телефоне, где смотреть
и собирались. Поэтому стили внутрь, колесо — inline SVG, ни одного `<script src>`.

**Собирает код, а не модель.** Правило, купленное трижды за этап 13: то, что
обязано случаться, делает код. Модель даёт содержание (миссию, горизонты,
оценки колеса — они лежат в мозге её рукой), форму даёт код.

Содержание утверждено владельцем до написания кода — «стратегия сверху, приборы
снизу», подробности в `.bpd/stages/16-strategicheskij-kontur/ДАШБОРД.md`.

**Пустое место — не ошибка.** Слой наполняется разговором, а не этапом. Нет
файла миссии — блок говорит «пока не заполнено» и страница не ломается.
"""

from __future__ import annotations

import html
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from math import cos, pi, sin
from pathlib import Path

import yaml

from todoist_mcp.client import TodoistClient, TodoistError

# Нормы приборов и работа по целям живут у сторожа завалов (`detectors.py`):
# витрина и утренний бриф обязаны показывать одно и то же, а до этапа 09
# нормы были объявлены здесь и второму потребителю пришлось бы их повторить.
from .detectors import (  # noqa: F401
    GAUGES,
    В_ИГРЕ_НОРМА,
    _спрятанные_проекты as _спрятанные,
    в_игре_список,
    видимые,
    мои,
    по_фильтру as _счёт,
    потеряшки_список,
    СФЕРЫ,
    без_сферы_список,
)

log = logging.getLogger(__name__)

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
YAML_BLOCK = re.compile(r"```yaml\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class Данные:
    """Всё, что попадает на страницу. Пустое поле рисуется как «пока не заполнено»."""

    миссия: str = ""
    черновики: list[str] = field(default_factory=list)
    горизонты: dict[str, str] = field(default_factory=dict)
    колесо: list[tuple[str, dict]] = field(default_factory=list)
    сферы: list[tuple[str, int]] = field(default_factory=list)
    приборы: list[tuple[str, int, str]] = field(default_factory=list)
    период: dict[str, int] = field(default_factory=dict)


# ── чтение мозга ─────────────────────────────────────────────────────────────

def _черновик(path: Path) -> bool:
    """Заметка ещё не подтверждена человеком (`status: draft` в шапке)."""
    if not path.exists():
        return False
    head = FRONTMATTER.match(path.read_text(encoding="utf-8"))
    return bool(head and re.search(r"^status:\s*draft\s*$", head.group(1), re.M))


def _тело(path: Path) -> str:
    """Содержательная часть заметки: без шапки, заголовка и служебных врезок.

    **Цитаты (`>`) на дашборд не идут.** В этом мозге блок-цитата — всегда
    служебное: закон модуля, объяснение валидатору, пометка «это черновик».
    Проверено вживую 31.07: первая версия вывалила на телефон рассуждение
    про `consensus: single` вместо миссии. Человеку нужно содержание,
    а разговор с валидатором пусть остаётся в файле.
    """
    if not path.exists():
        return ""
    текст = FRONTMATTER.sub("", path.read_text(encoding="utf-8"))
    строки = [
        s for s in текст.splitlines()
        if not s.startswith("# ") and not s.lstrip().startswith(">")
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(строки)).strip()


def _суть(path: Path) -> str:
    """Только первый содержательный раздел заметки — то, что человек хочет видеть.

    Дашборд — витрина, а не читалка файлов. В заметке миссии после сути идут
    таблица «откуда взято и что здесь домысел», раздел «что спросить», связи
    и история обновлений: всё это рабочий материал агента. Владелец открыл файл
    на телефоне и увидел именно их — значит выводить надо не файл, а суть.

    Берём тело первого `##`-раздела; нет заголовков — берём всё.
    """
    текст = _тело(path)
    разделы = re.split(r"^##\s+.*$", текст, flags=re.M)
    for кусок in разделы:
        if кусок.strip():
            return кусок.strip()
    return текст


def _горизонты(path: Path) -> dict[str, str]:
    """Разделы ГОД / КВАРТАЛ / МЕСЯЦ. Ищем по заголовку, а не по порядку."""
    текст = _тело(path)
    if not текст:
        return {}
    куски: dict[str, str] = {}
    текущий = ""
    for строка in текст.splitlines():
        заголовок = re.match(r"^##\s+(ГОД|КВАРТАЛ|МЕСЯЦ)\b(.*)$", строка.strip())
        if заголовок:
            текущий = заголовок.group(1)
            куски[текущий] = заголовок.group(2).strip() + "\n"
        elif текущий:
            куски[текущий] += строка + "\n"
    return {k: v.strip() for k, v in куски.items() if v.strip()}


def _колесо(path: Path) -> list[tuple[str, dict]]:
    """Ряд замеров из YAML-блока. Формат по читателю: файл читает КОД."""
    if not path.exists():
        return []
    блок = YAML_BLOCK.search(path.read_text(encoding="utf-8"))
    if not блок:
        return []
    try:
        данные = yaml.safe_load(блок.group(1)) or {}
    except yaml.YAMLError as err:
        log.warning("колесо баланса не разбирается: %s", err)
        return []
    if not isinstance(данные, dict):
        return []
    ряды = [(str(k), v) for k, v in данные.items() if isinstance(v, dict)]
    return sorted(ряды)[-6:]  # последние полгода: линия видна, страница не пухнет


# ── чтение архива ────────────────────────────────────────────────────────────

def _период(db_path: Path, дней: int) -> dict[str, int]:
    """Сколько закрыто за период. Считает снимок, а не журнал Todoist:
    на Free журнал живёт около недели, а снимок помнит всё."""
    if not db_path.exists():
        return {}
    порог = (date.today() - timedelta(days=дней)).isoformat()
    try:
        with sqlite3.connect(db_path) as db:
            строка = db.execute(
                "SELECT count(*) FROM todoist_closed WHERE completed_at >= ?", (порог,)
            ).fetchone()
    except sqlite3.Error as err:
        log.warning("архив закрытых не читается: %s", err)
        return {}
    return {"закрыто": строка[0] if строка else 0, "дней": дней}


def _по_сферам(db_path: Path, дней: int) -> list[tuple[str, int]]:
    """Сколько дел закрыто по каждой сфере. Порядок — как в `СФЕРЫ`, всегда
    все шесть: сфера с нулём — это тоже показание, и как раз самое говорящее."""
    счёт = {с: 0 for с in СФЕРЫ}
    if not db_path.exists():
        return list(счёт.items())
    порог = (date.today() - timedelta(days=дней)).isoformat()
    try:
        with sqlite3.connect(db_path) as db:
            строки = db.execute(
                "SELECT labels FROM todoist_closed WHERE completed_at >= ?", (порог,)
            ).fetchall()
    except sqlite3.Error as err:
        log.warning("закрытые по сферам не читаются: %s", err)
        return list(счёт.items())
    for (метки,) in строки:
        for м in (метки or "").split(","):
            if м in счёт:
                счёт[м] += 1
    return list(счёт.items())


# ── сборка данных ────────────────────────────────────────────────────────────

async def собрать(brain_dir: Path, db_path: Path, token: str, дней: int = 7) -> Данные:
    память = brain_dir / "память"
    черновики = [
        имя for имя, путь in (
            ("миссия", память / "знания" / "миссия.md"),
            ("ценности", память / "знания" / "ценности.md"),
            ("горизонты", память / "состояние" / "горизонты.md"),
        ) if _черновик(путь)
    ]
    данные = Данные(
        миссия=_суть(память / "знания" / "миссия.md"),
        черновики=черновики,
        горизонты=_горизонты(память / "состояние" / "горизонты.md"),
        колесо=_колесо(память / "журнал" / "колесо-баланса.md"),
        период=_период(db_path, дней),
        сферы=_по_сферам(db_path, дней),
    )
    try:
        async with TodoistClient(token) as client:
            приборы = [
                (имя, len(await _счёт(client, запрос)), норма) for имя, запрос, норма in GAUGES
            ]
            # «Потеряшки» и «В игре» фильтром тоже не выражаются: обоим нужно
            # знать, есть ли у задачи подзадачи, а такого условия в языке
            # запросов Todoist нет. Тянем все задачи один раз и считаем кодом —
            # тем же самым, что считает ночной обход, чтобы дашборд и обход
            # не разошлись в показаниях.
            все = await client.get_paginated("/tasks", params={"limit": 200}, cap=1000)
            все = видимые(await мои(client, все), await _спрятанные(client))
            потеряшек = len(потеряшки_список(все))
            ведём = len(в_игре_список(все))

            данные.приборы = [
                ("Потеряшки", потеряшек, "пусто"),
                ("В игре", ведём, f"не больше {В_ИГРЕ_НОРМА}"),
                ("Без сферы", len(без_сферы_список(все)), "0"),
                *приборы,
            ]
    except (TodoistError, OSError) as err:
        log.error("Todoist для дашборда недоступен: %s", err)
    return данные


# ── рисование ────────────────────────────────────────────────────────────────

def _э(текст: str) -> str:
    return html.escape(текст or "")


ЖИРНОЕ = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
КОД = re.compile(r"`([^`]+)`")


def _строка(текст: str) -> str:
    """Экранируем, потом отрисовываем жирное и код. Порядок именно такой:
    сначала гасим чужой HTML, и только потом ставим свой."""
    готово = _э(текст)
    готово = ЖИРНОЕ.sub(r"<strong>\1</strong>", готово)
    return КОД.sub(r"<code>\1</code>", готово)


def _абзацы(текст: str) -> str:
    """Абзацы, списки и подзаголовки. Не полный markdown — ровно то, чем
    написаны заметки стратегии: `**жирное**`, `` `код` ``, списки на дефисах, `###`.
    Иначе на телефоне видны сами звёздочки, а не жирный шрифт (проверено вживую)."""
    if not текст:
        return '<p class="пусто">Пока не заполнено — проговорим в разговоре.</p>'

    куски = []
    for блок in (k.strip() for k in текст.split("\n\n")):
        if not блок:
            continue
        строки = блок.splitlines()
        if all(s.lstrip().startswith(("- ", "* ")) for s in строки):
            пункты = "".join(f"<li>{_строка(s.lstrip()[2:])}</li>" for s in строки)
            куски.append(f"<ul>{пункты}</ul>")
        elif all(s.strip().startswith("|") for s in строки):
            куски.append(_таблица(строки))
        elif блок.startswith("#"):
            голова, _, хвост = блок.partition("\n")
            куски.append(f"<h4>{_строка(голова.lstrip('# '))}</h4>")
            if хвост.strip():
                куски.append(_абзацы(хвост.strip()))
        else:
            # Одиночный перенос строки — тот же абзац, а не разрыв. Иначе текст
            # рвётся посередине предложения там, где в файле кончилась строка.
            куски.append("<p>" + _строка(" ".join(s.strip() for s in строки)) + "</p>")
    return "".join(куски)


def _таблица(строки: list[str]) -> str:
    """Markdown-таблица в HTML. Без этого на телефоне видны сами палки и дефисы."""
    ряды = [
        [я.strip() for я in s.strip().strip("|").split("|")]
        for s in строки
        if not re.fullmatch(r"\|[\s|:-]+\|", s.strip())
    ]
    if not ряды:
        return ""
    шапка = "".join(f"<th>{_строка(я)}</th>" for я in ряды[0])
    тело = "".join(
        "<tr>" + "".join(f"<td>{_строка(я)}</td>" for я in ряд) + "</tr>" for ряд in ряды[1:]
    )
    return f'<div class="обёртка"><table><thead><tr>{шапка}</tr></thead><tbody>{тело}</tbody></table></div>'


def _колесо_svg(ряды: list[tuple[str, dict]]) -> str:
    """Колесо последнего замера. Inline SVG: картинка без единого запроса наружу."""
    if not ряды:
        return '<p class="пусто">Замеров ещё нет — первый сделаем на месячной стратсессии.</p>'

    _, последний = ряды[-1]
    сферы = [(k, v) for k, v in последний.items() if isinstance(v, (int, float))]
    if not сферы:
        return '<p class="пусто">В последнем замере нет чисел.</p>'

    R, центр, макс = 90, 110, 5
    кольца = "".join(
        f'<circle cx="{центр}" cy="{центр}" r="{R * i / макс:.1f}" class="кольцо"/>'
        for i in range(1, макс + 1)
    )
    точки, подписи = [], []
    for i, (имя, оценка) in enumerate(сферы):
        угол = -pi / 2 + 2 * pi * i / len(сферы)
        r = R * min(float(оценка), макс) / макс
        точки.append(f"{центр + r * cos(угол):.1f},{центр + r * sin(угол):.1f}")
        подписи.append(
            f'<text x="{центр + (R + 16) * cos(угол):.1f}" y="{центр + (R + 16) * sin(угол):.1f}" '
            f'class="подпись" text-anchor="middle">{_э(имя)} {оценка}</text>'
        )
    return (
        f'<svg viewBox="0 0 220 220" class="колесо" role="img" '
        f'aria-label="колесо баланса">{кольца}'
        f'<polygon points="{" ".join(точки)}" class="фигура"/>{"".join(подписи)}</svg>'
    )


def _линия(ряды: list[tuple[str, dict]]) -> str:
    """Ряд замеров таблицей. Перекос — это не «сегодня четвёрка»,
    а «съезжает третий месяц подряд», и увидеть это можно только рядом."""
    if len(ряды) < 2:
        return ""
    сферы = list(ряды[-1][1].keys())
    шапка = "".join(f"<th>{_э(s)}</th>" for s in сферы)
    строки = "".join(
        f"<tr><th>{_э(месяц)}</th>"
        + "".join(f"<td>{_э(str(оценки.get(s, '—')))}</td>" for s in сферы)
        + "</tr>"
        for месяц, оценки in ряды
    )
    return f'<table class="ряд"><thead><tr><th>месяц</th>{шапка}</tr></thead><tbody>{строки}</tbody></table>'


def _сферы_html(ряды: list[tuple[str, int]], дней: int) -> str:
    """Дела по сферам за месяц — полосками, теми же словами, что и колесо.

    **Стоит рядом с колесом намеренно.** Колесо мерит самочувствие, полоски —
    факт, и предмет разговора на стратсессии — расхождение этих двух картинок:
    «семья на тройку, а закрыто ноль дел». Раньше сравнивать было нечего:
    списка сфер было два, и они не пересекались ни одним словом.

    Оценки колеса из фактов НЕ считаются и считаться не будут: не у каждой
    сферы много дел, а «здоровье на двойку» человек знает про себя сам.
    """
    если_пусто = (
        f'<p class="пусто">За {дней} дн. ни одна закрытая задача не носила метку '
        f'сферы. Метки заведены 08.08.2026 — картинка наполнится за месяц, '
        f'потому что считает она закрытое, а закрытое до этого дня меток не знало.</p>'
    )
    if not ряды or not any(n for _, n in ряды):
        return если_пусто
    макс = max(n for _, n in ряды) or 1
    # Приставку `☸️` в подписи снимаем: рядом стоит колесо, а оно подписано
    # словами из `ценности.md` — «работа», «семья». Одна картинка со значком,
    # другая без него читаются как два разных списка, то есть ровно как та
    # болезнь, которую этап и лечил.
    полосы = "".join(
        f'<tr><th>{_э(имя.removeprefix("☸️"))}</th>'
        f'<td class="полоса"><span style="width:{100 * n / макс:.0f}%"></span></td>'
        f'<td class="число">{n}</td></tr>'
        for имя, n in ряды
    )
    return f'<table class="сферы"><tbody>{полосы}</tbody></table>'



def _приборы_html(приборы: list[tuple[str, int, str]]) -> str:
    if not приборы:
        return '<p class="пусто">Todoist не ответил — приборы не сняты.</p>'
    плитки = ""
    for имя, факт, норма in приборы:
        исправен = норма == "—" or (норма in ("пусто", "0") and факт == 0)
        плитки += (
            f'<div class="плитка {"зелёная" if исправен else "красная"}">'
            f'<div class="цифра">{факт}</div><div class="имя">{_э(имя)}</div>'
            f'<div class="норма">норма: {_э(норма)}</div></div>'
        )
    return f'<div class="плитки">{плитки}</div>'


СТИЛИ = """
:root { color-scheme: light dark; --фон:#fff; --текст:#1a1a1a; --тихий:#666;
        --рамка:#e2e2e2; --акцент:#2f6f4f; --тревога:#b03030; --карточка:#fafafa; }
@media (prefers-color-scheme: dark) {
  :root { --фон:#16181c; --текст:#e8e8e8; --тихий:#9aa0a6; --рамка:#2c2f36;
          --акцент:#6fbf8f; --тревога:#e07a7a; --карточка:#1d2027; }
}
* { box-sizing: border-box; }
body { margin:0; padding:16px; background:var(--фон); color:var(--текст);
       font:16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       max-width:900px; margin-inline:auto; }
h1 { font-size:22px; margin:0 0 4px; }
h2 { font-size:17px; margin:28px 0 10px; padding-bottom:6px;
     border-bottom:1px solid var(--рамка); }
h3 { font-size:15px; margin:16px 0 6px; color:var(--акцент); letter-spacing:.04em; }
.дата { color:var(--тихий); font-size:14px; margin:0 0 8px; }
.пусто { color:var(--тихий); font-style:italic; }
.черновик { background:var(--карточка); border:1px solid var(--тревога); color:var(--тревога);
            border-radius:8px; padding:8px 10px; font-size:13px; margin:0 0 14px; }
h4 { font-size:14px; margin:14px 0 4px; color:var(--тихий); text-transform:uppercase;
     letter-spacing:.04em; }
ul { margin:0 0 10px; padding-left:20px; }
li { margin-bottom:4px; }
code { background:var(--рамка); border-radius:4px; padding:1px 5px; font-size:13px; }
section { background:var(--карточка); border:1px solid var(--рамка);
          border-radius:12px; padding:14px 16px; margin-bottom:14px; }
p { margin:0 0 10px; }
.обёртка { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:14px; }
th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--рамка);
         vertical-align:top; }
.плитки + table th, .плитки + table td { white-space:nowrap; }
tbody th { font-weight:600; white-space:normal; }
tr.тревога td, tr.тревога th { color:var(--тревога); }
.плитки { display:grid; gap:10px; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); }
.плитка { border:1px solid var(--рамка); border-radius:10px; padding:10px; text-align:center; }
.плитка.зелёная { border-color:var(--акцент); }
.плитка.красная { border-color:var(--тревога); }
.цифра { font-size:26px; font-weight:700; line-height:1; }
.плитка.красная .цифра { color:var(--тревога); }
.плитка.зелёная .цифра { color:var(--акцент); }
.имя { font-size:14px; margin-top:4px; }
.норма { font-size:12px; color:var(--тихий); }
.колесо { width:100%; max-width:280px; height:auto; display:block; margin:0 auto; }
.кольцо { fill:none; stroke:var(--рамка); stroke-width:1; }
.фигура { fill:var(--акцент); fill-opacity:.28; stroke:var(--акцент); stroke-width:2; }
.подпись { font-size:9px; fill:var(--текст); }
footer { color:var(--тихий); font-size:12px; margin-top:22px; }
table.сферы { width:100%; }
table.сферы th { text-align:left; white-space:nowrap; padding-right:10px; font-weight:600; }
td.полоса { width:100%; }
td.полоса span { display:block; height:14px; border-radius:7px; background:var(--акцент); min-width:2px; }
td.число { text-align:right; padding-left:8px; font-variant-numeric:tabular-nums; }
"""


def нарисовать(данные: Данные, день: date) -> str:
    горизонты = "".join(
        f"<h3>{_э(имя)}</h3>{_абзацы(данные.горизонты.get(имя, ''))}"
        for имя in ("ГОД", "КВАРТАЛ", "МЕСЯЦ")
    )
    плашка = (
        '<p class="черновик">⚠️ Черновик: ' + _э(", ".join(данные.черновики))
        + " — собрано агентом из журнала и пользователем не подтверждено.</p>"
    ) if данные.черновики else ""
    период = данные.период
    период_html = (
        f"<p>Закрыто за последние {период['дней']} дн.: <strong>{период['закрыто']}</strong></p>"
        if период else '<p class="пусто">Снимков дел ещё нет — счёт появится через сутки.</p>'
    )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Стратегия · {день.isoformat()}</title>
<style>{СТИЛИ}</style></head><body>
<h1>Стратегия</h1>
<p class="дата">{день.strftime('%d.%m.%Y')}</p>
{плашка}

<section><h2>Миссия</h2>{_абзацы(данные.миссия)}</section>

<section><h2>Горизонты</h2>{горизонты}</section>

<section><h2>Колесо баланса — самочувствие</h2>{_колесо_svg(данные.колесо)}
<div class="обёртка">{_линия(данные.колесо)}</div></section>

<section><h2>Дела по сферам — факт за {данные.период.get('дней', 30)} дн.</h2>
{_сферы_html(данные.сферы, данные.период.get('дней', 30))}</section>

<section><h2>Приборы</h2>{_приборы_html(данные.приборы)}</section>

<section><h2>Период</h2>{период_html}</section>

<footer>Собрано ботом-коучем. Смысл живёт в памяти, работа — в Todoist;
здесь только вид сверху.</footer>
</body></html>"""


async def собрать_файл(brain_dir: Path, db_path: Path, token: str,
                       куда: Path | None = None, дней: int = 7) -> Path:
    """Собрать дашборд и положить файлом. Возвращает путь.

    Имя с датой — чтобы вчерашний и сегодняшний не путались в чате.
    """
    день = date.today()
    данные = await собрать(brain_dir, db_path, token, дней)
    папка = куда or Path(os.environ.get("DASHBOARD_DIR", "/tmp"))
    папка.mkdir(parents=True, exist_ok=True)
    путь = папка / f"стратегия-{день.isoformat()}.html"
    путь.write_text(нарисовать(данные, день), encoding="utf-8")
    log.info("дашборд собран: %s (%d Б)", путь, путь.stat().st_size)
    return путь
