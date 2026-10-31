"""Ночной снимок Todoist в SQLite — несгораемая история дел.

Беспокойство владельца 31.07: «если тудуист историю не хранит, её надо как-то
переносить в наш несгораемый архив». Так и есть — на бесплатном тарифе журнал
активности живёт около недели, а удалённая карточка исчезает без следа.

Правило, ради которого всё:

> **Прежде чем что-то в Todoist исчезнет, оно уже лежит в снимке.**

Только после этого удаление карточки безопасно. Этап 16 сам создаёт горючее:
он кладёт «зачем эта затея» в описание карточки — а описание умирает вместе
с карточкой.

**Закрытые за сутки обязательны.** Без них план/факт не посчитать: закрытая
задача исчезает из виду, а через год «планировал 4 цели, закрыл 3» × 24 месяца
даёт настоящую ёмкость человека — счёт, а не ощущение.

**Ноль токенов.** Факты копит код по будильнику; смысл читает модель раз в месяц
на итоге и пишет прозой в журнал. То, что обязано случаться, модели не поручают.

**В файлы мозга дамп не идёт** (прямой вопрос владельца): дом цифр — база,
дом смысла — журнал. В память попадает только проза месячного итога.

Размер: ~100 задач × ~300 Б ≈ 30 КБ в ночь, ~11 МБ в год. Полный снимок без
хитростей с дельтами — «система должна быть простой», а том уже в штатном бэкапе.

Берётся **минимум**: детекторы завалов, хронические переносы и утренний бриф
остаются этапу 09. Здесь только копится горючее.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from todoist_mcp.client import TodoistClient, TodoistError

from .detectors import мои

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS todoist_tasks (
    day          TEXT NOT NULL,          -- день снимка, YYYY-MM-DD
    task_id      TEXT NOT NULL,
    content      TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    project      TEXT NOT NULL DEFAULT '',
    section      TEXT NOT NULL DEFAULT '',
    parent_id    TEXT,
    labels       TEXT NOT NULL DEFAULT '',   -- через запятую
    priority     INTEGER,
    due          TEXT,                       -- YYYY-MM-DD или пусто
    deadline     TEXT,
    duration     INTEGER,
    added_at     TEXT,
    comments     TEXT NOT NULL DEFAULT '',   -- JSON-список строк
    PRIMARY KEY (day, task_id)
);
CREATE INDEX IF NOT EXISTS todoist_tasks_id ON todoist_tasks(task_id);

CREATE TABLE IF NOT EXISTS todoist_closed (
    task_id      TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    content      TEXT NOT NULL,
    project      TEXT NOT NULL DEFAULT '',
    -- Поля профиля: без времени заведения не посчитать, сколько задача прожила,
    -- а без меток — не сказать, чем отличается судьба короткой задачи от длинной.
    added_at     TEXT NOT NULL DEFAULT '',
    labels       TEXT NOT NULL DEFAULT '',
    priority     INTEGER,
    due          TEXT NOT NULL DEFAULT '',
    postponed    INTEGER NOT NULL DEFAULT 0,   -- сколько раз переезжала до закрытия
    PRIMARY KEY (task_id, completed_at)
);

CREATE TABLE IF NOT EXISTS todoist_diff (
    day     TEXT NOT NULL,
    kind    TEXT NOT NULL,      -- закрыта | появилась | исчезла | перенесена | изменена
    task_id TEXT NOT NULL,
    content TEXT NOT NULL,
    detail  TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (day, kind, task_id)
);
CREATE INDEX IF NOT EXISTS todoist_diff_day ON todoist_diff(day);
"""

# Поля, изменение которых считаем осмысленным. Порядок задач и служебные
# отметки сюда не входят: диффом должно быть видно работу, а не шум.
WATCHED = ("content", "description", "project", "section", "labels", "priority", "duration")


def _today() -> date:
    return date.today() if not os.environ.get("COACH_TZ") else _tz_today()


def _month_after(day: date) -> date:
    """Тот же день следующего месяца; 31-е там, где его нет, съезжает на конец."""
    год, месяц = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    из_месяца = [31, 29 if год % 4 == 0 and (год % 100 or год % 400 == 0) else 28,
                 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][месяц - 1]
    return date(год, месяц, min(day.day, из_месяца))


def _tz_today() -> date:
    from datetime import datetime

    return datetime.now(ZoneInfo(os.environ["COACH_TZ"])).date()


def _closed_row(task: dict, project: str = "") -> dict:
    """Строка закрытой задачи. Одна форма на ночной снимок и на добор истории."""
    return {
        "task_id": task.get("id", ""),
        "completed_at": task.get("completed_at", ""),
        "content": task.get("content", ""),
        "project": project,
        "added_at": task.get("added_at") or task.get("created_at") or "",
        "labels": ",".join(task.get("labels") or []),
        "priority": task.get("priority"),
        "due": ((task.get("due") or {}).get("date") or "")[:10],
        # Считает сам Todoist, и считает исторически — своим снимкам до этого
        # копить недели. У повторяющихся задач счётчик растёт от каждого
        # повтора, поэтому потребитель обязан их отсеивать.
        "postponed": task.get("postponed_count") or 0,
    }


CLOSED_INSERT = (
    "INSERT OR REPLACE INTO todoist_closed(task_id, completed_at, content, project,"
    " added_at, labels, priority, due, postponed)"
    " VALUES(:task_id, :completed_at, :content, :project, :added_at, :labels,"
    " :priority, :due, :postponed)"
)


class TodoistSnapshot:
    """Снимок дел на конец суток плюс дифф с прошлым снимком."""

    def __init__(self, db_path: Path, token: str) -> None:
        self.db_path = db_path
        self.token = token
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)
            self._migrate(db)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """Дотянуть живую базу до свежей схемы. Только добавляющие правки."""
        было = {row[1] for row in db.execute("PRAGMA table_info(todoist_closed)")}
        for колонка, тип in (("added_at", "TEXT NOT NULL DEFAULT ''"),
                             ("labels", "TEXT NOT NULL DEFAULT ''"),
                             ("priority", "INTEGER"),
                             ("due", "TEXT NOT NULL DEFAULT ''"),
                             ("postponed", "INTEGER NOT NULL DEFAULT 0")):
            if колонка not in было:
                db.execute(f"ALTER TABLE todoist_closed ADD COLUMN {колонка} {тип}")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    # --- сбор ---

    async def _collect(self) -> tuple[list[dict], list[dict]]:
        """Живые задачи с комментариями и закрытые за сутки."""
        async with TodoistClient(self.token) as client:
            projects = await client.get_paginated("/projects", params={"limit": 100})
            names = {p["id"]: p.get("name", "") for p in projects}
            sections = await client.get_paginated("/sections", params={"limit": 200})
            section_names = {s["id"]: s.get("name", "") for s in sections}

            # Снимок — история дел ЭТОГО человека. Чужие задачи общего проекта
            # хранит коуч их хозяина; здесь они превратили бы чужую работу
            # в свою — в план/факт, профиль и «закрыто за месяц».
            tasks = await мои(client, await client.get_paginated(
                "/tasks", params={"limit": 200}, cap=1000))

            # Комментарии спрашиваем У КАЖДОЙ задачи, по одному запросу.
            #
            # Дважды пытались сэкономить, и оба раза экономия молча теряла данные.
            # Первый заход тянул по проектам — `/comments?project_id=` отдаёт
            # комментарии САМОГО проекта, а не его задач: ноль при 140 задачах.
            # Второй заход спрашивал только там, где `note_count > 0`, — а поле
            # врёт: на живом аккаунте комментарий добавили, и note_count остался
            # нулём (проверка 31.07). То есть отбор по нему пропустил бы всё.
            #
            # Плата за честность — около сотни запросов в ночь. Это ноль токенов
            # и десяток секунд раз в сутки; потерянный комментарий стоит дороже.
            comments: dict[str, list[str]] = {}
            for task in tasks:
                try:
                    found = await client.get_paginated(
                        "/comments", params={"task_id": task["id"], "limit": 100}
                    )
                except TodoistError as err:
                    log.warning("комментарии задачи %s не забрались: %s", task["id"], err)
                    continue
                if found:
                    comments[task["id"]] = [c.get("content", "") for c in found]

            yesterday = (_today() - timedelta(days=1)).isoformat()
            today = _today().isoformat()
            try:
                # Список закрытых лежит под ключом `items`, а не `results`.
                # Раньше клиент знал только вторую форму и молча собирал пустоту:
                # 0 строк в `todoist_closed` при 1826 закрытых по счётчику Todoist
                # (найдено прогоном 01.08). Теперь обе формы знает сам клиент.
                closed = await client.get_paginated(
                    "/tasks/completed/by_completion_date",
                    params={"since": f"{yesterday}T00:00:00", "until": f"{today}T00:00:00",
                            "limit": 200},
                    cap=500,
                )
            except TodoistError as err:
                log.warning("закрытые за сутки не забрались: %s", err)
                closed = []
            closed = await мои(client, closed)

        rows = []
        for task in tasks:
            due = (task.get("due") or {}).get("date") or ""
            rows.append({
                "task_id": task["id"],
                "content": task.get("content", ""),
                "description": task.get("description") or "",
                "project": names.get(task.get("project_id"), ""),
                "section": section_names.get(task.get("section_id"), ""),
                "parent_id": task.get("parent_id"),
                "labels": ",".join(task.get("labels") or []),
                "priority": task.get("priority"),
                "due": due[:10],
                "deadline": (task.get("deadline") or {}).get("date") or "",
                "duration": (task.get("duration") or {}).get("amount"),
                "added_at": task.get("added_at") or task.get("created_at") or "",
                "comments": json.dumps(comments.get(task["id"], []), ensure_ascii=False),
            })

        done = [_closed_row(t, names.get(t.get("project_id"), "")) for t in closed]
        return rows, done

    # --- запись ---

    def _previous_day(self, db: sqlite3.Connection, before: str) -> str | None:
        row = db.execute(
            "SELECT day FROM todoist_tasks WHERE day < ? ORDER BY day DESC LIMIT 1", (before,)
        ).fetchone()
        return row[0] if row else None

    def _diff(self, db: sqlite3.Connection, day: str, rows: list[dict],
              closed: list[dict]) -> list[tuple]:
        """Что изменилось против прошлого снимка. Первый снимок диффа не даёт."""
        previous = self._previous_day(db, day)
        if previous is None:
            return []

        было = {
            r[0]: dict(zip(("task_id", *WATCHED, "due"), r))
            for r in db.execute(
                f"SELECT task_id, {', '.join(WATCHED)}, due FROM todoist_tasks WHERE day = ?",
                (previous,),
            )
        }
        стало = {r["task_id"]: r for r in rows}
        закрытые = {c["task_id"] for c in closed}

        changes: list[tuple] = []
        for task_id, было_ in было.items():
            if task_id in стало:
                continue
            if task_id in закрытые:
                changes.append((day, "закрыта", task_id, было_["content"], ""))
            else:
                # Исчезла, но не закрывалась — удалена. Из Todoist она пропала
                # без следа; здесь остаётся и она сама, и её описание.
                changes.append((day, "исчезла", task_id, было_["content"], "удалена или заархивирована"))

        for task_id, стало_ in стало.items():
            было_ = было.get(task_id)
            if было_ is None:
                changes.append((day, "появилась", task_id, стало_["content"], ""))
                continue
            if (было_["due"] or "") != (стало_["due"] or ""):
                changes.append((
                    day, "перенесена", task_id, стало_["content"],
                    f"{было_['due'] or 'без даты'} → {стало_['due'] or 'без даты'}",
                ))
            правки = [
                f"{поле}: {было_[поле]!r} → {стало_[поле]!r}"
                for поле in WATCHED
                if (было_[поле] or "") != (стало_[поле] or "")
            ]
            if правки:
                changes.append((day, "изменена", task_id, стало_["content"], "; ".join(правки)))
        return changes

    def _write(self, day: str, rows: list[dict], closed: list[dict]) -> dict[str, int]:
        with self._connect() as db:
            changes = self._diff(db, day, rows, closed)

            db.execute("DELETE FROM todoist_tasks WHERE day = ?", (day,))
            db.executemany(
                "INSERT INTO todoist_tasks(day, task_id, content, description, project, section,"
                " parent_id, labels, priority, due, deadline, duration, added_at, comments)"
                " VALUES(:day, :task_id, :content, :description, :project, :section,"
                " :parent_id, :labels, :priority, :due, :deadline, :duration, :added_at, :comments)",
                [dict(r, day=day) for r in rows],
            )
            db.executemany(CLOSED_INSERT, closed)
            db.execute("DELETE FROM todoist_diff WHERE day = ?", (day,))
            db.executemany(
                "INSERT OR REPLACE INTO todoist_diff(day, kind, task_id, content, detail)"
                " VALUES(?,?,?,?,?)",
                changes,
            )
        return {"задач": len(rows), "закрыто": len(closed), "изменений": len(changes)}

    # --- разовый добор истории ---

    async def backfill_closed(self, since: date, until: date | None = None) -> int:
        """Долить закрытые задачи задним числом. Возвращает, сколько строк легло.

        Зачем. Ночной снимок копит закрытые с той ночи, когда его починили,
        а профиль поведения (часы работы, срок жизни задачи) может считаться
        сразу: Todoist отдаёт историю **примерно за три месяца** — проверено
        01.08, с 01.05 отдаёт 57 задач, с 01.02 отвечает 400 «completion date
        range must be…». Значит профиль стартует с маем–июлем, а не с нуля.

        Просим помесячно, а не одним куском: граница окна у Todoist своя
        и точного числа дней он не называет — месяц заведомо внутри неё,
        и отказ на дальнем месяце не уносит с собой ближние.
        """
        until = until or _today()
        собрано: list[dict] = []
        async with TodoistClient(self.token) as client:
            # Имена проектов нужны обязательно. Первый добор их не спрашивал —
            # «проект мог переименоваться» — и показатель «где идёт работа»
            # честно показал «закрыто 0» по всем проектам при 56 закрытых
            # задачах. Пустое поле читается как факт «там ничего не делали»,
            # хотя означает «мы не спросили»; это хуже неточного имени.
            try:
                projects = await client.get_paginated("/projects", params={"limit": 100})
                names = {p["id"]: p.get("name", "") for p in projects}
            except TodoistError as err:
                log.warning("имена проектов для добора не забрались: %s", err)
                names = {}
            начало = since
            while начало < until:
                конец = min(_month_after(начало), until)
                try:
                    куски = await client.get_paginated(
                        "/tasks/completed/by_completion_date",
                        params={"since": f"{начало.isoformat()}T00:00:00",
                                "until": f"{конец.isoformat()}T00:00:00", "limit": 200},
                        cap=2000,
                    )
                except TodoistError as err:
                    log.warning("история с %s по %s не забралась: %s", начало, конец, err)
                    куски = []
                собрано.extend(куски)
                начало = конец

        rows = [_closed_row(t, names.get(t.get("project_id"), ""))
                for t in собрано if t.get("completed_at")]

        def _write() -> int:
            with self._connect() as db:
                db.executemany(CLOSED_INSERT, rows)
                return db.execute("SELECT COUNT(*) FROM todoist_closed").fetchone()[0]

        всего = await asyncio.to_thread(_write)
        log.info("добор истории с %s: пришло %d, в таблице стало %d", since, len(rows), всего)
        return всего

    async def run(self, day: date | None = None) -> dict[str, int]:
        """Снять снимок за день. Todoist недоступен — ноль и жалоба в лог.

        Ночной прогон из-за этого не роняем: снимок — контур сохранности,
        а не условие того, чтобы выжимка случилась.
        """
        day = day or _today()
        try:
            rows, closed = await self._collect()
        except (TodoistError, OSError) as err:
            log.error("снимок Todoist не снялся: %s", err)
            return {"задач": 0, "закрыто": 0, "изменений": 0}

        counts = await asyncio.to_thread(self._write, day.isoformat(), rows, closed)
        log.info(
            "снимок Todoist за %s: задач %d, закрыто за сутки %d, изменений %d",
            day, counts["задач"], counts["закрыто"], counts["изменений"],
        )
        return counts
