"""T4: examples/*.yml 必须与 build-deploy.yml 的真实接口对齐。

D3 激活后 caller 接口变了(host=Tailscale IP、加 ssh_user、6 个 secret)。
examples 是用户照抄的范例 —— 漏一个就让新服务"生成即部署不了"。这里把接口
契约钉死,例子漂移就 fail。
"""
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

# build-deploy.yml 显式声明的 6 个 secret —— caller 必须逐个传齐。
EXPECTED_SECRETS = {
    "ACR_USERNAME", "ACR_PASSWORD", "SSH_DEPLOY_KEY", "KNOWN_HOSTS",
    "TS_AUTHKEY", "CI_TEMPLATES_PAT",
}
TS_IP = re.compile(r"^100\.\d{1,3}\.\d{1,3}\.\d{1,3}$")  # Tailscale CGNAT 段


def _ship_with(path: Path):
    raw = yaml.safe_load(path.read_text())
    return raw["jobs"]["ship"]["with"], raw["jobs"]["ship"]["secrets"]


def test_caller_pins_v1_canary_pins_main():
    caller = yaml.safe_load((EXAMPLES / "caller-workflow.yml").read_text())
    canary = yaml.safe_load((EXAMPLES / "canary-workflow.yml").read_text())
    assert caller["jobs"]["ship"]["uses"].endswith("build-deploy.yml@v1")
    assert canary["jobs"]["ship"]["uses"].endswith("build-deploy.yml@main")


def test_examples_pass_all_six_secrets():
    for name in ("caller-workflow.yml", "canary-workflow.yml"):
        # secrets 解析成 6 键的 mapping 本身就排除了 `secrets: inherit`
        # (那样 yaml 会得到字符串 "inherit",.keys() 直接炸)。
        _, secrets = _ship_with(EXAMPLES / name)
        assert set(secrets.keys()) == EXPECTED_SECRETS, name


def test_examples_host_is_tailscale_ip_not_alias():
    # runner 没有 ~/.ssh/config,host 必须是 tailnet 可达地址,不能是别名(如 host-1)
    for name in ("caller-workflow.yml", "canary-workflow.yml"):
        with_, _ = _ship_with(EXAMPLES / name)
        assert TS_IP.match(str(with_["host"])), f"{name}: host 应是 Tailscale IP"
        assert with_["host"] != "host-1", name


def test_examples_pass_ssh_user():
    for name in ("caller-workflow.yml", "canary-workflow.yml"):
        with_, _ = _ship_with(EXAMPLES / name)
        assert with_.get("ssh_user"), f"{name}: 缺 ssh_user"
