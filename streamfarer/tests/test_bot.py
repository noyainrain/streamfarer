# pylint: disable=missing-docstring

from asyncio import create_task
from datetime import datetime, timezone
import logging
from tempfile import NamedTemporaryFile
from unittest import IsolatedAsyncioTestCase

from streamfarer.bot import Bot
from streamfarer.journey import OngoingJourneyError
from streamfarer.util import cancel

class TestCase(IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        logging.disable()

    async def asyncSetUp(self) -> None:
        # pylint: disable=consider-using-with
        self._f = NamedTemporaryFile()
        self.bot = Bot(database_url=self._f.name, now=self.now)
        self.events = self.bot.events()

        self.local = await self.bot.local.connect()
        for channel in await self.local.get_channels():
            await self.local.delete_channel(channel.url)
        self.channel = await self.local.create_channel('Frank')
        self.stream = await self.local.play(self.channel.url)

    async def asyncTearDown(self) -> None:
        await self.events.aclose() # type: ignore[misc]
        self._f.close()

    def now(self) -> datetime:
        return datetime(2024, 9, 13, tzinfo=timezone.utc)

class BotTestCase(TestCase):
    async def test_run(self) -> None:
        await self.bot.start_journey(self.channel.url, 'Roaming')
        task = create_task(self.bot.run())
        try:
            await self.stream.stop()
            event = await anext(self.events) # type: ignore[misc]
            self.assertEqual(event.type, 'journey-end')
        finally:
            await cancel(task)

    async def test_start_journey(self) -> None:
        journey = await self.bot.start_journey(self.channel.url, 'Roaming')

        self.assertEqual(journey.title, 'Roaming')
        self.assertEqual(journey.start_time, self.now())
        self.assertIsNone(journey.end_time)
        self.assertFalse(journey.deleted)
        self.assertIn(journey, self.bot.get_journeys())
        self.assertEqual(journey, self.bot.get_journey(journey.id))

        stays = journey.get_stays()
        self.assertEqual(len(stays), 1)
        stay = stays[0]
        self.assertEqual(stay.channel, self.channel)
        self.assertEqual(stay.start_time, self.now())
        self.assertIsNone(stay.end_time)
        self.assertEqual(stay.get_journey(), journey)

    async def test_start_journey_ongoing(self) -> None:
        await self.bot.start_journey(self.channel.url, 'Roaming')
        with self.assertRaises(OngoingJourneyError):
            await self.bot.start_journey(self.channel.url, 'Roaming')

    async def test_stream(self) -> None:
        stream = await self.bot.stream(self.channel.url)
        self.assertEqual(stream.channel, self.channel)

    async def test_stream_unknown_channel(self) -> None:
        with self.assertRaises(LookupError):
            await self.bot.stream('foo')
