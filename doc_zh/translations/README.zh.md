# RSIAgent 中文 README（根 README.md 完整翻译）

> 译注：本文件是仓库根 `README.md` 的完整中文翻译，供中文读者学习使用；表格与代码块保持原样，实际命令与链接以英文原版为准。翻译日期：2026-11。

<p align="center">
  <img src="../docs/assets/rsiagent-icon.svg" alt="RSIAgent 图标" width="104" height="104">
</p>

# RSIAgent

## 副标题

**面向新环境的递归自我改进的自主探索**（Autonomous Exploration for Recursive Self-improvement in New Environments）

## 徽章栏

[论文：arXiv](https://arxiv.org/pdf/2609.15364) · [Hugging Face 每日论文：2026-09-15 榜单第 6 名](https://huggingface.co/papers/2609.15364) · [网站：RSIAgent](https://aetherlabsai.github.io/RSIAgent/) · [许可证：Apache 2.0](../LICENSE) · [姊妹项目：RSIGame](https://github.com/WenyiWU0111/RSIGame)

（排名快照核验于 2026-09-16（UTC）：https://huggingface.co/papers/date/2026-09-15 ）

导航：[新闻](#新闻) · [演示](#演示) · [方法](#方法) · [结果](#结果) · [安装](#安装) · [文档](#文档) · [引用](#引用)

（演示视频与附件：见原 README 中的 user-attachments 链接。）

## 📰 新闻与亮点

<!-- 新条目加在上方，最新在前。 -->

> **[2026-09-15]** 🔥 **[机器之心](https://mp.weixin.qq.com/s/bg0tkvHsKTZ88uBNKUdVTQ) 头条报道**
>
> RSIAgent 让开源模型能够自主探索新环境，并在多个高难度基准上超越 GPT-6 Astra。
>
> [![微信公众号文章阅读数：43.8K](https://img.shields.io/badge/WeChat_reads-43.8K-2E7D32?style=flat-square&logo=wechat&logoColor=white)](https://mp.weixin.qq.com/s/bg0tkvHsKTZ88uBNKUdVTQ)

## 🧭 概览（Overview）

RSIAgent 是一个面向新数字环境的 **training-free（免训练）递归自我改进框架**。它协调课程智能体（Curriculum Agent）、执行智能体（Actor Agent）与验证智能体（Verifier Agent），去发现环境如何运作、用实际执行检验所学，并把可复用的知识保存在持久记忆中。模型参数在整个探索与下游任务执行过程中保持冻结。

论文的核心策略是**先广后深的探索（broad-then-deep exploration）**：先获取多样经验，再研究困难案例、隐藏约束与边界条件。得到的记忆包含流程、脚本与失败教训，Actor Agent 在下游任务执行时复用它们。

## 🎬 演示（Demos）

（演示视频：见原 README 中的 user-attachments 链接。）

## 🧩 框架（Framework）

[![RSIAgent 框架图：并行广度递归自我探索、串行深度递归自我探索，以及冻结记忆的测试期复用，以 FreeCAD 任务为例。](../docs/assets/framework.png)](../docs/assets/framework.png)

*论文原始框架图，以 FreeCAD 任务为例。广度经验被逐步精炼为定向记忆，然后冻结供复用。点击图片查看原始分辨率。[图片来源溯源](../docs/PAPER.md#figure-provenance)。*

## 🔄 方法（Method）

三个智能体承担递归学习循环：

- **课程智能体（Curriculum Agent）**：依据既有结果与积累的知识挑选有信息量的探索任务，并判断继续练习是否有益。
- **执行智能体（Actor Agent）**：通过可执行的 Python 或 Bash 程序与视觉观察操作软件。验证完成后，同一个 Actor Agent 提炼自身经验并与既有记忆对照整合。
- **验证智能体（Verifier Agent）**：独立检查任务要求与产生的环境状态。它的反馈为学习提供接地（grounding）；它不能读取 Actor Agent 的私有推理或记忆。

论文包含**两个探索阶段，随后是测试期记忆复用**。**RSI 与测试期执行使用同一智能体框架**，模型参数全程冻结。测试期记忆冻结，课程智能体与记忆更新被禁用。实现层面表现为三个运行时阶段：

| 运行时阶段 | 论文阶段 | 学习与执行 |
| --- | --- | --- |
| **Phase 1** | **广度递归自我探索（BRS）** | 课程智能体提出多样项目。Actor 智能体执行、Verifier 智能体从同一份起始记忆出发并行检查。完整波次结束后，Actor 智能体按顺序整合各自经验。 |
| **Phase 2** | **深度递归自我探索（DRS）** | 目标尝试暴露差距与脆弱的成功。课程智能体选择定向练习；每条已验证经验在后续练习或再次尝试之前先更新记忆。 |
| **Phase 3** | **测试期记忆复用** | Actor 智能体使用冻结记忆引导任务执行，通过与 RSI 期间相同的"动作-验证"环与 Verifier 智能体交互。任务执行与验证完成后进行密封的官方评估。 |

记忆是跨任务的持久学习状态。交互历史与任务环境在独立尝试之间重置；智能体框架保持不变。有接地的成功与失败都能提供有用教训。官方基准分数被隔离在学习环之外。参见[架构文档](../docs/ARCHITECTURE.md)了解角色接口、波次记忆屏障（wave memory barrier）与停止规则。

## 📊 结果（Results）

论文报告了共享 harness（协调 Actor Agent 与 Verifier Agent）在"有/无 RSI"两种条件下的**平均部分得分（%）**：

| 基准与报告覆盖 | RSIAgent w/o RSI | RSIAgent |
| --- | ---: | ---: |
| OSWorld 2.0 · 0808 offline · 82 任务 | 71.97 | **78.98** |
| Agents' Last Exam · Near-term · 67 任务 | 83.75 | **84.82** |

以上是论文报告的汇总值。RSI 列对 OSWorld 使用 41 条记录的 RSI 条目、对 ALE 使用 19 条，其余 41 个 OSWorld 任务与 48 个 ALE 任务保留基线分数。ALE 包含全部 67 个 Near-term 任务（含 3 个 GPU 任务基线结果）。RSI 列包含选定的重试与不同预算的 checkpoint；它不是对匹配重复运行的平均。ALE 还包含合格的本地重判与协议变体。完整范围、满分制指标与聚合细节见[论文与报告说明](../docs/PAPER.md)。

论文还研究了阶段消融、记忆增长与游戏开发。其失败分析指出改进的三个限制：练习可能错过相关的弱点、验证可能接受不完整的工作、记忆可能保存错误的规则。因此探索、验证与记忆整合的质量，与练习的数量同样重要。

## 🧪 基准（Benchmarks）

| 集成 | 固定版本 | 公开批量 |
| --- | --- | --- |
| OSWorld-V2 | 2026 年 8 月 8 日 | 108 任务，Docker/QEMU |
| Agents' Last Exam（ALE） | `d10fb61a14f9719774c3520c5763068b28ef5546` | 67 个 Near-term 任务；64 个 CPU 任务与 3 个已完成的 GPU 任务基线 |

三个 GPU 任务基线全部完成。Chroma Key 运行在 Google Cloud（GCP）VM 上，使用官方 ALE Windows 镜像与 NVIDIA L4 vWS GPU。

仓库只包含当前运行时。两个集成使用相同的 Actor、Verifier、Curriculum 与记忆协议。基准的搭建与评分不进入学习过程。

## 🗂️ 仓库布局（Repository layout）

```text
run_osworld.py        OSWorld 批处理入口
run_ale.py            ALE 的准备、执行与报告
benchmarks/
  osworld/           OSWorld 阶段、VM 适配器与评估
  ale/               ALE 宿主、worker 与 VM 适配器
core/                共享的 Actor 与 Verifier 运行时
explore/             Curriculum、学习、记忆与恢复
env/                 共享的访客传输与隔离
llm/                 模型客户端
config/              角色配置与基准配置
scripts/             环境搭建与单项研究辅助
tools/               准备、冒烟检查与恢复工具
tests/               回归测试
docs/                指南、架构与署名
```

两个根入口是基准运行的起点。OSWorld 的内部阶段是 `benchmarks/osworld/` 下的 Python 模块；职责见[架构文档的源码地图](../docs/ARCHITECTURE.md#source-map)。

## ⚙️ 安装（Installation）

使用 Python 3.12、[uv](https://docs.astral.sh/uv/getting-started/installation/)，以及一台装有 Docker 并能访问 `/dev/kvm` 的 Linux 宿主机。从仓库与你自己的模型 API 凭据开始：

```bash
git clone https://github.com/AetherLabsAI/RSIAgent.git
cd RSIAgent
cp .env.example .env
```

在 `.env` 中填写 `OPENROUTER_API_KEY`。路径默认指向本检出目录与相邻的基准目录。只有在使用不同布局时才需要导出 `RSIAGENT_ROOT`、`OSWORLD_ROOT` 或 `RSIAGENT_ENV_FILE`。

按下文为你想运行的基准完成安装。除非标明 `cd`，以下所有命令都在 `RSIAgent` 目录下执行。VM 镜像与基准资产另行下载。

### 🖥️ OSWorld

安装固定版本的 [OSWorld release](https://github.com/xlang-ai/OSWorld-V2/tree/v2026.08.08)，并按其 [Docker 安装指南](https://github.com/xlang-ai/OSWorld-V2/blob/v2026.08.08/docs/PROVIDER_SETUP.md#docker)操作。在 RSIAgent 检出目录下：

```bash
git clone --branch v2026.08.08 https://github.com/xlang-ai/OSWorld-V2.git ../OSWorld-V2
cd ../OSWorld-V2
uv sync --frozen
uv pip install --python .venv/bin/python -r ../RSIAgent/requirements.txt
source .venv/bin/activate
cd ../RSIAgent
```

准备基准资产与审计文件，然后检查 VM：

```bash
python tools/prepare_osworld_v2_release.py
python tools/build_p2_corpus.py
python tools/exam_fence.py build
python tools/smoke_osworld.py --output results/smoke/osworld
```

保持该环境处于激活状态，供下文的 OSWorld 批量命令使用。在新 shell 中用 `source ../OSWorld-V2/.venv/bin/activate` 激活。
`results/` 下的审计文件留在宿主机上，绝不进入智能体提示词或记忆。

### 🎓 ALE

安装脚本会克隆固定版本的上游源码，并安装独立的评分器（grader）与 worker 环境。这种分离防止两个项目的 Python 包互相遮蔽。

```bash
python3 scripts/setup_ale.py
../agents-last-exam/.venv/bin/python run_ale.py prepare --os linux
../agents-last-exam/.venv/bin/python run_ale.py prepare --os windows
```

每次准备只下载所选系统的镜像。Linux 约需 167 GiB，Windows 约需 157 GiB，另需下载与 VM 工作空间。准备、冒烟与运行要始终使用同一个 `--cache /path/with/space`。

在运行完整 CPU 队列前检查两种访客：

```bash
../agents-last-exam/.venv/bin/python run_ale.py smoke \
  --os linux --output results/smoke/ale_linux
../agents-last-exam/.venv/bin/python run_ale.py smoke \
  --os windows --output results/smoke/ale_windows
```

ALE 要求在当前源码与运行镜像上，对每个请求的 OS 各有一次成功冒烟。重复冒烟请使用新的 `--output` 目录。存储选项与上游指南见 [ALE 运维文档](../docs/ALE.md)。

## 🚀 运行批量（Run batches）

`--arm baseline` 表示不做 RSI 的任务执行；`--arm rsi` 表示先学习再做冻结记忆评估；`--arm both` 两者都跑。

### 🖥️ OSWorld

查看完整的 108 任务计划，然后运行：

```bash
python run_osworld.py --arm both --name osworld_run_01 --dry-run
python run_osworld.py --arm both --name osworld_run_01 --concurrency 1
```

`--dry-run` 只打印计划，不启动 VM、不调用模型、不写输出。日志与任务状态在 `results/batches/<name>/` 下。一个任务失败会停止其剩余阶段；其他任务继续。每次批量请选用新的 `--name`，因为既有输出永不被覆盖。

### 🎓 ALE

查看队列、运行受支持的任务并生成报告：

```bash
../agents-last-exam/.venv/bin/python run_ale.py plan --arm both
../agents-last-exam/.venv/bin/python run_ale.py run \
  --arm both --output results/ale/run_01 --concurrency 1
../agents-last-exam/.venv/bin/python run_ale.py report \
  --runs results/ale/run_01 --output results/ale/report_01
```

每次运行请使用新的 `--output` 目录。报告包含 `tasks.csv` 与 `summary.json`，把缺失结果显式标注，并拒绝重复的计分尝试。

两个入口都直接运行批量。先用 `--concurrency 1`；当宿主机有能力承载更多独立任务谱系时再调高。自定义 OSWorld 协议与恢复见[运维文档](../docs/OPERATIONS.md)。

## ✅ 验证（Validation）

可移植检查无需凭据、Docker 或基准安装即可运行：

```bash
uv venv .venv --python 3.12
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python tools/check_rsi_release.py
```

安装步骤中的 VM 冒烟命令使用合成文件检查传输、不可变记忆、候选重放、Verifier 隔离与 checkpoint 回滚。它们不调用模型或官方评分器。冒烟成功验证的是运行时机制；复现基准分数需要使用固定配置的完整实验。

## 🔗 相关项目（Related Projects）

- **[RSIGame](https://github.com/WenyiWU0111/RSIGame)** 把我们在递归自我改进上的研究扩展到自主游戏开发。智能体迭代地构建、测试与打磨游戏，并利用每一轮的反馈指导后续改进。它探索开发期间积累的经验如何帮助智能体改进自己创造的游戏。

  [论文](https://arxiv.org/abs/2609.39045) · [代码](https://github.com/WenyiWU0111/RSIGame)

## 📚 文档（Documentation）

- [架构](../docs/ARCHITECTURE.md)
- [OSWorld 运维与恢复](../docs/OPERATIONS.md)
- [ALE 搭建、批量与报告](../docs/ALE.md)
- [发布溯源与验证](../docs/RELEASE.md)
- [论文与报告范围](../docs/PAPER.md)
- [贡献指南](../docs/CONTRIBUTING.md) · [第三方署名](../docs/THIRD_PARTY.md)

## 📝 引用（Citation）

使用 RSIAgent 时请引用 [arXiv 预印本](https://arxiv.org/abs/2609.15364)：

```bibtex
@misc{zhu2026rsiagentautonomousexplorationrecursive,
      title={RSIAgent: Autonomous Exploration for Recursive Self-improvement in New Environments},
      author={Sibo Zhu and Shicheng Fan and Xinyue Wang and Wenyi Wu and Kun Zhou and Biwei Huang},
      year={2026},
      eprint={2609.15364},
      archivePrefix={arXiv},
      primaryClass={cs.AI},
      url={https://arxiv.org/abs/2609.15364},
}
```

## ⚖️ 许可证（License）

RSIAgent 采用 [Apache License 2.0](../LICENSE) 许可。第三方依赖与基准资产保留各自许可与条款；见[第三方署名](../docs/THIRD_PARTY.md)。
