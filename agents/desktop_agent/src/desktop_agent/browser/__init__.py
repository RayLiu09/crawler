"""Browser process ownership and lifecycle supervision."""

from .processes import (
    BrowserProcessSupervisor,
    BrowserShutdownError,
    OwnedProcessTree,
    ProcessIdentity,
    PsutilProcessBackend,
)

__all__ = [
    "BrowserProcessSupervisor",
    "BrowserShutdownError",
    "OwnedProcessTree",
    "ProcessIdentity",
    "PsutilProcessBackend",
]
