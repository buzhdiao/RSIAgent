# 02 · 广度探索 BRS（并行 Actor 波次与记忆整合）

BRS（Broad Recursive Self-exploration，Phase 1）的目标是"先广后深"：用 Curriculum 自著的并行波次快速积累多样经验。核心实现集中在 `explore/phase1_wave.py`（模块头即论文级说明）。

## 波次的三步节律

1. **作者阶段**：Curriculum 在专用 VM 上起草一波项目，发布 JSON 交接信封（`WAVE_HANDOFF`，`explore/phase1_wave.py:111`）。解析器 `parse_wave_handoff`（`:157`）只验证传输合法性：决策必须是 WAVE/SATURATED/STALLED，项目 ID 必须过白名单正则（`:116` 的 `_PROJECT_ID`），WAVE 必须带非空且可定位到 `WAVE_ROOT`（`:109`）的夹具树。写作跨段重试保留草稿、累计结算预算（`_author_wave`，`:254`）；连续无动作段只触发基础设施阻塞，绝不被当成收敛（`:122` 的 `_MAX_ACTIONLESS_WAVE_SEGMENTS`）。
2. **并行执行阶段**：每个分支从**同一份不可变波前记忆快照**出发（`:902` 安装快照），在独立 VM 上跑隔离的 Actor→Verifier（`_execute_branch`，`:478`）；线程池容量 = min(max_parallel, 波宽)（`:928` 的线程池），波宽由 Curriculum 决定，max_parallel 只是宿主调度容量。
3. **串行提交阶段——wave memory barrier**：所有分支拿到终态判定后，按自著顺序逐个恢复**同一** Actor 上下文做蒸馏→整合→审计→入账→安装（`_commit_branch_memory`，`:676`）。串行化同时消灭两类竞争：兄弟分支互相观察、并发写丢失更新。

## 快照与判定的可复现性

分支内部有三段式环境管理：Actor 工作后 `_fresh_vm` 丢弃其副作用、只重放候选给 Verifier；验证后再复位一次、重放候选并推回波前记忆，保证提交阶段从"与执行时相同的起点"重新出发（`explore/phase1_wave.py:581` 附近的三连调用）。Verifier 报告缺失时分支不允许带模糊结论进入提交（`:566`：无 PASS/FAIL 即基础设施错误）。

## 失败语义

- 分支物理故障允许**一次**从不可变夹具的干净重启（`_execute_branch_with_infra_retry`，`:604`）：归档未计分尝试、写重试回执、绝不提升其记忆；边界违规与已完成判定永不重试。
- 任一分支失败则整波不提交（`:984`：排空全部 future 后才处理错误）——屏障的"全部有效验证"是硬条件。
- 波间才检查应急预算 `project_budget`（`:865`），它不截断波、也不是收敛判据；SATURATED/STALLED 只能由 Curriculum 自宣（`:893`）。

## 证据留档

每波落盘 `curriculum_decision.json`、`outcomes.md` 与逐分支 `outcome.json`（含波前快照清单、记忆 before/after 与差异），下一波 Curriculum 的证据由上一波渲染结果拼接而来（`explore/phase1_wave.py:1045`）。断点恢复入口见 `explore/phase1_boundary_recovery.py`（只认完全闭合的边界）。
