"""``.env.example`` must describe exactly the settings the application reads.

A sample env file is the configuration reference most operators read first, and a
stale one is worse than none: it advertises variables that do nothing while real
behaviour — whether API keys are required, whether a Redis outage takes the API
down — goes undiscoverable. These tests make drift a failure in both directions:
a settings field nobody documented, and a documented variable no code reads.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
#: ``NAME=value``, or ``# NAME=value`` for a commented-out default.
KEY_PATTERN = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)

#: Variables deliberately shown as something other than the code default, with the
#: reason. A real fallback secret must never be reproduced in a file people copy,
#: so the placeholder is the point — and this list keeps the exemption honest
#: instead of weakening the check for everything else.
DELIBERATE_PLACEHOLDERS = {"SECRET_KEY": "change-me-in-production"}


def _matches(shown: str, default: object) -> bool:
    """Does the text in the example express the code default?

    Types matter: ``30`` and ``30.0`` are the same number for a float setting,
    while ``1`` and ``true`` are not interchangeable for a boolean one, and a
    bare ``0`` next to an empty string would silently agree if everything were
    compared as text.
    """
    if isinstance(default, bool):
        return shown == ("true" if default else "false")
    if isinstance(default, (int, float)):
        try:
            return float(shown) == float(default)
        except ValueError:
            return False
    return shown == str(default)


def _documented_keys() -> set[str]:
    return set(KEY_PATTERN.findall(ENV_EXAMPLE.read_text()))


def test_every_setting_is_documented() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    missing = sorted(fields - _documented_keys())
    assert not missing, f".env.example does not mention: {missing}"


def test_no_undocumented_variable_is_advertised() -> None:
    fields = {name.upper() for name in Settings.model_fields}
    unknown = sorted(_documented_keys() - fields)
    assert not unknown, f".env.example lists variables that do not exist: {unknown}"


def test_documented_defaults_match_the_settings_model() -> None:
    """Where the example states a default, it has to be the real default.

    Only scalar values are compared; URLs, paths and list settings legitimately
    differ per environment.
    """
    text = ENV_EXAMPLE.read_text()
    shown_values = {
        match.group(1): match.group(2).strip()
        for match in re.finditer(r"^([A-Z][A-Z0-9_]*)=([^\n]*)", text, re.M)
    }
    fields = Settings.model_fields
    checked = 0
    for key, shown in shown_values.items():
        if key in DELIBERATE_PLACEHOLDERS:
            assert shown.split(" #")[0].strip() == DELIBERATE_PLACEHOLDERS[key], key
            continue
        field = fields.get(key.lower())
        if field is None or not shown:
            continue
        default = field.default
        if not isinstance(default, (bool, int, float, str)) or default is None:
            continue
        # A trailing comment on the line is documentation, not part of the value.
        value = shown.split(" #")[0].strip()
        assert _matches(value, default), (
            f"{key}: example says {value!r}, code default is {default!r}"
        )
        checked += 1
    assert checked > 20, f"only {checked} defaults were comparable — did matching break?"


def test_the_file_carries_no_secret_values() -> None:
    """Placeholders only: an example file gets copied verbatim by tired humans."""
    lines = ENV_EXAMPLE.read_text().splitlines()
    assignments = {
        line.split("=", 1)[0].strip(): line.split("=", 1)[1].strip()
        for line in lines
        if "=" in line and not line.lstrip().startswith("#")
    }
    for key, placeholder in DELIBERATE_PLACEHOLDERS.items():
        assert assignments[key].split(" #")[0].strip() == placeholder
    assert not assignments.get("AI_API_KEY", "")
    # A pepper or scrape token committed here is the leak, not the example.
    for key in ("API_KEY_PEPPER", "METRICS_TOKEN"):
        assert key not in assignments or assignments[key].startswith("#"), key


def test_no_setting_is_silently_production_mandatory() -> None:
    """The production guards are documented where an operator will read them.

    ``Settings`` refuses to start in production with several of these left at
    their development values; the example file has to say so, otherwise the first
    deploy fails with a message nobody expects.
    """
    text = ENV_EXAMPLE.read_text().lower()
    guarded = ("debug", "log_json", "api_key_enforcement_enabled", "rate_limit_enabled")
    for key in guarded:
        assert key in text, f"{key.upper()} is part of the production guards but undocumented"
    assert "production" in text
