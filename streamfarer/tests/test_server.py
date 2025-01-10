# pylint: disable=missing-docstring

from tornado.httpclient import AsyncHTTPClient

from streamfarer.server import serve

from .test_bot import TestCase

class ServerTest(TestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.server = serve(port=16160)

    async def asyncTearDown(self) -> None:
        self.server.close()
        await super().asyncTearDown()

    async def test_get_index(self) -> None:
        await self.bot.start_journey(self.channel.url, 'Roaming')
        response = await AsyncHTTPClient().fetch(self.server.url)
        body = response.body.decode()
        self.assertIn('Roaming', body)
        self.assertIn('Streamfarer', body)
