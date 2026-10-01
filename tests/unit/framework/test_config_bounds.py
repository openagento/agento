"""Numeric bounds are enforced on every source (PRD E3-E5 §3.3, SEC-9).

Three halves, and they are not interchangeable:

* `config:set` refuses an out-of-bounds write (this file, part 1);
* `module:validate` refuses an out-of-bounds `config.json` DEFAULT, which is what makes
  the fallback trustworthy - there is no second default to fall back to (part 2);
* the resolver substitutes that default when ENV or a legacy DB row is out of bounds,
  unparseable, or the wrong type, rather than serving a value nothing validated (part 3).
"""

import json
from pathlib import Path

import pytest

from agento.framework.config_write import ConfigWriteError, validate_config_value

SYSTEM = {
    "stream/max_per_user": {"type": "integer", "label": "Streams per user", "min": 1, "max": 16},
    "retention/event_days": {"type": "integer", "label": "Event retention (days)", "min": 1},
    "unbounded_count": {"type": "integer", "label": "No bounds declared"},
    "title": {"type": "string", "label": "A string field"},
}
CONFIG = {"stream/max_per_user": 4, "retention/event_days": 90, "unbounded_count": 7, "title": "x"}


@pytest.fixture
def boundmod(tmp_path, monkeypatch):
    """A module on disk named `boundmod`, resolvable by _find_module_dir."""
    mod = tmp_path / "boundmod"
    mod.mkdir()
    (mod / "module.json").write_text(json.dumps(
        {"name": "boundmod", "version": "1.0.0", "description": "bounds"}
    ))
    (mod / "system.json").write_text(json.dumps(SYSTEM))
    (mod / "config.json").write_text(json.dumps(CONFIG))

    from agento.framework import core_config

    monkeypatch.setattr(core_config, "_find_module_dir", lambda name: mod if name == "boundmod" else None)
    return mod


# --- part 1: the write path --------------------------------------------------------


@pytest.mark.parametrize("value", ["0", "-1", "17", "999999"])
def test_a_value_outside_the_range_is_refused(boundmod, value):
    with pytest.raises(ConfigWriteError) as exc:
        validate_config_value("boundmod/stream/max_per_user", value)
    assert "1" in str(exc.value) and "16" in str(exc.value), str(exc.value)


@pytest.mark.parametrize("value", ["1", "4", "16"])
def test_a_value_inside_the_range_is_accepted(boundmod, value):
    validate_config_value("boundmod/stream/max_per_user", value)


def test_zero_is_refused_where_the_minimum_is_positive(boundmod):
    """0 is the value that disables a limit; it must never be writable."""
    with pytest.raises(ConfigWriteError):
        validate_config_value("boundmod/retention/event_days", "0")


def test_a_non_numeric_value_is_refused_for_a_numeric_field(boundmod):
    with pytest.raises(ConfigWriteError) as exc:
        validate_config_value("boundmod/stream/max_per_user", "abc")
    assert "abc" in str(exc.value)


def test_a_field_with_no_bounds_is_unaffected(boundmod):
    validate_config_value("boundmod/unbounded_count", "0")
    validate_config_value("boundmod/unbounded_count", "-5")


def test_a_non_numeric_field_is_unaffected(boundmod):
    validate_config_value("boundmod/title", "anything at all")


def test_only_the_upper_bound_when_only_max_is_declared(tmp_path, monkeypatch):
    mod = tmp_path / "maxonly"
    mod.mkdir()
    (mod / "module.json").write_text(json.dumps(
        {"name": "maxonly", "version": "1.0.0", "description": "m"}
    ))
    (mod / "system.json").write_text(json.dumps({"n": {"type": "integer", "label": "n", "max": 10}}))
    from agento.framework import core_config

    monkeypatch.setattr(core_config, "_find_module_dir", lambda name: mod if name == "maxonly" else None)
    validate_config_value("maxonly/n", "-100")          # no minimum declared
    with pytest.raises(ConfigWriteError):
        validate_config_value("maxonly/n", "11")


# --- part 2: the default is what makes the fallback trustworthy ---------------------


def test_module_validate_refuses_an_out_of_bounds_default(tmp_path):
    from agento.framework.module_validator import validate_module

    mod = tmp_path / "baddefault"
    mod.mkdir()
    (mod / "module.json").write_text(json.dumps(
        {"name": "baddefault", "version": "1.0.0", "description": "d"}
    ))
    (mod / "system.json").write_text(json.dumps(SYSTEM))
    (mod / "config.json").write_text(json.dumps({**CONFIG, "stream/max_per_user": 0}))

    errors = validate_module(mod)
    assert any("stream/max_per_user" in e and "range" in e for e in errors), errors


def test_module_validate_refuses_a_non_numeric_default(tmp_path):
    from agento.framework.module_validator import validate_module

    mod = tmp_path / "baddefault2"
    mod.mkdir()
    (mod / "module.json").write_text(json.dumps(
        {"name": "baddefault2", "version": "1.0.0", "description": "d"}
    ))
    (mod / "system.json").write_text(json.dumps(SYSTEM))
    (mod / "config.json").write_text(json.dumps({**CONFIG, "retention/event_days": "ninety"}))

    errors = validate_module(mod)
    assert any("retention/event_days" in e for e in errors), errors


def test_module_validate_accepts_defaults_inside_their_bounds(tmp_path):
    from agento.framework.module_validator import validate_module

    mod = tmp_path / "gooddefault"
    mod.mkdir()
    (mod / "module.json").write_text(json.dumps(
        {"name": "gooddefault", "version": "1.0.0", "description": "d"}
    ))
    (mod / "system.json").write_text(json.dumps(SYSTEM))
    (mod / "config.json").write_text(json.dumps(CONFIG))

    assert [e for e in validate_module(mod) if "bound" in e or "min" in e or "max" in e] == []


def test_shipped_core_modules_have_defaults_inside_their_bounds():
    """The static check that makes runtime substitution safe, run on what ships."""
    from agento.framework.module_validator import validate_config_defaults

    root = Path(__file__).resolve().parents[3] / "src" / "agento" / "modules"
    for mod in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        assert validate_config_defaults(mod) == [], mod.name


# --- part 3: the resolver substitutes the validated default -------------------------


BOUNDED = {"type": "integer", "label": "Streams per user", "min": 1, "max": 16}
UNBOUNDED = {"type": "integer", "label": "No bounds declared"}
DEFAULTS = {"stream/max_per_user": 4, "unbounded_count": 7}


def _resolve(field, schema=BOUNDED, env=None, db=None, defaults=None, monkeypatch=None):
    from agento.framework.config_resolver import resolve_field

    if env is not None:
        monkeypatch.setenv("CONFIG__BOUNDMOD__" + field.upper().replace("/", "__"), env)
    overrides = {f"boundmod/{field}": (db, False)} if db is not None else {}
    return resolve_field(
        "boundmod", field, schema, DEFAULTS if defaults is None else defaults, overrides
    )


@pytest.mark.parametrize("bad", ["0", "99", "-3", "abc", ""])
def test_an_out_of_bounds_env_value_falls_back_to_the_default(bad, monkeypatch):
    got = _resolve("stream/max_per_user", env=bad, monkeypatch=monkeypatch)
    assert (got.value, got.source) == (4, "config.json")


@pytest.mark.parametrize("bad", ["0", "99", "abc"])
def test_an_out_of_bounds_db_row_falls_back_to_the_default(bad, monkeypatch):
    got = _resolve("stream/max_per_user", db=bad, monkeypatch=monkeypatch)
    assert (got.value, got.source) == (4, "config.json")


def test_an_in_bounds_env_value_is_served(monkeypatch):
    got = _resolve("stream/max_per_user", env="9", monkeypatch=monkeypatch)
    assert (got.value, got.source) == (9, "env")


def test_an_in_bounds_db_row_is_served(monkeypatch):
    got = _resolve("stream/max_per_user", db="16", monkeypatch=monkeypatch)
    assert (got.value, got.source) == (16, "db")


def test_env_out_of_bounds_does_not_promote_the_db_row(monkeypatch):
    """The fallback is to the VALIDATED default, never to the other dynamic source."""
    got = _resolve("stream/max_per_user", env="0", db="12", monkeypatch=monkeypatch)
    assert (got.value, got.source) == (4, "config.json")


def test_an_unbounded_field_is_unaffected(monkeypatch):
    got = _resolve("unbounded_count", schema=UNBOUNDED, env="0", monkeypatch=monkeypatch)
    assert (got.value, got.source) == (0, "env")


def test_a_bounded_field_with_no_default_raises(monkeypatch):
    """Fail closed: there is nothing validated to serve (SEC-9)."""
    from agento.framework.config_resolver import MissingBoundedDefault

    with pytest.raises(MissingBoundedDefault) as exc:
        _resolve("stream/max_per_user", env="0", defaults={}, monkeypatch=monkeypatch)
    assert "stream/max_per_user" in str(exc.value)


def test_a_bounded_field_with_no_default_and_no_override_raises(monkeypatch):
    from agento.framework.config_resolver import MissingBoundedDefault

    with pytest.raises(MissingBoundedDefault):
        _resolve("stream/max_per_user", defaults={}, monkeypatch=monkeypatch)


# --- part 4: a `number` field is a number, and a bound that cannot refuse is no bound ---
#
# `float("nan")` compares False against BOTH ends of a range, so a range check alone lets
# it through and the field then holds a value no later comparison will ever refuse;
# `float("inf")` slips a one-sided bound the same way. And without a `number` branch in the
# coercion, the same field answered as a `float` from config.json and as a `str` from ENV
# or the DB.

NUMBER = {"type": "number", "label": "A rate", "min": 0, "max": 1}
UNBOUNDED_NUMBER = {"type": "number", "label": "No bounds declared"}


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_a_non_finite_value_is_refused_by_a_bounded_number_field(value):
    from agento.framework.config_schema import numeric_bound_error

    assert numeric_bound_error("rate", NUMBER, value) is not None


@pytest.mark.parametrize("value", ["inf", "nan"])
def test_a_non_finite_value_is_refused_by_a_one_sided_bound(value):
    """A minimum alone is where NaN and +inf are most obviously not refused by comparison."""
    from agento.framework.config_schema import numeric_bound_error

    assert numeric_bound_error("rate", {"type": "number", "min": 0}, value) is not None


def test_an_unbounded_number_field_is_left_alone():
    """Same contract as the unparseable case: only a field that declares a bound is refused."""
    from agento.framework.config_schema import numeric_bound_error

    assert numeric_bound_error("rate", UNBOUNDED_NUMBER, "nan") is None


@pytest.mark.parametrize("value", ["0", "0.25", "1"])
def test_a_finite_number_inside_the_range_is_accepted(value):
    from agento.framework.config_schema import numeric_bound_error

    assert numeric_bound_error("rate", NUMBER, value) is None


@pytest.mark.parametrize("bad", ["nan", "inf", "-1", "2"])
def test_a_bad_env_number_falls_back_to_the_validated_default(bad, monkeypatch):
    got = _resolve("rate", schema=NUMBER, env=bad,
                   defaults={"rate": 0.5}, monkeypatch=monkeypatch)
    assert (got.value, got.source) == (0.5, "config.json")


@pytest.mark.parametrize("source", ["env", "db"])
def test_a_number_resolves_to_a_float_from_every_source(source, monkeypatch):
    """One field, one type. A `str` from ENV and a `float` from config.json is two."""
    kwargs = {source: "0.25"}
    got = _resolve("rate", schema=NUMBER, defaults={"rate": 0.5}, monkeypatch=monkeypatch,
                   **kwargs)

    assert isinstance(got.value, float) and got.value == 0.25


def test_config_set_refuses_a_non_finite_number(tmp_path, monkeypatch):
    mod = tmp_path / "nummod"
    mod.mkdir()
    (mod / "module.json").write_text(json.dumps(
        {"name": "nummod", "version": "1.0.0", "description": "numbers"}))
    (mod / "system.json").write_text(json.dumps({"rate": NUMBER}))
    (mod / "config.json").write_text(json.dumps({"rate": 0.5}))
    from agento.framework import core_config

    monkeypatch.setattr(core_config, "_find_module_dir",
                        lambda name: mod if name == "nummod" else None)

    with pytest.raises(ConfigWriteError):
        validate_config_value("nummod/rate", "nan")


def test_module_validate_refuses_a_non_finite_default(tmp_path):
    """There is no second default to fall back to, so this one must be a real number."""
    from agento.framework.module_validator import validate_module

    mod = tmp_path / "nummod"
    mod.mkdir()
    (mod / "module.json").write_text(json.dumps(
        {"name": "nummod", "version": "1.0.0", "description": "numbers"}))
    (mod / "system.json").write_text(json.dumps({"rate": NUMBER}))
    (mod / "config.json").write_text('{"rate": 1e999}')     # JSON has no Infinity literal

    errors = validate_module(mod)

    assert any("rate" in e for e in errors), errors
