"""Normalize known driver temporal values without stringifying unsupported graph data."""

from typing import Any, cast

from neo4j.time import Date, DateTime, Duration, Time


def json_temporals(value: Any) -> Any:
    """Keep ISO temporal strings at JSON boundaries, including future note properties."""
    if isinstance(value, Date | DateTime | Duration | Time):
        return value.iso_format()
    if isinstance(value, dict):
        return {key: json_temporals(item) for key, item in cast(dict[str, Any], value).items()}
    if isinstance(value, list | tuple):
        return [json_temporals(item) for item in cast(list[Any] | tuple[Any, ...], value)]
    return value
