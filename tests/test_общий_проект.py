"""Общий проект: чужие задачи не попадают в «мои» списки бота.

Двое работают в одном проекте Todoist, у каждого свой коуч. Чужая задача —
дело другого человека: сводка дня, приборы обхода, дашборд и ночной снимок
её не считают. Читатели найдены поиском по классу (все выборки `/tasks`
и `/tasks/filter` в `src/`), отбор у всех один — `detectors.мои`.

Имена вымышленные: репозиторий публичный.
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path

import pytest
from todoist_mcp import people

from src import agenda, dashboard, detectors
from src import todoist_snapshot as снимок

Я, АННА = "1", "2"
ОБЩИЙ = "p-team"


@pytest.fixture(autouse=True)
def двое(monkeypatch):
    люди = people.Люди(я=Я, имена={Я: "Борис", АННА: "Анна"}, общие={ОБЩИЙ: [Я, АННА]})

    async def загрузить(client):
        return люди

    monkeypatch.setattr(people, "загрузить", загрузить)


def задача(tid, content, responsible=None, автор=Я, project_id=ОБЩИЙ, due=None, **поля):
    t = {"id": tid, "content": content, "project_id": project_id, "section_id": None,
         "parent_id": None, "responsible_uid": responsible, "added_by_uid": автор,
         "labels": [], "priority": 1, "description": "", "added_at": "2026-10-01",
         "due": {"date": due} if due else None}
    t.update(поля)
    return t


ВЧЕРА = (date.today() - timedelta(days=1)).isoformat()
ЗАДАЧИ = [
    задача("мой", "Моё дело", responsible=Я, due=ВЧЕРА),
    задача("её", "Дело Анны", responsible=АННА, due=ВЧЕРА),
    задача("руками", "Анна поставила в приложении", автор=АННА, due=ВЧЕРА),
    задача("дом", "Личное", project_id="p-home", due=ВЧЕРА),
]


class Todoist:
    """Отдаёт одни и те же задачи на все пути, которыми ходят читатели."""

    def __init__(self, задачи=ЗАДАЧИ):
        self.задачи = list(задачи)

    def __call__(self, token):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, path, params=None):
        assert path == "/tasks/filter", path
        return {"results": list(self.задачи)}

    async def get_paginated(self, path, params=None, key="results", cap=300):
        if path == "/projects":
            return [{"id": ОБЩИЙ, "name": "Работа"}, {"id": "p-home", "name": "Дом"}]
        if path == "/sections":
            return []
        if path in ("/tasks", "/tasks/completed/by_completion_date"):
            return list(self.задачи)
        if path == "/comments":
            return []
        raise AssertionError(path)


def ids(задачи):
    return {з["id"] for з in задачи}


def test_отбор_оставляет_моё_и_личное():
    assert ids(asyncio.run(detectors.мои(Todoist(), ЗАДАЧИ))) == {"мой", "дом"}


def test_приборы_обхода_по_фильтру_не_видят_чужого():
    assert ids(asyncio.run(detectors.по_фильтру(Todoist(), "overdue"))) == {"мой", "дом"}


def test_сводка_дня_считает_только_свои_просрочки(monkeypatch):
    monkeypatch.setattr(agenda, "TodoistClient", Todoist())
    сводка = asyncio.run(agenda.summary("токен"))
    assert "Просрочено: 2" in сводка, сводка


def test_дашборд_не_считает_чужих_потеряшек(tmp_path, monkeypatch):
    # без срока и без состояния — все четыре были бы потеряшками
    monkeypatch.setattr(dashboard, "TodoistClient", Todoist([dict(з, due=None) for з in ЗАДАЧИ]))
    (tmp_path / "память" / "состояние").mkdir(parents=True)
    данные = asyncio.run(dashboard.собрать(tmp_path, Path("/нет/такой.db"), "токен"))
    потеряшки = dict((имя, n) for имя, n, _ in данные.приборы)["Потеряшки"]
    assert потеряшки == 2


def test_ночной_снимок_хранит_только_своё(tmp_path, monkeypatch):
    monkeypatch.setattr(снимок, "TodoistClient", Todoist())
    хранилище = снимок.TodoistSnapshot(tmp_path / "coach.db", "токен")
    живые, закрытые = asyncio.run(хранилище._collect())
    assert {r["task_id"] for r in живые} == {"мой", "дом"}
    assert {r["task_id"] for r in закрытые} == {"мой", "дом"}


def test_ночной_обход_не_зовёт_чужое_потеряшкой(tmp_path, monkeypatch):
    monkeypatch.setattr(detectors, "TodoistClient", Todoist([dict(з, due=None) for з in ЗАДАЧИ]))
    monkeypatch.setattr(detectors, "GAUGES", ())
    свод = asyncio.run(detectors.обойти("токен", tmp_path / "coach.db"))
    текст = свод.текст()
    assert "Моё дело" in текст and "Личное" in текст, текст
    assert "Дело Анны" not in текст and "Анна поставила" not in текст
