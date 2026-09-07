"""Телеграм-канал коуча: приём голоса и текста, два пинга в день.

Здесь только доставка. Всё, что думает, живёт в engine.py.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from telegram import BotCommand, ReplyKeyboardRemove, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import agenda
from .archive import Archive
from .backstage import raise_task, погасить_карточки
from .brain import Brain
from .comments import Канал, build_undo_server
from .context_cost import ContextCost
from . import detectors
from .digest import Digester
from .engine import CoachEngine
from .inbox import Inbox
from . import glossary
from .memory_watch import MemoryWatch
from . import modes as режимы_модуль
from . import profile
from .promise import PromiseWatch
from .prompts import followups, load as load_prompt, missing as missing_prompts
from .recall import build_recall_server
from . import rituals
from .retry import retry_network
from . import rhythms
from .rhythms import describe, path_in, read, выключен
from . import settings as coach_settings
from .sessions import SessionStorage
from .startup_budget import check as check_budget
from .tidy_history import HistoryTidier
from .todoist_snapshot import TodoistSnapshot
from . import versions
from .voice import (
    DEFAULT_STT_MODEL,
    DEFAULT_STT_URL,
    OPENAI_STT_MODEL,
    OPENAI_STT_URL,
    Service,
    VoiceRecognizer,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("coach")

TELEGRAM_LIMIT = 4000
# Конец предложения — точка, вопрос, восклицание или многоточие, за которыми
# идёт пробел. Сокращения («т.е.», «и т.д.») цепляются сюда же, и это не беда:
# кусок просто окажется чуть короче, а рвётся он всё равно между словами.
КОНЕЦ_ПРЕДЛОЖЕНИЯ = re.compile(r"(?<=[.!?…])\s+")
MOSCOW = ZoneInfo("Europe/Moscow")
# Отметка живости для docker healthcheck. В /tmp, а не в /state: это одноразовое
# состояние процесса, ему нечего делать в бэкапе вместе с закладкой разговора.
HEARTBEAT_FILE = Path(os.environ.get("HEARTBEAT_FILE", "/tmp/coach-heartbeat"))
# Куда кладём присланные картинки, чтобы коуч открыл их инструментом Read.
# В /tmp, а не в /brain и не в /state: файл нужен ровно на один ответ. В мозге
# он попал бы в git, в состоянии — в бэкап. После ответа файл удаляем.
PHOTOS_DIR = Path(os.environ.get("PHOTOS_DIR", "/tmp/coach-photos"))
# Что Read умеет показать как изображение. Присланное иным форматом (heic и
# прочая экзотика) не притворяемся, что видим, — честно говорим владельцу.
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Меню команд — то, что открывается кнопкой слева от поля ввода. Раньше на его
# месте была нижняя клавиатура из шести кнопок; она занимала треть экрана
# в каждом разговоре, а нажимали её ноль раз. Меню не занимает ничего.
#
# Команда осталась печатающей за человека рукой, а не новой дверью. Обработчик
# не делает ничего своего: он отправляет в разговор ровно ту фразу, которую
# можно сказать голосом. Снеси меню целиком — всё продолжит работать словами.
# Скрытого кода за кнопкой нет и заводить его нельзя: это была бы вторая дверь
# со своей логикой, и разговор перестал бы быть единственным входом.
#
# Латиница в именах — требование Телеграма, кириллические он отвергает. Человек
# видит не имя, а описание: в меню рядом с командой стоит русская строка.
#
# Порядок в списке = порядок на экране. Сверху то, что не про стратсессии,
# ниже четыре стратсессии от мелкой к крупной, в конце выход и заявка.
#
# `/start` здесь намеренно нет. Её шлёт сам Телеграм, когда человек первый раз
# жмёт «Запустить», — обработчик ей нужен, а строка в витрине после первого дня
# не нужна никому.
КОМАНДЫ: tuple[tuple[str, str, str | None], ...] = (
    # У «нового листа» свой обработчик: он не разговаривает с коучем, а рвёт
    # закладку. Фразы нет, поэтому третье поле пустое.
    ("new", "Начать разговор с чистого листа", None),
    ("strategy", "Посмотреть свою стратегию",
     "Пришли дашборд стратегии"),
    # Свои обработчики: печатать коучу тут нечего. Ответ — механический список,
    # и собрать его дешевле кодом, чем гонять полный рюкзак ради четырёх строк.
    ("session", "Провести стратсессию", None),
    ("end", "Закончить стратсессию",
     "Закончить стратсессию"),
    ("idea", "Записать заявку: хочу, чтобы ты умел…",
     "Хочу подать заявку на новую способность коуча"),
    ("model", "Сменить модель, которой думает коуч", None),
    # Тоже свой обработчик и тоже без коуча: список механический. Стоит рядом
    # с `/model` — обе про устройство коуча, а не про работу с делами.
    #
    # Сначала её хотели спрятать (витрину ужимали намеренно), но перевесил
    # вопрос «а где её тогда искать»: ссылка в задаче Todoist находит команду
    # только когда обновление уже вышло, а посмотреть хочется и сразу после
    # прогона обновления — что реально встало.
    ("version", "Что стоит и есть ли обновления", None),
)

# Четыре стратсессии. В витрине их нет: они печатаются в ответ на `/session`
# и там кликабельны. Четыре строки подряд, отличающиеся одним словом, витрину
# только замусоривали — а выход из стратсессии (`/end`) в витрине остался,
# потому что искать его придётся изнутри, когда меню уже открыто не за этим.
#
# «Закончить» одна на все четыре: код не смотрит, какая шла.
# Третьим полем — ключ повода: подпись по-русски берётся оттуда, а не пишется
# здесь второй раз. Написанная здесь, она разъехалась бы с той, что коуч
# показывает в подписи под ответом.
СТРАТСЕССИИ: tuple[tuple[str, str, str], ...] = (
    ("week", "Проведём недельную стратсессию", "недельный"),
    ("month", "Проведём месячную стратсессию", "месячный"),
    ("quarter", "Проведём квартальную стратсессию", "квартальный"),
    ("year", "Проведём годовую стратсессию", "годовой"),
)

# Чем коуч думает. В витрине этих команд НЕТ намеренно: их печатает сам бот
# в ответ на `/model`, и там они кликабельны. Витрина из двенадцати пунктов
# перестаёт читаться, а выбирают модель раз в месяц.
#
# Зарегистрировать их всё равно обязательно. Телеграм делает кликабельным
# любое слово со слешем, хоть `/абракадабра`, — но текстовый обработчик стоит
# на «всё, кроме команд», и незарегистрированная команда уходит в никуда
# молча: ни ответа, ни ошибки. Тот же класс, что кнопка без повода.
МОДЕЛИ: tuple[tuple[str, str, str], ...] = (
    ("opus", "Opus", "claude-opus-5"),
    ("sonnet", "Sonnet", "claude-sonnet-5"),
    ("fable", "Fable", "claude-fable-5"),
    ("haiku", "Haiku", "claude-haiku-4-5-20251001"),
)

# Сколько знаков описания реально видно в меню на телефоне. Не лимит API —
# тот 256, и он тут ни при чём: обрезает экран. Под описание две строки,
# дальше многоточие. Померено по живому меню на айфоне 02.08.2026: «Хочу,
# чтобы коуч умел… — записать заявку» (40) видно целиком, «Стратегия: миссия,
# горизонты, цели, колесо баланса» (49) обрывается на сорок четвёртом.
#
# Шрифт пропорциональный, поэтому граница плавает на пару знаков в обе стороны:
# узкие буквы влезают, широкие нет. Сорок — с запасом на широкие.
#
# Число живёт здесь прибором, а не в чьей-то памяти: обрезанное описание
# на маке не видно вовсе, и заметить его можно только с телефона.
ВИДНО_ЗНАКОВ = 40

# Команды, на которые отвечает сам бот, а не коуч: механический список дешевле
# собрать кодом, чем гонять ради него полный рюкзак. Имя метода стоит рядом
# с именем команды намеренно — тогда «команда в витрине есть, а отвечать некому»
# ловится тестом, а не молчанием в телеграме. Тот же класс, что кнопка без повода.
СВОИ_ОБРАБОТЧИКИ: dict[str, str] = {
    "new": "on_reset",
    # `/reset` в витрине нет: она чинит застрявший обряд, а не ведёт разговор.
    "reset": "on_reset",
    "model": "on_model",
    "session": "on_session",
    "version": "on_version",
}

# Что печатает каждая команда. Собирается из тех же списков, а не рядом с ними:
# второй список разъехался бы с первым на первой же правке.
ФРАЗЫ_КОМАНД: dict[str, str] = (
    {имя: фраза for имя, _, фраза in КОМАНДЫ if фраза}
    | {имя: фраза for имя, фраза, _ in СТРАТСЕССИИ}
)

# Значки в списках, которые печатает бот. Палец — «вот это сейчас», квадратик —
# всё остальное. Один набор на оба списка: разные значки в двух похожих
# сообщениях читались бы как разница по смыслу, которой нет.
СЕЙЧАС, ПРОЧЕЕ = "👉🏻", "▪️"

# Сколько реплик сегодняшнего разговора переезжает в стратсессию мостиком.
# Двадцать — это примерно утро целиком; больше значило бы тащить в обряд
# вчерашние хвосты, за которыми есть выжимки.
МОСТИК_РЕПЛИК = 20

def env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default or "")
    if required and not value:
        raise SystemExit(f"Не задана переменная окружения {name}")
    return value


def _отрезать(текст: str, limit: int) -> tuple[str, str]:
    """Отделить от начала кусок не длиннее limit по самой крупной границе.

    Границы пробуем сверху вниз: конец абзаца, конец предложения, пробел между
    словами. Первая же найденная выигрывает — так кусок рвётся по самому
    крупному шву, который поместился.
    """
    окно = текст[: limit + 1]
    for граница in (_конец_абзаца, _конец_предложения, _конец_слова):
        место = граница(окно, limit)
        if место:
            return текст[:место].rstrip(), текст[место:].lstrip()
    # Ни одного шва на весь кусок — сплошная простыня без пробелов (ссылка,
    # набор в одно слово). Рубим по символам, но не посреди html-сущности:
    # половина «&quot;» приедет в телеграм мусором.
    место = _без_разрыва_сущности(текст, limit)
    return текст[:место], текст[место:]


def _конец_абзаца(окно: str, limit: int) -> int:
    место = окно.rfind("\n", 0, limit + 1)
    return место + 1 if место > 0 else 0


def _конец_предложения(окно: str, limit: int) -> int:
    место = 0
    for совпадение in КОНЕЦ_ПРЕДЛОЖЕНИЯ.finditer(окно):
        if совпадение.end() <= limit:
            место = совпадение.end()
    return место


def _конец_слова(окно: str, limit: int) -> int:
    место = окно.rfind(" ", 0, limit + 1)
    return место + 1 if место > 0 else 0


def _без_разрыва_сущности(текст: str, limit: int) -> int:
    """Отступить назад, если разрез пришёлся внутрь «&…;»."""
    начало = текст.rfind("&", 0, limit)
    if начало > 0 and ";" not in текст[начало:limit]:
        return начало
    return limit


# Сколько коуч может молчать ушами, прежде чем считать их отказавшими.
# Длинный опрос Telegram завершается каждые 10-15 секунд, так что здоровый бот
# обновляет отметку постоянно; три минуты — запас более чем десятикратный.
ГЛУХОТА_ПРЕДЕЛ_СЕК = int(env("HEARING_MAX_SILENCE_SECONDS", "180"))


class СлышащийТранспорт(HTTPXRequest):
    """Транспорт приёма обновлений, который отмечает время последнего удачного ответа.

    Зачем. 03.08.2026 в 20:57 балансир выхода переключил узел, соединение с Telegram
    оборвалось на полуслове (`RemoteProtocolError`), и приём сообщений умер НАСОВСЕМ —
    одна ошибка, ни одной попытки подняться. Процесс при этом остался жив и продолжал
    говорить: слал утреннюю сводку и напоминания. Глухота длилась 10,6 часа и вскрылась
    только когда владелец написал и не получил ответа.

    Отметку ставим ТОЛЬКО на транспорте приёма — у библиотеки он отдельный от того,
    которым бот отправляет. Ставить её на любом успешном запросе нельзя: исходящие идут
    каждую минуту и маскировали бы отказ ушей ровно так же, как его маскировал зелёный
    healthcheck. Признак живости обязан трогать приёмную половину, иначе он врёт.

    Читает отметку задача «сторож слуха» (CoachBot.watch_hearing).
    """

    последний_успех: float = 0.0

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Отсчёт с рождения транспорта: иначе первая же проверка увидит нулевую
        # отметку, сочтёт её протухшей и убьёт процесс, ещё не начав работу.
        СлышащийТранспорт.последний_успех = monotonic()

    async def do_request(self, *args, **kwargs):
        результат = await super().do_request(*args, **kwargs)
        СлышащийТранспорт.последний_успех = monotonic()
        return результат


class CoachBot:
    def __init__(self) -> None:
        self.owner_id = int(env("OWNER_TELEGRAM_ID", required=True))
        self.brain_dir = Path(env("BRAIN_DIR", "/brain"))
        state_dir = Path(env("STATE_DIR", "/state"))

        self.brain = Brain(self.brain_dir)
        self.todoist_token = env("TODOIST_API_TOKEN", required=True)
        # Заполняется в run(): через это приложение уходят файлы и жалобы канала.
        self.app: Application | None = None
        self.archive = Archive(Path(env("ARCHIVE_DB", "/archive/coach.db")))
        # Полка входящих: заявки, предложения памяти, промахи, поломки. Один
        # экземпляр на всех потребителей — движок, ночная проверка, ночные
        # находки, — потому что таблица одна и правила у неё одни.
        self.inbox = Inbox(Path(env("ARCHIVE_DB", "/archive/coach.db")))
        # Состав режимов приезжает из плагина. Сломан или отсутствует — падаем
        # здесь, одной понятной строкой: без состава неизвестно, что класть
        # коучу в голову, а зашитый в питон запасной был бы вторым домом.
        self.modes = режимы_модуль.load()
        # Цена контекста в токенах — факт от модели, а не пересчёт байтов.
        self.cost = ContextCost(Path(env("ARCHIVE_DB", "/archive/coach.db")))
        # Вторая дверь к коучу — комментарии в карточках задач. Заводится до
        # движка: движку нужна её кнопка отката.
        self.comments = Канал(
            token=self.todoist_token,
            db_path=Path(env("ARCHIVE_DB", "/archive/coach.db")),
            archive=self.archive,
            model=env("COACH_MODEL", "claude-fable-5"),
            сказать=self._сказать_владельцу,
            mode=self.modes[режимы_модуль.ФОНОВЫЙ],
            cost=self.cost,
        )
        self.engine = CoachEngine(
            brain_dir=self.brain_dir,
            session_storage=SessionStorage(state_dir / "session_id"),
            modes=self.modes,
            cost=self.cost,
            recall=build_recall_server(Path(env("ARCHIVE_DB", "/archive/coach.db"))),
            todoist_token=self.todoist_token,
            model=env("COACH_MODEL", "claude-fable-5"),
            effort=env("COACH_EFFORT", "medium"),
            calendar=self._calendar_config(),
            extra_dirs=[PHOTOS_DIR],
            dashboard={
                "brain_dir": self.brain_dir,
                "db_path": Path(env("ARCHIVE_DB", "/archive/coach.db")),
                "todoist_token": self.todoist_token,
                "send": self._send_document,
            },
            undo=build_undo_server(self.comments),
            inbox=self.inbox,
        )
        PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
        self.voice = self._voice_recognizer()
        # Ритмы живут в мозге, а не в .env: их меняет пользователь фразой в телеграме.
        self.rhythms, problems = read(self.brain_dir)
        if problems:
            log.error("ритмы в мозге сломаны, беру умолчания: %s", "; ".join(problems))
        # Настройки (модель и тумблеры кнопок) живут в мозге рядом с ритмами.
        # Файл читает код — значит код и заводит его, если человек ещё не завёл.
        coach_settings.ensure(self.brain_dir, datetime.now(MOSCOW).date().isoformat())
        values, beefs = coach_settings.read(self.brain_dir)
        if beefs:
            log.error("настройки в мозге сломаны, беру умолчания: %s", "; ".join(beefs))
        night_model = str(values["модель_ночной_работы"])
        # Вся фоновая работа идёт в фоновом режиме, и ставит его код: человек
        # ночную выжимку не заказывает и её головой не пользуется.
        фон = self.modes[режимы_модуль.ФОНОВЫЙ]
        self.digester = Digester(self.brain_dir, self.archive, model=night_model,
                                 mode=фон, cost=self.cost)
        self.tidier = HistoryTidier(self.brain_dir, model=night_model,
                                    mode=фон, cost=self.cost)
        self.memory_watch = MemoryWatch(self.brain_dir, self.inbox, model=night_model,
                                        mode=фон, cost=self.cost)
        # Несгораемая история дел: Todoist на Free помнит ~неделю, снимок помнит всё.
        self.snapshot = TodoistSnapshot(
            Path(env("ARCHIVE_DB", "/archive/coach.db")), self.todoist_token
        )
        # Обещание дня разбирается по тому же архиву и ложится в ту же базу:
        # дом цифр — база, дом смысла — журнал.
        self.promises = PromiseWatch(self.snapshot.db_path, model=night_model)
        self.broken_rhythms = "; ".join(problems)
        # Один разговор — значит одна очередь. Иначе две сессии подерутся за resume.
        self.lock = asyncio.Lock()

    @staticmethod
    def _voice_recognizer() -> VoiceRecognizer:
        """Основной сервис расшифровки — Groq (дешёвый), запасной — OpenAI.

        Если STT_API_KEY не задан (сервер со старым .env), основным становится
        OpenAI и запасного нет — бот всё равно поднимется и будет слышать голос.
        """
        proxy = env("HTTPS_PROXY") or None
        groq_key = env("STT_API_KEY")
        openai_key = env("OPENAI_API_KEY")
        if not (groq_key or openai_key):
            raise RuntimeError("нет ни STT_API_KEY, ни OPENAI_API_KEY — расшифровывать нечем")

        openai = (
            Service(
                name="OpenAI",
                api_key=openai_key,
                url=env("FALLBACK_STT_API_URL", OPENAI_STT_URL),
                model=env("FALLBACK_STT_MODEL", OPENAI_STT_MODEL),
            )
            if openai_key
            else None
        )
        if not groq_key:
            log.warning("STT_API_KEY не задан: расшифровка только через OpenAI, без запасного")
            return VoiceRecognizer(primary=openai, proxy=proxy)

        primary = Service(
            name="Groq",
            api_key=groq_key,
            url=env("STT_API_URL", DEFAULT_STT_URL),
            model=env("STT_MODEL", DEFAULT_STT_MODEL),
        )
        if openai is None:
            log.warning("OPENAI_API_KEY не задан: у расшифровки нет запасного сервиса")
        return VoiceRecognizer(primary=primary, fallback=openai, proxy=proxy)

    @staticmethod
    def _calendar_config() -> dict | None:
        """Google-календарь подключаем, только если заданы все три секрета.

        Часовой пояс — переменная COACH_TZ: переехал человек в другую страну,
        поменялась одна строка, код не трогаем. Выход к Google при нужде идёт
        через прокси из HTTPS_PROXY — там, где Google блокируется."""
        client_id = env("GOOGLE_CLIENT_ID")
        client_secret = env("GOOGLE_CLIENT_SECRET")
        refresh_token = env("GOOGLE_REFRESH_TOKEN")
        if not (client_id and client_secret and refresh_token):
            log.warning("Google-календарь не подключён: не заданы GOOGLE_* переменные")
            return None
        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "calendar_id": env("GOOGLE_CALENDAR_ID", "primary"),
            "tz": env("COACH_TZ", "Europe/Moscow"),
            "proxy": env("HTTPS_PROXY") or None,
        }

    # --- вспомогательное ---

    def _mine(self, update: Update) -> bool:
        user = update.effective_user
        if user and user.id == self.owner_id:
            return True
        log.warning("чужой стучится: id=%s", user.id if user else "?")
        return False

    async def _think_and_reply(
        self,
        text: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        channel: str = "text",
        archived_as: str | None = None,
        из_команды: bool = False,
        сочинил_код: bool = False,
    ) -> None:
        """Отдать реплику движку и ответить владельцу.

        `archived_as` нужен там, где движку уходит служебная обёртка, а в журнал
        должно лечь человеческое: у картинки в промпте путь к файлу, читать его
        в ночной выжимке незачем.
        """
        async with self.lock:
            # Обряд включает и выключает КОД, а не память модели: «включи»
            # обязано случаться. Под замком, как и всё остальное: перекладывание
            # закладок посреди чужого ответа увело бы разговор не туда.
            мостик = await self._переключить_обряд(text, context, из_команды, сочинил_код)
            # Метку берём ПОСЛЕ переключения: реплика «проведём недельную»
            # уже принадлежит стратсессии, которую сама и открыла, а «закончить»
            # — уже обычному разговору. Иначе у каждой стратсессии в архиве
            # не хватало бы первой реплики и лишней была бы последняя.
            ритуал = self.engine.обряд_ключ
            await self.archive.add_message(
                "user", channel, archived_as or text, self.engine.sessions.load(), ритуал
            )
            typing = asyncio.create_task(self._keep_typing(chat_id, context))
            mode = self.engine.режим()
            try:
                await self.brain.pull()
                # Рюкзак собирается ПОД РЕЖИМ. Выжимки движок подставит сам, если
                # разговор начинается заново; сколько их — решает режим.
                mode = self.engine.режим()
                memory = await asyncio.to_thread(self.archive.recent_digests, mode.окно)
                # Стратегия и профиль едут тем же заходом. Раньше это была просьба
                # в конституции «читай в начале разговора» — по десяти последним
                # сессиям она сработала в одной.
                strategy = await asyncio.to_thread(self.engine.файлы_стратегии, mode)
                # Сводку дел — к каждой реплике: коуч обязан знать картину дня всегда,
                # а не когда вспомнит сходить в Todoist.
                summary = await agenda.summary(self.todoist_token)
                answer = await self.engine.ask(
                    мостик + text if мостик else text, memory, summary, strategy, channel,
                )
                await self._check_load(mode, channel, summary, text)
                await self.brain.push(text[:60].replace("\n", " "))
            except Exception as error:  # доставляем боль владельцу, а не в лог-файл
                log.exception("сорвалось на ответе")
                answer = f"Сломался: {type(error).__name__}: {error}"
            finally:
                typing.cancel()
            await self.archive.add_message(
                "coach", channel, answer, self.engine.sessions.load(), ритуал
            )

        # Коуч мог только что поменять ритмы по просьбе пользователя — заметить это надо
        # сразу, а не через час: «пиши мне три раза в день» должно работать как фраза.
        await self._reread_rhythms(context, loud=True)
        # И режим тоже: «переключись в полный» — такая же фраза, как «пиши три
        # раза в день». Разговор при этом закроется и откроется заново — это
        # делает сам движок перед следующей репликой.
        await self._заметить_режим(context, mode)

        # Экранируем и шлём разметкой. Курсив нужен ровно одной строке — нашей
        # подписи; текст коуча после экранирования выглядит в точности как
        # раньше, потому что ни один его символ не может стать тегом. Обратный
        # порядок (не экранировать, а надеяться) сломал бы ответ на первой же
        # угловой скобке, а угловые скобки коуч пишет — он ими размечает
        # служебные врезки.
        куски = self._split(html.escape(answer))
        for номер, chunk in enumerate(куски, 1):
            # Значок режима — в самом конце последнего куска. Один символ,
            # читать не мешает, всегда на виду. Заголовок «Режим: рабочий»
            # перед каждым ответом надоел бы на третий день. А вот во время
            # обряда пометка словом уместна: это не фон, это особое состояние,
            # и из него надо помнить, как выйти.
            хвост = (f"\n\n<i>{html.escape(self._пометка())}</i>"
                     if номер == len(куски) else "")
            await context.bot.send_message(
                chat_id=chat_id, text=chunk + хвост, parse_mode=ParseMode.HTML,
            )

    def _пометка(self) -> str:
        """Чем помечен ответ: значок режима, а во время обряда — ещё и словом.

        Внутри стратсессии значка мало. Она меняет всё — голову, что коуч
        видит, чем кончится разговор, — и человек обязан помнить, где он
        находится и как выйти. Значок такого не говорит, а строчка говорит.
        """
        значок = self.engine.режим().значок
        ключ = self.engine.обряд_ключ
        повод = rituals.ПОВОДЫ.get(ключ) if ключ else None
        if повод:
            return f"{значок} Идёт {повод.какая} стратсессия. Выйти — /end"
        # Обряд есть, а повод неизвестен: файл состояния правили руками или
        # повод переименовали между выкатками. Молчать нельзя — человек сидит
        # в дорогой голове и не знает об этом.
        return f"{значок} Идёт стратсессия. Выйти — /end" if self.engine.обряд else значок

    async def _переключить_обряд(self, text: str, context: ContextTypes.DEFAULT_TYPE,
                                 из_команды: bool = False,
                                 сочинил_код: bool = False) -> str:
        """Начать или закончить стратсессию. Возвращает мостик в новый заход.

        **Включает только команда** (`из_команды=True`), то есть нажатие в меню
        или слэш. Те же слова, сказанные в разговоре, обряд не включают —
        коуч лишь подсказывает команду.

        **Служебный текст (`сочинил_код=True`) не смотрим вовсе.** Чек-ин и
        дожим человек не произносил: их сочинил код, и внутри лежит свод завалов
        с НАЗВАНИЯМИ задач Todoist. Куплено 12.08.2026: карточка «Провести
        недельную стратсессию» без метки сферы попала в свод — и утренний
        чек-ин вместо главного дела дня прислал подсказку «жми /week».

        Разводить пришлось потому, что команда меню не имеет своей логики:
        `/week` печатает за человека фразу «Проведём недельную стратсессию»,
        и ниже по течению она неотличима от сказанной вслух. Флаг — единственное
        место, где эта разница ещё видна.

        Мостик — это сегодняшний разговор, переложенный в начало обряда.
        Технически положить текст в середину разговора можно всегда (так каждый
        день работает сводка дел); окно выжимок кладут один раз в начале
        не потому, что «иначе нельзя», а чтобы не гонять сорок килобайт
        с каждой репликой. Здесь этой возможностью и пользуемся, чтобы нить
        не рвалась: человек продолжает разговор, а не начинает знакомство.
        """
        if сочинил_код:
            return ""

        if rituals.конец_ли(text):
            return await self._закончить_обряд(context)

        повод = rituals.по_фразе(text)
        if повод is None or self.engine.обряд:
            return ""   # обряд уже идёт — второй поверх него не начинаем

        if not из_команды:
            # СЛОВАМИ ОБРЯД БОЛЬШЕ НЕ ВКЛЮЧАЕТСЯ (06.08.2026, заявка владельца
            # от 04.08). Подсказываем команду и уходим: включает человек нажатием.
            #
            # Косяк, который это купил: «надо поправить скилл недельного обзора»
            # включило сам недельный обзор. Список ловит слова, а не смысл,
            # и разговор ПРО инструмент неотличим для него от просьбы применить.
            await context.bot.send_message(
                chat_id=self.owner_id,
                text=(f"Похоже, речь про {повод.вин}. Сам включить не могу — "
                      f"жми {self._команда_обряда(повод.ключ)}.\n"
                      f"Если ты просто про него говорил, ничего не нажимай."),
            )
            log.info("речь о стратсессии «%s» — подсказал команду, не включал", повод.что)
            return ""

        # Откладываем текущий разговор на полку и открываем обряд своим заходом.
        отложили = self.engine.sessions.отложить()
        self.engine.начать_обряд(повод.режим, повод.ключ)
        режим = self.engine.режим()
        log.info("начата стратсессия «%s», режим %s, прежний разговор %s",
                 повод.что, режим.имя, "отложен" if отложили else "отсутствовал")

        await context.bot.send_message(
            chat_id=self.owner_id,
            text=(f"{режим.значок} Включил {повод.вин}. Голова другая: миссия, "
                  f"ценности, горизонты и профиль теперь передо мной.\n"
                  f"Скажи «закончить стратсессию» или жми /end — вернёмся "
                  f"к прежнему разговору"
                  f"{'' if отложили else ' (его пока нет — начнём с чистого листа)'}."),
        )
        return self._мостик(повод)

    @staticmethod
    def _команда_обряда(ключ: str) -> str:
        """Слэш-команда стратсессии по ключу повода. Имя берём из того же
        списка, что и витрина, — написанное здесь второй раз, оно разъедется."""
        for имя, _, к in СТРАТСЕССИИ:
            if к == ключ:
                return f"/{имя}"
        return "/session"

    def _мостик(self, повод) -> str:
        """Сегодняшний разговор, переложенный в начало обряда."""
        сегодня = datetime.now(MOSCOW).date().isoformat()
        строки = self.archive.messages_of_day(сегодня)[-МОСТИК_РЕПЛИК:]
        куски = [
            f"<обряд>\nНачинается {повод.что}. Веди его по навыку `{повод.навык}` "
            f"— открой его, не веди по памяти. Не пересказывай эту записку вслух, "
            f"просто начни разговор.\n</обряд>"
        ]
        if строки:
            беседа = "\n".join(
                f"{'он' if роль == 'user' else 'ты'}: {текст.strip()[:400]}"
                for роль, _, _, текст in строки
            )
            куски.append(
                "<до_обряда>\nО чём вы уже говорили сегодня — чтобы не начинать\n"
                "знакомство заново и не переспрашивать сказанное.\n\n"
                f"{беседа}\n</до_обряда>"
            )
        return "\n\n".join(куски) + "\n\n"

    async def _закончить_обряд(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Снять разговор с полки и вернуться туда, где были."""
        if not self.engine.обряд:
            return ""
        было = self.engine.обряд_ключ or self.engine.обряд
        self.engine.кончить_обряд()
        вернулись = self.engine.sessions.вернуть()
        log.info("стратсессия «%s» закончена, разговор %s", было,
                 f"продолжен {вернулись}" if вернулись else "начат заново")
        режим = self.engine.режим()
        await context.bot.send_message(
            chat_id=self.owner_id,
            text=(f"{режим.значок} Стратсессия закончена, вернулись к обычному "
                  f"разговору." + ("" if вернулись else " Прежнего разговора не было — "
                                   "начинаем с чистого листа.")),
        )
        if not вернулись:
            return ""
        # Прежний разговор ничего не знает про обряд: он всё это время лежал
        # на полке. Без записки следующая реплика прилетела бы туда как
        # «закончить стратсессию» посреди утреннего разговора о делах —
        # коуч честно не понял бы, о чём речь.
        return (
            "<обряд>\nМы отлучались на стратсессию и вернулись к этому разговору. "
            "Что там решили — уже записано в память и в Todoist, пересказывать "
            "не надо. Просто продолжай с того места, где остановились: коротко "
            "подтверди возвращение и жди, что скажет пользователь.\n</обряд>\n\n"
        )

    async def _заметить_режим(self, context: ContextTypes.DEFAULT_TYPE, было) -> None:
        """Сказать вслух, если режим переключился.

        Переключает его сам коуч, правя `настройки.md` по просьбе пользователя, —
        то есть машинная правка, и по правилу проекта она обязана быть видимой.
        Молча сменившийся режим означал бы, что человек не знает, с какой
        головой он сейчас разговаривает.
        """
        стало = self.engine.режим()
        if стало.имя == было.имя:
            return
        log.info("режим переключён: %s → %s", было.имя, стало.имя)
        await context.bot.send_message(
            chat_id=self.owner_id,
            text=(f"{стало.значок} Переключил режим: {было.имя} → {стало.имя} "
                  f"({стало.повод}).\nСледующая реплика начнёт новый разговор — "
                  f"память подкладывается только в его начало."),
        )

    async def _send_document(self, path: Path, caption: str = "") -> bool:
        """Отправить файл владельцу. Возвращает, дошёл ли.

        Библиотека это умела всегда, а бот — нет: до 31.07 у него был только
        send_message. Проверено grep'ом, не памятью.
        """
        if self.app is None:
            log.error("файл «%s» некуда слать: приложение ещё не поднято", path.name)
            return False
        try:
            with path.open("rb") as файл:
                await self.app.bot.send_document(
                    chat_id=self.owner_id, document=файл,
                    filename=path.name, caption=caption or None,
                )
        except Exception:  # телеграм упал, файл великоват, сеть — исход один
            log.exception("не смог отправить файл %s", path)
            return False
        return True

    async def _сказать_владельцу(self, текст: str) -> None:
        """Сказать что-то в телеграм не в ответ на реплику, а от себя.

        Нужна фоновым сторожам: канал комментариев так жалуется на потолок.
        Приложение поднимается позже конструктора, поэтому проверяем.
        """
        if self.app is None:
            log.error("некому сказать: приложение ещё не поднято (%s)", текст[:60])
            return
        try:
            await self.app.bot.send_message(chat_id=self.owner_id, text=текст)
        except Exception:
            log.exception("не смог написать владельцу")

    async def watch_comments(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Опрос комментариев в Todoist — вторая дверь к коучу.

        Пустой цикл стоит 215 байт и один HTTP-запрос: модель просыпается
        только на обращение по имени. Молчим в логах о пустоте — 480 строк
        «ничего нет» в сутки прячут настоящую поломку.
        """
        # Модель канала берём из настроек мозга, как и модель разговора:
        # «переключись на Opus» обязано менять и работника комментариев.
        values, beefs = coach_settings.read(self.brain_dir)
        if values and not beefs:
            self.comments.model = str(values["модель_разговора"])
        try:
            итог = await self.comments.шаг()
        except Exception:
            log.exception("канал комментариев сорвался")
            return
        if итог["обращений"] or итог["новых"]:
            log.info(
                "канал комментариев: новых %d, обращений %d, разобрано %d",
                итог["новых"], итог["обращений"], итог["сделано"],
            )

    async def _keep_typing(self, chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            while True:
                await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    @staticmethod
    def _split(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
        """Нарезать длинный текст на куски, которые примет телеграм.

        Старая нарезка резала только по переносам строк, и на сплошном тексте
        без единого перевода строки была бессильна: строка длиннее лимита
        уезжала в кусок целиком. Расшифровка голосового — ровно такая стена
        букв, поэтому длинная диктовка роняла отправку с «Message is too long»
        (04.08, дважды), а вместе с ней и весь ответ коуча.

        Уступаем по очереди, от самой желанной границы к самой крайней:
        абзац → конец предложения → пробел между словами → голые символы.
        Каждая следующая ступень включается только там, где предыдущая не
        нашла места, поэтому обычный текст с абзацами режется как раньше.
        """
        куски: list[str] = []
        остаток = text.strip("\n")
        while остаток:
            if len(остаток) <= limit:
                куски.append(остаток)
                break
            голова, остаток = _отрезать(остаток, limit)
            куски.append(голова)
        return куски or ["…"]

    # --- обработчики ---

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._mine(update):
            return
        await self._think_and_reply(update.message.text, update.effective_chat.id, context)

    def _слова_для_распознавателя(self) -> list[str]:
        """Свои слова человека для подсказки распознавателю.

        Отдельным методом, а не строкой внутри обработчика голосового, ровно
        по причине самой заявки: слово лежало в памяти, а до распознавателя
        не доезжало. Молчаливо оборванная проводка — это и есть та беда;
        отсюда её видно тесту.
        """
        return glossary.слова(self.engine.brain_dir)

    async def on_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._mine(update):
            return
        chat_id = update.effective_chat.id
        message = update.message
        source = message.voice or message.audio or message.video_note

        # Путь голосового целиком идёт через Xray-мост: и скачивание из Telegram,
        # и расшифровка. Мост моргает — повторяем каждый шаг (см. retry.py).
        # Первые сбои проходят молча, но если возня затянулась — говорим об этом,
        # чтобы владелец не смотрел в тишину.
        warned = False

        async def warn(attempt: int, error: BaseException) -> None:
            nonlocal warned
            if warned or attempt < 3:
                return
            warned = True
            await context.bot.send_message(
                chat_id=chat_id, text="Связь шалит, пробую разобрать ещё раз — подожди."
            )

        async def switched(failed: str, taken: str) -> None:
            await context.bot.send_message(
                chat_id=chat_id, text=f"{failed} не отвечает — расшифровываю через {taken}."
            )

        typing = asyncio.create_task(self._keep_typing(chat_id, context))
        try:
            telegram_file = await retry_network(
                lambda: context.bot.get_file(source.file_id),
                what="получение ссылки на голосовое",
                on_retry=warn,
            )
            audio = bytes(
                await retry_network(
                    lambda: telegram_file.download_as_bytearray(),
                    what="скачивание голосового",
                    on_retry=warn,
                )
            )
            text = await self.voice.transcribe(
                audio, on_retry=warn, on_switch=switched,
                глоссарий=self._слова_для_распознавателя())
        except Exception as error:
            log.exception("расшифровка не удалась")
            await context.bot.send_message(chat_id=chat_id, text=f"Не разобрал голос: {error}")
            return
        finally:
            typing.cancel()

        if not text:
            await context.bot.send_message(chat_id=chat_id, text="Тишина — ничего не разобрал.")
            return

        # Расшифровка — ответом на само голосовое и цитатой, а не обычной
        # репликой. Владелец читал её как слова коуча: она приходила с его
        # стороны, обычным шрифтом, и микрофончика в начале не хватало, чтобы
        # понять, что это его собственные слова.
        #
        # Дописать подпись прямо в голосовое нельзя: бот правит только свои
        # сообщения, а голосовое отправил человек. Ближе к этому API не даёт,
        # поэтому берём два признака сразу — привязку к сообщению (видно,
        # к чему относится) и цитату курсивом (видно, что это не речь коуча).
        #
        # Длинная диктовка в одно сообщение не влезает, поэтому режем — и режем
        # ПОСЛЕ экранирования, чтобы в лимит попал ровно тот текст, который
        # уедет в телеграм. Запас в сотню знаков — на теги рамки вокруг куска.
        #
        # Показ расшифровки не должен решать, ответит ли коуч. 04.08 отказ
        # телеграма на этой самой отправке уронил обработчик до того, как
        # начиналась мысль: человек не получил ни текста, ни ответа, ни причины.
        # Поэтому сбой показа отсюда дальше не идёт — он только жалуется в лог.
        try:
            куски = self._split(html.escape(text), TELEGRAM_LIMIT - 100)
            for номер, кусок in enumerate(куски, 1):
                await context.bot.send_message(
                    chat_id=chat_id,
                    # Микрофончик — только на первом куске: он метит начало
                    # реплики, а не каждую её часть.
                    text=f"<blockquote><i>{'🎙 ' if номер == 1 else ''}{кусок}</i></blockquote>",
                    parse_mode=ParseMode.HTML,
                    # Привязка — тоже только у первого: телеграм и так покажет
                    # остальные следом, а три стрелки на одно голосовое рябят.
                    reply_to_message_id=message.message_id if номер == 1 else None,
                )
        except TelegramError:
            log.exception("расшифровку показать не смог, но думать иду")

        await self._think_and_reply(text, chat_id, context, channel="voice")

    @staticmethod
    def _image_source(message) -> tuple[object | None, str]:
        """Что прислали: сжатое фото или картинку файлом.

        У фото Telegram отдаёт лесенку размеров — берём последний, самый крупный:
        коучу нужны детали, а не превью.
        """
        if message.photo:
            return message.photo[-1], ".jpg"
        document = message.document
        if document and (document.mime_type or "").startswith("image/"):
            return document, Path(document.file_name or "").suffix.lower()
        return None, ""

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Картинка: кладём во временный файл и показываем коучу через Read.

        Зрение у бота не своё, а движка: инструмент Read у Claude Code открывает
        изображения и видит их. Поэтому работа телеграм-слоя простая — положить
        файл на диск, назвать путь и убрать за собой.

        Альбом из нескольких картинок Telegram присылает отдельными сообщениями,
        поэтому каждая разбирается своим ходом — связать их в одну мысль коуч
        может только по подписям.
        """
        if not self._mine(update):
            return
        chat_id = update.effective_chat.id
        message = update.message
        source, suffix = self._image_source(message)
        if source is None:
            return
        if suffix not in IMAGE_SUFFIXES:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Формат «{suffix or 'без расширения'}» я разглядеть не смогу. "
                "Пришли jpg, png, gif или webp — или обычным фото, без файла.",
            )
            return

        # Путь картинки, как и путь голосового, идёт через Xray-мост: мост моргает —
        # повторяем шаг (см. retry.py). Молчим, пока возня короткая.
        warned = False

        async def warn(attempt: int, error: BaseException) -> None:
            nonlocal warned
            if warned or attempt < 3:
                return
            warned = True
            await context.bot.send_message(
                chat_id=chat_id, text="Связь шалит, пробую забрать картинку ещё раз — подожди."
            )

        typing = asyncio.create_task(self._keep_typing(chat_id, context))
        try:
            telegram_file = await retry_network(
                lambda: context.bot.get_file(source.file_id),
                what="получение ссылки на картинку",
                on_retry=warn,
            )
            picture = bytes(
                await retry_network(
                    lambda: telegram_file.download_as_bytearray(),
                    what="скачивание картинки",
                    on_retry=warn,
                )
            )
        except Exception as error:
            log.exception("картинка не скачалась")
            await context.bot.send_message(chat_id=chat_id, text=f"Не забрал картинку: {error}")
            return
        finally:
            typing.cancel()

        PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
        path = PHOTOS_DIR / f"{update.update_id}{suffix}"
        await asyncio.to_thread(path.write_bytes, picture)

        caption = (message.caption or "").strip()
        prompt = (
            f"пользователь прислал картинку. Открой её инструментом Read по пути {path} — "
            "это изображение, ты его увидишь.\n"
            + (f"Подписал так: {caption}\n" if caption else "")
            + "Ответь по тому, что на картинке, и по подписи, если она есть."
        )
        try:
            await self._think_and_reply(
                prompt,
                chat_id,
                context,
                channel="photo",
                archived_as=f"[картинка] {caption}" if caption else "[картинка]",
            )
        finally:
            # Одноразовое сносим сразу: картинка нужна была ровно на этот ответ.
            path.unlink(missing_ok=True)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Сбой в обработке — в лог по-человечески, а не строкой «обработчиков нет».

        Обработчик ничего не чинит: 30.07.2026 разбор показал, что цикл поллинга
        в python-telegram-bot 22.8 настроен повторять попытки бесконечно и от
        сетевой ошибки не умирает, — значит регистрация обработчика не была
        лечением, вопреки записи в разборе от 25.07. Он нужен ради видимости:
        без него библиотека печатает трейсбек с формулировкой, которая уводит
        разбор не туда. Живучесть ушей держит внешний сторож, а не эта функция.
        """
        error = context.error
        log.error("сбой в обработке: %s: %s", type(error).__name__, error, exc_info=error)

    async def on_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._mine(update):
            return
        режим = self.engine.режим()
        await update.message.reply_text(
            "Я на связи. Наговаривай или пиши — разберём дела.\n"
            "Пингую утром в 10:00, днём в 14:30 и вечером в 20:00.\n"
            f"Сейчас режим {режим.значок} {режим.имя} — {режим.повод}.\n"
            "Кнопка меню слева от поля ввода просто печатает за тебя: "
            "то же самое можно сказать голосом.",
            # Нижняя клавиатура жила на стороне телеграма и после правки кода
            # сама не исчезнет — её надо снять вслух, один раз. Место для этого
            # ровно одно: /start шлёт сам телеграм, а больше ничего гарантированно
            # не случается. Тому, у кого клавиатуры и не было, снятие ничего
            # не стоит и ничего не показывает.
            reply_markup=ReplyKeyboardRemove(),
        )

    async def on_команда(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Команда меню = напечатанная за человека фраза, и больше ничего.

        Своей логики здесь нет намеренно. Всё, что команда умеет, умеет и голос:
        отличить `/week` от сказанного «проведём недельную стратсессию» ниже
        по течению невозможно, потому что дальше идёт одна и та же фраза.
        """
        if not self._mine(update):
            return
        # «/week@имя_бота» и «/week с понедельника» — телеграм отдаёт всё целиком.
        имя = update.message.text.lstrip("/").split()[0].split("@")[0]
        фраза = ФРАЗЫ_КОМАНД.get(имя)
        if not фраза:   # обработчик вешается только на знакомые — сюда не дойти
            return
        # Стираем «/week» из переписки: человек нажал кнопку в меню, а латиница
        # в ленте разговора выглядит мусором. Коучу и в архив уходит русская
        # фраза, её и видно в истории. Не стёрлось — это косметика, живём дальше.
        try:
            await update.message.delete()
        except TelegramError as сбой:
            log.debug("не смог стереть команду /%s: %s", имя, сбой)
        # `из_команды` — единственное место, где нажатие ещё отличимо от слов.
        # Ниже по течению идёт одна и та же фраза, а включать обряд с 06.08.2026
        # позволено только нажатию.
        await self._think_and_reply(фраза, update.effective_chat.id, context,
                                    из_команды=True)

    async def on_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Показать, чем коуч думает, и дать список кликабельных моделей.

        Коуча тут не зовём: список механический, а ответ на `/model` через
        модель стоил бы полного рюкзака ради четырёх строк — и считался бы
        той самой моделью, от которой человек уходит.
        """
        if not self._mine(update):
            return
        # Читаем так же, как читает движок: сломанный файл отдаёт умолчания
        # целиком, и показать надо их — это то, чем бот думает на самом деле,
        # а не то, что написано в непрочитанной строке.
        значения, _ = coach_settings.read(self.brain_dir)
        сейчас = значения.get("модель_разговора")
        # Имя модели не дублируем — оно и есть команда. Подпись «/opus — Opus»
        # это строка, которую человек читает дважды и оба раза зря.
        строки = [
            f"{СЕЙЧАС if код == сейчас else ПРОЧЕЕ} /{имя}"
            for имя, _, код in МОДЕЛИ
        ]
        # Незнакомое значение показываем как есть: настройки правит и человек
        # руками, и палец ни у кого — это не поломка, а повод увидеть, что стоит.
        известна = any(код == сейчас for _, _, код in МОДЕЛИ)
        шапка = ("Выбери модель:" if известна
                 else f"В настройках стоит «{сейчас}» — не из списка. Выбери модель:")
        await update.message.reply_text(f"{шапка}\n\n" + "\n".join(строки))

    async def on_session(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Показать четыре стратсессии кликабельными.

        Коуча не зовём по той же причине, что и в списке моделей: выбор делает
        человек, а список механический. Сам выбор уже идёт через разговор —
        команда печатает фразу, и стратсессию открывает код по этой фразе.
        """
        if not self._mine(update):
            return
        if self.engine.обряд:
            await update.message.reply_text(
                f"{self._пометка()} — она уже идёт. Вторую поверх не начинаю.\n"
                "Выйти — /end."
            )
            return
        # Подпись по-русски: имена команд английские, и «/quarter» без перевода
        # понятно не всякому. Значка тут нет — в отличие от списка моделей,
        # где палец показывает текущую; текущей стратсессии не бывает, мы
        # в эту ветку и не заходим, если она идёт.
        строки = [
            f"/{имя} — {rituals.ПОВОДЫ[ключ].какая}"
            for имя, _, ключ in СТРАТСЕССИИ
        ]
        await update.message.reply_text(
            "Какую стратсессию проводим?\n\n" + "\n".join(строки) +
            "\n\nТекущую рабочую сессию ставлю на паузу. Чтобы вернуться "
            "к обычному режиму общения, нажми в меню команду /end"
        )

    async def on_выбор_модели(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Переставить модель. Тоже без коуча: решение уже принято человеком."""
        if not self._mine(update):
            return
        имя = update.message.text.lstrip("/").split()[0].split("@")[0]
        выбор = next((м for м in МОДЕЛИ if м[0] == имя), None)
        if выбор is None:   # обработчик вешается только на знакомые — сюда не дойти
            return
        _, красиво, код = выбор
        # Под тем же замком, что и разговор: правка настроек посреди чужого
        # ответа означала бы, что половина хода думала одной моделью,
        # а половина другой.
        async with self.lock:
            получилось = await asyncio.to_thread(
                coach_settings.поставить_модель, self.brain_dir, код
            )
            if получилось:
                await self.brain.push(f"модель разговора → {красиво}")
        # Машинное действие обязано быть видимым — иначе непонятно, случилось ли.
        await update.message.reply_text(
            f"Готово, думаю моделью {красиво}. Разговор продолжается тот же."
            if получилось else
            f"Не смог переставить модель на {красиво} — настройки не поддались, "
            "подробности в логах. Пока думаю прежней."
        )

    async def on_version(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Из чего собран GTD-коуч и что из этого устарело.

        Коуча не зовём — по той же причине, что в списке моделей: ответ
        механический, а полный рюкзак ради пяти строк это дорого. Проверка
        идёт по сети (GitHub и npm), поэтому показываем «печатаю».
        """
        if not self._mine(update):
            return
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        отчёт = await versions.проверить()
        await update.message.reply_text(versions.отчёт_текстом(отчёт))

    async def on_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._mine(update):
            return
        async with self.lock:
            self.engine.sessions.clear()
        await update.message.reply_text(
            "Начали с чистого листа. Всё сказанное сегодня сохранено — "
            "ночью я перечитаю день целиком, вместе с этой частью."
        )

    async def ping(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        # В задании лежит ИМЯ промпта, а не текст: файл читается в момент пинга,
        # значит правка текста в плагине доезжает перезапуском, а не пересборкой.
        name, channel = context.job.data
        now = datetime.now(MOSCOW)
        # Утро понедельника и первое число — поводы для обзора. Раньше первого
        # числа код сам ЗАПУСКАЛ месячный итог; теперь коуч его ПРЕДЛАГАЕТ.
        # Разница не в вежливости: стратсессия по звонку не случается, а вот
        # переключение режима без согласия человека — случается, и он получает
        # дорогую голову там, где просил «что сегодня».
        prompt = load_prompt(name).text
        if name == "чекин-утро":
            # Бриф считается ЗДЕСЬ, а не ночью. Ночная проза состарилась бы
            # за семь часов, а второй вызов модели ничего бы не добавил:
            # утренний чек-ин и так идёт через модель с полным контекстом.
            # Код даёт цифры, коуч решает, что из них главное.
            prompt = self._with_brief(prompt, await self._обход())
            # Понедельник и первое число НЕ подменяют чек-ин, а дописывают
            # к нему записку. У утреннего чек-ина своя работа — главное дело
            # дня и обещание; отнимать её ради напоминания про обзор значит
            # менять одно нужное на другое. Записка едет рядом, как свод завалов.
            повод = rituals.по_календарю(now)
            # Календарь говорит, что период закрылся, а журнал — проводили
            # ли уже. Второе сильнее: стратсессию можно провести разговором,
            # без кнопки, и тогда код о ней не знает ничего, кроме записи
            # в журнале. Промах 24.08.2026: суббота, стратсессия проведена,
            # в понедельник коуч предложил её заново.
            if повод and not rituals.напоминать_ли(self.brain_dir, повод,
                                                   now.date()):
                когда = rituals.когда_проводили(self.brain_dir, повод)
                log.info("напоминание о стратсессии не поехало: %s была %s",
                         повод.что, когда)
                повод = None
            if повод:
                prompt += "\n\n" + load_prompt("напоминание-о-стратсессии").format(
                    что=повод.вин, повод=self._словами(now, повод),
                )
        sent_at = now.isoformat(timespec="seconds")
        await self._think_and_reply(prompt, self.owner_id, context, channel=channel,
                                    сочинил_код=True)
        self._schedule_followup(context, sent_at, attempt=1)

    @staticmethod
    def _словами(now, повод) -> str:
        """Какой именно период закрылся — по-человечески, а не «повод №2»."""
        if повод.ключ == "годовой":
            return f"{now.year - 1} год"
        if повод.ключ == "квартальный":
            # Тот же шаг назад и по той же причине: первого апреля закрылся
            # первый квартал, а не начался второй.
            вчера = now.date() - timedelta(days=1)
            return f"{(вчера.month - 1) // 3 + 1}-й квартал {вчера.year}"
        if повод.ключ == "месячный":
            # Подводим ЗАКРЫВШИЙСЯ месяц, а не начавшийся: первого августа
            # итог пишется за июль. Отсюда шаг на день назад.
            return f"месяц {(now.date() - timedelta(days=1)).strftime('%Y-%m')}"
        return "неделя"

    async def _обход(self) -> detectors.Свод:
        """Обход завалов. Упал — пустой свод и жалоба в лог: чек-ин важнее."""
        try:
            return await detectors.обойти(
                self.todoist_token, self.snapshot.db_path, self._calendar_config(),
                полка=self.inbox,
                горизонты=self.brain_dir / "память" / "состояние" / "горизонты.md",
            )
        except Exception:
            log.exception("обход завалов сорвался")
            return detectors.Свод()

    def _with_brief(self, prompt: str, свод: detectors.Свод) -> str:
        """Подложить свод к чек-ину. Пусто — не подкладываем ничего.

        Подстановка, а не просьба «посмотри завалы» в тексте: просьба
        срабатывает, если модель не отвлеклась, — на этом в июле протекло
        знание. Пустой свод не превращается в строку «всё в норме»: коуч
        не должен отчитываться о проверке, которая ничего не нашла.
        """
        # Давность — только чтение и только ради пометок «висит N-е сутки»:
        # писать счётчики утром нельзя, это работа ночи (см. `давность`).
        давности = detectors.давность(self.snapshot.db_path, свод)
        текст = свод.текст(давности)
        return f"{prompt}\n\n{текст}" if текст else prompt

    def _дожимов_в_запасе(self) -> int:
        """Сколько попыток достучаться у нас есть.

        Число называет человек в своих ритмах (`дожим_раз`), а не плагин числом
        файлов `дожим-N.md`: плагин общий у всех, а «дожимай меня дважды» или
        «не дожимай вовсе» — настройка про конкретного человека. Ноль законен.

        Текстов при этом может оказаться меньше, чем попыток, — тогда дожимаем
        столько, сколько есть, и не падаем.
        """
        return min(self.rhythms["дожим_раз"], len(followups()))

    def _schedule_followup(self, context: ContextTypes.DEFAULT_TYPE, sent_at: str, attempt: int) -> None:
        """Завести следующую попытку достучаться, если она ещё в запасе."""
        if attempt > self._дожимов_в_запасе():
            return
        context.job_queue.run_once(
            self.follow_up,
            when=timedelta(minutes=self.rhythms["дожим_минут"]),
            data=(sent_at, attempt),
            name=f"дожим-{attempt}",
        )

    async def follow_up(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Пользователь не отозвался на чек-ин — попробовать ещё раз, иначе отступить.

        Проверяем факт по архиву, а не по флагу в памяти: бот мог быть перезапущен,
        а ответ мог прийти любым каналом.
        """
        sent_at, attempt = context.job.data
        if await asyncio.to_thread(self.archive.answered_since, sent_at):
            log.info("дожим %s не нужен: пользователь отозвался", attempt)
            return
        now = datetime.now(MOSCOW)
        if now.hour >= self.rhythms["тихий_час"] or now.hour < 8:
            log.info("дожим %s отменён: время тихое (%s)", attempt, now.strftime("%H:%M"))
            return
        # Перечитываем запас, а не верим очереди: пока дожим ждал своего часа,
        # человек мог поставить себе `дожим_раз: 0`, а промпт — уехать из плагина.
        if attempt > self._дожимов_в_запасе():
            return
        attempts = followups()
        await self._think_and_reply(
            attempts[attempt - 1].text, self.owner_id, context, channel="nudge",
            сочинил_код=True,
        )
        self._schedule_followup(context, sent_at, attempt + 1)

    async def nightly_digest(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Ночью перечитать день, сложить конспект и начать разговор заново.

        Разговор не копится до упора в лимит: каждый день закрывается выжимкой,
        а преемственность держится журналом, а не длиной ленты сообщений.

        **Лента рвётся безусловно и первым делом.** До 07.09.2026 закладка
        стиралась только после удачной выжимки, и это оказалось дорого: у одного
        из пользователей ночная модель была недоступна на его подписке, выжимка
        не удавалась каждую ночь, закладка оставалась на месте — и один разговор
        рос месяц подряд, с 220 тысяч токенов до 645 тысяч в рюкзаке. Лимит
        подписки выгорал за пару диалогов, а в логе была одна строчка, которую
        никто не читал (заявка gtd-coach#1). Та же болезнь в миниатюре нашлась
        и на своём сервере: ночь на 5 сентября сорвалась, и 5-го рюкзак был
        97 тысяч вместо обычных семидесяти.

        Довод «а вдруг день пропадёт» больше не работает: разговор целиком лежит
        в архиве, и пропущенные дни догоняет `missing_days` следующей же ночью.
        Дыру в журнале починить можно, переплату за раздутый контекст — нельзя.
        """
        async with self.lock:
            session_id = self.engine.sessions.load()
            today = datetime.now(MOSCOW).date()
            yesterday = today - timedelta(days=1)
            # Ничего между этой строкой и захватом замка быть не должно: всё,
            # что стоит выше обрыва ленты, — это шанс до обрыва не дойти.
            self.engine.sessions.clear()
            try:
                await self.brain.pull()
                # Выжимку делаем всегда: день целиком лежит в архиве, даже если
                # ленту разговора сбросили через /new и session_id уже пуст.
                await self._свернуть_дни(session_id, yesterday)

                # Второй вопрос к тому же дню — уже про саму память: что пора
                # записать и где она сегодня подвела. Просьба в тексте конституции
                # («увидел паттерн — допиши») не срабатывала; будильник срабатывает.
                found = await self.memory_watch.run(
                    yesterday, self.digester.transcript_of(yesterday)
                )
                # Найденное молча в копилке — свалка. Дёргаем за рукав задачей
                # с датой: одна на копилку, не на каждую находку.
                #
                # Поводов два, и второй заведён 04.08. Раньше задача вставала
                # только на СВЕЖУЮ находку — значит её можно было закрыть,
                # не разобрав ни строки, и полная копилка молчала дальше.
                # Ровно та болезнь, которую проект ловит с 31.07: у правила
                # «разобранное вычёркивай» не было прибора. Теперь норма считается
                # по полке и накрывает все виды сразу (этап 20).
                висит, старая = await asyncio.to_thread(
                    self.memory_watch.залежалось, today
                )
                поводы = []
                if found.get("факты") or found.get("выводы"):
                    поводы.append(
                        f"Ночь {yesterday.isoformat()}: фактов {found['факты']}, "
                        f"выводов {found['выводы']}. Выводы записываются только "
                        f"после подтверждения пользователем."
                    )
                if висит:
                    поводы.append(
                        f"Залежалось: {висит} записей не разобрано, самая старая "
                        f"от {старая}. Запись закрывается статусом с итогом: "
                        f"подтверждённое — «сделана», отклонённое — «отклонена» "
                        f"с причиной, отложенное — «отложена» с датой возврата."
                    )
                if поводы:
                    await raise_task(self.todoist_token, "предложения", " ".join(поводы))
                if found.get("промахи"):
                    await raise_task(
                        self.todoist_token, "промахи",
                        f"Ночь {yesterday.isoformat()}: промахов {found['промахи']}.",
                    )

                # Разобранное закрыто на полке — карточка в Todoist больше не дело.
                # Сторож стоит здесь, а не рядом с закрытием: закрывают запись
                # разными путями (коуч кнопкой, человек скриптом в разборе),
                # и общего у этих путей — только наступившая ночь.
                if self.todoist_token:
                    await погасить_карточки(self.todoist_token, self.inbox)

                # Снимок дел — прежде укрупнений: он должен успеть сохранить то,
                # что человек может удалить завтра. Ноль токенов, чистый код.
                await self.snapshot.run(today)

                # Обещание дня против факта. Разбор идёт ПОСЛЕ снимка: список
                # закрытых за вчера должен быть уже в базе, иначе сверять не с чем.
                await self.promises.run(yesterday, self.digester.transcript_of(yesterday))

                # Обход завалов ночью нужен не ради брифа (тот считается утром),
                # а ради счёта суток: лампочка, горящая третьи сутки подряд, —
                # это уже долг, и он дёргает за рукав задачей. Прибор без
                # реакции — декорация.
                свод = await self._обход()
                # Ревизия приходит раз в неделю, в назначенный день (ритмы).
                # В остальные ночи счётчики суток всё равно двигаются — иначе
                # к среде было бы нечего показывать.
                ревизия = today.weekday() == rhythms.день_ревизии(self.rhythms)
                висит = await asyncio.to_thread(
                    detectors.запомнить, self.snapshot.db_path, свод, today,
                    поднимать=ревизия,
                )
                if висит:
                    # Один вызов на всё созревшее, а не по вызову на отклонение.
                    # Правило «одна задача на копилку» соблюдалось и так — вторая
                    # находка дописывает комментарий к первой, — но шесть
                    # комментариев подряд читаются как шесть проблем, хотя это
                    # один разговор. Поймано проверкой этапа: в первое же
                    # созревание их оказалось ровно шесть.
                    await raise_task(
                        self.todoist_token, "завал",
                        f"{today.isoformat()}, стоит третьи сутки и дольше:\n"
                        + "\n".join(f"- {о.строка}" for о in висит),
                    )

                # Профиль: показания приборов про самого человека. Состояние
                # перезаписываем каждую ночь, строку в журнал — раз в неделю,
                # в ту же ночь, что собирается недельная выжимка. Ряд, а не
                # одна оценка: важно не «сегодня столько», а «поехало».
                показатели = await asyncio.to_thread(
                    profile.собрать, self.snapshot.db_path, self.archive.path, today
                )
                if показатели:
                    await asyncio.to_thread(
                        profile.записать, self.brain_dir, показатели, today,
                        yesterday.weekday() == 6,
                    )

                # Укрупняем только законченные периоды и только по календарю.
                if yesterday.weekday() == 6:  # воскресенье закрыло неделю
                    await self.digester.make_week(yesterday)
                if today.day == 1:  # первое число: вчера закрыло месяц
                    await self.digester.make_month(yesterday)
                # Журнал держим в том же окне, что читает коуч, — вместе с адресами.
                self.digester.rotate()

                # Обзоры. Агент стратсессию сам не запускает — но задача с датой
                # ставится сама, потому что предложение в чате уползает вверх
                # за полдня. Ровно правило «фоновая работа ставит задачу с датой».
                await self._поставить_обзоры(today)

                # Обновления продукта. Ночью, потому что днём это никому
                # не срочно, а к утру задача уже стоит в списке дел.
                await self._сверить_версии()

                # Оглавление памяти. Валидатор ловит сироту и битую ссылку, но
                # срабатывает при записи; запись могла пройти мимо хука. Раз
                # в сутки проверяем ещё раз — расхождение обязано находиться
                # не тогда, когда о него споткнутся.
                await self._проверить_оглавление()

                if not await self.brain.push(f"выжимка за {yesterday.isoformat()}"):
                    # push вернул False и когда менять было нечего, и когда он упал.
                    # Различаем по логу; задачу поднимаем только если правки были.
                    if await self.brain.dirty():
                        await raise_task(
                            self.todoist_token, "синхронизация",
                            f"Ночь {yesterday.isoformat()}: изменения в мозге есть, "
                            f"а на GitHub они не уехали.",
                            inbox=self.inbox,
                        )

                # История дня подписывается по-человечески той же моделью,
                # что делала выжимку — сторож на маке для этого слишком туп и быстр.
                await self.tidier.tidy(yesterday)
            except Exception:
                log.exception("ночная выжимка сорвалась")


    async def _свернуть_дни(self, session_id: str | None, yesterday) -> None:
        """Сделать выжимку за вчера и догнать всё, что не свернулось раньше.

        Раньше здесь была одна строка на один день, и ровно поэтому сорванная
        ночь означала потерянный день навсегда. Теперь ночь смотрит всё окно
        памяти: день, в который разговаривали, а конспекта нет, — это работа,
        и она делается сегодня, даже если случилась позавчера.

        Каждый день ловится отдельно: упавший вторник не должен утаскивать
        за собой среду. А то, что так и не свернулось, дёргает за рукав задачей —
        иначе поломка снова окажется одной строчкой в логе, которую никто
        не читает, и это будет ровно тот случай, из-за которого этот код и пишется.
        """
        # `session_id` — закладка вчерашнего разговора, и годится она только
        # для вчера: у дней постарше своих закладок уже нет, а чужая подсунула
        # бы в выжимку не тот день. Для них источник один — архив.
        for день in self.digester.missing_days(yesterday):
            свой = session_id if день == yesterday else None
            try:
                await self.digester.make_day(свой, день)
            except Exception:
                log.exception("выжимка за %s не сделалась", день)

        осталось = self.digester.missing_days(yesterday)
        if осталось:
            await raise_task(
                self.todoist_token, "выжимка",
                f"Ночь {yesterday.isoformat()}: не свернулись дни "
                + ", ".join(д.isoformat() for д in осталось)
                + ". Разговор этих дней цел и лежит в архиве — потерян только "
                  "конспект, и следующие ночи будут пробовать снова.",
                inbox=self.inbox,
            )

    async def _поставить_обзоры(self, today) -> None:
        """Задачи на стратсессии — понедельник, первое число, начало квартала,
        первое января.

        Отдельные поводы, а не один список: у каждого своя дата и свой
        разговор. Вторая задача не заводится — `raise_task` допишет комментарий
        к уже стоящей, это его штатное поведение.

        Имя задачи берётся из повода, а не пишется здесь второй раз: написанное
        второй раз, оно разъедется с находкой в `backstage`, и задача перестанет
        вставать — молча.

        Квартальная стоит на апреле, июле и октябре, но не на январе: первого
        января закрывается год, и он крупнее. Три задачи подряд читаются как
        три дела, хотя разговор один.
        """
        def задача(ключ: str) -> str:
            return rituals.ПОВОДЫ[ключ].задача

        поводы = []
        if today.weekday() == 0:
            поводы.append((задача("недельный"), f"Неделя началась {today.isoformat()}."))
        if today.day == 1:
            закрывшийся = (today - timedelta(days=1)).strftime("%Y-%m")
            поводы.append((задача("месячный"), f"Закрылся {закрывшийся}."))
        if today.day == 1 and today.month in (4, 7, 10):
            вчера = today - timedelta(days=1)
            четверть = (вчера.month - 1) // 3 + 1
            поводы.append((задача("квартальный"),
                           f"Закрылся {четверть}-й квартал {вчера.year}."))
        if today.month == 1 and today.day == 1:
            поводы.append((задача("годовой"), f"Закрылся {today.year - 1} год."))
        for повод, заметка in поводы:
            исход = await raise_task(self.todoist_token, повод, заметка)
            log.info("задача на «%s»: %s", повод, исход or "не доехала до Todoist")

    async def _сверить_версии(self) -> None:
        """Что из частей продукта устарело — и задачей, а не сообщением.

        GTD-коуч собран из пяти частей, и все они обновляются молча: пока
        не спросишь, что вышло новое, не узнаешь. Спрашивает ночь.

        Задачу заводит только один повод — устаревшие репозитории: их тянет
        человек командой `./update.sh`. Движок сюда больше не попадает: его
        поднимает сама ночь (bump-engine.sh на сервере), и звать человека
        к тому, что делается без него, — это и есть лишняя задача.

        Одна задача на повод соблюдается сама: `raise_task` допишет комментарий
        к уже стоящей, второй не заведёт. Значит три новых коммита подряд дают
        одну задачу и три строки в ней, а не три задачи.
        """
        try:
            отчёт = await versions.проверить()
        except Exception:
            # Сверка версий — не то, ради чего можно уронить ночной прогон.
            log.exception("сверка версий сорвалась")
            return
        репозитории, движок = versions.устаревшие(отчёт)
        if репозитории:
            строки = "\n".join(
                f"- {с.имя}: {с.стоит} → {с.новее}"
                + (f", коммитов {с.отстал_на}" if с.отстал_на else "")
                for с in репозитории
            )
            исход = await raise_task(
                self.todoist_token, "обновление", f"Новее на GitHub:\n{строки}"
            )
            log.info("задача на обновление: %s", исход or "не доехала до Todoist")
        if движок:
            # Задачи здесь больше нет намеренно (30.08.2026). Движок выходит почти
            # каждый день, и каждая версия заводила человеку задачу сделать одно
            # и то же — он тридцать раз подряд нажимал одну кнопку. Поднимает
            # ночной bump-engine.sh на сервере; человека дёргает только отказ,
            # находкой `движок_не_встал`. Строка в логе остаётся: по ней видно,
            # что ночь заметила версию, а подъём должен случиться следующей ночью.
            log.info("движок: стоит %s, вышла %s — поднимет ночной bump-engine.sh",
                     движок.стоит, движок.новее)
        if not репозитории and not движок:
            log.info("версии сверены: свежее некуда")

    async def _проверить_оглавление(self) -> None:
        """Сверить точку входа в память с тем, что лежит на диске.

        Две беды, и они зеркальные: **сирота** — файл есть, а ссылки на него
        нет ниоткуда (найдёт его только тот, кто уже знает адрес); **битая
        ссылка** — ссылка есть, а файла нет. Валидатор ловит обе, но при записи,
        а запись могла пройти мимо хука — например, файл положил не коуч,
        а `git pull` с другой машины.

        Выжимки считаются в ССЫЛКАХ, но не в СИРОТАХ, и это не придирка.
        Первая версия исключала их целиком — и объявила битыми семь живых
        ссылок на дневные выжимки: файлы-то есть, просто мы на них не смотрели
        (поймано прогоном 02.08.2026). Сирота из выжимки, наоборот, ложная
        тревога: файл пишет ночной прогон, а список в оглавлении собирает он же
        чуть позже, и между этими двумя мгновениями выжимка законно ничья.
        """
        память = self.brain_dir / "память"
        индекс = память / "00-index.md"
        try:
            текст = индекс.read_text(encoding="utf-8")
        except OSError as err:
            log.error("оглавление памяти не читается: %s", err)
            return

        связанные = set(re.findall(r"\[\[([^\]|#]+)", текст))
        файлы = {путь.stem: путь for путь in память.rglob("*.md")
                 if путь.name != "00-index.md"}
        сироты = sorted(имя for имя, путь in файлы.items()
                        if имя not in связанные and "выжимки" not in путь.parts)
        битые = sorted(имя for имя in связанные if имя not in файлы)
        if not сироты and not битые:
            log.info("оглавление памяти сходится: %d заметок", len(файлы))
            return
        беда = []
        if сироты:
            беда.append("сироты (файл есть, ссылки нет): " + ", ".join(сироты))
        if битые:
            беда.append("битые ссылки (ссылка есть, файла нет): " + ", ".join(битые))
        строка = "; ".join(беда)
        log.warning("оглавление памяти разошлось: %s", строка)
        await raise_task(self.todoist_token, "оглавление", строка, inbox=self.inbox)

    async def _check_load(self, mode, channel: str, summary: str = "", prompt: str = "") -> None:
        """Сверить то, что реально уехало в контекст, с паспортом памяти.

        Сверяем только ПЕРВЫЙ ход сессии: в середине разговора выжимки уже не
        грузятся, а контекст растёт от самих реплик — померили бы болтовню,
        а не рюкзак. Признак первого хода даёт сам движок, а не догадка по
        закладке: к моменту сверки закладка уже перезаписана свежей сессией.

        Двумя сторожами сразу, и это не дубль. Токены отвечают на вопрос
        «во что обошёлся этот режим» и берутся у самой модели. Байты по строкам
        отвечают на другой — «не врёт ли паспорт про отдельную строку»; именно
        так поймали «объявлено 69 400, на деле 37 962». Токенов по строкам
        модель не разбивает, и заменить одно другим нельзя.
        """
        if not self.engine.последний_первый:
            return
        # Построчно сверяем только то, размер чего от режима НЕ зависит.
        # Конституция, файлы стратегии и всё окно целиком меняются по замыслу,
        # и требовать от них одного числа значило бы завести сторожа, который
        # ругается на исправную работу. Их охраняет потолок режима, в токенах.
        куски = len(self.archive.window_rows(mode.окно))
        окно = len(self.archive.recent_digests(mode.окно).encode("utf-8"))
        измерено = {
            # Не сумма окна, а размер куска: сумма растёт сама, пока база
            # наполняется, а размер куска — не растёт. Поехал он — значит
            # выжимки стали многословнее, и чинить надо их промпт.
            "выжимка — средний размер одного куска памяти": окно // куски if куски else 0,
            # Описания кнопок кладёт в контекст движок, а не наш код.
            # Держим строку ради видимости: она объявляет полезную нагрузку,
            # а не цену — цена целиком в токенах, и разница там в восемьдесят раз.
            "описания кнопок инструментов": await self.engine.tools_weight(),
            "сводка дел — агрегат Todoist к каждой реплике": len(summary.encode("utf-8")),
            # Свод завалов уезжает в контекст один раз в сутки — с утренним
            # чек-ином. Считаем его только тогда, когда он там правда есть:
            # в остальные разы это ноль, а не «забыли посчитать».
            "ночной обход дел — свод к утреннему чек-ину": (
                len(prompt.encode("utf-8"))
                - len(prompt.split(detectors.ЗАГОЛОВОК)[0].encode("utf-8"))
                if detectors.ЗАГОЛОВОК in prompt else 0
            ),
        }
        # Ноль означает «этой строки в контексте сейчас нет», а не «она усохла».
        # Свод обхода уезжает раз в сутки, с утренним чек-ином; сверять его
        # с паспортом в остальные двадцать три часа — это ругань на исправную
        # работу, а сторож, который кричит каждый день, приучает себя не читать.
        измерено = {имя: сколько for имя, сколько in измерено.items() if сколько}
        beef = check_budget(self.brain_dir, измерено, mode=mode.имя,
                            tokens=self.engine.последний_контекст)
        if beef:
            log.warning("стартовая загрузка разошлась с паспортом: %s", beef)
            # Сигнализация без реакции — декорация. Лампочка горит → задача с датой.
            await raise_task(self.todoist_token, "потолок", f"[{mode.имя}, {channel}] {beef}",
                             inbox=self.inbox)

    # --- ритмы ---

    # Ключ ритма → (имя будильника, имя промпта в плагине, канал архива).
    ALARMS = {
        "утро": ("утро", "чекин-утро", "morning"),
        "день": ("день", "чекин-день", "midday"),
        "вечер": ("вечер", "чекин-вечер", "evening"),
    }

    def _hang_alarms(self, queue) -> None:
        """Перевесить будильники по нынешним ритмам. Старые снимаются по имени."""
        for name in list(self.ALARMS) + ["выжимка"]:
            for job in queue.get_jobs_by_name(name):
                job.schedule_removal()
        for key, (name, prompt_name, channel) in self.ALARMS.items():
            # «день: нет» — человек отказался от дневного чек-ина. Будильник просто
            # не вешается: выключенный чек-ин не приходит, остальные приходят
            # по-прежнему. Раньше отказаться было нельзя вовсе.
            if выключен(self.rhythms, key):
                log.info("чек-ин «%s» выключен в ритмах — будильник не вешаю", key)
                continue
            queue.run_daily(
                self.ping, time=_parse(self.rhythms[key], MOSCOW),
                data=(prompt_name, channel), name=name,
            )
        queue.run_daily(
            self.nightly_digest, time=_parse(self.rhythms["ночная_выжимка"], MOSCOW), name="выжимка"
        )

    async def _reread_rhythms(self, context: ContextTypes.DEFAULT_TYPE, loud: bool) -> None:
        """Перечитать файл ритмов и, если он изменился, перевесить будильники.

        `loud` — говорить ли пользователю. После его собственной реплики говорим: он
        только что попросил поменять расписание и должен увидеть, что вышло.
        Из часового сторожа молчим про «всё по-прежнему», но про поломку — всегда.
        """
        fresh, problems = read(self.brain_dir)
        if problems:
            beef = "; ".join(problems)
            log.error("ритмы в мозге сломаны: %s", beef)
            if self.broken_rhythms != beef:
                self.broken_rhythms = beef
                await context.bot.send_message(
                    chat_id=self.owner_id,
                    text=(f"Файл ритмов не читается, живу по прежнему расписанию.\n{beef}\n\n"
                          f"Адрес: {path_in(self.brain_dir)}"),
                )
                # Сообщение уползёт вверх и забудется — дублируем задачей с датой.
                await raise_task(
                    self.todoist_token, "ритмы", f"{beef}\nАдрес: {path_in(self.brain_dir)}",
                    inbox=self.inbox,
                )
            return
        self.broken_rhythms = ""
        if fresh == self.rhythms:
            return
        changes = describe(fresh, self.rhythms)
        self.rhythms = fresh
        self._hang_alarms(context.job_queue)
        log.info("ритмы перечитаны: %s", changes)
        if loud:
            await context.bot.send_message(
                chat_id=self.owner_id, text=f"Расписание переставил: {changes}."
            )

    async def watch_rhythms(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._reread_rhythms(context, loud=False)

    # --- пульс ---

    async def heartbeat(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Отметка «рабочий цикл жив» для docker healthcheck (src/healthcheck.py).

        ⚠️ ГРАНИЦА, ПРОВЕРЕННАЯ ОЖОГОМ. Здесь раньше стояло: «задача крутится в том же
        asyncio-цикле, что и поллинг: встанет цикл — перестанет обновляться и файл».
        Это НЕВЕРНО, и 03.08.2026 обошлось в 10,6 часа глухоты: приём сообщений умер в
        20:57, а пульс продолжал тикать каждую минуту до утра, healthcheck был зелёный,
        Docker считал контейнер здоровым. Планировщик пережил смерть поллинга.

        Значит этот файл доказывает только одно: планировщик жив. Про уши он не говорит
        НИЧЕГО. Живость ушей отслеживает отдельно «сторож слуха» (watch_hearing) по
        отметке СлышащийТранспорт.последний_успех.
        """
        try:
            HEARTBEAT_FILE.write_text(datetime.now(MOSCOW).isoformat(timespec="seconds"))
        except OSError:
            log.exception("не смог записать пульс в %s", HEARTBEAT_FILE)

    async def watch_hearing(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Сторож слуха: заметил, что приём обновлений умер — уводит процесс в перезапуск.

        Живой длинный опрос завершается каждые 10-15 секунд и каждый раз обновляет
        отметку в СлышащийТранспорт. Отметка не двигалась дольше предела — приём мёртв,
        и сам он не воскреснет: 03.08.2026 после одной сетевой ошибки он не поднялся ни
        разу за 10,6 часа.

        Лечение — выход из процесса. Docker поднимет контейнер сам (restart policy
        `unless-stopped`), и подъём займёт секунды. Выходим жёстко, через os._exit:
        мягкое завершение упирается в тот же залипший приём (при остановке 04.08 в 07:45
        библиотека не смогла доделать последний запрос и ругнулась в лог).

        Почему это не дублирует внешний сторож «бот оглох». Тот судит по копящейся
        очереди, то есть замечает глухоту ТОЛЬКО когда владелец напишет; ночью очередь
        пуста, и глухой бот неотличим от здорового — ровно поэтому вчерашний отказ жил
        до утра. Здешняя отметка не зависит ни от кого и протухает через три минуты.
        Внешний сторож остаётся вторым рубежом на случай, если умрёт и этот.
        """
        молчание = monotonic() - СлышащийТранспорт.последний_успех
        if молчание < ГЛУХОТА_ПРЕДЕЛ_СЕК:
            return
        log.error(
            "УШИ МОЛЧАТ %.0f с (предел %s с): приём обновлений от Telegram мёртв. "
            "Выхожу из процесса — Docker поднимет заново.",
            молчание, ГЛУХОТА_ПРЕДЕЛ_СЕК,
        )
        # Логи пишутся в stdout контейнера; без сброса буферов последняя — и самая
        # нужная при разборе — строка не доедет, потому что os._exit ничего не закрывает.
        logging.shutdown()
        os._exit(1)

    # --- запуск ---

    @staticmethod
    async def _выставить_меню(application: Application) -> None:
        """Показать список команд в кнопке меню телеграма.

        Не сработало — жалуемся в лог и поднимаемся дальше: меню это витрина,
        а не дверь. Всё, что оно печатает, человек может сказать словами,
        и коуч без витрины остаётся полностью рабочим.
        """
        try:
            await application.bot.set_my_commands(
                [BotCommand(имя, описание) for имя, описание, _ in КОМАНДЫ]
            )
        except TelegramError:
            log.exception("не смог выставить меню команд")

    def run(self) -> None:
        # Тексты поведения живут в плагине. Нехватка обязана вскрыться здесь,
        # одной понятной строкой, а не ночью в 03:00 внутри ночного прогона.
        gone = missing_prompts()
        if gone:
            raise SystemExit(
                "В плагине нет промптов: " + ", ".join(gone) +
                ". Проверь, что клон плагина на сервере свежий и том /plugin смонтирован."
            )
        # Части конституции — та же история и та же цена ошибки. Молча
        # упростившийся коуч хуже отсутствующего: он продолжит отвечать,
        # просто перестанет быть собой, и понять это будет неоткуда.
        нет_частей = режимы_модуль.недостающие_части()
        if нет_частей:
            raise SystemExit(
                "В плагине нет частей конституции: " + ", ".join(нет_частей) +
                f" (папка {режимы_модуль.PARTS_DIR}). Коуч без них — не коуч."
            )
        application = (
            Application.builder()
            .token(env("TELEGRAM_BOT_TOKEN", required=True))
            # Витрину меню выставляем на каждом подъёме, а не однажды руками:
            # список команд живёт в коде, и подъём — единственный момент, когда
            # код и телеграм точно сходятся. Правка списка доезжает выкаткой.
            .post_init(self._выставить_меню)
            # Свой транспорт ТОЛЬКО для приёма обновлений — он ставит отметку живости
            # ушей, которую читает «сторож слуха». Аргументы повторяют то, что библиотека
            # подставляет сама (пул на одно соединение, HTTP/1.1); таймауты сознательно
            # не задаём — пусть остаются её собственные, чтобы не сломать длинный опрос.
            .get_updates_request(СлышащийТранспорт(connection_pool_size=1, http_version="1.1"))
            .build()
        )
        self.app = application  # через него инструмент дашборда шлёт файл

        application.add_handler(CommandHandler("start", self.on_start))
        # Команды меню — печатающая рука: одна и та же ручка на все.
        # Telegram принимает только латиницу в командах — кириллические он отвергает.
        application.add_handler(CommandHandler(list(ФРАЗЫ_КОМАНД), self.on_команда))
        # Те, на которые отвечает сам бот. Вешаются из общего списка, а не по
        # строке на команду: строка легко забывается, а пропущенная команда
        # уходит в никуда молча — ни ответа, ни ошибки.
        for имя, метод in СВОИ_ОБРАБОТЧИКИ.items():
            application.add_handler(CommandHandler(имя, getattr(self, метод)))
        application.add_handler(
            CommandHandler([имя for имя, _, _ in МОДЕЛИ], self.on_выбор_модели))
        application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE, self.on_voice))
        # Картинка приходит либо сжатым фото, либо файлом — ловим оба входа.
        application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, self.on_photo))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        application.add_error_handler(self.on_error)

        queue = application.job_queue
        self._hang_alarms(queue)
        queue.run_repeating(self.heartbeat, interval=60, first=1, name="пульс")
        # Сторож слуха. Первая проверка через 5 минут после подъёма — это и есть льготный
        # срок: пока Telegram не ответил ни разу, отметка стоит на времени рождения
        # транспорта, и без отсрочки бот убивал бы себя, не успев подключиться.
        queue.run_repeating(self.watch_hearing, interval=60, first=300, name="сторож слуха")
        # Сторож на случай, когда файл ритмов приехал со стороны — например, git pull
        # притащил правку с другой машины. Разговор такую правку не заметит.
        queue.run_repeating(self.watch_rhythms, interval=3600, first=3600, name="сторож ритмов")
        # Вторая дверь: комментарии в карточках задач. Три минуты — компромисс
        # между «ответил, пока не убрал телефон» и ничем: пустой цикл стоит
        # 215 байт и ноль токенов, платить за частоту тут нечем.
        queue.run_repeating(
            self.watch_comments,
            interval=int(env("COMMENTS_POLL_SECONDS", "180")),
            first=30, name="комментарии",
        )

        log.info("коуч поднялся: %s, владелец %s", describe(self.rhythms), self.owner_id)
        # Вебхуки в РФ не годятся — Telegram их не достучится, работаем поллингом.
        # Накопленное НЕ выбрасываем: бота перезапускает сторож «бот оглох» именно
        # тогда, когда очередь не разбирается, — и флаг «выбросить» уничтожал бы
        # ровно те сообщения, ради которых чинили (ожог 25.07 и 30.07.2026).
        # Цена: после долгого простоя коуч ответит на всё, что накопилось.
        application.run_polling(drop_pending_updates=False)


def _parse(value: str, tz: ZoneInfo) -> time:
    hour, _, minute = value.partition(":")
    return time(hour=int(hour), minute=int(minute or 0), tzinfo=tz)


if __name__ == "__main__":
    CoachBot().run()
