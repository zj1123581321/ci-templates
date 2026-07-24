# D3 忙锁部署门禁（busy-lock gate）最小设计 — 待审定

**状态**：已实现（2026-07-23），实现于 feat/busy-lock-gate。
**取代**：live-recorder 仓 `docs/handoff/d3-safe-deploy-drain.md` 草案中的 HTTP prepare 协议方案（该草案的问题定义、验收矩阵仍有效，机制选型在此改为文件锁）。

## 已拍板的决策（2026-07-23）

| 决策点 | 结论 |
| --- | --- |
| 门禁机制 | **B：flock 文件锁（忙锁）**，不做 HTTP prepare 协议 |
| 延期后补上线 | 手动 re-run（飞书黄卡带按钮）；跑一两个月看频率，烦了再加自动重试 |
| 忙时等待预算 | 默认 **600s（10 分钟）**，可由 caller 覆盖 |

## 机制一句话

服务的每个不可打断任务在存续期间对一个共享锁文件持有 **共享锁（LOCK_SH）**；
部署脚本替换容器前用 `flock -w <预算>` 申请 **排他锁（LOCK_EX）** ——
拿到即证明「无进行中任务，且新任务进不来」，并**握着锁**完成
`compose up -d` + 健康探针 +（如需）回滚；超时拿不到即 **deferred**：
保留旧容器，本次不替换。

内核锁语义免费提供三样草案里最难的东西：

- 原子 admission gate（LOCK_EX 期间新任务的 LOCK_SH 申请必然失败，无 TOCTOU 空档）；
- 「最终确认与 compose up 同一临界区」（全程握着 LOCK_EX）；
- 崩溃自愈（任何一方进程死亡，其锁由内核自动释放，不需要 TTL）。

## 锁文件与挂载约定

- 路径：`${DEPLOY_DIR}/.deploy-state/busy.lock`（复用现有 STATE_DIR）。
- compose 将 `./.deploy-state` **目录** bind mount 进容器（挂目录不挂单文件，
  避免 inode 替换导致锁失效）。同一内核下 bind mount 文件的 flock 跨容器边界有效。
- 服务侧：每个不可打断任务开始时 `open()` + `flock(LOCK_SH)`（每任务独立 fd），
  结束时关 fd；申请失败（部署正在独占）则本轮不启动新任务，下个周期重试。
  哪些任务算「不可打断」由各服务仓自行定义（live-recorder 草案建议
  waiting/pending/recording/finalizing，以该仓复审为准）。

## 部署脚本时序（pull_and_deploy.sh，opt-in 时）

```text
预拉镜像（锁外，pull 不动运行中容器；把 admission 关闭窗口从分钟级压到秒级）
    |
flock -w $BUSY_LOCK_TIMEOUT -x busy.lock      # 忙则有界等待，默认 600s
    |-- 超时 --> exit 3 (deferred)：不碰容器，不碰 last_good
    v
flock 整机部署锁 /var/lock/fleet-deploy.lock   # 现有锁；锁序固定 busy → host，无死锁环
    v
do_deploy()：retag + compose up -d + 探针 + (失败)回滚   # 全程握着 LOCK_EX
    v
脚本退出，两把锁随 fd 释放
```

- 锁序说明：各服务忙锁互不相同、整机锁只有一把，固定「先忙锁后整机锁」不构成环。
- LOCK_EX 实际持有时长 ≈ compose up + warmup + 探针（约 30–60s），
  期间新容器内的服务也拿不到 LOCK_SH，探针通过/回滚完成后才放行新任务。
- 等待期间**不占**整机部署锁：同主机其他服务的部署不受影响；
  只占本服务自己的 GitHub concurrency 队列（repo 级，后来的 push 自动顶替排队者）。

## 退出码与通知

| rc | 语义 | workflow 行为 |
| --- | --- | --- |
| 0 | 部署健康 | 不变 |
| 1 | 探针失败，已自动回滚 | 不变：job fail + **红色 P0 卡** |
| **3** | **deferred**：服务忙，超时未等到，旧容器保留 | deploy step 先写 `deferred=true` 到 GITHUB_OUTPUT 再非零退出；job fail（诚实反映新 SHA 未上线），但通知分流为**黄色卡**：「已构建未替换，空闲后点按钮 re-run」，不 @全员 |
| 255 | SSH 传输层瞬时失败 | 不变：重试 ≤3 次 |

镜像已推 ACR（不可变 SHA），re-run 无需重新 build（build 步骤重跑也只是快速命中）。

## 影响面清单

### ci-templates（本仓）

- `scripts/pull_and_deploy.sh`：新增可选 env `BUSY_LOCK_FILE`（空 = 关，现状不变）、
  `BUSY_LOCK_TIMEOUT`（默认 600）；预拉镜像；忙锁申请与 rc=3；约 20 行。
- `.github/workflows/build-deploy.yml`：新增 inputs `busy_lock_file`（默认空）、
  `busy_lock_timeout`（默认 "600"）透传 env；deploy step 的 rc=3 分支 + `deferred` output；
  通知步拆红/黄两张卡（`if: failure() && outputs.deferred != 'true'` / `== 'true'`）。
- 测试：
  - `test_pull_and_deploy.py`：忙锁被 SH 占住 → 有界等待；超时 → rc=3 且容器/last_good 未动；
    等待中释放 → 正常部署；LOCK_EX 期间新 SH 申请失败；未 opt-in → 行为逐字节不变。
  - `test_workflow_contract.py`：新 inputs 存在、仍只有 6 secrets、无 inherit、通知分流存在。
- README：opt-in 用法一段。

### live-recorder 仓（独立任务，本仓不动它）

- compose 挂载 `.deploy-state`；任务生命周期包 LOCK_SH；SH 申请失败的优雅跳过；
  顺带补 `stop_grace_period`（60–120s，与本门禁无关但同样保护收尾）。

### Video Transcribe API 仓（更后，同 live-recorder 模式）

## 上线顺序（bootstrap，防「假保护」）

1. ci-templates 变更合入，canary 走一遍（不 opt-in，行为应与现状全同）。
2. live-recorder 先实现锁 + 挂载，经现有 D3 正常部署一次（此时仍无门禁）。
3. live-recorder caller 再打开 `busy_lock_file` opt-in。
4. 验证：录制中 push → 观察 deferred + 黄卡 → 空闲 re-run 成功替换。
5. 按既有流程移 v1 tag。

**顺序不可倒**：先 opt-in 后实现锁 = 锁文件无人持有，部署畅通无阻，形同没有保护。
部署脚本发现锁文件不存在时会创建并打**显著 WARN**（Actions 日志可见），提示可能是
服务侧尚未接入；是否升级为 fail-closed（视为 deferred）留给审定裁决。

## 不做清单（减法）

| 不做 | 理由 |
| --- | --- |
| HTTP prepare 协议 / D3_DEPLOY_TOKEN / TTL | 内核 flock 覆盖其全部安全性诉求，且无鉴权与脱敏面；跨机/多副本需求出现（第三个服务）再评估 |
| 全局 lease / 队列 / 后台扫描器 | 草案原则，维持 |
| 蓝绿部署 | 扩大网络/存储/单例状态范围，与单机 compose 不匹配 |
| 拉长 stop_grace_period 当排空手段 | 仍先打断任务且长时间占整机锁；适度 grace 只作收尾兜底 |
| 自动定时重试 | 已拍板先手动；观察延期频率后再议 |
| registry.yaml 加字段 / 公共 SDK | opt-in 完全经 caller inputs 表达，registry 不承载部署时行为 |

## 验收矩阵（改写自草案）

| 场景 | 期望 |
| --- | --- |
| 未 opt-in | D3 行为逐字节不变（测试锁死） |
| 空闲时部署 | LOCK_EX 秒到，正常替换 + 现有探针/回滚 |
| 忙，预算内转空闲 | 等到后替换成功 |
| 忙满预算 | rc=3，旧容器保留，黄卡不红卡，last_good 不动 |
| LOCK_EX 期间新任务 | LOCK_SH 失败，任务不启动，无 TOCTOU |
| 部署方崩溃 | 锁随进程释放，服务恢复接活，无需人工 |
| 探针失败 | 现有回滚语义不变，且全程在 LOCK_EX 内 |
| 锁文件缺失（误配） | 创建 + 显著 WARN（或审定改 fail-closed） |

## 待审定 / 待验证

1. 锁文件缺失时的处理：采纳 **warn-and-proceed**（即当前实现：创建文件 + 打显著
   WARN，不 fail-closed）。
2. 容器内经 bind mount 对宿主文件 flock 的互斥性：**已在本机 docker 实测通过**
   （容器 SH 挡宿主 EX、宿主 EX 挡容器 SH、kill 容器后锁随进程自动释放）。
3. deferred 时 job 标红是否可接受：**采纳 job 标红 + 黄卡分流的方案**（红卡语义
   留给「真实故障」，黄卡语义是「正常的延期，构建已在库，等你手动 re-run」）。
