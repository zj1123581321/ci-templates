# ci-templates

舰队级（50+ 自托管服务）的**版本化 GitHub Actions 复用流水线** + **服务清单（registry）单一真相源**。

每个服务的 CI 缩成 ~10 行调用本仓的 `build-deploy.yml`；两条原有 shell 脚本
（`push_to_acr.sh` / `pull_and_deploy.sh`）变成这条 reusable workflow 的内脏。

> **不做 GitOps / K8s** —— 与 compose + SSH 舰队不匹配，过度建设。
> 这是 Layer A 底座 D3，与冻结的 Layer B（web-api 重工作流）解耦。

## 仓库结构

```
.github/workflows/
  build-deploy.yml      # 复用流水线 (workflow_call)，每服务 ~10 行调它
  ci.yml                # 本仓自测：registry 校验 + pytest
scripts/
  push_to_acr.sh        # build + 打 git-SHA 不可变 tag + push ACR
  pull_and_deploy.sh    # SSH 端：flock 锁 + 健康探针 + 失败自动回滚
  validate_registry.py  # registry.yaml schema + 唯一性 + DSN 明文校验
registry.schema.json    # registry 契约（JSON Schema draft 2020-12）
registry.yaml           # 舰队 host→service 单一真相源
examples/
  caller-workflow.yml   # 服务仓 caller 模板（钉 @v1）
  canary-workflow.yml   # canary 服务模板（钉 @main）
tests/                  # pytest（schema + 部署逻辑 + workflow 契约）
```

## 调用方（每服务 ~10 行）

把 `examples/caller-workflow.yml` 放进服务仓 `.github/workflows/deploy.yml`：

```yaml
jobs:
  ship:
    uses: zj1123581321/ci-templates/.github/workflows/build-deploy.yml@v1
    with:
      image_name: web-api
      host: 100.64.0.1           # Tailscale 可达 IP/MagicDNS（runner 无 ~/.ssh/config，不能用别名 host-1）
      ssh_user: deploy
      deploy_dir: /srv/automation/web-api
      healthcheck_url: http://localhost:8001/healthz
    secrets:                       # 6 个显式传，不用 inherit
      ACR_USERNAME: ${{ secrets.ACR_USERNAME }}
      ACR_PASSWORD: ${{ secrets.ACR_PASSWORD }}
      SSH_DEPLOY_KEY: ${{ secrets.SSH_DEPLOY_KEY }}
      KNOWN_HOSTS: ${{ secrets.KNOWN_HOSTS }}
      TS_AUTHKEY: ${{ secrets.TS_AUTHKEY }}             # runner 临时入 tailnet 连内网目标机
      CI_TEMPLATES_PAT: ${{ secrets.CI_TEMPLATES_PAT }} # 只读 PAT，拉私有 ci-templates 部署脚本
```

> `host` 自 D3 激活起是 **Tailscale 可达地址**（IP/MagicDNS），不再是 `~/.ssh/config` 别名 ——
> GitHub runner 上没有用户的 ssh config，临时入 tailnet 后只能按 IP 连。别名 `host-1` 仍用于 registry 与人读。

部署失败通知读取调用方 repo variables:
- `FEISHU_CI_WEBHOOK`: 目标飞书自定义机器人 webhook。
- `FEISHU_CI_TITLE_PREFIX`: 机器人关键词标题前缀；未配置时默认 `[zlxlabs·CI]`。

这两个变量通常由 `zlxlabs/gate-hub` 的 `scripts/onboard-repo.sh` 按 `registry.yaml`
里的 `notify_category` 写入，避免个人 / fordeal / 合伙人项目的 CI 卡混到同一群。

## 锁死的核心契约（来自 plan-eng-review）

| # | 契约 | 落点 |
|---|------|------|
| **A4** | secrets **显式声明，不 `inherit`** —— 只 6 个 secret 可见，最小权限 | `build-deploy.yml` `secrets:` 块 + `test_workflow_contract.py` |
| 爆炸半径 | caller 钉 `@v1` 不钉 `@main`；canary 仓先吃 `@main`，验证后移 v1 tag | `examples/*` |
| **A3** | 每主机 **flock** 串行化 + GitHub **concurrency group** `deploy-<host>` | `pull_and_deploy.sh` + `build-deploy.yml` |
| **A3** | git SHA **不可变 image tag**；记录"上一个 good"；回滚不覆盖并发部署 | `push_to_acr.sh` / `pull_and_deploy.sh` |
| **A3** | 健康探针真定义（endpoint/超时/重试/期望状态/warmup），失败 → **自动回滚** | `pull_and_deploy.sh` `health_probe()` |
| **A1** | registry **JSON Schema** + 唯一性约束 + **只存 DSN 引用** → CI fail fast | `registry.schema.json` / `validate_registry.py` |

## registry.yaml 字段清单（D4/D5/D7 共用）

每服务一条，全部必填（校验器强制）：

| 字段 | 说明 | 约束 |
|------|------|------|
| `id` | 服务 id（kebab-case），即 ACR image 名 | **唯一**，`^[a-z0-9][a-z0-9-]*$` |
| `git_url` | 源码仓 URL | `http(s)://` 或 `git@` |
| `default_branch` | 默认分支 | 非空 |
| `host` | 部署主机（`~/.ssh/config` 别名） | 非空 |
| `deploy_dir` | 主机上绝对路径（含 compose） | 绝对路径；`(host, deploy_dir)` **唯一** |
| `port` | 对外端口 | 1–65535，**唯一** |
| `glitchtip_project` | GlitchTip 项目名 | 非空 |
| `sentry_dsn_secret` | DSN 的 **secret 名**（如 `SENTRY_DSN_FOO`） | `^[A-Z][A-Z0-9_]*$`，**绝不存 DSN 明文** |
| `monitor_slug` | GlitchTip cron monitor slug | **唯一**，kebab-case |
| `heartbeat_url_secret` | （cron 服务才需）GlitchTip Heartbeat check-in URL 的 **secret 名**（如 `ZLX_HEARTBEAT_URL_FOO`） | `^[A-Z][A-Z0-9_]*$`，**绝不存 URL 明文**；运行时经 `ZLX_HEARTBEAT_URL` 注入 |
| `tier` | 环境/爆炸半径分层（D7） | enum `dev`/`staging`/`prod` |
| `rollback_safety` | 回滚是否安全 | enum `safe`/`unsafe`/`conditional` |
| `healthcheck_url` | （可选）覆盖默认探针 URL | — |

校验本地跑：

```bash
python scripts/validate_registry.py registry.yaml
```

CI 在 PR 时自动跑 —— 漏字段 / 重复 slug / 重复端口 / DSN 明文 一律 **fail fast**。

## 测试

```bash
pip install pytest pyyaml jsonschema
python -m pytest -q
```

- `test_registry_schema.py` —— 8 个坏 fixture（缺字段/重复 id/port/slug/路径、DSN 明文、heartbeat 明文、坏 enum）全部报错；好 registry 通过。
- `test_pull_and_deploy.py` —— flock 并发串行化、不可变 SHA tag、探针失败自动回滚、回滚不提升坏 tag（docker/curl mock，无需真实守护进程）。
- `test_workflow_contract.py` —— workflow 只声明 6 个 secret、无 `inherit`、per-host concurrency。
- `test_caller_examples.py` —— `examples/*.yml` 与真实接口对齐:6 secret、`ssh_user`、host 是 Tailscale IP、caller 钉 `@v1` / canary 钉 `@main`。

## 端到端 / canary（需真实凭证与主机，未在本机执行）

`workflow_call` 本体 + 真机 SSH 部署属**集成**，本地无法纯单测。推荐流程：

1. 配 6 个 secrets（个人账号无 org secrets，每 repo `gh secret set --repo` 配）：
   `ACR_USERNAME` / `ACR_PASSWORD` / `SSH_DEPLOY_KEY` / `KNOWN_HOSTS` / `TS_AUTHKEY` / `CI_TEMPLATES_PAT`。
2. 建一个低风险 canary 服务仓，用 `examples/canary-workflow.yml`（钉 `@main`）。
3. push → 观察 build→ACR→SSH 部署→探针→（人为让探针失败）→自动回滚。
4. 全绿后再 `git tag -f v1 && git push -f origin v1`，存量服务的 `@v1` caller 才吃到新流水线。

> ⚠️ 真实 canary 会向生产主机 `host-1` 部署并推 ACR，属对外不可逆操作，需人工授权后执行。

## 版本与爆炸半径

- caller **必须钉 `@v1`**（主版本 tag），不钉 `@main`。
- 只有 canary 仓吃 `@main`。验证通过后移动 `v1` tag 推平舰队。
- 一个坏 commit 进 `@main` 只炸 canary 一个，不会一次炸 50 个部署。
