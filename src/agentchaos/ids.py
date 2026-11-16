"""Seeded ID generation. Same seed and same call order give the same IDs."""

import random
from typing import Protocol


class IdGenerator(Protocol):
    def new(self, prefix: str) -> str: ...


class SeededIdGenerator:
    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._rng = random.Random(seed)
        self._count = 0

    @property
    def seed(self) -> int:
        return self._seed

    def new(self, prefix: str) -> str:
        # The counter guarantees uniqueness; the seeded suffix makes IDs differ by seed.
        self._count += 1
        return f"{prefix}_{self._count:04d}{self._rng.getrandbits(24):06x}"

    def reseed(self, seed: int) -> None:
        self._seed = seed
        self._rng = random.Random(seed)
        self._count = 0
