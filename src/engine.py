"""Движок коуча: Claude Agent SDK поверх памяти и инструментов.

Слой ничего не знает про Telegram — на вход текст, на выход текст. Когда
появится агент-шлюз (см. .bpd этапа 04), сюда придут другие потребители.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TaskNotificationMessage,
    TextBlock,
)

from . import modes as режимы_модуль
from .context_cost import ContextCost, контекст, разобрать
from .gcal import build_calendar_server
from .inbox import Inbox
from .inbox_tool import build_inbox_server
from .modes import Режим
from .settings import read as read_settings
from .sessions import SessionStorage
from .todoist import build_todoist_server
from .dashboard_tool import build_dashboard_server
from .wishes import build_wishes_server

log = logging.getLogger(__name__)

# Инструменты памяти + git (Bash ограничен списком разрешённых команд в settings)
MEMORY_TOOLS = ["Read", "Write", "Edit", "Glob", "Grep", "Bash", "TodoWrite"]

# Помощников коуч нанимать не может — и это не придирка, а свойство архитектуры.
# Движок здесь живёт ровно один ответ: `async with ClaudeSDKClient` закрывается,
# и всё, что он породил, умирает вместе с ним. Фоновый агент доработать не успевает
# НИКОГДА, а его осиротевшая запись потом присылает уведомление на каждом
# возобновлении сессии — и это уведомление съедает ход целиком (30.07.2026).
# Снимать запрет можно только вместе с переходом на долгоживущий клиент.
FORBIDDEN_TOOLS = ["Task"]

# Роль коуча — плагин gtd-coach, смонтированный только на чтение (том /plugin).
# Маркетплейс здесь не годится: движок в контейнере принимает лишь локальный путь
# (SdkPluginConfig = {"type": "local", "path": str}, проверено на SDK 0.2.124).
# Оттуда же приезжают части конституции и состав режимов — см. modes.py.
PLUGINS = [{"type": "local", "path": "/plugin"}]

ДНИ = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
МЕСЯЦЫ = ("", "января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")


def сегодня_строкой(сейчас: datetime | None = None) -> str:
    """Какое сегодня число и день недели — словами, в системный промпт.

    03.08.2026 коуч сказал «у тебя сегодня и понедельник», хотя понедельник
    уже шёл, и пользователь его поправил. Дата в контексте была, но вслух нигде
    не объявлялась, а день недели не назывался вовсе — считать его модели
    приходилось самой, и она ошиблась. Просьбой в конституции это не лечится:
    считать всё равно пришлось бы ей.

    Зона — `COACH_TZ`, как у обхода дел и повестки: нужна дата там, где живёт
    человек, а не там, где стоит сервер. Переезд в другую страну меняет одну строку
    в `.env`, а не код.
    """
    сейчас = сейчас or datetime.now(ZoneInfo(os.environ.get("COACH_TZ") or "Europe/Moscow"))
    return (f"Сегодня {ДНИ[сейчас.weekday()]}, {сейчас.day} {МЕСЯЦЫ[сейчас.month]} "
            f"{сейчас.year} года ({сейчас.date().isoformat()}).")


class CoachEngine:
    """Один непрерывный разговор с коучем."""

    def __init__(
        self,
        brain_dir: Path,
        session_storage: SessionStorage,
        modes: dict[str, Режим],
        todoist_token: str,
        model: str,
        inbox: Inbox,
        effort: str = "medium",
        calendar: dict | None = None,
        extra_dirs: list[Path] | None = None,
        dashboard: dict | None = None,
        undo=None,
        cost: ContextCost | None = None,
        recall=None,
    ) -> None:
        self.brain_dir = brain_dir
        self.sessions = session_storage
        # Состав режимов приезжает из плагина и за время жизни процесса
        # не меняется: том смонтирован только на чтение, правка доезжает
        # выкаткой и рестартом.
        self.modes = modes
        # Конституция склеивается из частей под режим — и кэшируется: файлы
        # на томе не меняются, а склейка происходит перед каждым ответом.
        self._prompt_cache: dict[tuple[str, ...], str] = {}
        # Счёт в токенах: факт от самой модели, а не пересчёт байтов.
        self.cost = cost
        # Что стоил последний ход и был ли он началом сессии. Сторож паспорта
        # смотрит сюда: сверять рюкзак с потолком имеет смысл ровно в первый
        # ход — дальше контекст растёт от самого разговора, и померишь болтовню.
        self.последний_контекст: int | None = None
        self.последний_первый = False
        # Идёт ли обряд — не поле, а взгляд в состояние на диске: он обязан
        # пережить рестарт вместе с закладкой (см. sessions.обряд). Ставит
        # и снимает код бота, когда человек сказал «проведём обзор» или нажал
        # кнопку. Пусто — живём по настройке из мозга.
        # Модель — умолчание на случай, если в мозге настройки ещё нет.
        # Настоящая берётся перед каждым ответом (см. _current_model): движок
        # живёт ровно один ответ, поэтому «переключись на Opus» работает
        # со следующей реплики, без перезапуска и без потери разговора.
        self.model = model
        self.effort = effort
        # Папки за пределами мозга, куда движку тоже нужен доступ. Сейчас это
        # присланные картинки: в мозге им не место (он репозиторий), а Read по
        # умолчанию видит только cwd — без этого списка он до них не дотянется.
        self.extra_dirs = [str(path) for path in (extra_dirs or [])]
        self.todoist_token = todoist_token
        self.calendar_config = calendar
        # Тумблеры кнопок: какие инструменты вообще объявлять движку. Живут
        # в мозге рядом с моделью, поэтому здесь только первое чтение —
        # дальше набор пересобирается, когда человек его поменял.
        self._switches: dict[str, dict[str, bool]] = self._read_switches()
        self.todoist_server = build_todoist_server(todoist_token, self._switches["todoist"])
        # Заявки: «хочу, чтобы ты умел X» — коуч записывает, а не делает.
        # Ложатся на полку входящих, туда же, куда ночные находки. Полка
        # передаётся снаружи и умолчания не имеет намеренно: она живёт в базе
        # рядом с архивом, а угаданный путь завёл бы её внутри мозга — то есть
        # двоичный файл внутри git-репозитория, который синхронизируется.
        self.inbox = inbox
        self.wishes_server = build_wishes_server(self.inbox, todoist_token)
        # Разбор предложений памяти. Только их: заявку, промах и поломку коуч
        # показать может, а сделать — нет, их чинит человек правкой кода.
        # Обзор, где восемнадцать заявок подряд получают ответ «это не ко мне»,
        # вытесняет то, ради чего обзор и собирался.
        self.inbox_server = build_inbox_server(self.inbox)
        # Дашборд — файл в телеграм. Отправка обязана случаться, значит инструмент.
        self.dashboard_server = build_dashboard_server(**dashboard) if dashboard else None
        # Откат того, что коуч сделал по комментарию в Todoist. Кнопка, а не
        # команда: «откати последнее» должно работать словами, как всё остальное.
        self.undo_server = undo
        # Догрузка прошлого. Кнопкой в телеграме таким не пользуются — это
        # инструмент модели, и «когда его звать» написано в его же описании:
        # описание модель видит в каждом ходу, а просьбу в конституции — через раз.
        self.recall_server = recall
        # Календарь подключаем только когда заданы креды — без них бот работает как прежде.
        self.calendar_server = (
            build_calendar_server(**calendar, switches=self._switches["calendar"])
            if calendar else None
        )

    async def tools_weight(self) -> int:
        """Сколько байт весит объявление кнопок — полезная нагрузка, не контекст.

        Величину держим ради тумблеров и ради видимости, но **ценой сессии она
        не является**. Зонды 01.08.2026: голый запрос 17 590 токенов, он же
        с 18 кнопками Todoist — 17 831. Восемнадцать кнопок стоят **241 токен**,
        около тринадцати за штуку, а не 750 байт: движок кладёт в контекст имена,
        а подробное описание подтягивает в момент вызова.

        Признак шире случая: байты полезной нагрузки и байты, доехавшие
        до контекста, — разные величины, и мерить надо вторые. Настоящая цена
        сессии считается в токенах и берётся из `usage` ответа (context_cost.py).
        """
        from mcp.types import ListToolsRequest

        total = 0
        for server in (self.todoist_server, self.calendar_server, self.wishes_server,
                       self.dashboard_server, self.undo_server, self.recall_server,
                       self.inbox_server):
            if not server:
                continue
            try:
                handler = server["instance"].request_handlers[ListToolsRequest]
                listed = (await handler(ListToolsRequest(method="tools/list"))).root.tools
            except Exception:  # версия SDK сменила внутренности — не повод падать
                log.warning("не удалось померить кнопки сервера %s", server.get("name"))
                continue
            payload = [
                {"name": t.name, "description": t.description, "inputSchema": t.inputSchema}
                for t in listed
            ]
            total += len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        return total

    def _settings(self) -> dict | None:
        """Настройки из мозга. Сломаны или недоступны — None, живём на прежних."""
        try:
            values, problems = read_settings(self.brain_dir)
        except Exception:  # мозг недоступен — не повод молчать
            return None
        if problems:
            log.error("настройки в мозге сломаны, оставляю прежние: %s", "; ".join(problems))
            return None
        return values

    def _read_switches(self) -> dict[str, dict[str, bool]]:
        values = self._settings() or {}
        return {
            "todoist": dict(values.get("кнопки_todoist") or {}),
            "calendar": dict(values.get("кнопки_календаря") or {}),
        }

    # --- режим ---

    def режим(self, values: dict | None = None) -> Режим:
        """В каком режиме собираем рюкзак прямо сейчас.

        Два источника, и это не два дома, а два разных факта с разным сроком
        жизни. **Настройка** в мозге — «обычно я работаю в таком режиме», живёт
        месяцами и меняется фразой. **Обряд** — «прямо сейчас идёт стратсессия»,
        живёт час и лежит в состоянии бота рядом с закладкой. Идёт обряд —
        он и решает; кончился — возвращаемся к настройке.

        Настройки сломаны или режима там нет — берём рабочий: он и есть
        умолчание обычного дня, а молча уехать в дорогой режим из-за опечатки
        нельзя.
        """
        if self.обряд and self.обряд in self.modes:
            return self.modes[self.обряд]
        if values is None:
            values = self._settings()
        имя = str((values or {}).get("режим") or режимы_модуль.ПО_УМОЛЧАНИЮ)
        return self.modes.get(имя) or self.modes[режимы_модуль.ПО_УМОЛЧАНИЮ]

    def _конституция(self, mode: Режим) -> str:
        """Роль коуча под этот режим плюс личная надстройка человека.

        Общая часть кэшируется — файлы плагина смонтированы только на чтение
        и за жизнь процесса не меняются. Надстройка НЕ кэшируется: она в мозге,
        её правят прямо в разговоре, и работать это обязано со следующей
        реплики — как ритмы и настройки.

        **Дата не кэшируется тем более.** Процесс живёт неделями, и вчерашнее
        число, застрявшее в кэше, было бы хуже отсутствия даты: коуч уверенно
        называл бы неверный день. Она приклеивается последней — там, где модель
        читает внимательнее всего.
        """
        части = mode.части
        if части not in self._prompt_cache:
            self._prompt_cache[части] = режимы_модуль.конституция(части)
        общая = self._prompt_cache[части]
        # Фоновому прогону без конституции надстройка тоже ни к чему: у него
        # нет роли, к которой её надстраивать. Дата ему тоже не нужна — свои
        # числа он считает кодом.
        if not общая:
            return общая
        своя = режимы_модуль.личная_надстройка(self.brain_dir)
        куски = [общая, своя, сегодня_строкой()] if своя else [общая, сегодня_строкой()]
        return "\n\n".join(куски)

    @property
    def обряд(self) -> str | None:
        """С какой головой идёт стратсессия. Пусто — стратсессии нет."""
        данные = self.sessions.обряд()
        return данные["режим"] if данные else None

    @property
    def обряд_ключ(self) -> str | None:
        """Какая именно стратсессия идёт: «недельный», «годовой»…"""
        данные = self.sessions.обряд()
        return данные.get("ключ") if данные else None

    def начать_обряд(self, режим: str, ключ: str) -> None:
        self.sessions.начать_обряд(режим, ключ)

    def кончить_обряд(self) -> None:
        self.sessions.кончить_обряд()

    def файлы_стратегии(self, mode: Режим) -> str:
        """Что режим кладёт в рюкзак кодом, кроме выжимок."""
        return режимы_модуль.файлы_стратегии(self.brain_dir, mode.файлы)

    def _refresh_tools(self, values: dict | None) -> None:
        """Пересобрать наборы кнопок, если человек поменял тумблеры.

        Сборка не ходит в сеть — это создание объектов по манифесту, — поэтому
        проверять можно перед каждым ответом. Без этого «выключи статистику»
        работало бы только после перезапуска, а обещано, что работает как фраза.
        """
        fresh = {
            "todoist": dict((values or {}).get("кнопки_todoist") or {}),
            "calendar": dict((values or {}).get("кнопки_календаря") or {}),
        }
        if fresh == self._switches:
            return
        log.info("тумблеры кнопок изменились, пересобираю наборы: %s", fresh)
        self._switches = fresh
        self.todoist_server = build_todoist_server(self.todoist_token, fresh["todoist"])
        if self.calendar_config:
            self.calendar_server = build_calendar_server(
                **self.calendar_config, switches=fresh["calendar"])

    def _options(self, resume: str | None, mode: Режим, values: dict | None) -> ClaudeAgentOptions:
        self._refresh_tools(values)
        mcp_servers = {"todoist": self.todoist_server, "wishes": self.wishes_server,
                       "inbox": self.inbox_server}
        allowed = ["mcp__todoist", "mcp__wishes", "mcp__inbox"]
        if mode.встроенные_инструменты:
            allowed = MEMORY_TOOLS + allowed
        if self.calendar_server is not None:
            mcp_servers["calendar"] = self.calendar_server
            allowed = allowed + ["mcp__calendar"]
        if self.dashboard_server is not None:
            mcp_servers["dashboard"] = self.dashboard_server
            allowed = allowed + ["mcp__dashboard"]
        if self.undo_server is not None:
            mcp_servers["undo"] = self.undo_server
            allowed = allowed + ["mcp__undo"]
        if self.recall_server is not None:
            mcp_servers["recall"] = self.recall_server
            allowed = allowed + ["mcp__recall"]
        return ClaudeAgentOptions(
            system_prompt=self._конституция(mode),
            cwd=str(self.brain_dir),
            model=str(values["модель_разговора"]) if values else self.model,
            effort=self.effort,
            resume=resume,
            permission_mode="bypassPermissions",
            mcp_servers=mcp_servers,
            allowed_tools=allowed,
            add_dirs=self.extra_dirs,
            disallowed_tools=FORBIDDEN_TOOLS,
            # Пустой список — это НЕ «умолчание»: он отключает встроенные
            # инструменты (Read, Write, Bash…), а их описания стоят 3 781 токен
            # в каждом запросе (замер 01.08.2026). None означает «как обычно».
            tools=None if mode.встроенные_инструменты else [],
            # Правила памяти и навыки плагина — 4 118 токенов. Разговору они
            # нужны (по ним пишется память), фоновому прогону — нет.
            setting_sources=["project"] if mode.правила_памяти else [],
            plugins=PLUGINS,
        )

    def сменился_режим(self) -> tuple[bool, Режим]:
        """Отличается ли нынешний режим от того, в котором собран разговор.

        Смена режима посреди разговора памяти не касается: выжимки и файлы
        стратегии подкладываются только в ПЕРВЫЙ запрос сессии. Поэтому
        «переключись в полный» без нового разговора не даёт ничего, кроме
        другой конституции поверх старого рюкзака. Значит режим меняется
        вместе с разговором — код закрывает текущий и открывает новый.
        """
        mode = self.режим()
        было = self.sessions.mode()
        return (bool(self.sessions.load()) and было is not None and было != mode.имя), mode

    async def ask(self, text: str, memory: str = "", agenda: str = "",
                  strategy: str = "", channel: str = "telegram") -> str:
        """Задать вопрос, продолжая прошлый разговор.

        Разговор начинается заново каждую ночь, поэтому в первый запрос новой
        сессии подкладываем выжимки прошлых дней — иначе коуч проснётся с
        чистой головой и заставит пользователя пересказывать вчерашнее. Тем же
        заходом едут файлы стратегии: их состав задаёт режим.

        Сводка дел подкладывается к КАЖДОЙ реплике, а не только к первой: дела
        меняются в течение дня, в том числе руками самого коуча. Это пара сотен
        токенов — цена того, чтобы он никогда не рассуждал о вчерашней картине.
        """
        values = self._settings()
        mode = self.режим(values)
        resume = self.sessions.load()
        if resume and self.sessions.mode() not in (None, mode.имя):
            log.info("режим сменился на «%s» — начинаю новый разговор", mode.имя)
            self.sessions.clear()
            resume = None
        prompt = self._wrap(text, memory if not resume else "",
                            agenda, strategy if not resume else "")
        self.sessions.save_mode(mode.имя)
        try:
            return await self._run(prompt, resume, mode=mode, values=values, channel=channel)
        except Exception:
            if resume is None:
                raise
            # Сессия могла протухнуть (например, память пересоздана) — начинаем свежую,
            # чтобы бот не онемел из-за одной битой ссылки.
            log.warning("не удалось продолжить сессию %s, начинаю новую", resume, exc_info=True)
            self.sessions.clear()
            self.sessions.save_mode(mode.имя)
            return await self._run(self._wrap(text, memory, agenda, strategy), None,
                                   mode=mode, values=values, channel=channel)

    @staticmethod
    def _wrap(text: str, memory: str, agenda: str, strategy: str = "") -> str:
        blocks = []
        if strategy.strip():
            blocks.append(
                "<стратегия>\n"
                "Куда пользователь идёт и почему. Это подложил код, а не ты сходил и прочитал:\n"
                "объявленное обязательным грузится кодом, иначе оно грузится через раз.\n"
                "Держи в голове молча — не пересказывай и не отчитывайся, что прочитал.\n\n"
                f"{strategy}\n"
                "</стратегия>"
            )
        if memory.strip():
            blocks.append(
                "<память_прошлых_дней>\n"
                "Это твои же выжимки прошлых разговоров с пользователем — самые свежие внизу.\n"
                "Опирайся на них молча: не пересказывай их и не ссылайся на них вслух.\n\n"
                f"{memory}\n"
                "</память_прошлых_дней>"
            )
        if agenda.strip():
            blocks.append(
                "<дела_сейчас>\n"
                "Свежий агрегат из Todoist, собран кодом только что. Todoist — дом дел,\n"
                "это его отражение: правится источник, а не отражение.\n\n"
                f"{agenda}\n"
                "</дела_сейчас>"
            )
        blocks.append(text)
        return "\n\n".join(blocks)

    async def _run(self, text: str, resume: str | None, *, mode: Режим,
                   values: dict | None, channel: str = "telegram",
                   second_try: bool = False) -> str:
        parts: list[str] = []
        heard_model = False
        notified = False
        subtype = None
        usage: object = None
        рюкзак = 0
        model = str(values["модель_разговора"]) if values else self.model
        async with ClaudeSDKClient(options=self._options(resume, mode, values)) as client:
            await client.query(text)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    # ПЕРВОЕ сообщение модели и есть рюкзак: столько было
                    # у неё в голове до первого слова. Дальше ход идёт цепочкой
                    # (сходил в Todoist, заглянул в календарь, ответил), и каждый
                    # шаг заезжает заново — итоговый `usage` считает их все.
                    if not heard_model:
                        рюкзак = контекст(разобрать(message.usage))
                    heard_model = True
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            parts.append(block.text)
                elif isinstance(message, TaskNotificationMessage):
                    # Движок докладывает о судьбе фонового агента. Нам он не нужен,
                    # но факт важен: такой доклад закрывает ход, не дав модели слова.
                    notified = True
                    log.warning("движок прислал уведомление о фоновой задаче: %r", message)
                elif isinstance(message, ResultMessage):
                    if message.session_id:
                        self.sessions.save(message.session_id)
                        self.sessions.save_mode(mode.имя)
                    subtype = message.subtype
                    usage = message.usage
                    if message.is_error:
                        log.error("движок вернул ошибку: %s", message.result)

        await self._записать_цену(channel, mode, model, usage, рюкзак,
                                  первый=resume is None)
        self._сверить_с_потолком(рюкзак, values)

        answer = "\n".join(p.strip() for p in parts if p.strip())
        if answer:
            return answer

        # Пустой ход. Раньше здесь молча подставлялась заглушка, и в docker logs не
        # оставалось ни следа: 30.07.2026 бот дважды ответил «коуч промолчал», а
        # найти причину удалось только по транскриптам сессии. Теперь — громко.
        log.error(
            "движок вернул пустой ответ: модель %s, уведомление о фоновой задаче %s, "
            "subtype=%s, повторная попытка=%s",
            "говорила" if heard_model else "молчала",
            "было" if notified else "не приходило",
            subtype,
            second_try,
        )
        if not heard_model and not second_try:
            # Модель не сказала ни слова — значит ход съело что-то служебное, а не она
            # сама. Повторяем ровно один раз и по свежей закладке: уведомление уже
            # доставлено и второй раз не придёт. Если модель говорила, но текста не
            # дала, — не повторяем, иначе рискуем сделать её работу дважды.
            log.warning("повторяю запрос один раз — ход съело служебное сообщение")
            return await self._run(text, self.sessions.load() or resume, mode=mode,
                                   values=values, channel=channel, second_try=True)
        return "…(коуч промолчал — похоже, что-то пошло не так)"

    def _сверить_с_потолком(self, рюкзак: int, values: dict | None) -> None:
        """Разговор раздулся выше потолка — оборвать ленту, не дожидаясь ночи.

        Страховка, а не основной механизм: ленту рвёт ночь, каждую ночь и
        безусловно. Но ночь — это код, который может не отработать, а счёт
        за раздутый контекст приходит человеку сразу. У пользователя из заявки
        gtd-coach#1 ночь не отрабатывала месяц, и рюкзак дорос до 645 тысяч
        токенов; сторож оборвал бы это на третий день.

        Мерим РЮКЗАК, а не весь ход: рюкзак — это то, что мы положили модели
        в голову до первого слова, и растёт он от накопленной ленты. Весь ход
        считает ещё и походы в Todoist с календарём, и по нему потолок кричал
        бы на любом длинном ответе.

        **Обряд не рвём.** Стратсессия — это один разговор по замыслу, и оборвать
        его посередине значит потерять нить ровно там, где она дороже всего.
        Она и так кончится сама, а следом её оборвёт ночь.
        """
        потолок = int((values or {}).get("потолок_разговора") or 0)
        if потолок <= 0 or рюкзак <= потолок:
            return
        if self.sessions.обряд():
            log.warning("рюкзак %d выше потолка %d, но идёт обряд — ленту не рву",
                        рюкзак, потолок)
            return
        log.warning(
            "рюкзак %d токенов выше потолка %d — обрываю ленту, следующая реплика "
            "пойдёт с чистого листа. Обычно это значит, что ночная работа "
            "не отрабатывает: смотри логи за ночь и модель_ночной_работы в мозге.",
            рюкзак, потолок,
        )
        self.sessions.clear()

    async def _записать_цену(self, channel: str, mode: Режим, model: str, usage: object,
                             рюкзак: int = 0, *, первый: bool = False) -> None:
        """Отметить в базе, во что обошёлся этот ход.

        Число берём у модели, а не считаем сами: `usage` — её собственный счёт.
        Счётчик не та вещь, ради которой коуч должен онеметь, поэтому любая
        поломка здесь остаётся в логе и разговор не трогает.
        """
        числа = разобрать(usage)
        if not числа:
            self.последний_контекст = None
            self.последний_первый = False
            return
        # Потолок стоит на РЮКЗАКЕ, а не на всём ходе: он про то, что мы
        # положили, а не про то, насколько усердной оказалась модель.
        self.последний_контекст = рюкзак or None
        self.последний_первый = первый
        log.info(
            "контекст: режим %s, канал %s — рюкзак %d, весь ход %d токенов "
            "(свежих %d, кэш +%d/%d), выдано %d",
            mode.имя, channel, рюкзак, контекст(числа), числа["input"],
            числа["cache_create"], числа["cache_read"], числа["output"],
        )
        if self.cost is None:
            return
        try:
            await self.cost.записать(channel, mode.имя, model, usage, рюкзак, первый)
        except Exception:
            log.exception("не смог записать цену контекста")
