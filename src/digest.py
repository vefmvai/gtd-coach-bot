"""Ночная выжимка: разговор дня превращается в короткую запись в журнале.

Смысл — не ждать, пока движок сам сожмёт разговор при упоре в лимит (тогда
он режет вслепую и теряет мелочи), а раз в сутки перечитать день целиком самой
умной моделью и оставить плотный конспект. Из этих дневных конспектов —
и только из них — собираются недели и месяцы: каждый уровень строится из ДНЕЙ,
а не из уровня выше. Так подробность уровня остаётся решением, а не следствием.
Читает коуч ровно 15 кусков: 7 дней, 5 недель, 3 месяца (см. WINDOW в archive.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from claude_agent_sdk import get_session_messages

from .archive import WINDOW, Archive
from .backrun import прогон
from .modes import Режим
from .prompts import load as load_prompt

log = logging.getLogger(__name__)

# У каждого уровня своя работа, иначе месяц получается пересказом недели.
# День — факты; неделя — что сдвинулось и что застряло; месяц — траектория.
LEVELS = {
    "week": ("неделю", "Работа этого уровня — что за неделю сдвинулось, а что застряло и почему."),
    "month": ("месяц", "Работа этого уровня — траектория месяца: куда всё двигалось, а не перечень дел."),
}
KIND = {"week": "недельная выжимка", "month": "месячная выжимка"}

# Столько символов разговора считается днём, о котором есть что сказать. Число
# стоит здесь одно на всех: его спрашивает и сама выжимка («говорить не о чем»),
# и поиск пропущенных дней. Два числа на один вопрос разъехались бы, и день
# оказался бы одновременно «пустым» для одного и «недоделанным» для другого —
# то есть задача про недоделанную ночь вставала бы каждую ночь и не гасла.
ПОРОГ = 200

# Выжимка — заметка мозга по стандарту Loreground, а не просто текстовый файл.
# Заголовок писался руками при перестройке памяти в этапе 12, но пишет-то файлы
# код: первая же новая выжимка вышла бы без заголовка, а валидатор считает такую
# заметку битой. Тип — `source`: выжимка ничего не утверждает про пользователя, она
# фиксирует сказанное. Корень провенанса — день разговора, у укрупнений корни
# те дни, из которых их собрали: пересказ своего корня не заводит.
FRONTMATTER = """---
title: {title}
type: source
schema_version: "1.0"
status: stable
created: {created}
source_type: personal-experience
reliability: C
author: {author}
ref: {ref}
root_id: [{roots}]
tags: [выжимка]
---

"""

# Список выжимок в точке входа собирается кодом между этими метками. Руками
# его вести нельзя: файлы ротируются каждую ночь, а ссылка на удалённый файл —
# это битая ссылка, которую валидатор находит, а человек нет.
INDEX_FILE = "00-index.md"
INDEX_START = "<!-- начало:выжимки — собирается кодом, src/digest.py -->"
INDEX_END = "<!-- конец:выжимки -->"


@dataclass
class DigestPaths:
    days: Path
    weeks: Path
    months: Path

    @classmethod
    def under(cls, brain_dir: Path) -> "DigestPaths":
        base = brain_dir / "память" / "журнал" / "выжимки"
        return cls(days=base / "дни", weeks=base / "недели", months=base / "месяцы")


class Digester:
    def __init__(self, brain_dir: Path, archive: Archive, model: str = "claude-fable-5",
                 mode: Режим | None = None, cost=None) -> None:
        self.brain_dir = brain_dir
        self.archive = archive
        self.model = model
        # Фоновый режим: ни встроенных инструментов, ни правил памяти.
        # Выжимка думает над текстом, по файлам не лазит и в знания не пишет.
        self.mode = mode
        self.cost = cost
        self.paths = DigestPaths.under(brain_dir)

    async def _summarize(self, prompt: str, system: str) -> str:
        return await прогон(
            mode=self.mode, prompt=prompt, system=system, model=self.model,
            effort="high",  # выжимка делается раз в сутки — экономить тут нечего
            cost=self.cost, channel="ночная выжимка", что="выжимка",
        )

    # --- день ---

    def _transcript(self, session_id: str) -> str:
        messages = get_session_messages(session_id, directory=str(self.brain_dir))
        lines: list[str] = []
        for item in messages:
            payload = item.message or {}
            role = payload.get("role") or item.type
            content = payload.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            text = text.strip()
            if text:
                who = "пользователь" if role == "user" else "Коуч"
                lines.append(f"{who}: {text}")
        return "\n\n".join(lines)

    def transcript_of(self, day: date) -> str:
        """Сырьё разговора за день — им кормится и выжимка, и ночная проверка памяти."""
        lines = []
        for role, channel, source, text in self.archive.messages_of_day(day.isoformat()):
            who = "Человек" if role == "user" else "Коуч"
            mark = " (голосом)" if channel == "voice" else ""
            if source == "laptop":
                mark = " (с ноутбука)"
            lines.append(f"{who}{mark}: {text}")
        return "\n\n".join(lines)

    async def make_day(self, session_id: str | None, day: date) -> Path | None:
        # Архив — основной источник: он переживает и обнуление сессии, и пересборку контейнера.
        transcript = self.transcript_of(day)
        if len(transcript) < ПОРОГ and session_id:
            transcript = self._transcript(session_id)
        if len(transcript) < ПОРОГ:
            log.info("за %s говорить не о чем — выжимку не делаю", day)
            return None

        day_prompt = load_prompt("выжимка-дня")
        summary = await self._summarize(
            day_prompt.format(day=day.isoformat()) + "\n\n" + transcript, day_prompt.system
        )
        if not summary or summary.strip().upper().startswith("ПУСТО"):
            return None

        self.paths.days.mkdir(parents=True, exist_ok=True)
        path = self.paths.days / f"{day.isoformat()}.md"
        head = FRONTMATTER.format(
            title=day.isoformat(),
            created=day.isoformat(),
            author="пользователь (со слов) + агент-коуч (запись)",
            ref=f"разговор {day.isoformat()}, записан агентом в мозг",
            roots=f"разговор-{day.isoformat()}",
        )
        path.write_text(f"{head}# {day.isoformat()} — день\n\n{summary}\n", encoding="utf-8")
        await self.archive.add_digest("day", day.isoformat(), summary)
        log.info("выжимка дня записана: %s", path)
        return path

    def missing_days(self, last: date, depth: int | None = None) -> list[date]:
        """Дни окна, где разговор был, а выжимки нет. Самый старый — первым.

        Ночь может не удаться: моделью не вышло (её нет на подписке, движок
        вернул ошибку, сеть легла). Раньше такой день терялся навсегда — выжимка
        делалась ровно за вчера и ровно один раз, второй попытки не предусмотрено.
        Поймано 07.09.2026 на живом сервере: в ночь на 5 сентября выжимка
        не сделалась, и дыра в журнале осталась насовсем.

        Догон нужен не сам по себе. Он — то, что позволяет рвать ленту разговора
        БЕЗУСЛОВНО: раньше ленту рвали только после удачной выжимки, потому что
        иначе день пропал бы. Теперь день не пропадает, и торговаться не о чем.

        Глубина — окно памяти (7 дней): дальше выжимка коучем всё равно уже
        не читается, и делать её незачем.
        """
        depth = WINDOW["day"] if depth is None else depth
        first = last - timedelta(days=depth - 1)
        готовы = {ключ for ключ, _ in
                  self.archive.day_digests(first.isoformat(), last.isoformat())}
        дни = []
        for шаг in range(depth):
            день = first + timedelta(days=шаг)
            if день.isoformat() in готовы:
                continue
            if len(self.transcript_of(день)) < ПОРОГ:
                continue  # в этот день не разговаривали — сворачивать нечего
            дни.append(день)
        return дни

    # --- укрупнение ---

    async def _rollup(
        self,
        days: list[tuple[str, str]],
        target: Path,
        title: str,
        period: str,
        period_key: str,
        closed: date,
    ) -> Path | None:
        """Собрать уровень из дневных выжимок. Один день — укрупнять нечего.

        `closed` — последний день периода: из него берётся дата сборки, чтобы
        заголовок заметки не зависел от того, когда именно запустили код.
        """
        if len(days) < 2:
            log.info("%s %s: дневных выжимок %d — укрупнять нечего", period, period_key, len(days))
            return None
        body = "\n\n---\n\n".join(f"{key}:\n{text}" for key, text in days)
        period_name, focus = LEVELS[period]
        rollup = load_prompt("выжимка-укрупнение")
        summary = await self._summarize(
            rollup.format(period=period_name, focus=focus) + "\n\n" + body, rollup.system
        )
        if not summary:
            return None
        head = FRONTMATTER.format(
            title=period_key,
            created=(closed + timedelta(days=1)).isoformat(),
            author="агент-коуч (сборка из дневных выжимок)",
            ref=f"{KIND[period]} {period_key}, собрана из дней {days[0][0]} — {days[-1][0]}",
            roots=", ".join(f"разговор-{key}" for key, _ in days),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{head}# {title}\n\n{summary}\n", encoding="utf-8")
        await self.archive.add_digest(period, period_key, summary)
        log.info("укрупнение записано: %s (дней на входе: %d)", target, len(days))
        return target

    async def make_week(self, week_end: date) -> Path | None:
        """Свернуть календарную неделю Пн–Вс, которая закончилась `week_end`.

        Ключ — номер ISO (`2026-W30`), а не диапазон дат: диапазон сортировался
        текстом вперемешку с ключами месяцев и вытеснял их из окна памяти.
        """
        start = week_end - timedelta(days=6)
        iso = week_end.isocalendar()
        key = f"{iso.year}-W{iso.week:02d}"
        days = self.archive.day_digests(start.isoformat(), week_end.isoformat())
        target = self.paths.weeks / f"{key}.md"
        title = f"Неделя {key} ({start.isoformat()} — {week_end.isoformat()})"
        return await self._rollup(days, target, title, "week", key, closed=week_end)

    async def make_month(self, day_of_month: date) -> Path | None:
        """Свернуть календарный месяц, которому принадлежит `day_of_month`.

        Именно календарный: раньше месяц собирался из недель, которые в нём
        *начались*, — и неделя 29 июня – 5 июля числилась июньской, а первых
        пяти дней июля в июльском месяце не было вовсе.
        """
        first = day_of_month.replace(day=1)
        last = (first + timedelta(days=31)).replace(day=1) - timedelta(days=1)
        key = first.isoformat()[:7]
        days = self.archive.day_digests(first.isoformat(), last.isoformat())
        target = self.paths.months / f"{key}.md"
        return await self._rollup(days, target, f"Месяц {key}", "month", key, closed=last)

    # --- ротация журнала ---

    def rotate(self) -> None:
        """Привести журнал в мозге к тому же окну, что читает коуч, и починить адреса.

        Два действия неразделимы: удалить файл, оставив ссылку на него в точке
        входа, — значит завести битую ссылку. Поэтому одна дверь, а не две.
        """
        self._prune()
        self._refresh_index()

    def _prune(self) -> None:
        """Держать в журнале то же окно, что видит коуч: 7 дневных файлов и 5 недельных.

        Удаляются только файлы. Тексты остаются строками в базе (оттуда их берут
        и окно памяти, и укрупнение) и в истории git — потери нет. Месяцы не
        трогаем: их дюжина в год, это долгая память, которую человек листает
        руками.
        """
        for folder, keep in ((self.paths.days, WINDOW["day"]), (self.paths.weeks, WINDOW["week"])):
            if not folder.exists():
                continue
            for path in sorted(folder.glob("*.md"))[:-keep]:
                path.unlink(missing_ok=True)
                log.info("файл выжимки убран из журнала: %s", path.name)

    def _refresh_index(self) -> None:
        """Переписать список выжимок в точке входа памяти между метками.

        Меток нет — молча ничего не делаем и говорим об этом в лог: чужой файл
        код правит только там, где ему это разрешили явно.
        """
        index = self.brain_dir / "память" / INDEX_FILE
        if not index.exists():
            return
        text = index.read_text(encoding="utf-8")
        if INDEX_START not in text or INDEX_END not in text:
            log.warning("в %s нет меток списка выжимок — список не обновлён", index)
            return

        lines = []
        for folder, name in ((self.paths.months, "месяцы"), (self.paths.weeks, "недели"),
                             (self.paths.days, "дни")):
            keys = sorted(path.stem for path in folder.glob("*.md")) if folder.exists() else []
            if keys:
                lines.append(" · ".join(f"[[{key}]]" for key in keys) + f" — {name}")
        block = "\n".join(f"- {line}" for line in lines) or "- пока пусто"

        head, _, rest = text.partition(INDEX_START)
        _, _, tail = rest.partition(INDEX_END)
        index.write_text(f"{head}{INDEX_START}\n{block}\n{INDEX_END}{tail}", encoding="utf-8")
        log.info("список выжимок в точке входа обновлён: %d строк", len(lines))
