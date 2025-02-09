"""Bot logic.

.. data:: VERSION

   Current version.
"""

import asyncio
from asyncio import Queue, sleep
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from functools import partial
from logging import getLogger
import sqlite3
from sqlite3 import IntegrityError, Row
from textwrap import indent
from typing import Annotated, ParamSpec, TypeVar

from pydantic import Field, TypeAdapter, validate_call

from . import context
from .core import Event, Text
from .journey import EndedJourneyError, Journey, OngoingJourneyError, PastJourneyError, Stay
from .services import (AuthenticationError, LocalService, LocalServiceAdapter, Service, Stream,
                       StreamTimeoutError, Twitch, TwitchAdapter)
from .util import Connection, add_column, randstr, urlorigin

VERSION = '0.1.17'

_P = ParamSpec('_P')
_R_co = TypeVar('_R_co', covariant=True)

class Bot:
    """Live stream traveling bot.

    .. attribute:: twitch

       Twitch adapter.

    .. attribute:: local

       Local livestreaming service adapter.

    .. attribute:: database_url

       SQLite database URL.

    .. attribute:: now

       Function that returns the current UTC date and time.
    """

    _JOURNEY_END_GRACE_PERIOD = timedelta(minutes=5)
    _SERVICE_TYPES_BY_URL = {LocalService.url: 'local', Twitch.url: 'twitch'}

    _AnyService = Annotated[Twitch | LocalService, Field(discriminator='type')]
    _ServiceModel: TypeAdapter[_AnyService] = TypeAdapter(_AnyService)

    @staticmethod
    def _retrying(
        func: Callable[_P, Awaitable[_R_co]]
    ) -> Callable[_P, Coroutine[None, None, _R_co]]:
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R_co:
            logger = getLogger(__name__)
            while True:
                try:
                    return await func(*args, **kwargs)
                except AuthenticationError:
                    logger.error('Failed to authenticate with the livestreaming service')
                    await asyncio.Event().wait()
                except OSError as e:
                    try:
                        detail = '\n' + indent('\n'.join(e.__notes__), ' ' * 4)
                    except AttributeError:
                        detail = ''
                    logger.warning('Failed to communicate with the livestreaming service (%s)%s', e,
                                   detail)
                    await sleep(1)
        return wrapper

    def __init__(self, *, database_url: str = 'streamfarer.db',
                 now: Callable[[], datetime] = partial(datetime.now, UTC)) -> None:
        self.twitch = TwitchAdapter()
        self.local = LocalServiceAdapter()
        self.database_url = database_url
        self.now = now
        self._db: Connection[Row] | None = None
        self._event_queues: set[Queue[Event]] = set()

        if context.bot.get(None):
            raise RuntimeError('Duplicate bot in task')
        context.bot.set(self)

    def events(self) -> AsyncGenerator[Event]:
        """Stream of bot events."""
        queue: Queue[Event] = Queue()
        self._event_queues.add(queue)
        async def generator() -> AsyncGenerator[Event]:
            try:
                while True:
                    yield await queue.get()
            finally:
                self._event_queues.remove(queue)
        return generator()

    def dispatch_event(self, event: Event) -> None:
        """Plumbing: Dispatch an *event*."""
        for queue in self._event_queues:
            queue.put_nowait(event)

    @_retrying
    async def _travel_on(self, journey: Journey, channel_url: str) -> Stay:
        stay = await journey.travel_on(channel_url)
        getLogger(__name__).info('Traveled on to %s', stay.channel.url)
        return stay

    @_retrying
    async def _resume(self, journey: Journey) -> Journey:
        assert journey.end_time
        timeout = (journey.end_time + self._JOURNEY_END_GRACE_PERIOD - self.now()).total_seconds()
        return await journey.resume(timeout=timeout)

    @_retrying
    async def _do_stay(self, stay: Stay) -> Stay | None:
        logger = getLogger(__name__)
        journey = stay.get_journey()

        while True:
            try:
                try:
                    stream = await self.stream(stay.channel.url)
                except LookupError:
                    logger.info('Channel at %s is offline', stay.channel.url)
                else:
                    async with stream:
                        async for event in stream:
                            assert isinstance(event, Stream.RaidEvent)
                            logger.info('Stream at %s raided %s', stay.channel.url,
                                        event.target_url)
                            try:
                                return await self._travel_on(journey, event.target_url)
                            except LookupError:
                                logger.info('Channel at %s is offline', event.target_url)
                        else:
                            logger.info('Stream at %s stopped', stay.channel.url)

                try:
                    journey = journey.end()
                    logger.info('Ended the journey %s', journey.title)
                except KeyError:
                    raise EndedJourneyError() from None
            except EndedJourneyError:
                logger.info('Journey “%s” ended', journey.title)
                return None

            try:
                journey = await self._resume(journey)
                logger.info('Stream at %s restarted', stay.channel.url)
                logger.info('Resumed the journey %s', journey.title)
            except StreamTimeoutError:
                logger.info('Stream at %s did not restart', stay.channel.url)
                return None
            except KeyError:
                logger.info('Journey “%s” has been deleted', journey.title)
                return None
            except PastJourneyError:
                logger.info('New journey started')
                return None

    async def _stay(self, stay: Stay) -> Stay | None:
        logger = getLogger(__name__)
        logger.info('Started staying at %s', stay.channel.url)
        try:
            return await self._do_stay(stay)
        finally:
            logger.info('Stopped staying at %s', stay.channel.url)

    async def _travel(self, journey: Journey) -> None:
        logger = getLogger(__name__)
        logger.info('Started traveling on %s', journey.title)
        try:
            stay: Stay | None = journey.get_stays()[0]
            while stay:
                stay = await self._stay(stay)
        finally:
            logger.info('Stopped traveling on %s', journey.title)

    async def run(self) -> None:
        """Run the bot."""
        logger = getLogger(__name__)
        logger.info('Started the bot')
        try:
            journey = next(iter(self.get_journeys()), None)
            if journey and not journey.end_time:
                await self._travel(journey)
            await asyncio.Event().wait()
        finally:
            logger.info('Stopped the bot')

    def get_journeys(self) -> list[Journey]:
        """Get all journeys, latest first.

        The latest journey may be ongoing.
        """
        with self.transaction() as db:
            rows = db.execute('SELECT * FROM journeys WHERE deleted = 0 ORDER BY start_time DESC')
            return [Journey.model_validate(dict(row)) for row in rows]  # type: ignore[misc]

    def get_journey(self, journey_id: str) -> Journey:
        """Get the journey with the given *journey_id*."""
        with self.transaction() as db:
            rows = db.execute('SELECT * FROM journeys WHERE id = ?', (journey_id, ))
            try:
                return Journey.model_validate(dict(next(rows)))
            except StopIteration:
                raise KeyError(journey_id) from None

    @validate_call # type: ignore[misc]
    async def start_journey(self, channel_url: str, title: Text) -> Journey:
        """Start a new journey at the given *channel_url*.

        *title* is the journey title.

        If authentication with the livestreaming service fails, an :exc:`AuthenticationError` is
        raised. If there is a problem communicating with the livestreaming service, an
        :exc:`OSError` is raised.
        """
        stream = await self.stream(channel_url)
        async with stream:
            with self.transaction() as db:
                start_time = self.now().isoformat()
                try:
                    rows = db.execute(
                        """
                        INSERT INTO journeys (id, title, start_time, end_time, deleted)
                        VALUES (?, ?, ?, ?, ?) RETURNING *
                        """,
                        (randstr(), title, start_time, None, False))
                except IntegrityError as e:
                    if 'journeys_end_time_index' in str(e):
                        raise OngoingJourneyError('Ongoing journey') from None
                    raise
                journey = Journey.model_validate(dict(next(rows)))
                db.execute(
                    """
                    INSERT INTO stays (
                        id, journey_id, channel_url, channel_name, channel_image_url, category,
                        start_time, end_time
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (randstr(), journey.id, stream.channel.url, stream.channel.name,
                     stream.channel.image_url, stream.category, start_time, None))
                return journey

    def get_services(self) -> list[Service[Stream]]:
        """Get connected livestreaming services."""
        with self.transaction() as db:
            return [self._ServiceModel.validate_python(dict(row)) # type: ignore[misc]
                    for row in db.execute('SELECT * FROM services ORDER BY type')]

    def get_service(self, service_type: str) -> Service[Stream]:
        """Get the connected livestreaming service of the given *service_type*."""
        with self.transaction() as db:
            rows = db.execute('SELECT * FROM services WHERE type = ?', (service_type, ))
            try:
                return Bot._ServiceModel.validate_python(dict(next(rows)))
            except StopIteration:
                raise KeyError(service_type) from None

    def get_service_at(self, url: str) -> Service[Stream]:
        """Get the connected livestreaming service at the given *url*."""
        try:
            return self.get_service(self._SERVICE_TYPES_BY_URL[url])
        except KeyError:
            raise KeyError(url) from None

    async def stream(self, channel_url: str, *, timeout: float | None = None) -> Stream:
        """Open the live stream at the given *channel_url*.

        Optionally, the channel is awaited to come online with a *timeout* in seconds.

        If authentication with the livestreaming service fails, an :exc:`AuthenticationError` is
        raised. If there is a problem communicating with the livestreaming service, an
        :exc:`OSError` is raised.
        """
        service = self.get_service_at(urlorigin(channel_url))
        return await service.stream(channel_url, timeout=timeout)

    def transaction(self) -> Connection[Row]:
        """Plumbing: Context manager to perform a transaction."""
        if not self._db:
            self._db = sqlite3.connect(self.database_url, factory=Connection)
            self._db.row_factory = Row
            self._db.execute('PRAGMA foreign_keys = 1')
            self._update(self._db)
        return self._db

    def _update(self, db: Connection[Row]) -> None:
        with db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS journeys (
                    id PRIMARY KEY,
                    title,
                    start_time,
                    end_time,
                    deleted,
                    CONSTRAINT deleted_end_time_check CHECK (deleted = 0 OR end_time IS NOT NULL)
                )
                """)
            db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS journeys_end_time_index ON journeys
                (coalesce(end_time, ''))
                """)
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS stays (
                    id PRIMARY KEY,
                    journey_id REFERENCES journeys,
                    channel_url,
                    channel_name,
                    channel_image_url,
                    category,
                    start_time,
                    end_time
                )
                """)
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS services (
                    type PRIMARY KEY,
                    api_url,
                    oauth_url,
                    eventsub_url,
                    websocket_url,
                    client_id,
                    client_secret,
                    token,
                    refresh_token
                )
                """)

            # Update Twitch.api_url
            add_column(db, 'services', 'api_url')
            db.execute(
                "UPDATE services SET api_url = ? WHERE type = 'twitch' AND api_url IS NULL",
                (TwitchAdapter.PRODUCTION_API_URL, ))

            # Update Twitch.eventsub_url
            add_column(db, 'services', 'eventsub_url')
            db.execute(
                """
                UPDATE services SET eventsub_url = 'https://api.twitch.tv/helix/eventsub/'
                WHERE type = 'twitch' AND eventsub_url IS NULL
                """)

            # Update Twitch.websocket_url
            add_column(db, 'services', 'websocket_url')
            db.execute(
                """
                UPDATE services SET websocket_url = 'wss://eventsub.wss.twitch.tv/ws'
                WHERE type = 'twitch' AND websocket_url IS NULL
                """)

            # Update Twitch.refresh_token
            add_column(db, 'services', 'refresh_token')
            db.execute(
                """
                UPDATE services SET refresh_token = ''
                WHERE type = 'twitch' AND refresh_token IS NULL
                """)

            # Update Stay.category
            add_column(db, 'stays', 'category', '?')

            # Update Channel.image_url
            add_column(
                db, 'stays', 'channel_image_url',
                'https://static-cdn.jtvnw.net/user-default-pictures-uv/'
                '998f01ae-def8-11e9-b95c-784f43822e80-profile_image-300x300.png')
