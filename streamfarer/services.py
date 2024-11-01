"""Livestreaming services."""

from __future__ import annotations

from asyncio import Queue, create_subprocess_exec, timeout
from asyncio.subprocess import Process, DEVNULL, PIPE
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
import errno
from functools import partial
from http import HTTPStatus
from logging import getLogger
from types import TracebackType
from typing import Annotated, ClassVar, Generic, Literal, ParamSpec, Self, TypeVar
from urllib.parse import quote

from pydantic import BaseModel, Discriminator, Tag, TypeAdapter, ValidationError, validate_call
from tornado.websocket import WebSocketClientConnection, websocket_connect

from . import context
from .core import Text
from .util import WebAPI

P = ParamSpec('P')
S = TypeVar('S', bound='Service[Stream]')
L_co = TypeVar('L_co', bound='Stream', covariant=True)

_T = TypeVar('_T')

class AuthenticationError(Exception):
    """Raised when authentication with a livestreaming service fails."""

class AuthorizationError(Exception):
    """Raised when getting authorization from a livestreaming service fails."""

class Channel(BaseModel): # type: ignore[misc]
    """Live stream channel.

    .. attribute:: url

       URL of the channel.

    .. attribute:: name

       Channel name.
    """

    url: str
    name: Text

class Stream(AbstractAsyncContextManager['Stream']):
    """Live stream.

    Any operation may raise an :exc:`AuthenticationError` if authentication fails or an
    :exc:`OSError` if there is a problem communicating with the livestreaming service.

    .. attribute:: channel

       Related channel.

    .. attribute:: service

       Livestreaming service.
    """

    channel: Channel
    service: Service[Stream]

    class Event(BaseModel): # type: ignore[misc]
        """Live stream event."""

    def __init__(self, channel: Channel, service: Service[Stream]) -> None:
        self.channel = channel
        self.service = service

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Event:
        raise NotImplementedError()

    async def aclose(self) -> None:
        """Close the stream."""
        raise NotImplementedError()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None,
        traceback: TracebackType | None
    ) -> None:
        await self.aclose()

    async def stop(self) -> None:
        """Stop the stream broadcast.

        If the stream does not support the utility, a :exc:`RuntimeError` is raised.
        """
        raise RuntimeError('Unsupported operation')

class Service(BaseModel, Generic[L_co]): # type: ignore[misc]
    """Connected livestreaming service.

    Any operation may raise an :exc:`AuthenticationError` if authentication fails or an
    :exc:`OSError` if there is a problem communicating with the service.

    .. attribute:: type

       Service type.
    """

    type: str

    async def stream(self, channel_url: str) -> L_co:
        """Open the live stream at the given *channel_url*.

        If required, the service is automatically reconnected.
        """
        service = self
        while True:
            try:
                return await service.open_stream(channel_url)
            except AuthenticationError as e:
                try:
                    service = await self.reconnect()
                    getLogger(__name__).info('Reconnected the livestreaming service')
                except AuthorizationError:
                    raise e from None

    async def open_stream(self, channel_url: str) -> L_co:
        """Open the live stream at the given *channel_url*."""
        raise NotImplementedError()

    async def reconnect(self) -> Self:
        """Reconnect the expired service.

        If getting authorization fails, an :exc:`AuthorizationError` is raised. If there is a
        problem communicating with the service, an :exc:`OSError` is raised.
        """
        service = await self.reauthorize()
        with context.bot.get().transaction() as db:
            data: dict[str, object] = service.model_dump()
            columns = ', '.join(f'{name} = ?' for name in data)
            db.execute(f'UPDATE services SET {columns} WHERE type = ?', (*data.values(), self.type))
        return service

    async def reauthorize(self) -> Self:
        """Refresh authorization and construct the service."""
        raise NotImplementedError()

    async def get_channels(self) -> list[Channel]:
        """Get all live stream channels.

        If the service does not support the utility, a :exc:`RuntimeError` is raised.
        """
        raise RuntimeError('Unsupported operation')

    async def create_channel(self, name: Text) -> Channel:
        """Create a live stream channel with the given *name*.

        If the service does not support the utility, a :exc:`RuntimeError` is raised.
        """
        raise RuntimeError('Unsupported operation')

    async def delete_channel(self, channel_url: str) -> None:
        """Delete the live stream channel at the given *channel_url*.

        If the service does not support the utility, a :exc:`RuntimeError` is raised.
        """
        raise RuntimeError('Unsupported operation')

    async def play(self, channel_url: str) -> L_co:
        """Broadcast a live stream at the given *channel_url*.

        If the service does not support the utility, a :exc:`RuntimeError` is raised.
        """
        raise RuntimeError('Unsupported operation')

class ServiceAdapter(Generic[P, S]):
    """Livestreaming service adapter."""

    async def connect(self, *args: P.args, **kwargs: P.kwargs) -> S:
        """Connect a livestreaming service.

        If getting authorization fails, an :exc:`AuthorizationError` is raised. If there is a
        problem communicating with the livestreaming service, an :exc:`OSError` is raised.
        """
        service = await self.authorize(*args, **kwargs)
        with context.bot.get().transaction() as db:
            data: dict[str, object] = service.model_dump()
            names = ', '.join(data)
            values = list(data.values())
            placeholders = ', '.join(['?'] * len(data))
            db.execute(f'INSERT OR REPLACE INTO services ({names}) VALUES ({placeholders})', values)
        return service

    async def authorize(self, *args: P.args, **kwargs: P.kwargs) -> S:
        """Get authorization and construct the service."""
        raise NotImplementedError()

class LocalStream(Stream):
    """Local live stream."""

    service: LocalService

    def __init__(self, channel: Channel, service: LocalService,
                 _delete: Callable[[], None]) -> None:
        super().__init__(channel, service)
        self._events: Queue[Stream.Event | Exception] = Queue()
        self._error: Exception | None = None
        self._delete = _delete

    async def __anext__(self) -> Stream.Event:
        event = self._error or await self._events.get()
        if isinstance(event, Exception):
            self._error = event
            raise event
        return event

    async def aclose(self) -> None:
        self._events.put_nowait(ConnectionResetError(errno.ECONNRESET, 'Closed stream'))

    async def stop(self) -> None:
        self._events.put_nowait(StopAsyncIteration())
        self._delete()

class LocalService(Service[LocalStream]): # type: ignore[misc]
    """Local livestreaming service."""

    # Work around Pylint missing docstrings from a generic parent (see
    # https://github.com/pylint-dev/pylint/issues/9766)
    # pylint: disable=missing-function-docstring

    _channels: ClassVar[dict[str, Channel]] = {}
    _streams: ClassVar[dict[str, LocalStream]] = {}

    type: Literal['local']

    async def open_stream(self, channel_url: str) -> LocalStream:
        return self._streams[channel_url]

    async def get_channels(self) -> list[Channel]:
        return list(self._channels.values())

    @validate_call # type: ignore[misc]
    async def create_channel(self, name: Text) -> Channel:
        channel = Channel(url=f'streamfarer:{quote(name)}', name=name)
        self._channels[channel.url] = channel
        return channel

    async def delete_channel(self, channel_url: str) -> None:
        try:
            await self._streams[channel_url].stop()
        except KeyError:
            pass
        self._channels.pop(channel_url, None)

    async def play(self, channel_url: str) -> LocalStream:
        stream = LocalStream(channel=self._channels[channel_url], service=self,
                             _delete=partial(self._delete_stream, channel_url))
        self._streams[channel_url] = stream
        return stream

    def _delete_stream(self, channel_url: str) -> None:
        self._streams.pop(channel_url, None)

    def __str__(self) -> str:
        return '📺 Local livestreaming service'

class LocalServiceAdapter(ServiceAdapter[[], LocalService]):
    """Local livestreaming service adapter."""

    async def authorize(self) -> LocalService:
        return LocalService(type='local')

class _TwitchToken(BaseModel): # type: ignore[misc]
    access_token: str
    refresh_token: str

class _TwitchMessage(BaseModel): # type: ignore[misc]
    class _Metadata(BaseModel): # type: ignore[misc]
        message_type: str

    metadata: _Metadata

class _TwitchSessionPayload(BaseModel): # type: ignore[misc]
    class _Session(BaseModel): # type: ignore[misc]
        id: str
        keepalive_timeout_seconds: int

    session: _Session

class _TwitchNotificationPayload(BaseModel): # type: ignore[misc]
    pass

class _TwitchStreamOfflinePayload(_TwitchNotificationPayload): # type: ignore[misc]
    pass

_AnyTwitchNotificationPayload = _TwitchStreamOfflinePayload

class _TwitchWelcomeMessage(_TwitchMessage): # type: ignore[misc]
    payload: _TwitchSessionPayload

class _TwitchReconnectMessage(_TwitchMessage): # type: ignore[misc]
    payload: _TwitchSessionPayload

class _TwitchKeepaliveMessage(_TwitchMessage): # type: ignore[misc]
    pass

class _TwitchNotificationMessage(_TwitchMessage): # type: ignore[misc]
    payload: _AnyTwitchNotificationPayload

def _twitch_message_type(data: dict[str, object]) -> str:
    metadata = data.get('metadata')
    message_type = metadata.get('message_type') if isinstance(metadata, dict) else None
    return message_type if isinstance(message_type, str) else ''

_AnyTwitchMessage = Annotated[
    Annotated[_TwitchWelcomeMessage, Tag('session_welcome')] |
    Annotated[_TwitchReconnectMessage, Tag('session_reconnect')] |
    Annotated[_TwitchKeepaliveMessage, Tag('session_keepalive')] |
    Annotated[_TwitchNotificationMessage, Tag('notification')],
    Discriminator(_twitch_message_type)
]
_AnyTwitchMessageModel: TypeAdapter[_AnyTwitchMessage] = TypeAdapter(_AnyTwitchMessage)

async def _post_twitch_token(oauth: WebAPI, endpoint: str, *, client_id: str, client_secret: str,
                             grant_type: str, **args: str) -> _TwitchToken:
    try:
        return _TwitchToken.model_validate(
            await oauth.call(
                'POST', endpoint,
                query={
                    'client_id': client_id,
                    'client_secret': client_secret,
                    'grant_type': grant_type,
                    **args
                }))
    except WebAPI.Error as e:
        if e.status == HTTPStatus.BAD_REQUEST:
            raise AuthorizationError(f'Failed authorization for {client_id}') from e
        raise

async def _read_twitch_message(websocket: WebSocketClientConnection, *,
                               keepalive: float = 0) -> _TwitchMessage:
    async with timeout(keepalive + 20):
        data = await websocket.read_message()
    if data is None:
        raise ConnectionResetError(errno.ECONNRESET, 'Closed stream')
    try:
        return _AnyTwitchMessageModel.validate_json(data)
    except ValidationError as e:
        error = OSError(errno.EPROTO, "Bad message")
        error.add_note(data.decode(errors='replace') if isinstance(data, bytes) else data)
        raise error from e

class TwitchStream(Stream):
    """Twitch live stream."""

    service: Twitch

    def __init__(self, channel: Channel, service: Twitch, _websocket: WebSocketClientConnection,
                 _keepalive: float, _channel_id: str) -> None:
        super().__init__(channel, service)
        self._websocket = _websocket
        self._keepalive = _keepalive
        self._eof = False
        self._channel_id = _channel_id

    async def __anext__(self) -> Stream.Event:
        while not self._eof:
            message = await _read_twitch_message(self._websocket, keepalive=self._keepalive)
            match message:
                case _TwitchNotificationMessage():
                    match message.payload:
                        case _TwitchStreamOfflinePayload():
                            self._eof = True
                            await self.aclose()
                        case _:
                            assert False
                case _TwitchKeepaliveMessage():
                    pass
                case _TwitchReconnectMessage():
                    await self.aclose()
                case _:
                    raise OSError(errno.EPROTO,
                                  f"Unexpected message type {message.metadata.message_type}")
        raise StopAsyncIteration()

    async def aclose(self) -> None:
        self._websocket.close()

    async def stop(self) -> None:
        if self.service.api_url == TwitchAdapter.PRODUCTION_API_URL:
            raise RuntimeError('Unsupported operation')
        await Twitch.cli('event', 'trigger', '--transport=websocket',
                         f'--to-user={self._channel_id}', 'streamdown')

class Twitch(Service[TwitchStream]): # type: ignore[misc]
    """Twitch connection."""

    # Work around Pylint missing docstrings from a generic parent (see
    # https://github.com/pylint-dev/pylint/issues/9766)
    # pylint: disable=missing-function-docstring

    type: Literal['twitch']
    """Web API URL."""
    api_url: str
    """OAuth URL."""
    oauth_url: str
    """EventSub API URL."""
    eventsub_url: str
    """EventSub WebSocket URL."""
    websocket_url: str
    """Application client ID."""
    client_id: str
    """Application client secret."""
    client_secret: str
    """Access token."""
    token: str
    """Refresh token."""
    refresh_token: str

    class _Page(BaseModel, Generic[_T]): # type: ignore[misc]
        data: list[_T]

    class _User(BaseModel): # type: ignore[misc]
        id: str
        display_name: str

    @staticmethod
    async def start_cli(*args: str, signal: str | None = None) -> Process:
        """Plumbing: Start Twitch CLI with command-line arguments *args*.

        Optionally, a start *signal* is awaited on stderr.

        If there is a problem starting Twitch CLI, an :exc:`OSError` is raised.
        """
        process = await create_subprocess_exec('twitch', *args, stdout=DEVNULL, stderr=PIPE)
        if signal:
            assert process.stderr
            async for line in process.stderr:
                if signal in line.decode():
                    break
        return process

    @staticmethod
    async def cli(*args: str) -> None:
        """Plumbing: Execute a Twitch CLI command with command-line arguments *args*.

        If there is a problem running Twitch CLI, an :exc:`OSError` is raised.
        """
        process = await Twitch.start_cli(*args)
        await process.communicate()
        if process.returncode != 0:
            raise OSError(errno.EINVAL, 'Error {process.returncode}')

    async def open_stream(self, channel_url: str) -> TwitchStream:
        login = channel_url.removeprefix('https://www.twitch.tv/')
        if login == channel_url:
            raise LookupError(channel_url)
        api = WebAPI(self.api_url,
                     headers={'Authorization': f'Bearer {self.token}', 'Client-Id': self.client_id})
        eventsub = WebAPI(self.eventsub_url, headers=api.headers)

        try:
            users = Twitch._Page[Twitch._User].model_validate(
                await api.call("GET", "users", query={'login': login}))
            try:
                user = users.data[0]
            except IndexError:
                raise LookupError(channel_url) from None

            websocket = await websocket_connect(self.websocket_url)
            try:
                message = await _read_twitch_message(websocket)
                if not isinstance(message, _TwitchWelcomeMessage):
                    raise OSError(errno.EPROTO,
                                  f"Unexpected message type {message.metadata.message_type}")

                await eventsub.call(
                    'POST', 'subscriptions',
                    data=self._subscription('stream.offline', {'broadcaster_user_id': user.id},
                                            message.payload.session.id))

                streams = Twitch._Page[object].model_validate(
                    await api.call('GET', 'streams', query={'user_id': user.id}))
                if not streams.data:
                    raise LookupError(channel_url)

                return TwitchStream(
                    channel=Channel(url=channel_url, name=user.display_name), service=self,
                    _websocket=websocket,
                    _keepalive=message.payload.session.keepalive_timeout_seconds,
                    _channel_id=user.id)
            except:
                websocket.close()
                raise
        except WebAPI.Error as e:
            if e.status == HTTPStatus.UNAUTHORIZED:
                raise AuthenticationError(f'Failed authentication of {self.client_id}') from e
            raise

    def _subscription(self, subscription_type: str, condition: dict[str, str],
                      session_id: str) -> dict[str, object]:
        return {
            'type': subscription_type,
            'version': '1',
            'condition': condition,
            'transport': {'method': 'websocket', 'session_id': session_id}
        }

    async def reauthorize(self) -> Self:
        token = await _post_twitch_token(
            WebAPI(self.oauth_url), 'token', client_id=self.client_id,
            client_secret=self.client_secret, grant_type='refresh_token',
            refresh_token=self.refresh_token)
        return self.copy(
            update={ # type: ignore[misc]
                'token': token.access_token,
                'refresh_token': token.refresh_token
            })

    def __str__(self) -> str:
        return f'📺 Twitch via application {self.client_id}'

class TwitchAdapter(
    ServiceAdapter[[str, str, str, str, str | None, str | None, str | None, str | None], Twitch]
):
    """Twitch adapter.

    *code* is an authorization code obtained via the *redirect_uri*. For a mock server, it is the ID
    of the authorizing user.

    .. attribute:: PRODUCTION_API_URL

       Production web API URL.
    """

    PRODUCTION_API_URL = 'https://api.twitch.tv/helix/'

    _PRODUCTION_OAUTH_URL = 'https://id.twitch.tv/oauth2/'

    async def authorize(
        self, client_id: str, client_secret: str, code: str, redirect_uri: str, api_url: str | None,
        oauth_url: str | None, eventsub_url: str | None, websocket_url: str | None
    ) -> Twitch:
        oauth = WebAPI(oauth_url or self._PRODUCTION_OAUTH_URL)
        if oauth.url == self._PRODUCTION_OAUTH_URL:
            token = await _post_twitch_token(
                oauth, 'token', client_id=client_id, client_secret=client_secret,
                grant_type='authorization_code', code=code, redirect_uri=redirect_uri)
        else:
            token = await _post_twitch_token(
                oauth, 'authorize', client_id=client_id, client_secret=client_secret,
                grant_type='user_token', user_id=code)

        return Twitch(
            type='twitch', api_url=api_url or self.PRODUCTION_API_URL, oauth_url=oauth.url,
            eventsub_url=eventsub_url or 'https://api.twitch.tv/helix/eventsub/',
            websocket_url=websocket_url or 'wss://eventsub.wss.twitch.tv/ws',
            client_id=client_id, client_secret=client_secret, token=token.access_token,
            refresh_token=token.refresh_token)
