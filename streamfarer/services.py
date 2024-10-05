"""Livestreaming services."""

from asyncio import create_subprocess_exec
from asyncio.subprocess import Process, PIPE
from http import HTTPStatus
from typing import ClassVar, Generic, Literal, ParamSpec, TypeVar
from urllib.parse import quote

from pydantic import BaseModel, validate_call

from . import context
from .core import Text
from .util import WebAPI

P = ParamSpec('P')
S = TypeVar('S', bound='Service')

_T = TypeVar('_T')

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

class Stream:
    """Live stream.

    .. attribute:: channel

       Related channel.
    """

    channel: Channel

    def __init__(self, channel: Channel) -> None:
        self.channel = channel

class Service(BaseModel): # type: ignore[misc]
    """Connected livestreaming service.

    Any operation may raise an :exc:`AuthorizationError` if getting authorization fails or an
    :exc:`OSError` if there is a problem communicating with the service.

    .. attribute:: type

       Service type.
    """

    type: str

    async def stream(self, channel_url: str) -> Stream:
        """Open the live stream at the given *channel_url*."""
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

    async def play(self, channel_url: str) -> Stream:
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

class LocalService(Service): # type: ignore[misc]
    """Local livestreaming service."""

    _channels: ClassVar[dict[str, Channel]] = {}
    _streams: ClassVar[dict[str, Stream]] = {}

    type: Literal['local']

    async def stream(self, channel_url: str) -> Stream:
        return self._streams[channel_url]

    async def get_channels(self) -> list[Channel]:
        return list(self._channels.values())

    @validate_call # type: ignore[misc]
    async def create_channel(self, name: Text) -> Channel:
        channel = Channel(url=f'streamfarer:{quote(name)}', name=name)
        self._channels[channel.url] = channel
        return channel

    async def delete_channel(self, channel_url: str) -> None:
        self._channels.pop(channel_url, None)
        self._streams.pop(channel_url, None)

    async def play(self, channel_url: str) -> Stream:
        stream = Stream(channel=self._channels[channel_url])
        self._streams[channel_url] = stream
        return stream

    def __str__(self) -> str:
        return '📺 Local livestreaming service'

class LocalServiceAdapter(ServiceAdapter[[], LocalService]):
    """Local livestreaming service adapter."""

    async def authorize(self) -> LocalService:
        return LocalService(type='local')

class Twitch(Service): # type: ignore[misc]
    """Twitch connection."""

    type: Literal['twitch']
    """Web API URL."""
    api_url: str
    """OAuth URL."""
    oauth_url: str
    """Application client ID."""
    client_id: str
    """Application client secret."""
    client_secret: str
    """Access token."""
    token: str

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
        process = await create_subprocess_exec('twitch', *args, stderr=PIPE)
        if signal:
            assert process.stderr
            async for line in process.stderr:
                if signal in line.decode():
                    break
        return process

    async def stream(self, channel_url: str) -> Stream:
        login = channel_url.removeprefix('https://www.twitch.tv/')
        if login == channel_url:
            raise LookupError(channel_url)
        api = WebAPI(self.api_url,
                     headers={'Authorization': f'Bearer {self.token}', 'Client-Id': self.client_id})

        try:
            users = Twitch._Page[Twitch._User].model_validate(
                await api.call("GET", "users", query={'login': login}))
            try:
                user = users.data[0]
            except IndexError:
                raise LookupError(channel_url) from None

            streams = Twitch._Page[object].model_validate(
                await api.call('GET', 'streams', query={'user_id': user.id}))
            if not streams.data:
                raise LookupError(channel_url)
        except WebAPI.Error as e:
            if e.status == HTTPStatus.UNAUTHORIZED:
                raise AuthorizationError(f'Failed authorization of {self.client_id}') from e
            raise

        return Stream(channel=Channel(url=channel_url, name=user.display_name))

    def __str__(self) -> str:
        return f'📺 Twitch via application {self.client_id}'

class TwitchAdapter(ServiceAdapter[[str, str, str, str, str | None, str | None], Twitch]):
    """Twitch adapter.

    *code* is an authorization code obtained via the *redirect_uri*. For a mock server, it is the ID
    of the authorizing user.
    """

    _PRODUCTION_OAUTH_URL = 'https://id.twitch.tv/oauth2/'

    class _Token(BaseModel): # type: ignore[misc]
        access_token: str

    async def authorize(self, client_id: str, client_secret: str, code: str, redirect_uri: str,
                        api_url: str | None, oauth_url: str | None) -> Twitch:
        oauth = WebAPI(oauth_url or self._PRODUCTION_OAUTH_URL)
        if oauth.url == self._PRODUCTION_OAUTH_URL:
            endpoint = 'token'
            query = {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': redirect_uri}
        else:
            endpoint = 'authorize'
            query={'grant_type': 'user_token', 'user_id': code}

        try:
            token = TwitchAdapter._Token.model_validate(
                await oauth.call(
                    'POST', endpoint,
                    query={**query, 'client_id': client_id, 'client_secret': client_secret}))
        except WebAPI.Error as e:
            if e.status == HTTPStatus.BAD_REQUEST:
                raise AuthorizationError(f'Failed authorization of {client_id}') from e
            raise

        return Twitch(
            type='twitch', api_url=api_url or 'https://api.twitch.tv/helix/', oauth_url=oauth.url,
            client_id=client_id, client_secret=client_secret, token=token.access_token)
