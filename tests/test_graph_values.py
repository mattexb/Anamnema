"""Temporal conversion handles typed nested values, never a catch-all string fallback."""

import pytest
from neo4j.time import Date, DateTime, Duration, Time
from pydantic import JsonValue, TypeAdapter, ValidationError

from anamnema.graph_values import json_temporals


def test_known_temporals_nested_in_note_data_are_iso_strings():
    values = (Date(2026, 1, 1), DateTime(2026, 1, 1), Time(12, 30), Duration(days=2))
    converted = json_temporals({"times": values, "nested": [{"when": values[0]}], "flag": False})
    assert converted["times"] == [value.iso_format() for value in values]
    assert converted["nested"] == [{"when": "2026-01-01"}]
    assert converted["flag"] is False
    TypeAdapter(JsonValue).validate_python(converted)


def test_unknown_objects_remain_invalid_instead_of_becoming_strings():
    unsupported = object()
    assert json_temporals(unsupported) is unsupported
    with pytest.raises(ValidationError):
        TypeAdapter(JsonValue).validate_python(json_temporals({"unknown": unsupported}))
