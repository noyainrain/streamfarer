# pylint: disable=missing-docstring

import logging
from tempfile import NamedTemporaryFile
from unittest import IsolatedAsyncioTestCase

from streamfarer.bot import Bot

class TestCase(IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        logging.disable()

    async def asyncSetUp(self) -> None:
        self._f = NamedTemporaryFile()
        self.bot = Bot(database_url=self._f.name)
        self.local = await self.bot.local.connect()

    async def asyncTearDown(self) -> None:
        self._f.close()
