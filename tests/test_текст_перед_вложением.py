"""Текст, присланный прямо перед файлом, уходит коучу вместе с файлом.

Поймано 18.10.2026: «переслать с комментарием» Telegram шлёт двумя
сообщениями — сперва комментарий, потом файл. Бот ответил на комментарий
сразу и сказал «вложение не доехало», хотя файл стоял в очереди следующим.
"""

import asyncio
from types import SimpleNamespace as NS

import src.main as main
from src.main import CoachBot


def _бот(monkeypatch, файл_прочтён=True):
    monkeypatch.setattr(main, "ПАУЗА_ТЕКСТА", 0.05)
    бот = CoachBot.__new__(CoachBot)
    бот._mine = lambda update: True
    бот._ждущий_текст, бот._ждущий_ответ = "", None
    бот.ответы, бот.подписи = [], []

    async def think(text, chat_id, context, **kw):
        бот.ответы.append(text)

    async def файл(update, context, подпись):
        бот.подписи.append(подпись)
        return файл_прочтён

    бот._think_and_reply, бот._файл = think, файл
    return бот


def _текст(t):
    return NS(effective_chat=NS(id=1), message=NS(text=t))


def _документ(caption=None):
    return NS(effective_chat=NS(id=1), message=NS(caption=caption))


def test_комментарий_и_файл_следом_один_ответ(monkeypatch):
    бот = _бот(monkeypatch)

    async def сценарий():
        await бот.on_text(_текст("вот стратсессия"), None)
        await бот.on_document(_документ("5 октября"), None)
        await asyncio.sleep(0.1)

    asyncio.run(сценарий())
    assert бот.подписи == ["вот стратсессия\n5 октября"]
    assert бот.ответы == []  # отдельного ответа на текст нет


def test_текст_без_вложения_отвечается_после_паузы(monkeypatch):
    бот = _бот(monkeypatch)

    async def сценарий():
        await бот.on_text(_текст("привет"), None)
        assert бот.ответы == []
        await asyncio.sleep(0.1)

    asyncio.run(сценарий())
    assert бот.ответы == ["привет"]


def test_куски_длинного_текста_склеиваются(monkeypatch):
    бот = _бот(monkeypatch)

    async def сценарий():
        await бот.on_text(_текст("первая часть"), None)
        await бот.on_text(_текст("вторая часть"), None)
        await asyncio.sleep(0.1)

    asyncio.run(сценарий())
    assert бот.ответы == ["первая часть\nвторая часть"]


def test_файл_не_прочтён_текст_не_пропадает(monkeypatch):
    бот = _бот(monkeypatch, файл_прочтён=False)

    async def сценарий():
        await бот.on_text(_текст("глянь архив"), None)
        await бот.on_document(_документ(), None)
        await asyncio.sleep(0.1)

    asyncio.run(сценарий())
    assert бот.ответы == ["глянь архив"]
