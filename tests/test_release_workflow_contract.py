"""Static contract checks for the independent multi-image reusable workflow."""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "build-deploy-release.yml"


def load():
    raw = yaml.safe_load(WORKFLOW.read_text())
    return raw, raw.get("on", raw.get(True))


def test_release_workflow_contract_and_six_secrets():
    raw, trigger = load()
    assert "workflow_call" in trigger
    call = trigger["workflow_call"]
    assert call["inputs"]["images_json"]["required"] is True
    assert call["inputs"]["host"]["required"] is True
    assert set(call["secrets"]) == {
        "ACR_USERNAME", "ACR_PASSWORD", "SSH_DEPLOY_KEY", "KNOWN_HOSTS", "TS_AUTHKEY", "CI_TEMPLATES_PAT"
    }
    assert all(spec.get("required") is True for spec in call["secrets"].values())
    assert raw["permissions"] == {"contents": "read"}
    assert raw["concurrency"]["cancel-in-progress"] is False
    assert "inputs.host" in str(raw["concurrency"]["group"])


def test_release_workflow_pins_actions_and_has_atomic_build_gate():
    text = WORKFLOW.read_text()
    assert "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5" in text
    assert "tailscale/github-action@4e4c49acaa9818630ce0bd7a564372c17e33fb4d" in text
    assert "normalize_release.py" in text
    assert "push_to_acr.sh" in text
    assert "release_deploy.sh" in text
    assert "D3_RELEASE_TAG" in text
    assert "255" in text
    assert "ssh " in text

