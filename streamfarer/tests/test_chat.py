# pylint: disable=missing-docstring

from asyncio import create_task

from streamfarer.bot import MessageEvent
from streamfarer.chat import chat
from streamfarer.core import Event
from streamfarer.util import cancel

from .test_bot import TestCase

class ChatTest(TestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.bot.start_journey(self.channel.url, 'Roaming')
        self._run_task = create_task(self.bot.run())
        self.addAsyncCleanup(cancel, self._run_task)
        self._chat_task = create_task(chat(intro_delay=0))
        self.addAsyncCleanup(cancel, self._chat_task)

    async def test_on_journey_travel_on(self) -> None:
        next_channel = await self.local.create_channel('Misha')
        await self.local.play(next_channel.url, self.category)

        self.tick()
        await self.local.raid(self.channel.url, next_channel.url)
        await anext(self.events) # type: ignore[misc]
        event: Event = await anext(self.events)
        assert isinstance(event, MessageEvent)
        self.assertIn('Ahoy!', event.message.text)
