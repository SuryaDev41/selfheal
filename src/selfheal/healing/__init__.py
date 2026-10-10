"""Cache-backed selector healing for browser test execution."""

from .cache import LocatorCache
from .locator import HealingMatch, LocatorHealer

__all__ = ["HealingMatch", "LocatorCache", "LocatorHealer"]
