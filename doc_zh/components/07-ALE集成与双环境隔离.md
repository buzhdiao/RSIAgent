# 07 · ALE 集成与双环境隔离

Agents' Last Exam（ALE）是第二个基准（67 个 Near-term 任务）。集成层在 `benchmarks/ale/`，入口是 `run_ale.py`。

## 双进程架构

`benchmarks/ale/host.py:1` 声明：ALE 的供给（provisioning）与评分（grading）**只存在于外层宿主进程**。运行时把工作分成两层：

- **外层进程**（host）：任务队列、VM/镜像生命周期、评分、报告（`run_ale.py:122` 的 `main` 把 plan/run/prepare/smoke/report 五个动作路由到这里与 `prepare.py`/`smoke.py`/`report.py`）。
- **worker 进程**（`benchmarks/ale/worker.py:1`）：密封的智能体 worker——只接收公开任务输入与 VM 传输，没有评分器。共享学习运行时（`core/`、`explore/`）跑在 worker 里。

两层进程使用**不同的 Python 虚拟环境**（`run_ale.py:47` 的 `--worker-python` 默认 `.venv-ale-worker/bin/python`；`scripts/setup_ale.py` 建立独立的 grader 与 worker 环境），防止两个上游项目的 Python 包互相遮蔽（shadowing）——这是"双环境隔离"的第一层含义。

## 双操作系统的隔离

第二层含义是 Linux/Windows 两种客户机镜像彼此独立：`run_ale.py:75` 的 `--os linux|windows`；每次 prepare 只下载所选镜像（Linux 约 167 GiB / Windows 约 157 GiB）。Windows 侧有专门的适配：`benchmarks/ale/windows.py` 与 `windows_job.py` 处理 Windows 访客的作业与传输差异。准备、冒烟、正式运行必须用同一个 `--cache`（`run_ale.py:39`）复用镜像。

## 密封与容量

- **密封 worker**：`benchmarks/ale/worker.py` 明确"接收公开任务输入与 VM 传输，无评分器"；外层的评分与智能体环节物理分离。
- **checkpoint 运行镜像**：`run_ale.py:18` 的 `--runner-image rsiagent-ale-checkpoint:v1`——worker 跑在可回滚的容器镜像上。
- **容量池**：`benchmarks/ale/capacity.py` 用 `--pool-root`（`run_ale.py:68`，默认 `/var/tmp/rsiagent-ale-capacity`）的文件锁协调多进程 VM 名额；`--vm-limit` 1-16 限制宿主 VM 总量。
- **学习集成**：`benchmarks/ale/learning.py` 与 `practice.py` 把 BRS/DRS 接到 ALE 任务上；协议映射在 `protocol.py`；传输在 `transport.py`。

## 冒烟与报告

正式 CPU 队列前必须对每种 OS 冒烟成功（`benchmarks/ale/smoke.py`）：用合成文件检查传输、不可变记忆、候选重放、Verifier 隔离与 checkpoint 回滚——不调用模型或官方评分器；重复冒烟要用新的 `--output`（`run_ale.py:54`）。报告阶段（`benchmarks/ale/report.py`）汇总多个 run：产出 `tasks.csv` 与 `summary.json`，缺失结果显式标注，重复计分尝试直接拒绝（`run_ale.py:116` 的 report 分支）。

## 与 OSWorld 的同构性

两个集成使用同一套 Actor/Verifier/Curriculum/记忆协议（根 README）；差异只在适配层：OSWorld 是"三阶段模块 + Docker/QEMU 单镜像"，ALE 是"外层供给/评分 + worker 学习 + 双 OS 双环境"。学习侧的断点恢复、wave memory barrier、sealed evaluation 语义在两边完全一致——这正是"框架不变、基准可替换"的设计目标。
