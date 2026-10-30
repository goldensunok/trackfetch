"""Provider adapters.

Each module in this package wraps exactly one external mechanism and exposes it
through the interface in ``base.py``. Nothing outside this package imports a
provider module directly; the registry does it.
"""

from trackfetch.providers.base import (
    BrowseEntry,
    Provider,
    ProviderContext,
    SearchOptions,
)
from trackfetch.providers.errors import TrackfetchError

__all__ = [
    "BrowseEntry",
    "Provider",
    "ProviderContext",
    "SearchOptions",
    "TrackfetchError",
]
