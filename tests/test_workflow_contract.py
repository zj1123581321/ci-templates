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


# --- busy-lock gate: inputs 透传 + rc=3 deferred 分流 + 双卡通知 --------------

def test_busy_lock_inputs_declared_with_safe_defaults():
    raw, trigger = _load()
    inputs = trigger["workflow_call"]["inputs"]

    assert "busy_lock_file" in inputs
    assert inputs["busy_lock_file"].get("default") == ""
    assert inputs["busy_lock_file"].get("required") is not True

    assert "busy_lock_timeout" in inputs
    assert inputs["busy_lock_timeout"].get("default") == "600"


def test_busy_lock_env_is_passed_through_to_deploy_step():
    text = WORKFLOW.read_text()
    assert "BUSY_LOCK_FILE" in text
    assert "BUSY_LOCK_TIMEOUT" in text
    assert "inputs.busy_lock_file" in text
    assert "inputs.busy_lock_timeout" in text


def test_deferred_exit_code_writes_output_before_nonzero_exit():
    text = WORKFLOW.read_text()
    assert "deferred=true" in text
    assert "GITHUB_OUTPUT" in text
    # rc=3 判断必须先于笼统的 "!= 255" 判断,否则 deferred 会被误判为
    # "已按探针门自动回滚" 报错退出,语义就错了。
    idx_rc3 = text.index('"$rc" -eq 3')
    idx_rc_ne_255 = text.index('"$rc" -ne 255')
    assert idx_rc3 < idx_rc_ne_255, "rc=3 分支必须在 != 255 判断之前"


def test_remote_script_path_is_unique_per_run():
    # code review round 4 (P1): a fixed remote path (/tmp/pull_and_deploy.sh) is
    # NOT isolated across different service repos deploying to the same host —
    # GitHub concurrency groups are per-repo, not cross-repo. Two service repos
    # racing on the same box would clobber each other's script file, silently
    # executing whatever got written last (e.g. an old script without the
    # busy-lock gate logic, even though the busy-lock env vars were passed).
    # The remote path must be unique per workflow run so concurrent deploys
    # from different repos never collide on the same file.
    text = WORKFLOW.read_text()

    assert "GITHUB_RUN_ID" in text, "remote script path must be derived from the run id to be unique per run"

    # the same variable must be used both when scp'ing the script up and when
    # ssh invoking `bash <path>` — otherwise the two paths could drift apart.
    assert "REMOTE_SCRIPT" in text, "expected a REMOTE_SCRIPT variable naming the unique remote path"
    assert '${REMOTE_SCRIPT}' in text or "$REMOTE_SCRIPT" in text

    # used-then-deleted: the remote script must be cleaned up after execution.
    assert "rm -f" in text
    rm_idx = text.index("rm -f")
    nearby = text[max(0, rm_idx - 200): rm_idx + 200]
    assert "REMOTE_SCRIPT" in nearby, "rm -f must target the same REMOTE_SCRIPT path that was scp'd up"

    # regression guard: the old fixed path must be gone entirely.
    assert ":/tmp/pull_and_deploy.sh" not in text, "fixed remote path must not reappear — it's the bug this test guards against"


def test_rc3_rechecks_remote_state_after_prior_transport_failure():
    # code review round 5 (P1): a 255 (SSH transport failure) can happen AFTER the
    # remote deploy already finished (compose up + probe passed + last_good_tag
    # written) but before the exit code made it back over the wire. A retry then
    # sees rc=3 (busy lock held by the just-started new container) and — without
    # this recheck — would misreport "deferred: old container kept", which is the
    # opposite of what actually happened. The workflow must track whether a 255
    # occurred earlier in this run, and if so, verify via the host's
    # last_good_tag before trusting rc=3's "deferred" story.
    text = WORKFLOW.read_text()

    assert "had_transport_failure" in text, (
        "must track whether an earlier attempt in this run hit rc=255, so a later "
        "rc=3 can be recognised as potentially stale rather than trusted at face value"
    )
    assert "last_good_tag" in text, (
        "rc=3 recheck must consult the host's last_good_tag to see whether an "
        "earlier (transport-failed) attempt actually already deployed GIT_SHA"
    )

    # had_transport_failure must be initialised before the loop, set on the 255
    # branch, and consulted inside the rc=3 branch — not just present anywhere.
    idx_init = text.index("had_transport_failure=0")
    idx_rc3 = text.index('"$rc" -eq 3')
    idx_rc_ne_255 = text.index('"$rc" -ne 255')
    idx_set = text.index("had_transport_failure=1")
    assert idx_init < idx_rc3, "had_transport_failure must be initialised before the loop"
    # the flag is set once rc is known to be 255, i.e. after the "!= 255" guard
    assert idx_rc_ne_255 < idx_set, "had_transport_failure=1 must be set on the confirmed-255 path"

    # regression guard: deferred=true must still exist as the fallback outcome.
    assert "deferred=true" in text, "rc=3 must still be able to fall through to deferred when the recheck doesn't confirm success"


def test_red_card_step_skips_when_deferred():
    text = WORKFLOW.read_text()
    assert "deferred != 'true'" in text


def test_yellow_card_step_exists_for_deferred_without_at_all():
    text = WORKFLOW.read_text()
    assert "deferred == 'true'" in text
    assert "部署延期" in text

    # 定位黄卡那一段(从"部署延期"关键词往后切),只在这一段里断言不含 <at id=all>;
    # 红卡那段仍然应该含 <at id=all>,不能用"全文不含"这种粗暴断言。
    idx = text.index("部署延期")
    yellow_section = text[idx:]
    assert "<at id=all>" not in yellow_section

    red_idx = text.index("Feishu 部署失败卡")
    # 红卡段落(该 step 起,到黄卡关键词出现前)仍然要 @全员
    red_section = text[red_idx:idx]
    assert "<at id=all>" in red_section
