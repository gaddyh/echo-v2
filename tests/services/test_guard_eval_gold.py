"""Validation tests for the human-authored Guard Gold loader."""

from __future__ import annotations

import json

import pytest

from echo_v2.services.guard_eval_gold import load_guard_gold


def _write(tmp_path, payload):
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _scenario(**overrides):
    value = {
        "id": "case",
        "messages": [{"id": "m1", "sender": "other", "text": "hi"}],
        "snapshots": [{"after_message_id": "m1", "expected": {}}],
    }
    value.update(overrides)
    return value


def test_canonical_gold_loads_all_scenarios() -> None:
    gold = load_guard_gold()
    assert len(gold.scenarios) == 6
    assert gold.scenario(gold.scenarios[0].scenario_id).title
    with pytest.raises(KeyError):
        gold.scenario("missing")


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({}, TypeError),
        ({"scenarios": [], "source": None}, TypeError),
        ({"scenarios": [_scenario(), _scenario()], "source": {}}, ValueError),
        ({"scenarios": [_scenario(messages=[{"id": "m1", "sender": "x", "text": "a"}, {"id": "m1", "sender": "x", "text": "b"}])], "source": {}}, ValueError),
        ({"scenarios": [_scenario(snapshots=[])], "source": {}}, ValueError),
    ],
)
def test_gold_loader_rejects_invalid_fixture_shapes(tmp_path, payload, error) -> None:
    with pytest.raises(error):
        load_guard_gold(_write(tmp_path, payload))


def test_gold_loader_rejects_invalid_checkpoint_references(tmp_path) -> None:
    with pytest.raises(ValueError):
        load_guard_gold(_write(tmp_path, {"source": {}, "scenarios": [_scenario(snapshots=[{"after_message_id": "m9", "expected": {}}])]}))
    with pytest.raises(TypeError):
        load_guard_gold(_write(tmp_path, {"source": {}, "scenarios": [_scenario(snapshots=[None])]}))
    with pytest.raises(ValueError):
        load_guard_gold(_write(tmp_path, {"source": {}, "scenarios": [_scenario(snapshots=[{"after_message_id": "m1", "expected": {}}, {"after_message_id": "m1", "expected": {}}])]}))


def test_gold_loader_rejects_non_object_scenario_and_mapping_types(tmp_path) -> None:
    with pytest.raises(TypeError):
        load_guard_gold(_write(tmp_path, {"source": {}, "scenarios": [None]}))
    with pytest.raises(TypeError):
        load_guard_gold(_write(tmp_path, {"source": {}, "scenarios": [_scenario(snapshots=[{"after_message_id": "m1", "expected": []}])]}))


def test_gold_loader_rejects_invalid_expectation_types(tmp_path) -> None:
    with pytest.raises(TypeError):
        load_guard_gold(_write(tmp_path, {"source": {}, "scenarios": [_scenario(snapshots=[{"after_message_id": "m1", "expected": {"required_signals": "bad"}}])]}))
    with pytest.raises(TypeError):
        load_guard_gold(_write(tmp_path, {"source": {}, "scenarios": [_scenario(alert_window=[]) ]}))
