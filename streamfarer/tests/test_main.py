# pylint: disable=missing-docstring

from asyncio import create_task
from contextlib import redirect_stderr, redirect_stdout
from contextvars import Context
from io import StringIO
from tempfile import NamedTemporaryFile

from streamfarer.__main__ import main

from .test_bot import TestCase

class MainTest(TestCase):
    async def main(self, *args: str) -> tuple[int, str]:
        with (
            redirect_stderr(StringIO()), redirect_stdout(StringIO()) as stdout,
            NamedTemporaryFile() as message_log
        ):
            code = await create_task(
                main('python3 -m streamfarer', f'--database-url={self.bot.database_url}',
                     f'--message-log={message_log.name}', *args),
                context=Context())
        return code, stdout.getvalue()

    async def test_journey(self) -> None:
        journey = await self.bot.start_journey(self.channel.url, 'Roaming')
        code, stdout = await self.main('journey')
        self.assertEqual(code, 0)
        self.assertIn(journey.id, stdout)

    async def test_start(self) -> None:
        code, _ = await self.main('start', self.channel.url, 'Roaming')
        self.assertEqual(code, 0)
        self.assertTrue(self.bot.get_journeys())

    async def test_edit(self) -> None:
        journey = await self.bot.start_journey(self.channel.url, 'Roaming')
        code, _ = await self.main('edit', journey.id, 'Drifting')
        self.assertEqual(code, 0)
        self.assertEqual(self.bot.get_journey(journey.id).title, 'Drifting')

    async def test_end(self) -> None:
        journey = await self.bot.start_journey(self.channel.url, 'Roaming')
        code, _ = await self.main('end')
        self.assertEqual(code, 0)
        self.assertTrue(self.bot.get_journey(journey.id).end_time)

    async def test_delete(self) -> None:
        journey = await self.bot.start_journey(self.channel.url, 'Roaming')
        journey.end()
        code, _ = await self.main('delete', journey.id)
        self.assertEqual(code, 0)
        self.assertTrue(self.bot.get_journey(journey.id).deleted)

    async def test_service(self) -> None:
        code, stdout = await self.main('service')
        self.assertEqual(code, 0)
        self.assertIn(self.local.name, stdout)
