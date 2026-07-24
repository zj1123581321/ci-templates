#!/usr/bin/env python3
"""Validate and normalize a D3 multi-image release declaration.

The workflow deliberately hands this program JSON as one input value and only
uses the generated, line-oriented manifests afterwards.  No caller-provided
JSON is interpolated into a shell program.  The manifest grammar is deliberately
small so the SSH-side script needs only bash, docker, curl and flock.

Image entries have the following fields:

``image_name`` (required), ``build_context`` (required), ``dockerfile``
(required), and optional ``build_alias``.  Entries sharing a build alias must
point at the same context and Dockerfile; the first entry is built and later
entries are published by tag only.  This is useful for worker aliases and
prevents accidentally rebuilding the same artifact.

Probe entries have ``url`` and optional ``expect_status`` (default 200).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
ALIAS_RE = IMAGE_RE
STATUS_RE = re.compile(r"^[1-5][0-9][0-9]$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
FORBIDDEN_URL_RE = re.compile(r"[\s;|&$`(){}<>\[\]\\\"']")
IMAGE_KEYS = frozenset({"image_name", "build_context", "dockerfile", "build_alias"})
PROBE_KEYS = frozenset({"url", "expect_status"})


class ValidationError(ValueError):
    """Input is not safe to pass to the release pipeline."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{label} must be a non-empty string")
    if CONTROL_RE.search(value):
        raise ValidationError(f"{label} contains a control character")
    return value


def _relative_path(value: object, label: str) -> str:
    value = _text(value, label)
    if label.endswith("build_context") and value == ".":
        return value
    if value.startswith("/") or value.startswith("\\"):
        raise ValidationError(f"{label} must be relative")
    if "\\" in value:
        raise ValidationError(f"{label} must not contain backslashes")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
        raise ValidationError(f"{label} contains unsafe characters")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValidationError(f"{label} contains an unsafe path component")
    if value.startswith("-"):
        raise ValidationError(f"{label} must not start with '-'")
    return value


def _validate_image(raw: object, index: int) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValidationError(f"image {index} must be an object")
    unknown = set(raw) - IMAGE_KEYS
    if unknown:
        raise ValidationError(f"image {index} has unknown field(s): {', '.join(sorted(unknown))}")
    missing = IMAGE_KEYS - {"build_alias"} - set(raw)
    if missing:
        raise ValidationError(f"image {index} missing required field(s): {', '.join(sorted(missing))}")
    image_name = _text(raw.get("image_name"), f"image {index}.image_name")
    if not IMAGE_RE.fullmatch(image_name):
        raise ValidationError(f"image {index}.image_name is unsafe: {image_name!r}")
    build_context = _relative_path(raw.get("build_context"), f"image {index}.build_context")
    dockerfile = _relative_path(raw.get("dockerfile"), f"image {index}.dockerfile")
    build_alias = raw.get("build_alias", image_name)
    build_alias = _text(build_alias, f"image {index}.build_alias")
    if not ALIAS_RE.fullmatch(build_alias):
        raise ValidationError(f"image {index}.build_alias is unsafe: {build_alias!r}")
    return {
        "image_name": image_name,
        "build_context": build_context,
        "dockerfile": dockerfile,
        "build_alias": build_alias,
    }


def _validate_probe(raw: object, index: int) -> tuple[str, str]:
    if not isinstance(raw, dict):
        raise ValidationError(f"probe {index} must be an object")
    unknown = set(raw) - PROBE_KEYS
    if unknown:
        raise ValidationError(f"probe {index} has unknown field(s): {', '.join(sorted(unknown))}")
    url = _text(raw.get("url"), f"probe {index}.url")
    if len(url) > 2048 or FORBIDDEN_URL_RE.search(url):
        raise ValidationError(f"probe {index}.url contains unsafe characters")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValidationError(f"probe {index}.url must be an absolute http(s) URL")
    try:
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValidationError(f"probe {index}.url has an invalid port")
    except ValueError as exc:
        raise ValidationError(f"probe {index}.url has an invalid port") from exc
    status = raw.get("expect_status", 200)
    if isinstance(status, int) and not isinstance(status, bool):
        status = str(status)
    status = _text(status, f"probe {index}.expect_status")
    if not STATUS_RE.fullmatch(status):
        raise ValidationError(f"probe {index}.expect_status must be an HTTP status 100-599")
    return url, status


def normalize(images_raw: object, probes_raw: object) -> tuple[list[dict[str, str]], list[tuple[str, str]], list[tuple[str, str, str, str, list[str]]]]:
    if not isinstance(images_raw, list) or not images_raw:
        raise ValidationError("images_json must be a non-empty array")
    images = [_validate_image(item, i) for i, item in enumerate(images_raw)]
    names = [item["image_name"] for item in images]
    if len(names) != len(set(names)):
        raise ValidationError("image_name values must be unique")
    if probes_raw is None:
        probes_raw = []
    if not isinstance(probes_raw, list):
        raise ValidationError("probes_json must be an array")
    probes = [_validate_probe(item, i) for i, item in enumerate(probes_raw)]

    # (alias, context, dockerfile, canonical image, all published names)
    grouped: dict[str, list[dict[str, str]]] = {}
    for item in images:
        grouped.setdefault(item["build_alias"], []).append(item)
    builds = []
    for alias, entries in grouped.items():
        source = (entries[0]["build_context"], entries[0]["dockerfile"])
        if any((entry["build_context"], entry["dockerfile"]) != source for entry in entries[1:]):
            raise ValidationError(f"build_alias {alias!r} points at different build_context/dockerfile values")
        builds.append((alias, source[0], source[1], entries[0]["image_name"], [e["image_name"] for e in entries]))
    return images, probes, builds


def _write_manifest(path: Path, images, probes) -> None:
    lines = ["D3_RELEASE_MANIFEST=1"]
    for image in images:
        # The registry path is completed in the workflow; image_name is kept
        # separate so the remote script can retag locally without string eval.
        lines.append(f"image\t{image['image_name']}\t{image['image_name']}")
    for url, status in probes:
        lines.append(f"probe\t{url}\t{status}")
    _atomic_write(path, "\n".join(lines) + "\n")


def _write_builds(path: Path, builds) -> None:
    lines = ["D3_RELEASE_BUILDS=1"]
    for alias, context, dockerfile, canonical, names in builds:
        lines.append(f"build\t{alias}\t{context}\t{dockerfile}\t{canonical}\t{','.join(names)}")
    _atomic_write(path, "\n".join(lines) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8", newline="\n")
    temp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images-json", required=True)
    parser.add_argument("--probes-json", default="[]")
    parser.add_argument("--manifest-out", required=True, type=Path)
    parser.add_argument("--builds-out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        images_raw = json.loads(args.images_json)
        probes_raw = json.loads(args.probes_json)
        images, probes, builds = normalize(images_raw, probes_raw)
        _write_manifest(args.manifest_out, images, probes)
        _write_builds(args.builds_out, builds)
    except (json.JSONDecodeError, ValidationError, OSError) as exc:
        print(f"release input invalid: {exc}", file=sys.stderr)
        return 2
    print(f"validated {len(images)} image(s), {len(probes)} probe(s), {len(builds)} build(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
