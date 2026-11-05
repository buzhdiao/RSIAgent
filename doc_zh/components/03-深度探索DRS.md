# 03 · 深度探索 DRS（定向练习与即时记忆更新）

DRS（Deep Recursive Self-exploration，Phase 2）在 BRS 之后进行：先做真实开发目标尝试，暴露差距与脆弱的成功，再由 Curriculum 选择定向练习；**每条已验证经验在后续练习/尝试之前先落记忆**。

## 目标/练习生命周期状态机

`core/self_evolving_loop.py:218` 的 `run_self_evolving_loop` 是无评估器状态机：全新 Actor 尝试目标 → 一个持久上下文的 Verifier 调查该尝试的每次候选修订（钩子注入，`:191` 的 `SelfEvolvingLoopHooks`）→ 按判定分支：

- **FAIL**：同一 Actor 必学（`actor_learn` 钩子），然后调用 `evolve`——Curriculum 审阅学习诊断、驱动练习 Actor→Verifier 内环，之后才允许新目标尝试。
- **PASS**：默认策略 `curriculum_review`（`core/self_evolving_loop.py:46`、`:57`）下仍请 Curriculum 复盘"是否值得再做一次对照练习"；另一策略 `verifier_pass` 则在 PASS 与其记忆提交后停止。两策略都不改变"谁判定任务"。
- **UNVERIFIED**：如实终止为 `target_harness_unverified`，不计分（`:353` 附近）。

关键纪律：到达 Curriculum 的"接地结果"必须已被同一 Actor 学过（`:390` 的守卫）；Curriculum 的 STALLED 是语义停止条件而非正确性判定——停滞后的最终目标测试无论判定如何都把**活状态**交出供密封测量（`:352` 的 `curriculum_stalled_final_target_test`）。

## 一次练习尝试的内环

`explore/target_learning.py:326` 的 `_run_practice_attempt` 实现"一次提交/一次判定"内环：VM 复位 → 重放夹具 → 推送当前记忆 → Actor 交接阶段（形态检查 `_require_owned_project_shape`，`:173`，可由 Agent 自行修复的发布缺陷单列 `PracticePublicationError`）→ 捕获候选 → Verifier 阶段（复位 VM、重放原始夹具并舞台化只读证据 `_stage_original_practice_fixtures`，`:121`，再重放候选）。Agentic 路径下 Actor 记忆/交接/执行日志被机械隐藏（`:519` 附近的控制配置派生）。

## 经验晋升（即时记忆更新）

`explore/target_learning.py:217` 的 `_promote_learning` 是 DRS 的记忆纪律落地：蒸馏（`self_evolving_actor_memory_distillation_msg`）→ 整合（reconciliation）→ 学习诊断（`_run_learning_diagnosis`）→ 拉取终态记忆 → 静默审计（任务泄漏绊线，审计删改即整批拒绝）→ 写日志 → 原子安装进规范库。审计面构造 `_target_audit_surface`（`:82`）把任务可见输入显式授权，豁免是窄而显式的。

## 可恢复性

内环支持证据恢复变体（`resume_state` 非空时走 `explore/practice_evidence_recovery.py`），在证据已捕获后崩溃的场景续跑；状态机边界 `SelfEvolvingStart`（`core/self_evolving_loop.py:138`）只接受"完成练习后的 admitted boundary"，基础设施停止绝不能冒充语义决策。
