# pylint: disable=missing-docstring

from contextlib import redirect_stdout
from io import StringIO
from unittest import IsolatedAsyncioTestCase

from streamfarer.__main__ import main

class MainTest(IsolatedAsyncioTestCase):
    async def main(self, *args: str) -> tuple[int, str]:
        with redirect_stdout(StringIO()) as f:
            code = await main('python3 -m streamfarer', *args)
        return code, f.getvalue()

    async def test(self) -> None:
        code, stdout = await self.main('--help')
        self.assertEqual(code, 0)
        self.assertIn('Streamfarer', stdout)
