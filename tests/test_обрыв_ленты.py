"""Разговор не растёт бесконечно: ночь рвёт ленту, а потолок страхует.

Заявка gtd-coach#1 (07.09.2026). У пользователя ночная модель была недоступна
на его подписке, ночная выжимка не удавалась — и закладка разговора не стиралась,
потому что стиралась она ТОЛЬКО после удачной выжимки. Один разговор рос месяц:
рюкзак поднялся с 220 тысяч токенов до 645 тысяч, лимит подписки выгорал за пару
диалогов, а в логе была одна строка, которую никто не читал.

Та же болезнь в миниатюре нашлась и на своём сервере: в ночь на 5 сентября
выжимка сорвалась, и 5-го рюкзак был 97 тысяч вместо обычных семидесяти.

Здесь проверяется всё, чем это лечится:

1. ночь рвёт ленту безусловно — сорванная выжимка её не отменяет;
2. пропущенный день догоняется следующей ночью, а не теряется навсегда;
3. недоделанное дёргает за рукав задачей, а не строкой в логе;
4. потолок обрывает разговор, если ночь не отработала несколько раз подряд.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import date, timedelta
from pathlib import Path

import pytest

from src.archive import Archive
from src.digest import ПОРОГ, Digester
from src.sessions import SessionStorage


# --- 1. Ночь рвёт ленту безусловно ------------------------------------------
#
# Ночной прогон целиком не запускается: он просит живой телеграм-контекст.
# Поэтому смотрим в разобранный код — тем же приёмом, что сторож гашения
# карточек в test_inbox.py. Дёшево и ловит ровно то, что сломалось.

def _ночь() -> ast.AsyncFunctionDef:
    дерево = ast.parse(Path(__file__).resolve().parents[1].joinpath("src/main.py")
                       .read_text(encoding="utf-8"))
    ночь = next((у for у in ast.walk(дерево)
                 if isinstance(у, ast.AsyncFunctionDef) and у.name == "nightly_digest"), None)
    assert ночь is not None, "ночной прогон переименовали — сторож ослеп, почини его"
    return ночь


def _зовут_clear(узел) -> bool:
    return any(isinstance(у, ast.Call) and isinstance(у.func, ast.Attribute)
               and у.func.attr == "clear" for у in ast.walk(узел))


def test_ночь_рвёт_ленту_и_это_не_под_условием():
    """Обрыв ленты не должен зависеть ни от чего — в этом весь смысл правки.

    Именно условие «если выжимка удалась» и стоило пользователю месяца роста
    контекста. Вернуть его обратно легко и незаметно, поэтому здесь стоит сторож.
    """
    ночь = _ночь()
    assert _зовут_clear(ночь), "ночь перестала рвать ленту разговора — контекст снова растёт"

    for узел in ast.walk(ночь):
        if isinstance(узел, ast.If) and _зовут_clear(узел):
            pytest.fail(
                "обрыв ленты снова оказался под условием — это ровно та поломка, "
                "из-за которой рюкзак пользователя дорос до 645 тысяч токенов"
            )


def test_ночь_рвёт_ленту_до_всякой_работы():
    """Обрыв стоит выше `try`, а не внутри него.

    Внутри `try` он пережил бы удачную ночь, но не упавшую на первой же строке —
    а падает ночь именно так: `brain.pull()` первый в очереди.
    """
    ночь = _ночь()
    внутри_замка = next(у for у in ночь.body if isinstance(у, ast.AsyncWith)).body
    до_try = []
    for шаг in внутри_замка:
        if isinstance(шаг, ast.Try):
            break
        до_try.append(шаг)
    assert any(_зовут_clear(шаг) for шаг in до_try), \
        "обрыв ленты уехал внутрь try — упавшая на первом шаге ночь его не выполнит"


# --- 2 и 3. Догон пропущенных дней ------------------------------------------

class Стенд:
    """Архив и журнал во временной папке; модель подменена заглушкой."""

    def __init__(self, tmp_path: Path, ломать: bool = False) -> None:
        self.archive = Archive(tmp_path / "coach.db")
        self.digester = Digester(tmp_path / "brain", self.archive)
        self.ломать = ломать
        self.свёрнуто: list[str] = []

        async def выжимка(prompt: str, system: str = "") -> str:
            if self.ломать:
                # Ровно то, что делает `прогон`, когда модель недоступна:
                # пустая строка и строчка в логе.
                return ""
            self.свёрнуто.append(prompt)
            return "конспект дня"

        self.digester._summarize = выжимка

    def поговорить(self, day: date) -> None:
        """День с настоящим разговором — длиннее порога, иначе сворачивать нечего."""
        текст = "рассказываю, как прошёл день, и о чём договорились. " * 10
        assert len(текст) > ПОРОГ
        asyncio.run(self.archive.add_message("user", "telegram", текст, None, None))
        # Сообщение ложится сегодняшним числом — переставляем день руками.
        with self.archive._connect() as db:
            db.execute("UPDATE messages SET day=? WHERE day!=?",
                       (day.isoformat(), day.isoformat()))


def test_пропущенный_день_догоняется_следующей_ночью(tmp_path):
    """Сорванная ночь оставляет дыру — и следующая ночь её закрывает."""
    вчера = date(2026, 9, 4)
    сломанная = Стенд(tmp_path, ломать=True)
    сломанная.поговорить(вчера)

    asyncio.run(сломанная.digester.make_day(None, вчера))
    assert сломанная.digester.missing_days(вчера) == [вчера], \
        "день не свернулся, а в недоделанных не числится — догонять будет нечего"

    # Следующая ночь: модель снова отвечает, окно то же.
    сломанная.ломать = False
    сегодня = вчера + timedelta(days=1)
    for день in сломанная.digester.missing_days(сегодня):
        asyncio.run(сломанная.digester.make_day(None, день))
    assert сломанная.digester.missing_days(сегодня) == [], \
        "пропущенный день не догнали — дыра в журнале осталась навсегда"


def test_день_без_разговора_недоделанным_не_считается(tmp_path):
    """Иначе задача про сорванную ночь вставала бы каждую ночь и не гасла."""
    стенд = Стенд(tmp_path)
    молчали = date(2026, 9, 4)
    assert стенд.digester.missing_days(молчали) == []


def test_свёрнутый_день_в_недоделанные_не_возвращается(tmp_path):
    стенд = Стенд(tmp_path)
    день = date(2026, 9, 4)
    стенд.поговорить(день)
    asyncio.run(стенд.digester.make_day(None, день))
    assert стенд.digester.missing_days(день) == []


def test_догон_не_лезет_дальше_окна_памяти(tmp_path):
    """Выжимку старше окна коуч всё равно не читает — делать её незачем."""
    стенд = Стенд(tmp_path)
    давно = date(2026, 9, 4)
    стенд.поговорить(давно)
    поздно = давно + timedelta(days=30)
    assert стенд.digester.missing_days(поздно) == []


def test_ночь_поднимает_задачу_на_недоделанное():
    """Поломка обязана дёргать за рукав, а не оставаться строкой в логе.

    Ровно этого не хватило пользователю из заявки: ночь падала месяц молча.
    """
    дерево = ast.parse(Path(__file__).resolve().parents[1].joinpath("src/main.py")
                       .read_text(encoding="utf-8"))
    сворачивание = next((у for у in ast.walk(дерево)
                         if isinstance(у, ast.AsyncFunctionDef) and у.name == "_свернуть_дни"), None)
    assert сворачивание is not None, "догон дней переименовали — сторож ослеп"
    поводы = {у.args[1].value for у in ast.walk(сворачивание)
              if isinstance(у, ast.Call) and isinstance(у.func, ast.Name)
              and у.func.id == "raise_task" and len(у.args) > 1
              and isinstance(у.args[1], ast.Constant)}
    assert "выжимка" in поводы, \
        "недоделанная выжимка больше не поднимает задачу — поломка снова молчит"

    from src.backstage import FINDINGS, ПОЛОМКИ
    assert "выжимка" in FINDINGS, "повода «выжимка» нет в реестре — raise_task упадёт"
    assert "выжимка" in ПОЛОМКИ, "сорванная ночь — поломка, её надо считать по ночам"


# --- 4. Потолок на рюкзак ----------------------------------------------------

class ДвижокЗаглушка:
    """Только то, что нужно сторожу потолка: закладка и метод сверки."""

    def __init__(self, path: Path) -> None:
        self.sessions = SessionStorage(path)

    _сверить_с_потолком = None  # подставляется ниже настоящим методом


@pytest.fixture
def движок(tmp_path):
    from src.engine import CoachEngine
    ДвижокЗаглушка._сверить_с_потолком = CoachEngine._сверить_с_потолком
    заглушка = ДвижокЗаглушка(tmp_path / "session_id")
    заглушка.sessions.save("разговор-1")
    return заглушка


def test_рюкзак_выше_потолка_рвёт_ленту(движок):
    движок._сверить_с_потолком(645_436, {"потолок_разговора": 300_000})
    assert движок.sessions.load() is None, \
        "рюкзак вдвое выше потолка, а разговор продолжается — страховка не сработала"


def test_обычный_рюкзак_ленту_не_трогает(движок):
    движок._сверить_с_потолком(71_307, {"потолок_разговора": 300_000})
    assert движок.sessions.load() == "разговор-1", \
        "обычный день оборвал разговор — коуч будет терять нить на ровном месте"


def test_нулевой_потолок_выключает_сторожа(движок):
    движок._сверить_с_потолком(2_000_000, {"потолок_разговора": 0})
    assert движок.sessions.load() == "разговор-1"


def test_обряд_не_рвётся_посередине(движок):
    """Стратсессия — один разговор по замыслу, её обрывает ночь, а не потолок."""
    движок.sessions.начать_обряд("полный", "недельный")
    движок._сверить_с_потолком(645_436, {"потолок_разговора": 300_000})
    assert движок.sessions.load() == "разговор-1", \
        "потолок оборвал стратсессию посередине — нить потеряна там, где она дороже всего"


def test_потолок_живёт_в_настройках_мозга(tmp_path):
    """Число — настройка, а не константа в питоне: у ученика подписка другая."""
    from src import settings as coach_settings

    coach_settings.ensure(tmp_path, "2026-09-07")
    values, беды = coach_settings.read(tmp_path)
    assert not беды
    assert values["потолок_разговора"] >= coach_settings.МИНИМАЛЬНЫЙ_ПОТОЛОК
    assert "потолок_разговора" in coach_settings.path_in(tmp_path).read_text(encoding="utf-8")


def test_слишком_низкий_потолок_отвергается(tmp_path):
    """Разговор, который рвётся каждые три реплики, — это не экономия."""
    from src import settings as coach_settings

    файл = coach_settings.path_in(tmp_path)
    файл.parent.mkdir(parents=True)
    файл.write_text(
        '```yaml\nмодель_разговора: "claude-fable-5"\n'
        'модель_ночной_работы: "claude-fable-5"\nпотолок_разговора: 500\n```\n',
        encoding="utf-8",
    )
    values, беды = coach_settings.read(tmp_path)
    assert беды and "потолок_разговора" in беды[0]
    assert values == dict(coach_settings.DEFAULTS)


def test_старый_файл_настроек_дотягивается_до_схемы(tmp_path):
    """У кого файл заведён до этой правки — строка должна доехать сама."""
    from src import settings as coach_settings

    файл = coach_settings.path_in(tmp_path)
    файл.parent.mkdir(parents=True)
    файл.write_text(
        '# Настройки\n\n```yaml\nмодель_разговора: "claude-opus-5"\n'
        'модель_ночной_работы: "claude-fable-5"\nрежим: "рабочий"\n```\n',
        encoding="utf-8",
    )
    coach_settings.ensure(tmp_path, "2026-09-07")
    текст = файл.read_text(encoding="utf-8")
    assert "потолок_разговора" in текст, "новая настройка до старого файла не доехала"
    assert 'модель_разговора: "claude-opus-5"' in текст, "чужой выбор затёрли"
