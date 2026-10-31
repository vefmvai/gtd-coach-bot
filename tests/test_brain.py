"""Отправка мозга на GitHub: застрявший коммит обязан уехать следующей ночью.

Поломка #233 (ночь 18.09.2026): коммит выжимки сделался, а отправка не прошла.
Тогда это вылечилось само — следующей ночью была свежая правка, и `push`
увёз оба коммита. Но `push` смотрел только на незакоммиченное: в тихую ночь
(человек молчит — выжимки нет, правок нет) застрявший коммит не уезжал вовсе,
а `dirty()` при этом честно говорил «есть что отдавать», и задача человеку
поднималась каждую ночь, хотя отправить никто даже не пробовал.

Здесь настоящий git: подделка не поймала бы ровно то, что ломается.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from src.brain import Brain


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def мозг(tmp_path):
    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    клон = tmp_path / "brain"
    git("clone", "-q", str(origin), str(клон), cwd=tmp_path)
    git("config", "user.email", "t@t", cwd=клон)
    git("config", "user.name", "t", cwd=клон)
    (клон / "a.md").write_text("первое", encoding="utf-8")
    git("add", "-A", cwd=клон)
    git("commit", "-q", "-m", "начало", cwd=клон)
    git("push", "-q", "-u", "origin", "main", cwd=клон)
    return клон, origin


def test_застрявший_коммит_уезжает_в_тихую_ночь(мозг):
    клон, origin = мозг
    brain = Brain(клон)

    # Ночь первая: правка есть, а GitHub недоступен.
    (клон / "a.md").write_text("выжимка", encoding="utf-8")
    git("remote", "set-url", "origin", str(origin) + "-нет-такого", cwd=клон)
    assert asyncio.run(brain.push("выжимка")) is False
    assert asyncio.run(brain.dirty()) is True

    # Ночь вторая: сеть вернулась, но новых правок нет.
    git("remote", "set-url", "origin", str(origin), cwd=клон)
    assert asyncio.run(brain.push("тихая ночь")) is True
    assert asyncio.run(brain.dirty()) is False
    assert git("rev-parse", "main", cwd=origin) == git("rev-parse", "HEAD", cwd=клон)


def test_нечего_отдавать_значит_нечего(мозг):
    клон, _ = мозг
    brain = Brain(клон)
    assert asyncio.run(brain.push("пусто")) is False
    assert asyncio.run(brain.dirty()) is False
