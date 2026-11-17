"""The environment surface `brian evolve` integrates through.

A version file that names an environment variable the loader does not read is worse than no version file:
it records a change, an operator exports it, and nothing happens. These tests fire every entry of
`ENV_VARS` through the real `load_settings`, so a name that drifts fails here instead of in deployment.
"""
from __future__ import annotations

import dataclasses

import pytest

from brian.config import DEFAULT_CALIBRATION_PATH, ENV_VARS, load_settings


def _probe(settings, section: str, field: str):
    return getattr(getattr(settings, section), field)


def _replacement(value):
    """A value of the same type that is never equal to the default."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 7
    if isinstance(value, float):
        return round(value + 0.125, 3)
    return "%s-probe" % value


def _env_text(value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def test_every_mapped_variable_is_read_by_the_loader(monkeypatch):
    defaults = load_settings()
    expected = {}
    for (section, field), name in ENV_VARS.items():
        target = _replacement(_probe(defaults, section, field))
        monkeypatch.setenv(name, _env_text(target))
        expected[(section, field)] = target

    loaded = load_settings()
    for (section, field), target in expected.items():
        actual = _probe(loaded, section, field)
        assert actual == target, (section, field, ENV_VARS[(section, field)], actual, target)


def test_mapped_fields_exist_on_their_sections():
    settings = load_settings()
    for section, field in ENV_VARS:
        section_obj = getattr(settings, section, None)
        assert section_obj is not None, section
        names = {item.name for item in dataclasses.fields(section_obj)}
        assert field in names, (section, field)


def test_unset_variables_restore_the_defaults(monkeypatch):
    """The rollback story in every version file is "unset the variable"."""
    pristine = load_settings()
    for (section, field), name in ENV_VARS.items():
        monkeypatch.setenv(name, _env_text(_replacement(_probe(pristine, section, field))))

    assert load_settings().gate.reuse_report_within_s != pristine.gate.reuse_report_within_s

    for name in ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)

    restored = load_settings()
    for (section, field) in ENV_VARS:
        assert _probe(restored, section, field) == _probe(pristine, section, field), (section, field)
    assert restored.calibration.path == DEFAULT_CALIBRATION_PATH


def test_analyze_actions_is_configurable(monkeypatch):
    """Round 2's routing policy used to be a CLI flag only, i.e. unpersistable."""
    monkeypatch.setenv("BRIAN_GATE_ANALYZE_ACTIONS", "page_human")
    monkeypatch.setenv("BRIAN_GATE_ALWAYS_ANALYZE", "0")
    settings = load_settings()
    assert settings.gate.analyze_actions == "page_human"
    assert settings.gate.always_analyze_alerts is False


def test_reuse_window_is_configurable(monkeypatch):
    monkeypatch.setenv("BRIAN_GATE_REUSE_REPORT_S", "300")
    assert load_settings().gate.reuse_report_within_s == 300


def test_no_mapped_variable_is_claimed_twice():
    names = list(ENV_VARS.values())
    assert len(names) == len(set(names))


@pytest.mark.parametrize("section,field", sorted(ENV_VARS))
def test_each_entry_is_individually_load_bearing(monkeypatch, section, field):
    """Setting one variable must move exactly the field it names."""
    pristine = load_settings()
    name = ENV_VARS[(section, field)]
    target = _replacement(_probe(pristine, section, field))
    monkeypatch.setenv(name, _env_text(target))

    loaded = load_settings()
    assert _probe(loaded, section, field) == target
    for (other_section, other_field) in ENV_VARS:
        if (other_section, other_field) == (section, field):
            continue
        assert _probe(loaded, other_section, other_field) == _probe(pristine, other_section, other_field)
