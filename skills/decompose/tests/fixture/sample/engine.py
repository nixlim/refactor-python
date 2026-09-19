from contextlib import contextmanager
from math import sqrt


class Engine:
    factor = 2

    def __init__(self, value):
        self.value = value

    def calculate(self):
        # Keep this comment when relocating.
        return sqrt(self.value) * self.factor

    @staticmethod
    def add(left, right):
        return left + right

    @classmethod
    def build(cls, value):
        return cls(value)

    @property
    def doubled(self):
        return self.value * 2

    @contextmanager
    def temporary(self):
        old = self.value
        try:
            yield self
        finally:
            self.value = old

    def closure(self, amount):
        value = self.value

        def add_value(number):
            return number + value

        return add_value(amount)

    def nonlocal_closure(self):
        value = 0

        def increment():
            nonlocal value
            value += 1

        increment()
        return value

    def __hidden(self):
        return self.value

    def private_reader(self):
        return self.__hidden()


def summarize(values, factor):
    total = 0
    for value in values:
        total += value * factor

    average = total / len(values)
    rounded = round(average, 2)

    if rounded < 0:
        return 0
    return rounded
