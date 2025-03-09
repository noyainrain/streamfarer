# pylint: disable=missing-docstring

from asyncio import Queue, create_task, sleep
from collections.abc import AsyncGenerator
import json
import logging
import sqlite3
from string import ascii_lowercase
from unittest import IsolatedAsyncioTestCase, TestCase

from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.web import Application, RequestHandler

from streamfarer.util import WebAPI, add_column, amerge, cancel, nested, randstr, urlorigin

class RandstrTest(TestCase):
    def test(self) -> None:
        text = randstr()
        self.assertEqual(len(text), 16)
        self.assertLessEqual(set(text), set(ascii_lowercase))

class UrloriginTest(TestCase):
    def test(self) -> None:
        origin = urlorigin('https://example.org/cats?age=7#a')
        self.assertEqual(origin, 'https://example.org')

class CancelTest(IsolatedAsyncioTestCase):
    async def test(self) -> None:
        task = create_task(sleep(1))
        await cancel(task)
        self.assertTrue(task.cancelled())

class AmergeTest(IsolatedAsyncioTestCase):
    @staticmethod
    async def queue_aiter(queue: Queue[str | None]) -> AsyncGenerator[str]:
        while True:
            item = await queue.get()
            queue.task_done()
            if item is None:
                return
            yield item

    @staticmethod
    async def infinity() -> AsyncGenerator[None]:
        while True:
            yield

    @staticmethod
    async def tick() -> None:
        # Wait for amerge() to continue (one loop iteration for anext() and two for wait())
        for _ in range(3):
            await sleep(0)

    async def test_for(self) -> None:
        letters: Queue[str | None] = Queue()
        digits: Queue[str | None] = Queue()

        async def produce() -> None:
            letters.put_nowait('a')
            await self.tick()
            digits.put_nowait('1')
            await self.tick()
            letters.put_nowait('b')
            await self.tick()
            digits.put_nowait(None)
            await self.tick()
            letters.put_nowait(None)
        create_task(produce())
        generator = amerge(self.queue_aiter(letters), self.queue_aiter(digits))

        items = [item async for item in generator]
        self.assertEqual(items, ['a', '1', 'b']) # type: ignore[misc]

    async def test_aclose(self) -> None:
        infinity = self.infinity()
        generator = amerge(infinity)
        await anext(generator) # type: ignore[misc]

        await generator.aclose() # type: ignore[misc]
        with self.assertRaises(StopAsyncIteration):
            await anext(infinity) # type: ignore[misc]

class NestedTest(TestCase):
    def test(self) -> None:
        data = nested({'id': 'a', 'cat_name': 'Frank', 'cat_age': 7}, 'cat')
        self.assertEqual(data, {'id': 'a', 'cat': {'name': 'Frank', 'age': 7}}) # type: ignore[misc]

class Echo(RequestHandler):
    def get(self, status: str | None) -> None:
        self._respond(status)

    def post(self, status: str | None) -> None:
        self._respond(status)

    def _respond(self, status: str | None) -> None:
        data: object | None = None
        if self.request.body:
            data = json.loads(self.request.body)
        query = {name: values[0].decode() for name, values in self.request.query_arguments.items()}
        headers: dict[str, str] = dict(self.request.headers)
        if status:
            self.set_status(int(status))
        self.write({'data': data, 'query': query, 'headers': headers}) # type: ignore[misc]

class AddColumnTest(TestCase):
    def test(self) -> None:
        db = sqlite3.connect(':memory:')
        with db:
            db.execute('CREATE TABLE cats (name)')
            db.execute("INSERT INTO cats (name) VALUES ('Frank')")
            add_column(db, 'cats', '"age"', 7)
            rows = db.execute('SELECT * FROM cats')
            columns: tuple[tuple[str, None, None, None, None, None, None], ...] = rows.description
            cat: tuple[str, int] = next(rows)
        self.assertEqual([column[0] for column in columns], ['name', '"age"']) # type: ignore[misc]
        self.assertEqual(cat, ('Frank', 7))

class WebAPITest(AsyncHTTPTestCase):
    def setUp(self) -> None:
        super().setUp()
        logging.disable()

    def get_app(self) -> Application:
        return Application([(r'/api/echo(?:/(\d{3}))?$', Echo)]) # type: ignore[misc]

    @gen_test # type: ignore[misc]
    async def test_call(self) -> None: # type: ignore[misc]
        api = WebAPI(self.get_url('/api/'), query={'token': 'secret'},
                     headers={'Authorization': 'Bearer: secret'})
        echo = await api.call('POST', 'echo', data={'age': 7}, query={'name': 'Frank'})
        self.assertEqual(echo.get('data'), {'age': 7}) # type: ignore[misc]
        self.assertEqual(echo.get('query'),
                         {'token': 'secret', 'name': 'Frank'}) # type: ignore[misc]
        headers = echo.get('headers')
        assert isinstance(headers, dict)
        self.assertEqual(headers.get('Content-Type'), 'application/json')
        self.assertEqual(headers.get('Authorization'), 'Bearer: secret')

    @gen_test # type: ignore[misc]
    async def test_call_error(self) -> None: # type: ignore[misc]
        api = WebAPI(self.get_url('/api/'))
        with self.assertRaises(WebAPI.Error) as e:
            await api.call('POST', 'echo/400', data={'message': 'Meow!'})
        self.assertEqual(e.exception.error.get('data'), {'message': 'Meow!'}) # type: ignore[misc]
        self.assertFalse(e.exception.error.get('query'))
        self.assertEqual(e.exception.status, 400)

    @gen_test # type: ignore[misc]
    async def test_call_communication_problem(self) -> None: # type: ignore[misc]
        api = WebAPI('https://example.invalid/')
        with self.assertRaises(OSError):
            await api.call('GET', 'echo')
