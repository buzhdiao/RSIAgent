# 04 · 测试期 memory 冻结复用

Phase 3 用**冻结的记忆快照**在"与 RSI 相同的智能体框架"里执行任务：交互历史与任务环境重置，Curriculum 与记忆更新被禁用，官方评估器在动作-验证环结束后才运行（sealed evaluation）。

## 记忆是 Agent 自有的目录

Agent 的记忆是它在客户机里完全拥有的 `~/.memory`。宿主侧的 `explore/commit.py` 只做三件事（模块头声明）：**搬运**（`push_memory`/`pull_memory`，`:43`/`:110`，tar.gz + 分块 base64 传输，拉取带三次重试与 `__pycache__` 过滤）、**归档**（`journal`，`:199`，追加式每回合 tgz + JSONL 元数据，Agent 不可见，纯回滚/审计保险）、**静默审计**（`silent_audit`，`:318`，8-gram shingle 任务泄漏绊线，拒绝只写宿主日志、绝不反馈给 Agent——不给它学绕过的机会）。宿主从不代写记忆内容。

## 冻结的机械承诺：内容哈希

"测期用的记忆 = 学习结束时的记忆"不是一句口头承诺，而是可复现的内容哈希：`explore/memory_hash.py:53` 的 `canonical_sha256` 对规范 JSON（排序键、无无效空白、拒绝 NaN）做 SHA-256；`:61` 的 `tree_sha256` 把 {相对路径: 文件哈希} 映射规范化后取根摘要，逐项校验相对路径（拒绝 `/` 开头与 `..` 逃逸）与哈希格式，任何不合法即整体拒绝。状态文件中记录的记忆清单/哈希（如 `explore/phase1_wave.py:735` 的 `evolve_parallel_phase1` 落盘的 `memory_manifest`）让 sealed evaluation 的输入可审计、可重放。

## 冻结后的读法：检索自由

冻结记忆以"序言 + 机械清单"方式暴露给 Actor：`explore/charter.py:16` 的 `memory_preamble` 只给文件名与大小，不注入任何文件正文，也没有特权文件名——列出/搜索/预览/重读/判断相关性都是 Actor 自己的动作。序言明确告知：记忆可能错误/过时，须对照活环境验证；本地工作期的记忆编辑会被丢弃，无法更新持久记忆。这保证了"测期复用"不预置宿主检索偏置，也堵住了测期学习。

## 记忆写入的三重防线

学习期的记忆写入由 `explore/practice_loop.py:359` 的 `_atomic_install_memory` 保证原子性：暂存目录写全 + 逐文件 fsync（`:340` 的 `_fsync_tree`）→ 同文件系统 rename 换入 → 失败回滚换回，记忆永不出现半新半旧状态。写入前还有静默审计与语料校验（`explore/commit.py:279` 的 `validate_instruction_corpus` 校验语料来源证明），写入后写 journal 供回放。测期模式下这些写入路径被禁用，只剩只读挂载。

## 记忆与基准分数的隔离

官方分数与评估器输出被挡在学习环外：`core/loop.py:1` 的尝试循环结构上拿不到评分器；`benchmarks/osworld/pipeline.py:1` 的单阶段进程模型让 Phase 3 分数无法回流。记忆里也不许出现任务泄漏——8-gram 语料来自任务指令（`explore/commit.py:265` 的 `build_corpus`，仅宿主侧），记忆文件与语料共享 shingle 即被拒绝。
