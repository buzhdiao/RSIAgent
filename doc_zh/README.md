# RSIAgent 中文学习文档（doc_zh）

本目录是 RSIAgent（training-free 多智能体递归自改进框架，arXiv:2609.15364）的中文学习文档工程，面向想读懂这套代码的工程师与研究者。所有条目都以仓库真实代码为依据，标注 `文件:行号`，可对照源码逐处验证。

术语约定（与论文一致）：**BRS** = Broad Recursive Self-exploration（广度递归自我探索，Phase 1）；**DRS** = Deep Recursive Self-exploration（深度递归自我探索，Phase 2）；**wave memory barrier** = 波次记忆屏障（并行分支全部验证完成后才串行合并记忆）；**sealed evaluation** = 密封评估（官方评分器只在智能体环节结束后运行，分数不出学习环）。

## 目录结构

```
doc_zh/
├── README.md               本索引
├── components/             组件原理 8 篇
│   ├── 01-总体架构与三智能体协议.md
│   ├── 02-广度探索BRS.md
│   ├── 03-深度探索DRS.md
│   ├── 04-测试期memory冻结复用.md
│   ├── 05-Verifier隔离原则.md
│   ├── 06-OSWorld集成与VM适配.md
│   ├── 07-ALE集成与双环境隔离.md
│   └── 08-批量运行断点恢复与审计文件.md
├── translations/
│   └── README.zh.md        根 README 完整中文翻译
└── techniques-100.md       Python 高级技法与重要库 100 讲
```

## components 8 篇速览

| 篇 | 主题 | 一句话 | 关键入口 |
| --- | --- | --- | --- |
| 01 | 总体架构与三智能体协议 | Curriculum/Actor/Verifier 三角色 + 宿主框架的职责切分与传输/语义二分 | `core/loop.py`、`explore/charter.py` |
| 02 | 广度探索 BRS | Curriculum 自著并行波次、波前共享快照、wave memory barrier 串行整合 | `explore/phase1_wave.py` |
| 03 | 深度探索 DRS | 目标尝试暴露差距 → 定向练习 → 即时记忆更新 → 新鲜目标重试 | `core/self_evolving_loop.py`、`explore/target_learning.py` |
| 04 | 测试期 memory 冻结复用 | 冻结快照、内容哈希承诺、只读检索自由 | `explore/commit.py`、`explore/memory_hash.py`、`explore/charter.py` |
| 05 | Verifier 隔离原则 | Verifier 不可读 Actor 私有推理；效果隔离执行器与回滚 | `core/verifier.py`、`core/verifier_runtime.py` |
| 06 | OSWorld 集成与 VM 适配 | 三阶段协议编排、QEMU/Docker 适配、任务面与密封评分 | `benchmarks/osworld/`、`env/vm.py` |
| 07 | ALE 集成与双环境隔离 | 外层进程供给/评分、worker 进程学习、grader/worker 双虚拟环境 | `benchmarks/ale/`、`run_ale.py` |
| 08 | 批量运行/断点恢复/审计 | 计划-执行分离、输出永不覆盖、恢复只认闭合边界、审计文件不出学习环 | `run_osworld.py`、`run_ale.py`、`explore/phase1_boundary_recovery.py` |

## 阅读路线建议

1. 先读根 README（见 `translations/README.zh.md`）建立论文级图景；
2. 读 01 与 02/03 理解 BRS/DRS 两阶段控制流；
3. 读 04/05 理解记忆与验证这两条"质量防线"；
4. 读 06/07/08 理解工程化集成与批处理；
5. 配合 `techniques-100.md` 查阅代码中反复出现的 Python 技法。

## 与源码注释的关系

本次工程同时在核心代码中插入了逐行中文注释（只增不改，行为零变化）。入口文件、BRS 波次、DRS 状态机、记忆读写/冻结、LLM 客户端等关键实现已全覆盖；`core/verifier.py`、`core/loop.py`、`explore/practice_loop.py`、`explore/charter.py`、`explore/target_isolation.py` 等超大文件覆盖了模块定位与关键类/函数段落。建议先读注释再对照本目录文档。
