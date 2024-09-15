# pylint: disable=missing-docstring

from typing import Generic, TypeVar

from pydantic import BaseModel

from streamfarer.services import AuthorizationError, Twitch
from streamfarer.util import WebAPI

from .test_bot import TestCase

T = TypeVar('T')

class ServiceAdapaterTest(TestCase):
    async def test_connect(self) -> None:
        service = await self.bot.local.connect()
        self.assertEqual(self.bot.get_services(), [service]) # type: ignore[misc]

class TwitchTestCase(TestCase):
    _API_PORT = 16160

    class _Page(BaseModel, Generic[T]): # type: ignore[misc]
        data: list[T]

    class _Client(BaseModel): # type: ignore[misc]
        ID: str
        Secret: str

    class _User(BaseModel): # type: ignore[misc]
        id: str

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()

        self._api_process = await Twitch.start_cli('mock-api', 'start', f'--port={self._API_PORT}',
                                                   signal='Mock server started')
        units = WebAPI(f'http://localhost:{self._API_PORT}/units/')

        clients = TwitchTestCase._Page[TwitchTestCase._Client].model_validate(
            await units.call('GET', 'clients'))
        client = clients.data[0]
        self.client_id = client.ID
        self.client_secret = client.Secret

        users = TwitchTestCase._Page[TwitchTestCase._User].model_validate(
            await units.call('GET', 'users'))
        self.code = users.data[0].id

        self.redirect_uri = ''
        self.oauth_url = f'http://localhost:{self._API_PORT}/auth/'

    async def asyncTearDown(self) -> None:
        self._api_process.terminate()
        await self._api_process.wait()
        await super().asyncTearDown()

class TwitchAdapterTest(TwitchTestCase):
    async def test_authorize(self) -> None:
        twitch = await self.bot.twitch.authorize(self.client_id, self.client_secret, self.code,
                                                 self.redirect_uri, self.oauth_url)
        self.assertEqual(twitch.oauth_url, self.oauth_url)
        self.assertEqual(twitch.client_id, self.client_id)
        self.assertEqual(twitch.client_secret, self.client_secret)
        self.assertTrue(twitch.token)

    async def test_authorize_invalid_code(self) -> None:
        with self.assertRaises(AuthorizationError):
            await self.bot.twitch.authorize('foo', 'foo', 'foo', self.redirect_uri, self.oauth_url)

    async def test_authorize_communication_problem(self) -> None:
        with self.assertRaises(OSError):
            await self.bot.twitch.authorize(self.client_id, self.client_secret, self.code,
                                            self.redirect_uri, 'https://example.invalid/')
