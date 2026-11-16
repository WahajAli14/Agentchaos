"""Known starting state for the mock service databases."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class Fixture:
    # sku -> units on hand. Inventory is the only service with seed data.
    stock: Mapping[str, int] = field(default_factory=dict)


DEFAULT_FIXTURE = Fixture(
    stock=MappingProxyType({"SKU-WIDGET": 100, "SKU-GADGET": 10, "SKU-SCARCE": 1})
)
