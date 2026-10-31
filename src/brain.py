"""Синхронизация мозга: та же папка памяти, что у коуча в Claude Code.

Перед разговором подтягиваем свежее, после — отдаём написанное. Мозг один,
каналов два, поэтому расходиться им нельзя.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class Brain:
    def __init__(self, repo_dir: Path) -> None:
        self.repo_dir = repo_dir

    async def _git(self, *args: str, check: bool = False) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(self.repo_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        text = output.decode("utf-8", "replace").strip()
        code = process.returncode or 0
        if code and check:
            raise RuntimeError(f"git {' '.join(args)}: {text}")
        return code, text

    async def pull(self) -> None:
        """Подтянуть чужие правки. Свои локальные при конфликте не теряем."""
        code, text = await self._git("pull", "--rebase", "--autostash")
        if code:
            log.warning("git pull не прошёл: %s", text)
            await self._git("rebase", "--abort")

    async def dirty(self) -> bool:
        """Есть ли что отдавать: незакоммиченное или неотправленные коммиты.

        Нужно, чтобы отличить «отдавать было нечего» от «отдать не смогли»:
        `push` возвращает False в обоих случаях, а поднимать задачу человеку
        надо только во втором.
        """
        _, status = await self._git("status", "--porcelain")
        if status:
            return True
        code, ahead = await self._git("rev-list", "--count", "@{u}..HEAD")
        return bool(code == 0 and ahead.strip() not in ("", "0"))

    async def push(self, reason: str) -> bool:
        """Закоммитить и отправить, если что-то поменялось или застряло.

        Застрявшее — коммит прошлой ночи, который не уехал (поломка #233,
        18.09.2026). Раньше `push` смотрел только на незакоммиченное, и в тихую
        ночь без правок застрявший коммит не пробовали отправить вовсе, а
        `dirty()` всё равно поднимал задачу — тревога без попытки починиться.
        """
        _, status = await self._git("status", "--porcelain")
        if status:
            await self._git("add", "-A")
            await self._git("commit", "-m", f"Коуч (телеграм): {reason}")
        elif not await self.dirty():
            return False
        code, text = await self._git("push")
        if code:
            log.warning("git push не прошёл: %s", text)
            return False
        log.info("мозг обновлён: %s", reason)
        return True
