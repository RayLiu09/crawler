"""Browser automation contracts, engine implementations and process supervision."""

from .base import (
    BrowserDriver,
    BrowserElement,
    BrowserLaunchOptions,
    By,
)
from .errors import (
    BrowserDriverUnavailableError,
    BrowserError,
    BrowserTimeoutError,
)
from .processes import (
    BrowserProcessSupervisor,
    BrowserShutdownError,
    OwnedProcessTree,
    ProcessIdentity,
    ProcessIdProvider,
    PsutilProcessBackend,
)

__all__ = [
    "BrowserDriver",
    "BrowserDriverUnavailableError",
    "BrowserElement",
    "BrowserError",
    "BrowserLaunchOptions",
    "BrowserProcessSupervisor",
    "BrowserShutdownError",
    "BrowserTimeoutError",
    "By",
    "OwnedProcessTree",
    "ProcessIdProvider",
    "ProcessIdentity",
    "PsutilProcessBackend",
]
