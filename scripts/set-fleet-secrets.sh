#!/usr/bin/env bash
# 给一个服务仓配齐 D3 流水线需要的 6 个 GitHub secret。
#
# 个人账号无 org secrets,每个仓得各配一遍(架构文档 5.4 的已知苦役)。
# 这脚本把"逐个手敲"收成一条命令,值统一从 ci-templates/.env + 本机 SSH key 取,
# 给舰队第 3..50 个服务接入复用。
#
# 用法:
#   scripts/set-fleet-secrets.sh <owner/repo> <deploy_host_ip> [ssh_key_path]
# 例:
#   scripts/set-fleet-secrets.sh your-org/your-service 100.64.0.1
#
# 前置:.env 里要有 ACR_USERNAME / ACR_PASSWORD / GH_PAT_TOKEN / TS_AUTHKEY。
# TS_AUTHKEY 过期了用 TS_API_KEY 经 Tailscale API 重生成再写回 .env(见 README 轮换段)。
set -euo pipefail

REPO="${1:?用法: set-fleet-secrets.sh <owner/repo> <deploy_host_ip> [ssh_key_path]}"
HOST_IP="${2:?缺 deploy_host_ip(Tailscale 100.x IP)}"
SSH_KEY="${3:-$HOME/.ssh/id_ed25519}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
[ -f "$ENV_FILE" ] || { echo "✗ 找不到 $ENV_FILE"; exit 1; }
[ -f "$SSH_KEY" ] || { echo "✗ 找不到 SSH 私钥 $SSH_KEY"; exit 1; }

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

: "${ACR_USERNAME:?.env 缺 ACR_USERNAME}"
: "${ACR_PASSWORD:?.env 缺 ACR_PASSWORD}"
: "${GH_PAT_TOKEN:?.env 缺 GH_PAT_TOKEN}"
: "${TS_AUTHKEY:?.env 缺 TS_AUTHKEY(用 TS_API_KEY 经 API 生成后写回 .env)}"

echo "→ 配 secret 到 $REPO(部署目标 $HOST_IP,SSH key $SSH_KEY)"

# 重建该 host 的 known_hosts(避免依赖本机 ~/.ssh/known_hosts 的历史条目)
KNOWN_HOSTS="$(ssh-keyscan -T 10 "$HOST_IP" 2>/dev/null)"
[ -n "$KNOWN_HOSTS" ] || { echo "✗ ssh-keyscan $HOST_IP 没拿到 host key(目标在线吗/入 tailnet 了吗)"; exit 1; }

gh secret set ACR_USERNAME     --repo "$REPO" --body "$ACR_USERNAME"
gh secret set ACR_PASSWORD     --repo "$REPO" --body "$ACR_PASSWORD"
gh secret set CI_TEMPLATES_PAT --repo "$REPO" --body "$GH_PAT_TOKEN"
gh secret set TS_AUTHKEY        --repo "$REPO" --body "$TS_AUTHKEY"
gh secret set KNOWN_HOSTS       --repo "$REPO" --body "$KNOWN_HOSTS"
gh secret set SSH_DEPLOY_KEY    --repo "$REPO" < "$SSH_KEY"

echo "✓ 6 个 secret 已配齐:"
gh secret list --repo "$REPO"
