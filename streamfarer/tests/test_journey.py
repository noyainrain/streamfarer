# pylint: disable=missing-docstring

from streamfarer.journey import OngoingJourneyError

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

    def test_end(self) -> None:
        journey = self.journey.end()
        self.assertEqual(journey.end_time, self.now())
        stays = journey.get_stays()
        self.assertTrue(stays)
        self.assertEqual(stays[0].end_time, self.now())

    def test_end_deleted_journey(self) -> None:
        self.journey.end()
        self.journey.delete()
        with self.assertRaises(KeyError):
            self.journey.end()

    def test_delete(self) -> None:
        self.journey.end()
        self.journey.delete()
        journey = self.bot.get_journey(self.journey.id)
        self.assertTrue(journey.deleted)
        self.assertNotIn(journey, self.bot.get_journeys())

    def test_delete_ongoing_journey(self) -> None:
        with self.assertRaises(OngoingJourneyError):
            self.journey.delete()
