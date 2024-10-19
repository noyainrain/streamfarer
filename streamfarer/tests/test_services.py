# pylint: disable=missing-docstring

from contextlib import AbstractContextManager
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

from streamfarer.services import AuthorizationError, Channel, Service, Stream, Twitch
from streamfarer.util import WebAPI

from .test_bot import TestCase

T = TypeVar('T')

class ServiceTestProtocol(Protocol):
    service: Service[Stream]
    channel: Channel
    offline_channel: Channel

    # pylint: disable=invalid-name
    def assertEqual(self, first: object, second: object) -> None: ...
    def assertRaises(self,
                     expected_exception: type[BaseException]) -> AbstractContextManager[object]: ...

class WithStreamTests:
    async def test_anext(self: ServiceTestProtocol) -> None:
        stream = await self.service.stream(self.channel.url)
        async with stream:
            await stream.stop()
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)

    async def test_anext_closed_stream(self: ServiceTestProtocol) -> None:
        stream = await self.service.stream(self.channel.url)
        await stream.aclose()
        with self.assertRaises(ConnectionResetError):
            await anext(stream)

class WithServiceTests:
    async def test_stream(self: ServiceTestProtocol) -> None:
        stream = await self.service.stream(self.channel.url)
        async with stream:
            self.assertEqual(stream.channel, self.channel)

    async def test_stream_unknown_channel(self: ServiceTestProtocol) -> None:
        with self.assertRaises(LookupError):
            await self.service.stream('foo')

    async def test_stream_offline_channel(self: ServiceTestProtocol) -> None:
        with self.assertRaises(LookupError):
            await self.service.stream(self.offline_channel.url)

class ServiceAdapaterTest(TestCase):
    async def test_connect(self) -> None:
        service = await self.bot.local.connect()
        self.assertEqual(self.bot.get_services(), [service]) # type: ignore[misc]

class LocalStreamTest(TestCase, WithStreamTests):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.service = self.local
        self.offline_channel = await self.local.create_channel('Misha')

class LocalServiceTest(TestCase, WithServiceTests):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.service = self.local
        self.offline_channel = await self.local.create_channel('Misha')

class TwitchTestCase(TestCase):
    _API_PORT = 16160
    _WEBSOCKET_PORT = 16161

    class _Page(BaseModel, Generic[T]): # type: ignore[misc]
        data: list[T]

    class _Client(BaseModel): # type: ignore[misc]
        ID: str
        Secret: str

    class _User(BaseModel): # type: ignore[misc]
        id: str
        login: str
        display_name: str

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()

        self._api_process = await Twitch.start_cli('mock-api', 'start', f'--port={self._API_PORT}',
                                                   signal='Mock server started')
        self._websocket_process = await Twitch.start_cli(
            'event', 'websocket', 'start-server', f'--port={self._WEBSOCKET_PORT}',
            '--require-subscription', signal='Started WebSocket server')
        units = WebAPI(f'http://localhost:{self._API_PORT}/units/')

        clients = TwitchTestCase._Page[TwitchTestCase._Client].model_validate(
            await units.call('GET', 'clients'))
        client = clients.data[0]
        self.client_id = client.ID
        self.client_secret = client.Secret

        users = TwitchTestCase._Page[TwitchTestCase._User].model_validate(
            await units.call('GET', 'users'))
        user, offline_user = users.data[:2]
        self.code = user.id
        self.channel = Channel(url=f'https://www.twitch.tv/{user.login}', name=user.display_name)
        self.offline_channel = Channel(url=f'https://www.twitch.tv/{offline_user.login}',
                                       name=offline_user.display_name)

        self.redirect_uri = ''
        self.api_url = f'http://localhost:{self._API_PORT}/mock/'
        self.oauth_url = f'http://localhost:{self._API_PORT}/auth/'
        self.eventsub_url=f'http://localhost:{self._WEBSOCKET_PORT}/eventsub/'
        self.websocket_url=f'ws://localhost:{self._WEBSOCKET_PORT}/ws'

    async def asyncTearDown(self) -> None:
        self._websocket_process.terminate()
        await self._websocket_process.communicate()
        self._api_process.terminate()
        await self._api_process.communicate()
        await super().asyncTearDown()

class TwitchStreamTest(TwitchTestCase, WithStreamTests):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.service = await self.bot.twitch.connect(
            self.client_id, self.client_secret, self.code, self.redirect_uri, self.api_url,
            self.oauth_url, self.eventsub_url, self.websocket_url)

class TwitchTest(TwitchTestCase, WithServiceTests):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.service = await self.bot.twitch.connect(
            self.client_id, self.client_secret, self.code, self.redirect_uri, self.api_url,
            self.oauth_url, self.eventsub_url, self.websocket_url)

class TwitchAdapterTest(TwitchTestCase):
    async def test_authorize(self) -> None:
        twitch = await self.bot.twitch.authorize(
            self.client_id, self.client_secret, self.code, self.redirect_uri, self.api_url,
            self.oauth_url, self.eventsub_url, self.websocket_url)
        self.assertEqual(twitch.oauth_url, self.oauth_url)
        self.assertEqual(twitch.client_id, self.client_id)
        self.assertEqual(twitch.client_secret, self.client_secret)
        self.assertTrue(twitch.token)

    async def test_authorize_invalid_code(self) -> None:
        with self.assertRaises(AuthorizationError):
            await self.bot.twitch.authorize('foo', 'foo', 'foo', self.redirect_uri, self.api_url,
                                            self.oauth_url, self.eventsub_url, self.websocket_url)

    async def test_authorize_communication_problem(self) -> None:
        with self.assertRaises(OSError):
            await self.bot.twitch.authorize(
                self.client_id, self.client_secret, self.code, self.redirect_uri, self.api_url,
                'https://example.invalid/', self.eventsub_url, self.websocket_url)
