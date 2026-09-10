"""Every object in the source schema rejects missing and unknown fields."""

import copy
from typing import Any

import pytest

from tests.test_schemas import _load_schemas, _source


def _object_paths(value: object, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    paths = [path] if isinstance(value, dict) else []
    if isinstance(value, dict):
        for key, child in value.items():
            paths.extend(_object_paths(child, (*path, key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.extend(_object_paths(child, (*path, index)))
    return paths


def test_all_source_objects_have_closed_required_keys() -> None:
    schema = _load_schemas()
    source = _source()
    for path in _object_paths(source):
        original: Any = source
        for part in path:
            original = original[part]
        for omitted in [None, *original]:
            changed = copy.deepcopy(source)
            target: Any = changed
            for part in path:
                target = target[part]
            if omitted is None:
                target["unknown"] = "must not pass"
            else:
                del target[omitted]
            with pytest.raises(ValueError, match="^invalid source$"):
                schema.validate_source(changed)


def test_nested_duplicate_json_keys_are_refused() -> None:
    schema = _load_schemas()
    with pytest.raises(ValueError, match="^invalid JSON$"):
        schema.parse_json('{"contacts":[{"name":{"display":"first","display":"second"}}]}')
