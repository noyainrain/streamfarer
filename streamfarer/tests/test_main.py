# pylint: disable=missing-docstring

from asyncio import create_task
from contextlib import redirect_stdout
from contextvars import Context
from io import StringIO

from streamfarer.__main__ import main

from .test_bot import TestCase

class MainTest(TestCase):
    async def main(self, *args: str) -> tuple[int, str]:
        with redirect_stdout(StringIO()) as f:
            code = await create_task(
                main('python3 -m streamfarer', f'--database-url={self.bot.database_url}', *args),
                context=Context())
        return code, f.getvalue()

    async def test_service(self) -> None:
        code, stdout = await self.main('service')
        self.assertEqual(code, 0)
        self.assertIn('📺 Local livestreaming service', stdout)
