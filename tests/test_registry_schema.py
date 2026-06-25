"""TDD: registry.yaml schema validator.

The validator must catch — fail fast, not silently break production:
  - missing required fields
  - bad enum values (typos in tier / rollback_safety)
  - duplicate service id / port / monitor_slug / (host, deploy_dir)
  - a Sentry DSN stored in plaintext instead of a secret *reference*

`validate_file(path)` returns a list of human-readable error strings;
empty list == valid.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from validate_registry import validate_file  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
VALID = FIXTURES / "valid"
INVALID = FIXTURES / "invalid"


def test_valid_registry_passes():
    errors = validate_file(VALID / "registry.yaml")
    assert errors == [], f"expected no errors, got: {errors}"


def test_real_registry_passes():
    """The repo's own registry.yaml must always validate."""
    errors = validate_file(REPO_ROOT / "registry.yaml")
    assert errors == [], f"registry.yaml is invalid: {errors}"


@pytest.mark.parametrize(
    "fixture,needle",
    [
        ("missing_field.yaml", "port"),
        ("bad_enum.yaml", "tier"),
        ("duplicate_id.yaml", "id"),
        ("duplicate_port.yaml", "port"),
        ("duplicate_slug.yaml", "monitor_slug"),
        ("duplicate_host_dir.yaml", "deploy_dir"),
        ("dsn_plaintext.yaml", "dsn"),
        ("heartbeat_plaintext.yaml", "heartbeat"),
    ],
)
def test_invalid_registry_fails(fixture, needle):
    errors = validate_file(INVALID / fixture)
    assert errors, f"{fixture} should have produced errors but passed"
    blob = " ".join(errors).lower()
    assert needle.lower() in blob, (
        f"{fixture}: expected an error mentioning '{needle}', got: {errors}"
    )


def test_cli_exits_nonzero_on_bad_registry():
    """The CI entrypoint must exit non-zero so the pipeline fails."""
    import subprocess

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "validate_registry.py"),
         str(INVALID / "missing_field.yaml")],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "port" in (result.stdout + result.stderr).lower()


def test_cli_exits_zero_on_good_registry():
    import subprocess

    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "validate_registry.py"),
         str(VALID / "registry.yaml")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
