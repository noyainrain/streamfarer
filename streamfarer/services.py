"""Livestreaming services."""

from __future__ import annotations

import asyncio
from asyncio import (Future, InvalidStateError, Queue, Task, create_subprocess_exec, create_task,
                     gather, get_running_loop, shield, wait)
from asyncio.subprocess import Process, DEVNULL, PIPE
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
import errno
from functools import partial
from http import HTTPStatus
import json
from logging import getLogger
from types import TracebackType
from typing import (Annotated, ClassVar, Concatenate, Generic, Literal, ParamSpec, Self, TypeAlias,
                    TypeVar)
from urllib.parse import quote_plus, urljoin

from pydantic import BaseModel, Discriminator, Tag, TypeAdapter, ValidationError, validate_call
from tornado.websocket import WebSocketClientConnection, websocket_connect

from . import context
from .core import Text
from .util import WebAPI, amerge

P = ParamSpec('P')
R_co = TypeVar('R_co', covariant=True)
S = TypeVar('S', bound='Service[Stream]')

_T = TypeVar('_T')

class AuthenticationError(Exception):
    """Raised when authentication with a livestreaming service fails."""

class AuthorizationError(Exception):
    """Raised when getting authorization from a livestreaming service fails."""

class StreamTimeoutError(Exception):
    """Raised when awaiting a channel to come online times out."""

class Channel(BaseModel):
    """Live stream channel.

    .. attribute:: url

       URL of the channel.

    .. attribute:: name

       Channel name.

    .. attribute:: image_url

       URL of the channel image.
    """

    url: str
    name: Text
    image_url: str

def authenticated(
    func: Callable[Concatenate[S, P], Awaitable[R_co]]
) -> Callable[Concatenate[S, P], Coroutine[None, None, R_co]]:
    """Decorator for a service operation that requires authentication.

    If an :exc:`AuthenticationError` is raised, the service is reconnected.
    """
    async def wrapper(self: S, /, *args: P.args, **kwargs: P.kwargs) -> R_co:
        while True:
            try:
                return await func(self, *args, **kwargs)
            except AuthenticationError as e:
                try:
                    await self.reconnect()
                    getLogger(__name__).info('Reconnected the livestreaming service')
                except AuthorizationError:
                    raise e from None
    return wrapper

class Stream(AbstractAsyncContextManager['Stream']):
    """Live stream.

    Reading from the stream may raise an :exc:`OSError` if there is a problem communicating with the
    livestreaming service.

    .. attribute:: channel

       Related channel.

    .. attribute: category

       Stream category.

    .. attribute:: service

       Livestreaming service.
    """

    channel: Channel
    category: Text
    service: Service[Stream]

    class Event(BaseModel):
        """Live stream event."""

    class RaidEvent(Event):
        """Dispatched when the stream raids another.

        .. attribute:: target_url

           URL of the target channel.
        """

        target_url: str

    def __init__(self, channel: Channel, category: Text, service: Service[Stream]) -> None:
        self.channel = channel
        self.category = category
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

# Work around the Pydantic mypy plugin failing for type variables defined before their bound class
L_co = TypeVar('L_co', bound=Stream, covariant=True)

class Service(BaseModel, Generic[L_co]):
    """Connected livestreaming service.

    Any operation may raise an :exc:`AuthenticationError` if authentication fails or an
    :exc:`OSError` if there is a problem communicating with the service.

    .. attribute:: type

       Service type.

    .. attribute:: url

       Origin URL of the service.

    .. attribute:: name

       Service name.
    """

    url: ClassVar[str]
    name: ClassVar[str]

    type: str

    @authenticated
    async def stream(self, channel_url: str, *, timeout: float | None = None) -> L_co:
        """Open the live stream at the given *channel_url*.

        Optionally, the channel is awaited to come online with a *timeout* in seconds.
        """
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

    async def get_channel(self, channel_url: str) -> Channel:
        """Get the live stream channel at the given *channel_url*.

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

    async def play(self, channel_url: str, category: Text) -> None:
        """Broadcast a live stream at the given *channel_url* in a *category*.

        If the service does not support the utility, a :exc:`RuntimeError` is raised.
        """
        raise RuntimeError('Unsupported operation')

    async def stop(self, channel_url: str) -> None:
        """Stop the live stream broadcast at the given *channel_url*.

        If the service does not support the utility, a :exc:`RuntimeError` is raised.
        """
        raise RuntimeError('Unsupported operation')

    async def raid(self, channel_url: str, target_url: str) -> None:
        """Raid the live stream at the given *target_url* from the given *channel_url*.

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

    def __init__(self, channel: Channel, category: Text, service: LocalService,
                 _events: Queue[Stream.Event | Exception], _close: Callable[[], None]) -> None:
        super().__init__(channel, category, service)
        self._events = _events
        self._error: Exception | None = None
        self._close = _close

    async def __anext__(self) -> Stream.Event:
        event = self._error or await self._events.get()
        if isinstance(event, Exception):
            self._error = event
            raise event
        return event

    async def aclose(self) -> None:
        self._close()

class LocalService(Service[LocalStream]):
    """Local livestreaming service."""

    # Work around Pylint missing docstrings from a generic parent (see
    # https://github.com/pylint-dev/pylint/issues/9766)
    # pylint: disable=missing-function-docstring

    @dataclass
    class _Broadcast:
        category: Text
        streams: set[Queue[Stream.Event | Exception]]

    url: ClassVar[str] = 'streamfarer:'
    name: ClassVar[str] = 'Local Livestreaming Service'

    _channels: ClassVar[dict[str, Channel]] = {}
    _broadcasts: ClassVar[dict[str, Future[_Broadcast]]] = {}

    type: Literal['local']

    @authenticated
    async def stream(self, channel_url: str, *, timeout: float | None = None) -> LocalStream:
        channel = await self.get_channel(channel_url)
        future = self._broadcasts[channel_url]
        try:
            broadcast = future.result()
        except InvalidStateError:
            if timeout is None:
                raise LookupError(channel_url) from None
            try:
                async with asyncio.timeout(timeout):
                    broadcast = await shield(future)
            except TimeoutError:
                raise StreamTimeoutError() from None

        events: Queue[Stream.Event | Exception] = Queue()
        broadcast.streams.add(events)
        stream = LocalStream(channel=channel, category=broadcast.category, service=self,
                             _events=events, _close=partial(self._close_stream, broadcast, events))
        return stream

    def _close_stream(self, broadcast: _Broadcast, events: Queue[Stream.Event | Exception]) -> None:
        broadcast.streams.discard(events)
        events.put_nowait(ConnectionResetError(errno.ECONNRESET, 'Closed stream'))

    async def get_channels(self) -> list[Channel]:
        return list(self._channels.values())

    async def get_channel(self, channel_url: str) -> Channel:
        return self._channels[channel_url]

    @validate_call # type: ignore[misc]
    async def create_channel(self, name: Text) -> Channel:
        path = quote_plus(name)
        channel = Channel(url=f'streamfarer:{path}', name=name, image_url=f'streamfarer:{path}.png')
        self._channels.setdefault(channel.url, channel)
        future: Future[LocalService._Broadcast] = get_running_loop().create_future()
        self._broadcasts.setdefault(channel.url, future)
        return channel

    async def delete_channel(self, channel_url: str) -> None:
        await self.stop(channel_url)
        self._broadcasts.pop(channel_url, None)
        self._channels.pop(channel_url, None)

    @validate_call # type: ignore[misc]
    async def play(self, channel_url: str, category: Text) -> None:
        try:
            self._broadcasts[channel_url].set_result(
                LocalService._Broadcast(category=category, streams=set()))
        except InvalidStateError:
            pass

    async def stop(self, channel_url: str) -> None:
        try:
            broadcast = self._broadcasts[channel_url].result()
        except (KeyError, InvalidStateError):
            return
        future: Future[LocalService._Broadcast] = get_running_loop().create_future()
        self._broadcasts[channel_url] = future
        for events in broadcast.streams:
            events.put_nowait(StopAsyncIteration())

    async def raid(self, channel_url: str, target_url: str) -> None:
        try:
            broadcast = self._broadcasts[channel_url].result()
        except InvalidStateError:
            raise LookupError(channel_url) from None
        target = await self.get_channel(target_url)
        for events in broadcast.streams:
            events.put_nowait(Stream.RaidEvent(target_url=target.url))

    def __str__(self) -> str:
        return f'📺 {self.name}'

class LocalServiceAdapter(ServiceAdapter[[], LocalService]):
    """Local livestreaming service adapter."""

    async def authorize(self) -> LocalService:
        return LocalService(type='local')

class _TwitchToken(BaseModel):
    access_token: str
    refresh_token: str

class _TwitchMessage(BaseModel):
    class _Metadata(BaseModel):
        message_type: str

    metadata: _Metadata

class _TwitchSessionPayload(BaseModel):
    class _Session(BaseModel):
        id: str
        keepalive_timeout_seconds: int

    session: _Session

class _TwitchWelcomeMessage(_TwitchMessage):
    payload: _TwitchSessionPayload

class _TwitchReconnectMessage(_TwitchMessage):
    payload: _TwitchSessionPayload

class _TwitchKeepaliveMessage(_TwitchMessage):
    pass

class _TwitchNotificationMessage(_TwitchMessage):
    payload: dict[str, object]

def _twitch_message_type(data: dict[str, object]) -> str | None:
    metadata = data.get('metadata')
    message_type = metadata.get('message_type') if isinstance(metadata, dict) else None
    return message_type if isinstance(message_type, str) else None

_AnyTwitchMessage = Annotated[
    Annotated[_TwitchWelcomeMessage, Tag('session_welcome')] |
    Annotated[_TwitchReconnectMessage, Tag('session_reconnect')] |
    Annotated[_TwitchKeepaliveMessage, Tag('session_keepalive')] |
    Annotated[_TwitchNotificationMessage, Tag('notification')],
    Discriminator(_twitch_message_type)
]
_AnyTwitchMessageModel: TypeAdapter[_AnyTwitchMessage] = TypeAdapter(_AnyTwitchMessage)

class _TwitchChannelRaidNotification(BaseModel):
    class Event(BaseModel):
        # pylint: disable=missing-class-docstring
        to_broadcaster_user_login: str

    event: Event

class _TwitchStreamOfflineNotification(BaseModel):
    pass

def _twitch_subscription_type(data: dict[str, object]) -> str | None:
    subscription = data.get('subscription')
    subscription_type = subscription.get('type') if isinstance(subscription, dict) else None
    return subscription_type if isinstance(subscription_type, str) else None

_AnyTwitchNotification = Annotated[
    Annotated[_TwitchChannelRaidNotification, Tag('channel.raid')] |
    Annotated[_TwitchStreamOfflineNotification, Tag('stream.offline')],
    Discriminator(_twitch_subscription_type)
]
_TwitchNotificationModel: TypeAdapter[_AnyTwitchNotification] = TypeAdapter(_AnyTwitchNotification)

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

class _EventSub:
    # pylint: disable=missing-docstring

    Notification: TypeAlias = 'dict[str, object] | Cancellation'

    class Cancellation:
        pass

    def __init__(self, api_url: str, websocket_url: str, client_id: str, token: str) -> None:
        self.websocket_url = websocket_url
        self.client_id = client_id
        self._api = WebAPI(api_url,
                           headers={'Authorization': f'Bearer {token}', 'Client-Id': client_id})
        self._websocket: WebSocketClientConnection | None = None
        self._session: _TwitchSessionPayload._Session | None = None
        self._connect_task: Task[None] | None = None
        self._read_task: Task[None] | None = None
        self._subscriptions: dict[str, Queue[_EventSub.Notification]] = {}

    async def subscribe(self, subscription_type: str,
                        condition: Mapping[str, str]) -> AsyncGenerator[Notification]:
        async def generator() -> AsyncGenerator[_EventSub.Notification]:
            if not self._subscriptions:
                await self._connect()
            assert self._session
            notifications: Queue[_EventSub.Notification] = Queue()
            self._subscriptions[subscription_type] = notifications
            try:
                try:
                    await self._api.call(
                        'POST', 'subscriptions',
                        data={
                            'type': subscription_type,
                            'version': '1',
                            'condition': condition,
                            'transport': {'method': 'websocket', 'session_id': self._session.id}
                        })
                except WebAPI.Error as e:
                    match e.status:
                        case HTTPStatus.UNAUTHORIZED:
                            raise AuthenticationError(
                                f'Failed authentication of {self.client_id}'
                            ) from e
                        case HTTPStatus.TOO_MANY_REQUESTS:
                            # CONFLICT is only relevant for duplicate subscriptions with the same
                            # WebSocket
                            raise OSError(errno.EAGAIN, 'Exceeded subscription limit') from e
                        case _:
                            raise
                yield {}

                while True:
                    while not notifications.empty():
                        notification = notifications.get_nowait()
                        yield notification
                        notifications.task_done()
                        if isinstance(notification, _EventSub.Cancellation):
                            return
                    await self._read()
            finally:
                # The subscription should be cancelled here (see
                # https://dev.twitch.tv/docs/api/reference/#delete-eventsub-subscription), but at
                # the moment usually no new subscriptions are made after closing a notification
                # stream

                del self._subscriptions[subscription_type]
                if not self._subscriptions:
                    self._close()

        # Subscribe to notifications and ensure closing the generator always performs a cleanup
        notifications = generator()
        await anext(notifications) # type: ignore[misc]
        return notifications

    async def _connect(self) -> None:
        if not self._connect_task:
            async def func() -> None:
                try:
                    self._websocket = await websocket_connect(self.websocket_url)
                    try:
                        message = await self._read_message()
                        if message is None:
                            raise ConnectionResetError(errno.ECONNRESET, 'Closed connection')
                        if not isinstance(message, _TwitchWelcomeMessage):
                            raise OSError(
                                errno.EPROTO,
                                f"Unexpected message type {message.metadata.message_type}")
                        self._session = message.payload.session
                    except:
                        self._close()
                        raise
                finally:
                    self._connect_task = None
            self._connect_task = create_task(func())
        await shield(self._connect_task)

    def _close(self) -> None:
        assert self._websocket
        self._websocket.close()
        self._websocket = None
        self._session = None

    async def _read(self) -> None:
        if not self._read_task:
            async def func() -> None:
                try:
                    message = await self._read_message()
                    match message:
                        case _TwitchNotificationMessage():
                            notification: _EventSub.Notification = message.payload
                            notification_type = _twitch_subscription_type(message.payload)
                        case _TwitchKeepaliveMessage():
                            return
                        case _TwitchReconnectMessage() | None:
                            notification = _EventSub.Cancellation()
                            notification_type = None
                        case _:
                            raise OSError(
                                errno.EPROTO,
                                f"Unexpected message type {message.metadata.message_type}")

                    for subscription_type, notifications in self._subscriptions.items():
                        if notification_type in (subscription_type, None):
                            notifications.put_nowait(notification)
                finally:
                    self._read_task = None
            self._read_task = create_task(func())
        await shield(self._read_task)

    async def _read_message(self) -> _TwitchMessage | None:
        assert self._websocket
        timeout = (self._session.keepalive_timeout_seconds if self._session else 0) + 20
        async with asyncio.timeout(timeout):
            data = await self._websocket.read_message()
        if data is None:
            return None
        try:
            return _AnyTwitchMessageModel.validate_json(data)
        except ValidationError as e:
            error = OSError(errno.EPROTO, "Bad message")
            error.add_note(data.decode(errors='replace') if isinstance(data, bytes) else data)
            raise error from e

class TwitchStream(Stream):
    """Twitch live stream."""

    service: Twitch

    def __init__(
        self, channel: Channel, category: Text, service: Twitch,
        _offline: AsyncGenerator[_EventSub.Notification],
        _raids: AsyncGenerator[_EventSub.Notification]
    ) -> None:
        super().__init__(channel, category, service)
        self._offline = _offline
        self._raids = _raids
        self._notifications = amerge(self._offline, self._raids)
        self._eof = False

    async def __anext__(self) -> Stream.Event:
        try:
            data = await anext(self._notifications) # type: ignore[misc]
        except StopAsyncIteration:
            if self._eof:
                raise
            raise ConnectionResetError(errno.ECONNRESET, 'Closed stream') from None
        if isinstance(data, _EventSub.Cancellation):
            await self.aclose()
            raise ConnectionResetError(errno.ECONNRESET, 'Closed stream')

        try:
            notification = _TwitchNotificationModel.validate_python(data)
        except ValidationError as e:
            await self.aclose()
            error = OSError(errno.EPROTO, "Bad notification")
            error.add_note(json.dumps(data))
            raise error from e
        match notification:
            case _TwitchChannelRaidNotification():
                return Stream.RaidEvent(
                    target_url=urljoin(self.service.url,
                                       notification.event.to_broadcaster_user_login))
            case _TwitchStreamOfflineNotification():
                self._eof = True
                await self.aclose()
                raise StopAsyncIteration()

    async def aclose(self) -> None:
        await self._notifications.aclose() # type: ignore[misc]
        # Close the sources in case the notifications generator has not started yet
        await gather(self._offline.aclose(), self._raids.aclose()) # type: ignore[misc]

class Twitch(Service[TwitchStream]):
    """Twitch connection."""

    # Work around Pylint missing docstrings from a generic parent (see
    # https://github.com/pylint-dev/pylint/issues/9766)
    # pylint: disable=missing-function-docstring

    url: ClassVar['str'] = 'https://www.twitch.tv'
    name: ClassVar['str'] = 'Twitch'

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

    class _Page(BaseModel, Generic[_T]):
        data: list[_T]

    class _User(BaseModel):
        id: str
        display_name: str
        profile_image_url: str

    class _Stream(BaseModel):
        game_name: str

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
            raise OSError(errno.EINVAL, f'Error {process.returncode}')

    @authenticated
    async def stream(self, channel_url: str, *, timeout: float | None = None) -> TwitchStream:
        service = context.bot.get().get_service(self.type)
        assert isinstance(service, Twitch)
        user = await self._get_user(channel_url)

        async with AsyncExitStack() as cleanups:
            eventsub = _EventSub(self.eventsub_url, self.websocket_url, self.client_id,
                                 service.token)
            async def subscribe(
                subscription_type: str, condition: Mapping[str, str]
            ) -> AsyncGenerator[_EventSub.Notification]:
                notifications = await eventsub.subscribe(subscription_type, condition)
                cleanups.push_async_callback(notifications.aclose) # type: ignore[misc]
                return notifications
            subscriptions: list[Coroutine[None, None, AsyncGenerator[_EventSub.Notification]]] = [
                subscribe('stream.online', {'broadcaster_user_id': user.id}),
                subscribe('stream.offline', {'broadcaster_user_id': user.id}),
                subscribe('channel.raid', {'from_broadcaster_user_id': user.id})
            ]
            tasks = [create_task(subscription) for subscription in subscriptions]
            await wait(tasks)
            online, offline, raids = (task.result() for task in tasks)

            while True:
                streams = Twitch._Page[Twitch._Stream].model_validate(
                    await self._call('GET', 'streams', query={'user_id': user.id}))
                try:
                    stream = streams.data[0]
                    break
                except IndexError:
                    pass

                if timeout is None:
                    raise LookupError(channel_url)
                try:
                    async with asyncio.timeout(timeout):
                        await anext(online) # type: ignore[misc]
                except TimeoutError:
                    raise StreamTimeoutError() from None
            await online.aclose() # type: ignore[misc]

            cleanups.pop_all()
        return TwitchStream(
            channel=Channel(url=channel_url, name=user.display_name,
                            image_url=user.profile_image_url),
            category=stream.game_name, service=self, _offline=offline, _raids=raids)

    def _subscription(self, subscription_type: str, condition: dict[str, str],
                      session_id: str) -> dict[str, object]:
        return {
            'type': subscription_type,
            'version': '1',
            'condition': condition,
            'transport': {'method': 'websocket', 'session_id': session_id}
        }

    async def reauthorize(self) -> Self:
        service = context.bot.get().get_service(self.type)
        assert isinstance(service, Twitch)
        token = await _post_twitch_token(
            WebAPI(self.oauth_url), 'token', client_id=self.client_id,
            client_secret=self.client_secret, grant_type='refresh_token',
            refresh_token=service.refresh_token)
        return self.copy(
            update={ # type: ignore[misc]
                'token': token.access_token,
                'refresh_token': token.refresh_token
            })

    async def stop(self, channel_url: str) -> None:
        if self.api_url == TwitchAdapter.PRODUCTION_API_URL:
            raise RuntimeError('Unsupported operation')
        user = await self._get_user(channel_url)
        await Twitch.cli('event', 'trigger', '--transport=websocket', f'--to-user={user.id}',
                         'streamdown')

    async def raid(self, channel_url: str, target_url: str) -> None:
        if self.api_url == TwitchAdapter.PRODUCTION_API_URL:
            raise RuntimeError('Unsupported operation')
        user = await self._get_user(channel_url)
        target = await self._get_user(target_url)
        await Twitch.cli('event', 'trigger', '--transport=websocket', f'--from-user={user.id}',
                         f'--to-user={target.id}', 'raid')

    def get_login(self, channel_url: str) -> str:
        """Get the user login from a *channel_url*."""
        login = channel_url.removeprefix(f'{self.url}/')
        if login == channel_url or not login:
            raise ValueError(f'Bad channel_url {channel_url}')
        return login

    async def _call(self, method: str, endpoint: str, *, data: Mapping[str, object] | None = None,
                    query: Mapping[str, str] = {}) -> dict[str, object]:
        # pylint: disable=dangerous-default-value
        service = context.bot.get().get_service(self.type)
        assert isinstance(service, Twitch)
        api = WebAPI(
            self.api_url,
            headers={'Authorization': f'Bearer {service.token}', 'Client-Id': self.client_id})
        try:
            return await api.call(method, endpoint, data=data, query=query)
        except WebAPI.Error as e:
            if e.status == HTTPStatus.UNAUTHORIZED:
                raise AuthenticationError(f'Failed authentication of {self.client_id}') from e
            raise

    async def _get_user(self, channel_url: str) -> Twitch._User:
        try:
            login = self.get_login(channel_url)
        except ValueError:
            raise LookupError(channel_url) from None
        users = Twitch._Page[Twitch._User].model_validate(
            await self._call("GET", "users", query={'login': login}))
        try:
            return users.data[0]
        except IndexError:
            raise LookupError(channel_url) from None

    def __str__(self) -> str:
        return f'📺 {self.name} via application {self.client_id}'

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
