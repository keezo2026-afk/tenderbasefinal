"""Log destination, format and redaction must not depend on import order.

Every module creates its logger at import time with ``get_logger()``, and structlog
caches a bound logger on first use. Without care that means the *first* configuration
in the process wins for some loggers and not others — which in this codebase showed up
as a script's ``--json`` payload being unparsable because a log line landed on stdout,
and, far worse, as log lines emitted by early-imported modules bypassing the redaction
processor. These tests pin the behaviour that makes both impossible.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.logging import get_logger

pytestmark = pytest.mark.unit

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

# conftest sets LOG_LEVEL=WARNING to keep test output quiet; these tests assert on
# emitted lines, so they ask for INFO explicitly rather than inheriting the ambient level.
QUIET = Settings(app_env="test", log_level="INFO")


def _settings(**overrides: object) -> Settings:
    return QUIET.model_copy(update=overrides)


@pytest.fixture(autouse=True)
def _restore_logging():
    """configure_logging mutates process-global state; hand it back untouched."""
    yield
    from app.logging import configure_logging

    configure_logging()  # back to the ambient, process-wide configuration


def test_a_logger_created_before_configuration_is_still_routed(capsys):
    """The exact failure this module exists to prevent.

    A logger obtained before the stream was chosen must still write to the chosen
    stream: modules cannot control when they are imported, so "configure first" is
    not a promise the codebase can keep.
    """
    from app.logging import configure_logging

    early = get_logger("tenderbase.early")
    buffer = io.StringIO()
    configure_logging(QUIET, stream=buffer)

    early.info("written_after_reconfiguration", api_key="hunter2")

    assert "written_after_reconfiguration" in buffer.getvalue()
    # Nothing leaked to the default destination, which is what stdout is for.
    assert "written_after_reconfiguration" not in capsys.readouterr().out


def test_redaction_applies_whenever_the_logger_was_created():
    """Redaction is a property of the pipeline, not of the caller's import order."""
    from app.logging import configure_logging

    before = get_logger("tenderbase.before")
    buffer = io.StringIO()
    configure_logging(QUIET, stream=buffer)
    after = get_logger("tenderbase.after")

    before.info("early", secret="s3cr3t-value")
    after.info("late", password="another-secret")

    out = buffer.getvalue()
    assert "s3cr3t-value" not in out
    assert "another-secret" not in out
    assert out.count("***redacted***") == 2


def test_json_rendering_follows_the_settings():
    from app.logging import configure_logging

    buffer = io.StringIO()
    configure_logging(_settings(app_env="production", log_json=True), stream=buffer)
    get_logger("tenderbase.json").info("json_event", field="value")

    payload = json.loads(buffer.getvalue().strip().splitlines()[-1])
    assert payload["event"] == "json_event"
    assert payload["field"] == "value"
    assert payload["service"] == "TenderBase API"


def test_context_ids_reach_log_lines_emitted_by_any_module():
    from app.logging import configure_logging, request_id_ctx

    buffer = io.StringIO()
    configure_logging(QUIET, stream=buffer)
    token = request_id_ctx.set("req-123")
    try:
        get_logger("tenderbase.ctx").info("with_context")
    finally:
        request_id_ctx.reset(token)

    assert "req-123" in buffer.getvalue()


def test_scripts_that_emit_json_send_logs_to_stderr() -> None:
    """A script's stdout is its result; a stray log line makes it unparsable.

    Checked by reading the sources because the property is about the *entrypoint*,
    and an accidental `configure_logging()` without a stream in one script would
    break `... --json | jq` for its users with no test failure anywhere else.
    """
    offenders = []
    for script in sorted(SCRIPTS.glob("*.py")):
        text = script.read_text()
        if '"--json"' not in text:
            continue
        if re.search(r"configure_logging\(\s*\)", text):
            offenders.append(script.name)
    assert not offenders, (
        f"{offenders} print JSON on stdout but configure logging to stdout; pass stream=sys.stderr"
    )


def test_stdout_of_a_json_script_is_machine_readable() -> None:
    """End-to-end proof, on the mechanism rather than a grep.

    Runs a child process that logs *and* prints JSON, and asserts stdout is one
    parseable document — the failure mode being a log line glued to the front of it.
    """
    program = (
        "import json, sys\n"
        "from app.logging import configure_logging, get_logger\n"
        "from app.config import Settings\n"
        "log = get_logger('probe')\n"
        "from app.logging import configure_logging\n"
        "configure_logging(Settings(app_env='test', log_level='INFO'), stream=sys.stderr)\n"
        "log.info('noise_before_output')\n"
        "print(json.dumps({'ok': True}))\n"
        "log.info('noise_after_output')\n"
    )
    env = {**os.environ, "LOG_LEVEL": "INFO", "APP_ENV": "test"}
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(SCRIPTS.parent),
        env=env,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"ok": True}
    assert "noise_before_output" in completed.stderr
    assert "noise_after_output" in completed.stderr
