# pylint: disable=missing-docstring

from asyncio import create_task, sleep
from datetime import UTC, datetime, timedelta
import logging
from tempfile import NamedTemporaryFile
from unittest import IsolatedAsyncioTestCase

from streamfarer.bot import Bot, MessageEvent
from streamfarer.core import Event
from streamfarer.journey import OngoingJourneyError
from streamfarer.services import LocalService, Message
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
        self._now = datetime(2024, 9, 13, tzinfo=UTC)

        self.local = await self.bot.local.connect()
        for channel in await self.local.get_channels():
            await self.local.delete_channel(channel.url)
        self.channel = await self.local.create_channel('Frank')
        self.category = 'Just Catting'
        await self.local.play(self.channel.url, self.category)

    async def asyncTearDown(self) -> None:
        await self.events.aclose() # type: ignore[misc]
        self._f.close()

    def now(self) -> datetime:
        return self._now

    def tick(self) -> datetime:
        self._now += timedelta(minutes=1)
        return self._now

class BotTestCase(TestCase):
    async def test_run(self) -> None:
        next_channel = await self.local.create_channel('Misha')
        await self.local.play(next_channel.url, self.category)
        await self.bot.start_journey(self.channel.url, 'Roaming')

        task = create_task(self.bot.run())
        try:
            # Let the task start up
            await sleep(0)

            await self.bot.message('Meow!')
            event: Event = await anext(self.events)
            self.assertEqual(
                event,
                MessageEvent(
                    type='message',
                    message=Message(frm='Streamfarer', to=self.channel.name, text='Meow!')))

            await self.local.ban(self.channel.url)
            event = await anext(self.events)
            assert isinstance(event, MessageEvent)
            self.assertFalse(event.message.frm)
            self.assertEqual(event.message.to, self.channel.name)

            self.tick()
            await self.local.raid(self.channel.url, next_channel.url)
            event = await anext(self.events)
            self.assertEqual(event.type, 'journey-travel-on')

            await self.local.stop(next_channel.url)
            event = await anext(self.events)
            self.assertEqual(event.type, 'journey-end')

            await self.local.play(next_channel.url, self.category)
            event = await anext(self.events)
            self.assertEqual(event.type, 'journey-resume')
        finally:
            await cancel(task)

    async def test_start_journey(self) -> None:
        journey = await self.bot.start_journey(self.channel.url, 'Roaming',
                                               description='An adventure.')

        self.assertEqual(journey.title, 'Roaming')
        self.assertEqual(journey.description, 'An adventure.')
        self.assertEqual(journey.start_time, self.now())
        self.assertIsNone(journey.end_time)
        self.assertFalse(journey.deleted)
        self.assertIn(journey, self.bot.get_journeys())
        self.assertEqual(journey, self.bot.get_journey(journey.id))

        stays = journey.get_stays()
        self.assertEqual(len(stays), 1)
        stay = stays[0]
        self.assertEqual(stay.channel, self.channel)
        self.assertEqual(stay.category, self.category)
        self.assertEqual(stay.start_time, self.now())
        self.assertIsNone(stay.end_time)
        self.assertEqual(stay.get_journey(), journey)

    async def test_start_journey_ongoing(self) -> None:
        await self.bot.start_journey(self.channel.url, 'Roaming')
        with self.assertRaises(OngoingJourneyError):
            await self.bot.start_journey(self.channel.url, 'Roaming')

    def test_get_service_at(self) -> None:
        service = self.bot.get_service_at(LocalService.url)
        self.assertEqual(service, self.local)

    async def test_stream(self) -> None:
        stream = await self.bot.stream(self.channel.url)
        self.assertEqual(stream.channel, self.channel)

    async def test_stream_unknown_channel(self) -> None:
        with self.assertRaises(LookupError):
            await self.bot.stream('foo')
