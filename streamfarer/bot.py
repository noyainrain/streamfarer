"""Bot logic.

.. data:: VERSION

   Current version.
"""

import asyncio
from asyncio import Queue, sleep
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from functools import partial
from logging import getLogger
import sqlite3
from sqlite3 import IntegrityError, Row
from typing import Annotated

from pydantic import Field, TypeAdapter, validate_call

from . import context
from .core import Event, Text
from .journey import Journey, OngoingJourneyError, Stay
from .services import (AuthenticationError, LocalService, LocalServiceAdapter, Service, Stream,
                       Twitch, TwitchAdapter)
from .util import Connection, add_column, randstr

VERSION = '0.1.5'

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

    _AnyService = Annotated[Twitch | LocalService, Field(discriminator='type')]
    _ServiceModel: TypeAdapter[_AnyService] = TypeAdapter(_AnyService)

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

    async def _stay(self, stay: Stay) -> None:
        logger = getLogger(__name__)
        logger.info('Started staying at %s', stay.channel.url)
        try:
            while True:
                try:
                    try:
                        stream = await self.stream(stay.channel.url)
                    except LookupError:
                        break
                    async with stream:
                        await anext(stream, None)
                        break
                except AuthenticationError:
                    logger.error('Failed to authenticate with the livestreaming service')
                    await asyncio.Event().wait()
                except OSError as e:
                    logger.warning('Failed to communicate with the livestreaming service (%s)', e)
                    await sleep(1)
        finally:
            logger.info('Stopped staying at %s', stay.channel.url)

    async def _travel(self, journey: Journey) -> None:
        logger = getLogger(__name__)
        logger.info('Started traveling on %s', journey.title)
        try:
            try:
                stay = journey.get_stays()[0]
            except IndexError:
                return
            await self._stay(stay)
            try:
                journey.end()
                logger.info('Ended the journey %s', journey.title)
            except KeyError:
                pass
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

        If getting authorization from the livestreaming service fails, an :exc:`AuthorizationError`
        is raised. If there is a problem communicating with the livestreaming service, an
        :exc:`OSError` is raised.
        """
        stream = await self.stream(channel_url)

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
                INSERT INTO stays(id, journey_id, channel_url, channel_name, start_time, end_time)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (randstr(), journey.id, stream.channel.url, stream.channel.name, start_time, None))
            return journey

    def get_services(self) -> list[Service[Stream]]:
        """Get connected livestreaming services."""
        with self.transaction() as db:
            return [self._ServiceModel.validate_python(dict(row)) # type: ignore[misc]
                    for row in db.execute('SELECT * FROM services ORDER BY type')]

    async def stream(self, channel_url: str) -> Stream:
        """Open the live stream at the given *channel_url*.

        If getting authorization from the livestreaming service fails, an :exc:`AuthorizationError`
        is raised. If there is a problem communicating with the livestreaming service, an
        :exc:`OSError` is raised.
        """
        for service in self.get_services():
            try:
                return await service.stream(channel_url)
            except LookupError:
                pass
        raise LookupError(channel_url)

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
