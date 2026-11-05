# 06 · OSWorld 集成与 VM 适配

OSWorld-V2 是 RSIAgent 的第一个基准。集成层在 `benchmarks/osworld/`，VM 适配在 `env/`。

## 三阶段协议编排

`benchmarks/osworld/pipeline.py:1` 编排三个"泄漏分离"的阶段，**每次进程调用只执行一个阶段**（`--phase phase1|phase2|phase3`，由 `run_osworld.py:50` 的 `build_plan` 生成三条命令）：官方分数诞生于 Phase 3，结构上无法回流同一次协议运行的学习阶段。协议文件 `protocol.json` 由 `pipeline.py` 的 `load_protocol`（`:119` 附近）加载校验，运行根目录名经 `_safe_run_root`（`:95` 附近）防路径注入。

- Phase 1：`benchmarks/osworld/phase1.py:1`——无评分器的分布引导探索，只启动 null-task 桌面，官方任务加载器与评估器**不被导入**；并行波次模式下委托 `explore/phase1_wave.py`。
- Phase 2：`benchmarks/osworld/phase2.py` + `core/self_evolving_loop.py:218`——目标/练习生命周期。
- Phase 3：`benchmarks/osworld/task.py:1`——冻结记忆执行 + 密封评估：Agent 只拿公开题面与 VM 句柄；官方评估只在动作-验证环返回后运行；评估器传输重试保留已完成 VM、绝不重进 Agent。

## VM 适配：鸭子类型的两个动词

`env/vm.py:1` 是客户机抽象——对 v2 `DesktopEnv` 鸭子类型（从不 import 它），只用 `env.controller.http_server`。两个动词都是代码：`run_command`（单条 shell，探测/检查）与 `run_script`（整个程序，base64 管道送入——code-as-policy 的原语）。程序合并输出先写 VM 内日志再 cat 回，**超时也留下可恢复的部分轨迹**（此前适配器超时丢失全部轨迹，而轨迹是核心信号）。无 GUI、无 grounder、无截图——纯文本+代码智能体。

## 回滚与出口封印

- `env/qemu_rollback.py:1`：QEMU 回滚事务——计分状态是 QEMU checkpoint，Verifier 的全部效果发生在其后，`rollback` 恢复磁盘/RAM/进程/套接字/GUI；QEMU 客户机之外的共享服务不在事务内。
- `explore/target_isolation.py:1`：出口封印（target-egress-seal-v1，`:46`）。桌面 Agent 在客户机里是 root，客户机防火墙不算边界——策略装在拥有 QEMU dockerbridge tap 的 Docker 容器一层：iptables 链把流量重定向到宿主进程内的 `FilteringTcpGateway`（`:496`），只放行 HTTP Host / TLS SNI 恰好等于唯一任务网站的流量；网关自己做 DNS，客户机选不了上游 IP；空项目模式禁止一切客户机发起的公网流量。
- `env/verifier_control.py` 与 `env/controller_oom.py`：Verifier 执行器控制与控制器 OOM 处理。

## 任务面与资产

`benchmarks/osworld/task_surface.py` 读取任务"公开面"（题面指令），`run_osworld.py:108` 在缺少 `config/target_queries/<task>.md` 时用它归一化目标指令；`tools/prepare_osworld_v2_release.py` 校验基准资产，`OSWORLD_FILE_BASE_URL` 把任务资产目录暴露为本地服务（`run_osworld.py:200`）。评估器更正（如有）必须显式标识、与未修改的官方评估分开报告（`benchmarks/osworld/evaluator_corrections.py`）。

## 版本锁定

`config/osworld/baseline.lock.json`（`run_osworld.py:44`）冻结基准版本、任务数、种子、网站后缀与资产目录；`config/runtime_paths.py` 的 `resolve_osworld_root` 按 `RSIAGENT_ROOT`/`OSWORLD_ROOT` 约定解析上游检出位置。固定版本是论文"密封评估"可复现的前提。
