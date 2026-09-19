import os
import unittest
from unittest.mock import patch

from sample.engine import Engine, summarize


class TestEngine(unittest.TestCase):
    def test_calculate(self):
        self.assertEqual(Engine(9).calculate(), 6)

    def test_descriptors(self):
        engine = Engine.build(9)
        self.assertEqual(engine.doubled, 18)
        self.assertEqual(engine.add(1, 2), 3)
        with engine.temporary():
            engine.value = 10
        self.assertEqual(engine.value, 9)

    def test_closure(self):
        self.assertEqual(Engine(9).closure(3), 12)

    def test_patch(self):
        with patch.object(
            Engine,
            'calculate',
            return_value=42,
        ):
            self.assertEqual(Engine(9).calculate(), 42)
        self.assertEqual(Engine.calculate(Engine(9)), 6)

    def test_summary(self):
        self.assertEqual(summarize([2, 4], 2), 6)


def load_tests(loader, tests, pattern):
    shard = os.environ.get('EXAMPLE_SHARD')
    if shard is None:
        return tests
    selected = [test for group in tests for test in group]
    return unittest.TestSuite(test for index, test in enumerate(selected) if index % 2 == int(shard))
