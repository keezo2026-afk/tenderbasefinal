"""Connector package.

Importing this package registers every built-in connector with the registry.
"""

from app.connectors.base import (  # noqa: F401
    DiscoveryTarget,
    FetchResult,
    ProcurementConnector,
    RawItem,
    SourceContext,
)
from app.connectors.browser import BrowserConnector  # noqa: F401
from app.connectors.custom.etender import ETenderOCDSConnector  # noqa: F401
from app.connectors.html import HTMLListingConnector  # noqa: F401
from app.connectors.http import HTTPJSONConnector  # noqa: F401
from app.connectors.pdf import PDFRepositoryConnector  # noqa: F401
from app.connectors.registry import (  # noqa: F401
    build_connector,
    get_connector_class,
    list_connectors,
    register_connector,
    registered_keys,
)
from app.connectors.wordpress import WordPressConnector  # noqa: F401

__all__ = [
    "BrowserConnector",
    "DiscoveryTarget",
    "ETenderOCDSConnector",
    "FetchResult",
    "HTMLListingConnector",
    "HTTPJSONConnector",
    "PDFRepositoryConnector",
    "ProcurementConnector",
    "RawItem",
    "SourceContext",
    "WordPressConnector",
    "build_connector",
    "get_connector_class",
    "list_connectors",
    "register_connector",
    "registered_keys",
]
