# pylint: disable=missing-docstring

import json
import logging

from tornado.testing import AsyncHTTPTestCase, gen_test
from tornado.web import Application, RequestHandler

from streamfarer.util import WebAPI

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

class WebAPITest(AsyncHTTPTestCase):
    def setUp(self) -> None:
        super().setUp()
        logging.disable()

    def get_app(self) -> Application:
        return Application([(r'/api/echo(?:/(\d{3}))?$', Echo)]) # type: ignore[misc]

    @gen_test # type: ignore[misc]
    async def test_call(self) -> None:
        api = WebAPI(self.get_url('/api/'), query={'token': 'secret'},
                     headers={'Authorization': 'Bearer: secret'})
        echo = await api.call('POST', 'echo', data={'age': 7}, query={'name': 'Frank'})
        self.assertEqual(echo.get('data'), {'age': 7}) # type: ignore[misc]
        self.assertEqual(echo.get('query'),
                         {'token': 'secret', 'name': 'Frank'}) # type: ignore[misc]
        headers = echo.get('headers')
        assert isinstance(headers, dict)
        self.assertEqual(headers.get('Authorization'), 'Bearer: secret')

    @gen_test # type: ignore[misc]
    async def test_call_error(self) -> None:
        api = WebAPI(self.get_url('/api/'))
        with self.assertRaises(WebAPI.Error) as e:
            await api.call('POST', 'echo/400', data={'message': 'Meow!'})
        self.assertEqual(e.exception.error.get('data'), {'message': 'Meow!'}) # type: ignore[misc]
        self.assertFalse(e.exception.error.get('query'))
        self.assertEqual(e.exception.status, 400)

    @gen_test # type: ignore[misc]
    async def test_call_communication_problem(self) -> None:
        api = WebAPI('https://example.invalid/')
        with self.assertRaises(OSError):
            await api.call('GET', 'echo')
