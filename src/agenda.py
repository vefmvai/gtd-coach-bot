"""Сводка дел: маленький агрегат Todoist, который коуч обязан знать всегда.

Правило (PROJECT.md, 31.07.2026): принудительно в контекст идёт только то, что
коуч обязан знать всегда; подробности он достаёт инструментом, когда они
понадобились. Вывалить все задачи — сотни тысяч токенов и нежизнеспособно.
Вывалить агрегат — пара сотен.

Почему кодом, а не просьбой «посмотри Todoist» в конституции: просьба в тексте
срабатывает, если модель не отвлеклась. Ровно на этом в июле протекло знание
(разбор 31.07). Код по будильнику случается всегда.

Дом факта остаётся один — Todoist. Это отражение, и правится оно только через
источник: хочешь другую сводку — меняй дела, а не сводку.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from todoist_mcp.client import TodoistClient, TodoistError

from .detectors import мои

log = logging.getLogger(__name__)

# Метки веса из этапа 03: вес задачи — время, а не штука.
MINUTES = {"⏱️S": 20, "⏱️M": 120, "⏱️L": 240}


def _today() -> date:
    return datetime.now(ZoneInfo(os.environ.get("COACH_TZ") or "Europe/Moscow")).date()


def _due_date(task: dict) -> date | None:
    raw = (task.get("due") or {}).get("date") or ""
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _hours(tasks: list[dict]) -> float:
    minutes = sum(MINUTES.get(label, 0) for task in tasks for label in task.get("labels") or [])
    return round(minutes / 60, 1)


async def _by_filter(client: TodoistClient, query: str, limit: int = 50) -> list[dict]:
    # Чужие задачи общего проекта в сводку дня не идут: это дела другого
    # человека, и считать их своими просрочками — значит дожимать чужим.
    data = await client.get("/tasks/filter", params={"query": query, "limit": limit})
    return await мои(client, data.get("results", []) if isinstance(data, dict) else (data or []))


async def summary(token: str) -> str:
    """Одна-две строки про день. Пусто — значит Todoist не ответил; это не повод молчать.

    Ошибку глотаем намеренно: сводка — приправа, а не блюдо. Коуч должен ответить
    пользователю даже когда Todoist лежит, и у него остаётся инструмент, чтобы сходить
    за делами руками.
    """
    try:
        async with TodoistClient(token) as client:
            now = _today()
            current = await _by_filter(client, "today | overdue")
            ahead = await _by_filter(client, "14 days")
    except TodoistError as err:
        log.warning("сводка дел не собралась: %s", err)
        return ""
    except Exception:
        log.exception("сводка дел не собралась")
        return ""

    overdue = [t for t in current if (d := _due_date(t)) and d < now]
    today = [t for t in current if t not in overdue]
    urgent = [t for t in today if (t.get("priority") or 1) == 4]

    lines = [
        f"Сегодня {now.isoformat()}: дел {len(today)}"
        + (f", из них p1 — {len(urgent)}" if urgent else "")
        + (f", по меткам времени ~{_hours(today)} ч" if _hours(today) else "")
        + (f". Просрочено: {len(overdue)}" if overdue else ". Просроченного нет")
    ]

    later = sorted(
        ((d, t) for t in ahead if (d := _due_date(t)) and d > now), key=lambda pair: pair[0]
    )
    if later:
        when, task = later[0]
        lines.append(f"Ближайший срок после сегодня: «{task['content']}» — {when.isoformat()}.")

    lines.append("Это агрегат. Нужны сами дела — сходи инструментом find_tasks.")
    return "\n".join(lines)
