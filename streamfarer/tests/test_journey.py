# pylint: disable=missing-docstring

from streamfarer.core import Event
from streamfarer.journey import EndedJourneyError, OngoingJourneyError, PastJourneyError

from .test_bot import TestCase

class JourneyTest(TestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.journey = await self.bot.start_journey(self.channel.url, 'Roaming')

    def test_edit(self) -> None:
        journey = self.journey.edit(title='Drifting')
        self.assertEqual(journey.title, 'Drifting')

    def test_edit_deleted_journey(self) -> None:
        self.journey.end()
        self.journey.delete()
        with self.assertRaises(KeyError):
            self.journey.edit(title='Drifting')

    async def test_end(self) -> None:
        journey = self.journey.end()
        self.assertEqual(journey.end_time, self.now())
        event = await anext(self.events) # type: ignore[misc]
        self.assertEqual(event, Event(type='journey-end'))
        self.assertEqual(journey.get_latest_stay().end_time, self.now())

    def test_end_deleted_journey(self) -> None:
        self.journey.end()
        self.journey.delete()
        with self.assertRaises(KeyError):
            self.journey.end()

    async def test_resume(self) -> None:
        self.journey.end()
        await anext(self.events) # type: ignore[misc]

        journey = await self.journey.resume()
        self.assertIsNone(journey.end_time)
        self.assertEqual(await anext(self.events), # type: ignore[misc]
                         Event(type='journey-resume'))
        self.assertIsNone(journey.get_latest_stay().end_time)

    async def test_resume_past_journey(self) -> None:
        self.journey.end()
        self.tick()
        journey = await self.bot.start_journey(self.channel.url, 'Roaming')
        journey.end()

        with self.assertRaises(PastJourneyError):
            await self.journey.resume()

    async def test_resume_deleted_journey(self) -> None:
        self.journey.end()
        self.journey.delete()
        with self.assertRaises(KeyError):
            await self.journey.resume()

    def test_delete(self) -> None:
        self.journey.end()
        self.journey.delete()
        journey = self.bot.get_journey(self.journey.id)
        self.assertTrue(journey.deleted)
        self.assertNotIn(journey, self.bot.get_journeys())

    def test_delete_ongoing_journey(self) -> None:
        with self.assertRaises(OngoingJourneyError):
            self.journey.delete()

    async def test_travel_on(self) -> None:
        channel = await self.local.create_channel('Misha')
        await self.local.play(channel.url, self.category)
        self.tick()

        stay = await self.journey.travel_on(channel.url)
        self.assertEqual(stay.journey_id, self.journey.id)
        self.assertEqual(stay.channel, channel)
        self.assertEqual(stay.category, self.category)
        self.assertEqual(stay.start_time, self.now())
        self.assertIsNone(stay.end_time)
        stays = self.journey.get_stays()
        self.assertEqual(len(stays), 2)
        self.assertIn(stay, stays)
        self.assertEqual(stays[1].end_time, self.now())

    async def test_travel_on_ended_journey(self) -> None:
        channel = await self.local.create_channel('Misha')
        await self.local.play(channel.url, self.category)
        self.journey.end()
        with self.assertRaises(EndedJourneyError):
            await self.journey.travel_on(channel.url)
