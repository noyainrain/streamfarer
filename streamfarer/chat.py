"""Chat UI."""

from asyncio import Task, create_task, sleep
from logging import getLogger

from . import context
from .core import Text, format_duration
from .journey import Stay, StayEvent
from .services import AccessError, AuthenticationError
from .util import cancel, urlorigin

def _message(text: Text) -> None:
    async def func() -> None:
        logger = getLogger(__name__)
        try:
            await context.bot.get().message(text)
        except AccessError as e:
            logger.warning('Failed to send the message (%s)', e)
        except (AuthenticationError, OSError) as e:
            logger.error('Failed to send the message (%s)', e)
    create_task(func())

async def chat(*, intro_delay: float = 60) -> None:
    """Run the chat UI.

    When arriving at a live stream, the bot is introduced after an *intro_delay* in seconds.
    """
    bot = context.bot.get()
    intro_task: Task[None] | None = None
    async def intro(stay: Stay) -> None:
        nonlocal intro_task
        try:
            await sleep(intro_delay)
            journey = stay.get_journey()
            service = bot.get_service_at(urlorigin(stay.channel.url))
            _message(
                f"Ahoy! I'm Streamfarer, a tiny art experiment, and I've been traveling through "
                f"{service.name} for {format_duration(journey.duration)} now. It would be amazing "
                f"if you would raid someone at the end of stream, so that I can continue my "
                f"journey. Happy streaming! 💙")
        finally:
            intro_task = None

    logger = getLogger(__name__)
    logger.info('Started the chat')
    try:
        async for event in bot.events():
            if isinstance(event, StayEvent) and event.type == 'journey-travel-on':
                if intro_task:
                    await cancel(intro_task)
                intro_task = create_task(intro(event.stay))
    finally:
        if intro_task:
            await cancel(intro_task)
        logger.info('Stopped the chat')
