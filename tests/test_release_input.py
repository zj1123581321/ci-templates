"""TDD contract for the multi-image release declaration normalizer."""
import json
import subprocess
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "normalize_release.py"
def run_normalizer(tmp_path, images, probes=None):
    manifest = tmp_path / "release.manifest"
    builds = tmp_path / "builds.manifest"
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--images-json",
        json.dumps(images),
        "--manifest-out",
        str(manifest),
        "--builds-out",
        str(builds),
    ]
    if probes is None:
        probes = [{"url": "http://localhost/health", "expect_status": 200}]
    cmd += ["--probes-json", json.dumps(probes)]
    return subprocess.run(cmd, capture_output=True, text=True), manifest, builds
def valid_images():
    return [
        {"image_name": "frontend", "build_context": ".", "dockerfile": "Dockerfile"},
        {"image_name": "backend", "build_context": "services/api", "dockerfile": "Dockerfile"},
    ]
def test_normalizes_images_and_multiple_probes(tmp_path):
    probes = [
        {"url": "http://127.0.0.1:8080/", "expect_status": 200},
        {"url": "http://127.0.0.1:8000/healthz", "expect_status": 204},
    ]
    result, manifest, builds = run_normalizer(tmp_path, valid_images(), probes)
    assert result.returncode == 0, result.stderr
    assert "image\tfrontend\tfrontend" in manifest.read_text()
    assert "image\tbackend\tbackend" in manifest.read_text()
    assert "probe\thttp://127.0.0.1:8000/healthz\t204" in manifest.read_text()
    assert builds.read_text().count("build\t") == 2
def test_rejects_unknown_fields_and_duplicate_names(tmp_path):
    unknown = [{"image_name": "a", "build_context": ".", "dockerfile": "Dockerfile", "run": "x"}]
    result, _, _ = run_normalizer(tmp_path, unknown)
    assert result.returncode != 0
    assert "unknown" in result.stderr.lower()
    duplicate = [
        {"image_name": "a", "build_context": ".", "dockerfile": "Dockerfile"},
        {"image_name": "a", "build_context": "other", "dockerfile": "Dockerfile"},
    ]
    result, _, _ = run_normalizer(tmp_path, duplicate)
    assert result.returncode != 0
    assert "unique" in result.stderr.lower()
def test_rejects_control_chars_and_dangerous_paths(tmp_path):
    bad = [
        {"image_name": "bad\nname", "build_context": ".", "dockerfile": "Dockerfile"},
        {"image_name": "ok", "build_context": "../../etc", "dockerfile": "Dockerfile"},
        {"image_name": "ok2", "build_context": ".", "dockerfile": "/tmp/Dockerfile"},
    ]
    for item in bad:
        result, _, _ = run_normalizer(tmp_path, [item])
        assert result.returncode != 0, item
def test_build_alias_deduplicates_build_and_requires_same_source(tmp_path):
    images = [
        {"image_name": "worker", "build_alias": "worker", "build_context": ".", "dockerfile": "Dockerfile"},
        {"image_name": "worker-cron", "build_alias": "worker", "build_context": ".", "dockerfile": "Dockerfile"},
    ]
    result, _, builds = run_normalizer(tmp_path, images)
    assert result.returncode == 0, result.stderr
    assert builds.read_text().count("build\t") == 1
    assert "worker-cron" in builds.read_text()
    conflicting = [
        {"image_name": "a", "build_alias": "same", "build_context": ".", "dockerfile": "Dockerfile"},
        {"image_name": "b", "build_alias": "same", "build_context": "sub", "dockerfile": "Dockerfile"},
    ]
    result, _, _ = run_normalizer(tmp_path, conflicting)
    assert result.returncode != 0
    assert "build_alias" in result.stderr
def test_rejects_probe_injection_and_bad_status(tmp_path):
    result, _, _ = run_normalizer(
        tmp_path,
        valid_images(),
        [{"url": "http://localhost/health;touch /tmp/pwn", "expect_status": 200}],
    )
    assert result.returncode != 0
def test_release_requires_at_least_one_probe(tmp_path):
    result, _, _ = run_normalizer(tmp_path, valid_images(), [])
    assert result.returncode != 0
    assert "probe" in result.stderr.lower()
    result, _, _ = run_normalizer(
        tmp_path, valid_images(), [{"url": "http://localhost/health", "expect_status": 999}]
    )
    assert result.returncode != 0
