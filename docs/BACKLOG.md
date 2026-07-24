# BACKLOG

审查中判"接受不修"的事项，附理由。重新评估的触发条件写在各条目里。

## 2026-07-24 · Codex review R1 · P2 · 忙锁临界区内的二次 pull

**现象**：opt-in 门禁路径先在锁外预拉镜像，但拿到 LOCK_EX 后 `do_deploy()` → `deploy_tag()` 仍会再调一次 `pull_image()`。registry 恰在此刻抖动时，重试与退避（≤3 次、退避 10s×n）会在持有忙锁+整机锁期间发生，延长 admission 关闭窗口并阻塞同机其他部署。

**接受理由**：immutable SHA 已预拉，registry 健康时二次 pull 是秒级 no-op；registry 异常时 `pull_image` 有重试上限和本地镜像 fallback，锁内延长**有界**且仅发生在"registry 恰好在部署瞬间不可用"的小概率场景。消除它需要引入"已预拉标记"一类新状态，违反"不为 P2 新增机制"的修复纪律。

**重评触发**：若实际观测到因 registry 抖动导致部署互相阻塞/服务长时间无法接单，再考虑在 deploy_tag 内加"本地已有该 SHA 则跳过 pull"的判断（注意会同时改变未 opt-in 路径的行为，需补测试）。
