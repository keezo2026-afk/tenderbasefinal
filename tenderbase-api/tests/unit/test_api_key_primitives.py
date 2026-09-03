"""Pure API-key primitives: scope parsing and key shape.

`parse_scopes` decides what a grant means, and it is fed from three directions —
the CLI (argparse list), a JSON body (list of strings) and operators writing a
comma-separated list — so the accepted shapes are pinned here rather than inferred
from whichever transport a route test happens to use.
"""

from __future__ import annotations

import pytest

from app.db.models.security import SCOPE_PRESETS
from app.enums import API_KEY_SCOPES
from app.errors import ValidationError
from app.services.api_key_service import generate_raw_key, hash_api_key, parse_scopes

pytestmark = pytest.mark.unit


class TestParseScopes:
    def test_none_grants_the_readonly_preset(self):
        assert parse_scopes(None) == list(SCOPE_PRESETS["readonly"])

    @pytest.mark.parametrize(
        "given",
        [
            ["read:tenders", "read:statistics"],
            ["read:tenders,read:statistics"],
            "read:tenders,read:statistics",
            ["read:tenders", " read:statistics "],
            ["read:tenders,read:tenders,read:statistics"],
        ],
    )
    def test_separators_and_duplicates_do_not_change_the_grant(self, given):
        assert parse_scopes(given) == ["read:tenders", "read:statistics"]

    def test_a_preset_inside_a_list_expands(self):
        parsed = parse_scopes(["readonly", "read:documents"])
        assert set(SCOPE_PRESETS["readonly"]) <= set(parsed)
        assert "read:documents" in parsed
        assert len(parsed) == len(set(parsed))

    def test_a_typo_is_rejected_rather_than_dropped(self):
        with pytest.raises(ValidationError) as excinfo:
            parse_scopes(["read:tender"])
        assert excinfo.value.code == "INVALID_SCOPE"
        assert sorted(excinfo.value.details["allowed"]) == sorted(API_KEY_SCOPES)

    def test_a_comma_typo_is_rejected_too(self):
        """The CLI's list form must not smuggle 'read:tenders read:x' past validation."""
        with pytest.raises(ValidationError) as excinfo:
            parse_scopes(["read:tenders,read:tender"])
        assert "read:tender" in str(excinfo.value)

    def test_nothing_selected_is_rejected(self):
        with pytest.raises(ValidationError):
            parse_scopes(["", "  "])

    def test_every_documented_scope_parses(self):
        """The docs list the valid scopes; this keeps that list true."""
        assert parse_scopes(list(API_KEY_SCOPES)) == list(API_KEY_SCOPES)


class TestKeyShape:
    def test_prefix_is_visible_and_the_secret_is_long(self):
        raw_key, prefix, secret = generate_raw_key("test")
        assert prefix.startswith("tb_test_")
        assert raw_key == f"{prefix}_{secret}"
        assert len(secret) >= 40, "a 256-bit token in base64url is ~43 characters"

    def test_environment_label_is_sanitised(self):
        # A label is cosmetic, but it is pasted into headers and logs, so it must not
        # be able to carry structure characters.
        raw_key, prefix, _ = generate_raw_key("Prod-Usr/East 2")
        assert prefix.startswith("tb_produsr"), prefix
        assert all(ch.isalnum() or ch == "_" for ch in prefix)
        assert "\n" not in raw_key and " " not in raw_key

    def test_two_calls_never_produce_the_same_key(self):
        assert generate_raw_key("live")[0] != generate_raw_key("live")[0]

    def test_hash_is_deterministic_per_pepper(self):
        raw_key = generate_raw_key("live")[0]
        first = hash_api_key(raw_key, pepper="pepper-a")
        assert first == hash_api_key(raw_key, pepper="pepper-a")
        assert first != hash_api_key(raw_key, pepper="pepper-b")
        assert len(first) == 64  # sha256 hex, which is what the column stores

    def test_empty_key_is_refused(self):
        with pytest.raises(ValidationError):
            hash_api_key("", pepper="p")
