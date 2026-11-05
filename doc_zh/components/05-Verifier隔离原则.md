# 05 · Verifier 隔离原则（不可读 Actor 私有推理）

论文的 Verifier Agent 独立检查任务要求与实际环境；它**不能读 Actor 的私有推理或记忆**。这不是提示词上的客套话，而是一整套机械实现。

## 为什么必须隔离

`core/verifier.py:1` 的模块头给出因果：Actor 自己的检查会继承 Actor 自己的错误假设（"语义纠缠"假完成），再多提示也查不出来。结构性修复是**独立性**——零历史的检查者只看任务指令与活机器，自行重导出需求、只读探测、独立判定；验收 = Actor 自检通过 **且** 独立检查未发现违规。

## 隔离的机械实现

1. **配置层**：分支级控制配置把 Actor 私有面拉黑。`explore/phase1_wave.py:452` 的 `_branch_control_config` 以 `dataclasses.replace` 派生配置：`verifier_hide_actor_memory=True`、私有路径集合合并 `ACTOR_HANDOFF` 与 `ACTOR_EXECUTION_EVIDENCE`（`explore/target_learning.py:519` 的练习路径同理）。私有路径由 `core/verifier_runtime.py` 的执行器归一化强制（`VerifierSession.acquire_executor`，`core/verifier.py:239` 起）。
2. **执行器层（效果隔离）**：`core/verifier.py:866` 的 `verify_agentic` 运行"持久、效果隔离"的 Verifier——其 Program 执行器把候选文件系统变只读、隔离进程/网络/GUI 副作用，只留一块私有可写草稿区。Verifier 调查的是受回滚保护的候选（`env/qemu_rollback.py:1`：QEMU checkpoint 之上的回滚事务，恢复磁盘/RAM/进程/套接字/GUI 状态），其客户机改动在检查后被回滚，Actor 的工作恢复时不残留 Verifier 痕迹。
3. **时序层**：执行证据（Actor 自证程序确实跑过的档案）只在独立 Verifier **终止之后**才挂载（`explore/target_learning.py:570` 附近的注释与 `_push_actor_execution_evidence` 调用），杜绝它继承 Actor 的叙事或自检。
4. **提示词层**：验证上下文明说"Actor 私有记忆、交接、推理与执行日志已被宿主隐藏，请独立调查"（`explore/phase1_wave.py:554`、`explore/target_learning.py:537`）。

## 持久上下文与三态判定

`VerifierSession`（`core/verifier.py:198`）继承 list，会话即消息列表：跨同一任务/项目的修订保留 Verifier 自己的推理与发现；但证据新鲜度刻意对每次候选检查局部化——早前的探测可能描述的是 Actor 随后改过的工件。判定刻意三态（`core/verifier.py:1360` 的 `verify_independent`）：`pass`/`wrong`/`unverified`；判 wrong 必须有具体违规证据；unverified 在预算规则下可能迟接受（could-not-confirm 不永远否决），但接受决策被抽成纯函数 `core/loop.py:545` 的 `uv_accept_ok`——一次 WRONG 即撤销疑点利益，报告缺失是基础设施缺席而非"候选正确"的证据。

## 隔离的边界

回滚事务不覆盖客户机外的共享远端服务——文档要求用独立服务账号或串行化共享可变外部状态的研究（`docs/ARCHITECTURE.md`）。BRS 分支中的 Verifier 还被进一步限制：隐藏 Actor 记忆、不走进化路线、失败不触发进化（`explore/phase1_wave.py:452`）。防泄漏审计表面同样覆盖 Verifier 提示词：`core/verifier.py` 与 `explore/charter.py` 都被测试扫描（Constraint #0）。
