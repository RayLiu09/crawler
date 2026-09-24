"""Recruitment source adapters."""

from .base import CollectionControl, SourceAdapter
from .fake import FakeSourceAdapter
from .zhaopin import ZhaopinBrowserConfig, ZhaopinSourceAdapter

__all__ = [
    "CollectionControl",
    "FakeSourceAdapter",
    "SourceAdapter",
    "ZhaopinBrowserConfig",
    "ZhaopinSourceAdapter",
]
