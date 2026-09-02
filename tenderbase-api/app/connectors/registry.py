"""Connector registry.

Connectors register themselves with the ``@register_connector`` decorator and
are resolved at runtime by key (preferred) or by connector type (fallback), so
adding a source never requires touching the ingestion engine or the API.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

from app.connectors.base import ProcurementConnector
from app.enums import ConnectorType
from app.errors import ConnectorNotRegisteredError

C = TypeVar("C", bound=type[ProcurementConnector])

_REGISTRY: dict[str, type[ProcurementConnector]] = {}
_DEFAULT_BY_TYPE: dict[ConnectorType, str] = {}


def register_connector(*, default_for_type: bool = False):  # noqa: ANN201
    """Class decorator registering a connector implementation.

    ``default_for_type=True`` marks the class as the fallback implementation
    for its :class:`ConnectorType`.
    """

    def decorator(cls: C) -> C:
        key = cls.key
        if not key or key == "base":
            raise ValueError(f"{cls.__name__} must define a unique 'key'")
        existing = _REGISTRY.get(key)
        if existing is not None and existing is not cls:
            raise ValueError(f"Connector key '{key}' is already registered by {existing.__name__}")
        _REGISTRY[key] = cls
        if default_for_type:
            _DEFAULT_BY_TYPE[ConnectorType.parse(cls.connector_type)] = key
        return cls

    return decorator


def get_connector_class(
    key: str | None = None, connector_type: ConnectorType | str | None = None
) -> type[ProcurementConnector]:
    """Resolve a connector class by explicit key, falling back to its type."""
    if key:
        try:
            return _REGISTRY[key]
        except KeyError as exc:
            raise ConnectorNotRegisteredError(
                f"No connector registered with key '{key}'", details={"key": key}
            ) from exc
    if connector_type is not None:
        parsed = ConnectorType.parse(connector_type)
        default_key = _DEFAULT_BY_TYPE.get(parsed)
        if default_key:
            return _REGISTRY[default_key]
        raise ConnectorNotRegisteredError(
            f"No default connector registered for type '{parsed}'",
            details={"connector_type": str(parsed)},
        )
    raise ConnectorNotRegisteredError("Either a connector key or a connector type is required")


def build_connector(
    key: str | None = None,
    connector_type: ConnectorType | str | None = None,
    *,
    fetcher: Any | None = None,
) -> ProcurementConnector:
    """Instantiate a connector, injecting the fetcher dependency."""
    return get_connector_class(key, connector_type)(fetcher=fetcher)


def list_connectors() -> list[dict[str, Any]]:
    """Describe every registered connector (used by ``GET /sources/connectors``)."""
    return sorted(
        (cls(fetcher=None).describe() for cls in _REGISTRY.values()),
        key=lambda item: item["key"],
    )


def registered_keys() -> Iterable[str]:
    return tuple(sorted(_REGISTRY))


def clear_registry() -> None:  # pragma: no cover - test helper
    _REGISTRY.clear()
    _DEFAULT_BY_TYPE.clear()
