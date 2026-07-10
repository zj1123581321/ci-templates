"""T3: static contract checks on the reusable build-deploy.yml.

eng-review A4 (codex#5): the workflow must declare secrets EXPLICITLY and must
NOT use `secrets: inherit`, so a compromised ci-templates can never reach
unrelated org secrets. Also asserts the per-host concurrency group (A3).
"""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-deploy.yml"

EXPECTED_SECRETS = {
    "ACR_USERNAME", "ACR_PASSWORD", "SSH_DEPLOY_KEY", "KNOWN_HOSTS",
    "TS_AUTHKEY",          # runner 入 Tailscale 连内网目标机
    "CI_TEMPLATES_PAT",    # 只读 PAT,checkout private ci-templates 的部署脚本
}

PINNED_ACTIONS = {
    "actions/checkout": "34e114876b0b11c390a56381ad16ebd13914f8d5",
    "tailscale/github-action": "4e4c49acaa9818630ce0bd7a564372c17e33fb4d",
}


def _load():
    # PyYAML parses the `on:` key as boolean True — load and normalise.
    raw = yaml.safe_load(WORKFLOW.read_text())
    trigger = raw.get("on", raw.get(True))
    return raw, trigger


def test_workflow_is_workflow_call():
    _, trigger = _load()
    assert "workflow_call" in trigger, "must be a reusable workflow"


def test_workflow_uses_least_privilege_and_immutable_action_references():
    raw, _ = _load()
    assert raw.get("permissions") == {"contents": "read"}

    text = WORKFLOW.read_text()
    for action, sha in PINNED_ACTIONS.items():
        assert f"uses: {action}@{sha}" in text, f"{action} must be pinned to its full commit SHA"


def test_secrets_declared_explicitly_not_inherited():
    raw, trigger = _load()
    # strip comments — the contract is about real YAML, not prose
    code = "\n".join(
        ln for ln in WORKFLOW.read_text().splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "inherit" not in code, "must NOT use `secrets: inherit`"
    secrets = trigger["workflow_call"].get("secrets", {})
    assert isinstance(secrets, dict), "secrets must be an explicit mapping"
    assert set(secrets.keys()) == EXPECTED_SECRETS, (
        f"workflow must declare exactly {EXPECTED_SECRETS}, got {set(secrets.keys())}"
    )
    for name, spec in secrets.items():
        assert spec and spec.get("required") is True, f"{name} must be required"


def test_ssh_has_keepalive_and_retry():
    # ai-info canary 出现过偶发 `Connection reset ...:22`(重跑即过)。50 服务规模
    # 会常遇 → scp/ssh 必须带 keepalive,且部署 step 自带重试,别让一次抖动炸部署。
    text = WORKFLOW.read_text()
    assert "ServerAliveInterval" in text, "scp/ssh 需 -o ServerAliveInterval 防连接被静默掐断"
    assert "ServerAliveCountMax" in text
    assert "ConnectTimeout" in text, "连接阶段也要有超时,避免挂死"
    low = text.lower()
    assert "retry" in low or "attempt" in low, "部署 step 需对瞬时 SSH 失败重试"
    # 关键:只重试 SSH 传输层失败(退出码 255),不重试脚本真实失败(探针挂→已回滚→exit 1)。
    # 否则一个坏部署会被重拉/重部/重滚 3 遍。255 是 ssh 自身连接失败的专用码。
    assert "255" in text, "重试必须区分 SSH 传输失败(255)与真实部署失败(脚本 exit 1)"


def test_per_host_concurrency_group():
    raw, _ = _load()
    concurrency = raw.get("concurrency", {})
    assert "inputs.host" in str(concurrency.get("group", "")), (
        "concurrency group must key on the target host (A3)"
    )
    assert concurrency.get("cancel-in-progress") is False, (
        "must not cancel an in-flight deploy mid-rollout"
    )


def test_deploy_notify_title_prefix_is_repo_variable_with_default():
    text = WORKFLOW.read_text()

    assert "FEISHU_TITLE_PREFIX" in text
    assert "vars.FEISHU_CI_TITLE_PREFIX" in text
    assert "[zlxlabs·CI]" in text
    assert "f\"🔴 {title_prefix} P0 部署失败" in text
