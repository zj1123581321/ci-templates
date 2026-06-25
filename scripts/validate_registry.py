#!/usr/bin/env python3
"""Validate registry.yaml against registry.schema.json + fleet-level invariants.

Two layers of checking:
  1. JSON Schema  -> required fields, types, enums, formats (fail fast on typos).
  2. Cross-record -> uniqueness of id / port / monitor_slug / (host, deploy_dir),
                     and a guard that no plaintext DSN leaked into the registry.

Usage:
    validate_registry.py [registry.yaml] [--schema registry.schema.json]

Exit code 0 == valid, 1 == invalid (so CI fails the pipeline).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMA = REPO_ROOT / "registry.schema.json"

# A leaked value looks like  scheme://...  — a secret *reference* (env var name)
# never contains "://". Applies to every "store the reference, not the value" field.
_URL_RE = re.compile(r"://")

# (field, label) pairs that must hold a secret *reference* name, never the value.
_SECRET_REF_FIELDS = [
    ("sentry_dsn_secret", "dsn"),
    ("heartbeat_url_secret", "heartbeat url"),
]

# (field, human label) pairs that must be globally unique across services.
_UNIQUE_SCALAR_FIELDS = [
    ("id", "service id"),
    ("port", "port"),
    ("monitor_slug", "monitor_slug"),
]


def _load_yaml(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _schema_errors(registry, schema) -> list[str]:
    validator = Draft202012Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(registry), key=lambda e: list(e.path)):
        loc = "/".join(str(p) for p in err.path) or "<root>"
        errors.append(f"schema: {loc}: {err.message}")
    return errors


def _uniqueness_errors(registry) -> list[str]:
    errors = []
    services = registry.get("services", []) if isinstance(registry, dict) else []

    for field, label in _UNIQUE_SCALAR_FIELDS:
        seen: dict = {}
        for svc in services:
            if not isinstance(svc, dict) or field not in svc:
                continue
            val = svc[field]
            if val in seen:
                errors.append(
                    f"uniqueness: duplicate {label} {val!r} "
                    f"(services {seen[val]!r} and {svc.get('id', '?')!r})"
                )
            else:
                seen[val] = svc.get("id", "?")

    # (host, deploy_dir) must be unique together.
    seen_paths: dict = {}
    for svc in services:
        if not isinstance(svc, dict):
            continue
        host, deploy_dir = svc.get("host"), svc.get("deploy_dir")
        if host is None or deploy_dir is None:
            continue
        key = (host, deploy_dir)
        if key in seen_paths:
            errors.append(
                f"uniqueness: duplicate (host, deploy_dir) "
                f"{host}:{deploy_dir} (services {seen_paths[key]!r} and "
                f"{svc.get('id', '?')!r})"
            )
        else:
            seen_paths[key] = svc.get("id", "?")
    return errors


def _secret_leak_errors(registry) -> list[str]:
    errors = []
    services = registry.get("services", []) if isinstance(registry, dict) else []
    for svc in services:
        if not isinstance(svc, dict):
            continue
        for field, label in _SECRET_REF_FIELDS:
            val = svc.get(field)
            if isinstance(val, str) and _URL_RE.search(val):
                errors.append(
                    f"secret-leak: service {svc.get('id', '?')!r} {field} looks like "
                    f"a plaintext {label}, not a secret reference: {val!r}. "
                    f"Store the NAME of the secret (e.g. SENTRY_DSN_FOO) instead."
                )
    return errors


def validate(registry, schema) -> list[str]:
    """Return a list of error strings (empty == valid)."""
    errors = _schema_errors(registry, schema)
    # Cross-record checks still run even if schema failed — surface everything.
    errors += _uniqueness_errors(registry)
    errors += _secret_leak_errors(registry)
    return errors


def validate_file(registry_path, schema_path=DEFAULT_SCHEMA) -> list[str]:
    registry_path = Path(registry_path)
    schema_path = Path(schema_path)
    if not registry_path.exists():
        return [f"io: registry file not found: {registry_path}"]
    try:
        registry = _load_yaml(registry_path)
    except yaml.YAMLError as exc:
        return [f"io: failed to parse YAML {registry_path}: {exc}"]
    with open(schema_path, "r", encoding="utf-8") as fh:
        schema = json.load(fh)
    return validate(registry, schema)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Validate the fleet registry.")
    parser.add_argument(
        "registry",
        nargs="?",
        default=str(REPO_ROOT / "registry.yaml"),
        help="path to registry.yaml",
    )
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA))
    args = parser.parse_args(argv)

    errors = validate_file(args.registry, args.schema)
    if errors:
        print(f"FAIL: {args.registry} has {len(errors)} error(s):", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(f"OK: {args.registry} is valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
