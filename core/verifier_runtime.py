"""Candidate-safe, code-as-policy runtimes for the Verifier Agent.

The historical ``effect_isolated`` boundary runs Verifier programs in Linux
namespaces where the candidate filesystem is read-only and Actor process/network/GUI
channels are absent.  The experimental ``rollback_mirror`` boundary instead saves
the complete QEMU state, lets the Verifier observe and test that live state, and
restores the checkpoint before grading.  In both modes Actor-private paths remain
hidden and Verifier-authored effects cannot enter the scored candidate.

The final report is published directly to the host harness and never enters the
guest. There are no verifier-specific observation, file-size, probe-count, or
program-count caps; the ordinary run wall clock remains the emergency ceiling.
"""
from __future__ import annotations

import base64
import os
import pathlib
import posixpath
import re
import secrets
import shlex
import tempfile
import time
from dataclasses import dataclass
# 本模块为 Verifier Agent（独立验证代理）提供“候选安全、代码即策略（code-as-policy）”
# 的运行时：effect_isolated 用 Linux 命名空间隔离，rollback_mirror 用 QEMU 快照
# 事务回滚；最终报告只发布给宿主 harness，绝不进入客户机。

AGENTIC_VERIFIER_SYSTEM = """\
You are an independent Verifier Agent inspecting one completed candidate on a real
Ubuntu machine. You decide how to investigate, what programs and tools to use, how
deeply to test, and when the evidence is sufficient. You do not repair the task.

Use exactly ONE plain JSON action object per reply. You may run arbitrary code with:
- {"program":{"lang":"python","code":"<complete Python program>"}}
- {"program":{"lang":"bash","code":"<complete Bash program>"}}
You may inspect pixels with:
- {"look":{"path":"/absolute/local/path","question":"..."}}
- {"look":{"path":"screen:","question":"..."}}
When your investigation is complete, publish your freely organized report with:
- {"program":{"lang":"verifier-report","code":"<complete report>"}}
The report must contain exactly one standalone VERDICT: PASS or VERDICT: FAIL line.
Done does not publish a Verifier decision.

Your Python/Bash runs are mechanically effect-isolated from the Actor's candidate:
the machine filesystem is read-only, the Actor's processes, network, GUI, and IPC
channels are unavailable, and privilege escalation is disabled. Your private
network namespace has loopback only: you may start and query your own local service,
but cannot reach the Actor's service or any external endpoint. VERIFIER_SCRATCH
names your private writable tmpfs; it persists across your Program actions and may
hold copies, renders, extracted archives, databases, or any other investigative
artifacts. /tmp is also writable but lasts only for the current Program. Copy inputs
to VERIFIER_SCRATCH before using a tool that expects to write beside its input. A
normal Look may inspect files you generated in VERIFIER_SCRATCH. These mechanics
protect the scored state without prescribing your method.

Treat observations as evidence, not proof by assertion. Derive every binding
requirement from the authoritative task and falsify nearby plausible substitutes.
PASS only when every material requirement is affirmatively supported. If a material
requirement is violated or remains unsupported after investigation, publish FAIL
with the concrete evidence. Causal learning and repair remain the Actor Agent's role.
"""
# （上述 system prompt 大意：Verifier 是在真实 Ubuntu 机器上检查一个已完成候选的
# 独立代理——每轮恰好一个 JSON 动作（Program/Look/verifier-report）；报告必须含
# 恰好一行独立 VERDICT: PASS/FAIL；运行被机械式效果隔离（effect isolation）：
# 文件系统只读、Actor 进程/网络/GUI/IPC 不可达、禁提权、私有网络命名空间仅回环、
# VERIFIER_SCRATCH 私有 tmpfs 跨动作持久；把观察当证据而非断言，从权威任务推导
# 每条约束并证伪邻近的貌似替代品；因果学习与修复仍是 Actor 的职责。）

AGENTIC_VERIFIER_NUDGE = (
    'Your reply contained no valid Verifier action. Reply with exactly one plain '
    'JSON object: a Python/Bash Program, a Look on an absolute local path or screen:, '
    'or a verifier-report Program when your decision is ready.')
    # 轻推（nudge）：回复中没有合法动作时要求重新给出恰好一个 JSON 动作。

AGENTIC_VERIFIER_STRICT_NUDGE = (
    'Your reply described an investigation step but performed none. Emit exactly '
    'one Python/Bash Program, Look, or final verifier-report action now.')
    # 严格推（strict nudge）：描述了调查步骤但什么都没做，要求立即执行动作。

AGENTIC_VERIFIER_PREMATURE_DONE = (
    'Done does not publish a Verifier decision. Continue investigating with arbitrary '
    'Python/Bash or Look, then finish with one verifier-report Program containing '
    'your complete report and exactly one standalone VERDICT line.')
    # 过早 Done 纠正：Done 不发布 Verifier 决定，必须继续调查并以 verifier-report 收尾。

AGENTIC_VERIFIER_USER_REMINDER = (
    "VERIFIER ROLE ACTION CONTRACT: You remain a full investigation Agent. Use one "
    "arbitrary Python/Bash Program or Look per turn; programs are mechanically "
    "effect-isolated and VERIFIER_SCRATCH persists. Publish the final decision with "
    "verifier-report, not Done.")
    # 每轮用户消息末尾附加的角色行动契约提醒（效果隔离 + SCRATCH 持久）。


_EFFECT_ISOLATION_DESCRIPTION = """\
Your Python/Bash runs are mechanically effect-isolated from the Actor's candidate:
the machine filesystem is read-only, the Actor's processes, network, GUI, and IPC
channels are unavailable, and privilege escalation is disabled. Your private
network namespace has loopback only: you may start and query your own local service,
but cannot reach the Actor's service or any external endpoint. VERIFIER_SCRATCH
names your private writable tmpfs; it persists across your Program actions and may
hold copies, renders, extracted archives, databases, or any other investigative
artifacts. /tmp is also writable but lasts only for the current Program. Copy inputs
to VERIFIER_SCRATCH before using a tool that expects to write beside its input. A
normal Look may inspect files you generated in VERIFIER_SCRATCH. These mechanics
protect the scored state without prescribing your method."""
# （上述段：effect_isolated 的机制描述，嵌入 system prompt 的对应位置。）

_ROLLBACK_MIRROR_DESCRIPTION = """\
Your Python/Bash runs execute in a transactional mirror of the exact live Actor
machine. At the beginning of this inspection the harness saved a complete QEMU
checkpoint. You can observe its files, processes, localhost services, network, GUI,
and IPC and may run arbitrary tests. When this inspection ends, the harness restores
that checkpoint before the candidate is graded, so none of your in-VM effects enter
the scored state. Privilege escalation is disabled and harness-owned Actor-private
paths are absent. VERIFIER_SCRATCH is a private writable tmpfs that persists across
your Program actions. State in an external service outside this QEMU guest cannot be
rolled back: observe such services without sending mutating requests. Establish the
original candidate's properties before destructive experiments; a property created
only by your own test is not candidate evidence. These mechanics protect the scored
state without prescribing your method."""
# （上述段：rollback_mirror（回滚镜像）的机制描述——检查开始时 harness 保存完整
# QEMU 检查点（checkpoint），Verifier 可观察并测试活动状态；检查结束、评分之前
# harness 恢复检查点，因此在 VM 内的任何效果都不进入计分状态；QEMU 之外的
# 外部服务无法回滚，只许观察不许发变更请求；破坏性实验前先确立候选原有性质。"""


def agentic_verifier_system_for(
        execution_mode="effect_isolated", *, orienting=False,
        allow_evolve=False, allow_unverified=False):
    """Return the role prompt whose mechanics match the active executor."""
    # 按当前执行器装配角色提示：选择基础模板并按模式/阶段替换机制描述。
    from env.qemu_rollback import (
        ROLLBACK_MIRROR,
        normalize_verifier_execution_mode,
    )
    # 延迟导入以避免循环依赖。

    mode = normalize_verifier_execution_mode(execution_mode)
    # 规整执行模式。
    base = (AGENTIC_VERIFIER_ORIENTATION_SYSTEM if orienting else
            (AGENTIC_VERIFIER_ROUTE_SYSTEM if allow_evolve
             else AGENTIC_VERIFIER_SYSTEM))
    # 依阶段选择模板：定向（orientation）> 路由（route/allow_evolve）> 默认裁决。
    if allow_unverified and not orienting and not allow_evolve:
        base = base.replace(
            "The report must contain exactly one standalone VERDICT: PASS or VERDICT: FAIL line.",
            "The report must contain exactly one standalone VERDICT: PASS, "
            "VERDICT: FAIL, or VERDICT: UNVERIFIED line.",
        ).replace(
            "PASS only when every material requirement is affirmatively supported. If a material\n"
            "requirement is violated or remains unsupported after investigation, publish FAIL\n"
            "with the concrete evidence. Causal learning and repair remain the Actor Agent's role.",
            "PASS only when every material requirement is affirmatively supported. Publish FAIL\n"
            "when concrete evidence establishes a material violation. Publish UNVERIFIED when a\n"
            "material claim remains unresolved after investigation or credible instruments disagree.\n"
            "UNVERIFIED requests reproducible evidence from the same Actor Agent; it is neither\n"
            "acceptance nor proof of failure. Reconcile conflicting channels before PASS rather than\n"
            "silently privileging one instrument. Causal learning and repair remain the Actor's role.",
        )
        # 允许 UNVERIFIED 判定时改写裁决词表：UNVERIFIED 表示向同一 Actor 索取可复现
        # 证据，既非接受也非失败证明；PASS 前必须调和冲突通道。
    if mode == ROLLBACK_MIRROR:
        base = base.replace(
            _EFFECT_ISOLATION_DESCRIPTION, _ROLLBACK_MIRROR_DESCRIPTION)
        # 回滚镜像模式：把机制描述替换为事务镜像版本。
    return base


# The unified loop changes only the terminal transport token. Investigation
# authority and effect isolation remain identical to the validated Agent runtime.
# （既有英文注释：统一循环只改终局传输令牌；调查权威与效果隔离与已验证运行时相同。）
AGENTIC_VERIFIER_ROUTE_SYSTEM = AGENTIC_VERIFIER_SYSTEM.replace(
    "The report must contain exactly one standalone VERDICT: PASS or VERDICT: FAIL line.",
    "The report must contain exactly one standalone ROUTE: HANDOFF, ROUTE: REVISE, "
    "or ROUTE: EVOLVE line.",
).replace(
    "PASS only when every material requirement is affirmatively supported. If a material\n"
    "requirement is violated or remains unsupported after investigation, publish FAIL\n"
    "with the concrete evidence. Causal learning and repair remain the Actor Agent's role.",
    "HANDOFF only when every material requirement is affirmatively supported. Otherwise,\n"
    "publish REVISE or EVOLVE as defined in the authoritative project charter, with the\n"
    "concrete evidence. Causal learning and repair remain the Actor Agent's role.",
)
# 路由模式 system prompt：终局令牌从 VERDICT 改为 ROUTE（HANDOFF/REVISE/EVOLVE，
# 依权威项目章程定义）。

AGENTIC_VERIFIER_ROUTE_NUDGE = (
    'Your reply contained no valid Verifier action. Reply with exactly one plain '
    'JSON object: a Python/Bash Program, a Look on an absolute local path or screen:, '
    'or a verifier-report Program containing one HANDOFF / REVISE / EVOLVE route '
    'when your decision is ready.')
    # 路由模式轻推。

AGENTIC_VERIFIER_ROUTE_STRICT_NUDGE = (
    'Your reply described an investigation step but performed none. Emit exactly '
    'one Python/Bash Program, Look, or final verifier-report route action now.')
    # 路由模式严格推。

AGENTIC_VERIFIER_ROUTE_PREMATURE_DONE = (
    'Done does not publish a Verifier route. Continue investigating with arbitrary '
    'Python/Bash or Look, then finish with one verifier-report Program containing '
    'your complete report and exactly one standalone ROUTE line.')
    # 路由模式过早 Done 纠正。

AGENTIC_VERIFIER_ROUTE_USER_REMINDER = (
    "VERIFIER ROLE ACTION CONTRACT: You remain a full investigation Agent. Use one "
    "arbitrary Python/Bash Program or Look per turn; programs are mechanically "
    "effect-isolated and VERIFIER_SCRATCH persists. Publish HANDOFF, REVISE, or "
    "EVOLVE through verifier-report, not Done.")
    # 路由模式每轮契约提醒。


AGENTIC_VERIFIER_ORIENTATION_SYSTEM = AGENTIC_VERIFIER_SYSTEM.replace(
    "one completed candidate",
    "the trusted task-start environment during private orientation before any "
    "Actor Agent action",
).replace(
    "The report must contain exactly one standalone VERDICT: PASS or VERDICT: FAIL line.",
    "The private report must contain exactly one standalone "
    "STAGE: ORIENTATION_READY line.",
).replace(
    "Treat observations as evidence, not proof by assertion. Derive every binding\n"
    "requirement from the authoritative task and falsify nearby plausible substitutes.\n"
    "PASS only when every material requirement is affirmatively supported. If a material\n"
    "requirement is violated or remains unsupported after investigation, publish FAIL\n"
    "with the concrete evidence. Causal learning and repair remain the Actor Agent's role.",
    "No candidate exists in this stage. Freely learn the authoritative environment,\n"
    "preserve evidence, and use or author any private instruments you judge useful.\n"
    "An executable evaluator is optional. Do not grade, route, or repair anything.",
)
# 定向（orientation）阶段 system prompt：对象是“任何 Actor 动作之前的受信任务起始
# 环境”；报告令牌为 STAGE: ORIENTATION_READY；该阶段无候选，自由学习环境、保存
# 证据、可自建工具，但不得评分/路由/修复。

AGENTIC_VERIFIER_ORIENTATION_NUDGE = (
    'Your reply contained no valid Verifier action. Reply with exactly one plain '
    'JSON object: a Python/Bash Program, a Look on an absolute local path or screen:, '
    'or a verifier-report Program containing STAGE: ORIENTATION_READY when your '
    'private task-start orientation is ready.')
    # 定向阶段轻推。

AGENTIC_VERIFIER_ORIENTATION_PREMATURE_DONE = (
    'Done does not complete Verifier orientation. Continue with arbitrary Python/Bash '
    'or Look, then publish one verifier-report with exactly one standalone '
    'STAGE: ORIENTATION_READY line when you judge the orientation sufficient.')
    # 定向阶段过早 Done 纠正。

AGENTIC_VERIFIER_ORIENTATION_USER_REMINDER = (
    "VERIFIER ROLE ACTION CONTRACT — STAGE: ORIENTATION. Use one arbitrary "
    "Python/Bash Program or Look per turn; VERIFIER_SCRATCH persists. No candidate "
    "exists, so do not grade or route. Publish STAGE: ORIENTATION_READY through "
    "verifier-report when you judge your private task-start orientation sufficient.")
    # 定向阶段每轮契约提醒。


def agentic_verifier_user_message(
        message, allow_evolve=False, stage="candidate_verification",
        execution_mode="effect_isolated", allow_unverified=False):
    """Preserve every observation and append the role's truthful action contract."""
    # 保留全部观察原文，并在末尾追加与阶段/模式相符的真实行动契约。
    if stage == "orientation":
        reminder = AGENTIC_VERIFIER_ORIENTATION_USER_REMINDER
        # 定向阶段使用定向提醒。
    else:
        reminder = (AGENTIC_VERIFIER_ROUTE_USER_REMINDER if allow_evolve
                    else AGENTIC_VERIFIER_USER_REMINDER)
        # 路由/裁决模式各用其提醒。
        if allow_unverified and not allow_evolve:
            reminder = reminder.replace(
                "Publish the final decision",
                "Publish PASS, FAIL, or UNVERIFIED")
            # 允许 UNVERIFIED 时同步改写提醒词表。
    from env.qemu_rollback import ROLLBACK_MIRROR, normalize_verifier_execution_mode

    if normalize_verifier_execution_mode(execution_mode) == ROLLBACK_MIRROR:
        reminder = reminder.replace(
            "programs are mechanically effect-isolated",
            "programs run inside the checkpointed rollback mirror")
        # 回滚镜像模式：把“效果隔离”表述改为“检查点回滚镜像内运行”。
    return (message or "").rstrip() + "\n\n" + reminder
    # 原消息去尾空白后接空行与提醒。


@dataclass
class VerifierTrace:
    """Duck-compatible result consumed by :func:`core.loop.run_attempt`."""
    # 与 env.vm.Trace 鸭子兼容的结果类型，供 core.loop.run_attempt 消费。

    stdout: str
    # 合并输出（含 [exit N] 尾行）。
    exit_code: int | None = None
    # 退出码（未知为 None）。
    secs: float = 0.0
    # 耗时（秒）。
    timed_out: bool = False
    # 是否超时。
    infra_fail: bool = False
    # 是否基础设施故障（非候选证据）。


def _trace(message, exit_code=0, *, infra_fail=False):
    # 构造带 [exit N] 尾行的 VerifierTrace 的便捷函数。
    suffix = f"\n[exit {exit_code}]"
    return VerifierTrace(
        stdout=(message or "") + suffix,
        exit_code=exit_code,
        infra_fail=infra_fail,
    )


class AgenticVerifierInfrastructureError(RuntimeError):
    """The Verifier's trusted execution boundary became unavailable.

    This is deliberately distinct from a Verifier verdict.  Callers must abort or
    mechanically recover the run; they may never expose it to an Agent as evidence
    about candidate correctness.
    """
    # （docstring 大意：Verifier 受信执行边界不可用；与 Verifier 裁决刻意区分，
    # 调用方必须中止或机械恢复，绝不能作为候选正确性的证据暴露给 Agent。）


class AgenticVerifierNoProgressError(AgenticVerifierInfrastructureError):
    """Repeated actionless segments need review of the preserved inspection.

    Rebuilding an execution boundary cannot repair an actionless model context.
    Task runners should preserve the VM for recovery and leave scoring sealed.
    """
    # （docstring 大意：反复无动作分段需要人工审查保存的检查过程；重建执行边界
    # 修不好无动作的模型上下文；任务运行器应保留 VM 供恢复、评分保持封存。）


def _run_script_with_staging_fallback(vm, lang, code, *, timeout, cap):
    """Use the new Verifier-only staging fallback without breaking live workers.

    Long benchmark waves can have already-imported ``VM`` instances when a newer
    verifier_runtime module is imported lazily. Such workers expose the previous
    run_script signature. Falling back to that exact legacy call preserves their
    pre-patch behavior instead of crashing at the Verifier handoff.
    """
    # （docstring 大意：使用 Verifier 专属的暂存回退而不破坏在跑 worker——
    # 惰性导入新模块时旧 VM 实例可能只有旧 run_script 签名，此时回退到旧调用。）
    try:
        return vm.run_script(
            lang, code, timeout=timeout, cap=cap,
            allow_staging_fallback=True)
        # 首选：带暂存回退的新签名。
    except TypeError as exc:
        if "unexpected keyword argument 'allow_staging_fallback'" not in str(exc):
            raise
            # 其他 TypeError 一律上抛（真正的 bug 不能吞）。
        return vm.run_script(lang, code, timeout=timeout, cap=cap)
        # 旧签名 worker：按原行为调用。


def _normalize_private_paths(hide_actor_memory=False, private_paths=()):
    # 规整 Actor 私有路径集合：校验绝对路径、去重、可选追加默认记忆路径。
    paths = list(private_paths or ())
    if hide_actor_memory:
        paths.append("/home/user/.memory")
        # hide_actor_memory 开关：把 Actor 记忆目录加入隐藏集合。
    normalized = []
    for path in paths:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("Verifier private paths must be absolute guest paths")
            # 必须是客户机绝对路径。
        candidate = posixpath.normpath(path)
        if candidate == "/" or "\x00" in candidate or "\n" in candidate:
            raise ValueError("Verifier private path is unsafe")
            # 根路径/空字节/换行都不安全，直接拒绝。
        if candidate not in normalized:
            normalized.append(candidate)
            # 规整后去重。
    return tuple(normalized)


def _is_private_path(path: str, private_paths=()) -> bool:
    # 判定某路径是否等于或位于任一私有路径之下。
    normalized = posixpath.normpath(path)
    return any(normalized == private
               or normalized.startswith(private.rstrip("/") + "/")
               for private in private_paths)


def validate_verifier_look_path(
        path, hide_actor_memory=False, private_paths=()):
    """Reject remote/option-like Look targets before a trusted renderer sees them."""
    # 在受信渲染器接触之前拒绝远程/选项形 Look 目标（防 SSRF/注入到渲染器）。
    if not isinstance(path, str) or "\x00" in path or "\n" in path:
        return False, "Verifier Look path is malformed."
        # 非字符串或含控制字符：格式非法。
    if path.lower().startswith("screen:"):
        display = path.split(":", 1)[1].strip()
        if not display or re.fullmatch(r":?\d+(?:\.\d+)?", display):
            return True, ""
            # screen: 目标只接受空或数字显示器号（如 :0 / 0.0）。
        return False, "Verifier screen Look requires a numeric display."
        # 其余 screen: 变体拒绝。
    if not path.startswith("/"):
        return False, "Verifier Look requires an absolute local path or screen:."
        # 只允许绝对本地路径或 screen:。
    hidden = _normalize_private_paths(hide_actor_memory, private_paths)
    if _is_private_path(path, hidden):
        return False, "That path is private to the Actor Agent."
        # Actor 私有路径对 Verifier 隐藏。
    return True, ""


class AgenticVerifierExecutor:
    """Execute Verifier-authored code without changing the scored candidate.

    Both modes give the Verifier an unbounded private tmpfs workspace and drop all
    capabilities. ``effect_isolated`` additionally makes the visible filesystem
    read-only and creates private PID/network/IPC/UTS namespaces.
    ``rollback_mirror`` instead exposes the exact live namespaces inside a QEMU
    transaction and restores the checkpoint when this executor closes.

    If the selected protection primitive is unavailable, execution fails closed as
    infrastructure. It never falls back to ordinary unprotected model execution.
    """
    # （docstring 大意：两种模式都给 Verifier 无上限私有 tmpfs 并完全降权；
    # effect_isolated 额外把可见文件系统只读并建私有 PID/网络/IPC/UTS 命名空间；
    # rollback_mirror 则在 QEMU 事务内暴露完整活动命名空间并在关闭时恢复检查点；
    # 保护原语不可用时按基础设施 fail-closed，绝不回退到无保护执行。）

    def __init__(self, vm, *, hide_actor_memory=False, private_paths=(),
                 scratch_archive=None, execution_mode="effect_isolated"):
        from env.qemu_rollback import (
            ROLLBACK_MIRROR,
            QemuRollbackTransaction,
            normalize_verifier_execution_mode,
        )
        # 延迟导入回滚模块。

        self._vm = vm
        self._execution_mode = normalize_verifier_execution_mode(execution_mode)
        self._hide_actor_memory = bool(hide_actor_memory)
        self._private_paths = _normalize_private_paths(
            hide_actor_memory, private_paths)
        # 记录执行模式与规整后的 Actor 私有路径集合。
        self._scratch_archive = scratch_archive
        # 可选的先前 scratch 归档（用于跨执行恢复私有工作区）。
        checkpoint_options = {}
        if hasattr(vm, "verifier_checkpoint_timeout"):
            checkpoint_options["timeout"] = vm.verifier_checkpoint_timeout
        # VM 可声明自定义检查点超时。
        self._rollback_transaction = (
            QemuRollbackTransaction(vm, **checkpoint_options)
            if self._execution_mode == ROLLBACK_MIRROR else None)
        # 回滚镜像模式：创建 QEMU 事务对象；经典模式为 None。
        self._rollback_private_paths_hidden = False
        # 镜像内私有路径隔离是否已完成。
        password = str(getattr(getattr(vm, "env", None),
                               "client_password", "") or "")
        self._sudo_password_b64 = base64.b64encode(
            password.encode("utf-8")).decode("ascii")
        # harness 持有的桌面 sudo 凭据（base64），仅供受信包装脚本提权。
        self._workspace = "/mnt/rsiagent_verifier_" + secrets.token_hex(16)
        # 私有 scratch 工作区路径（随机化，挂载点）。
        self._namespace_pid = None
        self._desktop_uid = None
        self._desktop_gid = None
        # 私有挂载命名空间 keeper 的 PID 与桌面用户 uid/gid。
        self._initialized = False
        self._workspace_attempted = False
        self._closed = False
        self._init_reason = ""
        self._last_namespace_check = 0.0
        # 初始化状态、尝试标记、关闭标记、失败原因与最近命名空间健康检查时刻。
        self.published_report = ""
        self._report_validator = None
        self._report_expectation = "the required standalone terminal token"
        # 已发布的最终报告、词法校验器与期望说明。

    @property
    def workspace(self):
        return self._workspace

    @property
    def vm(self):
        return self._vm

    @property
    def private_paths(self):
        return self._private_paths

    @property
    def execution_mode(self):
        return self._execution_mode
    # 只读属性：工作区路径、VM、私有路径集合、执行模式。

    def clear_published_report(self):
        self.published_report = ""
        # 清空已发布报告（开始新一轮检查时调用）。

    def _run_harness_script(self, lang, code, *, timeout, cap):
        # 受信 harness 脚本的统一传输入口：优先走 root-only 控制通道，
        # 否则退回普通 run_script（带暂存回退）。
        control = getattr(getattr(self._vm, "env", None),
                          "_rsiagent_verifier_control", None)
        if control is not None:
            return control.run_script(lang, code, timeout=timeout, cap=cap)
            # provision 阶段装好的 VerifierControl 通道存在则优先使用。
        return _run_script_with_staging_fallback(
            self._vm, lang, code, timeout=timeout, cap=cap)
        # 否则用普通 VM 传输。

    def set_report_validator(self, validator, expectation):
        """Configure lexical host-channel validation for the active Agent stage."""
        # 配置当前阶段的报告词法校验器（如必须含 VERDICT/ROUTE/STAGE 令牌）。
        self._report_validator = validator
        self._report_expectation = str(
            expectation or "the required standalone terminal token")

    def __call__(self, lang, code, timeout=600, cap=0):
        # Verifier 的动作入口：verifier-report 发布报告；python/bash 进入隔离执行。
        language = (lang or "").strip().lower()
        if language == "verifier-report":
            if not isinstance(code, str) or not code.strip():
                return _trace("Verifier report is empty.", 2)
                # 空报告直接拒绝（退出码 2）。
            self.published_report = code
            # 保存报告（发布给宿主 harness，绝不进客户机）。
            validator = self._report_validator
            if callable(validator) and validator(code) is None:
                return _trace(
                    "Verifier report was preserved but NOT accepted by the host "
                    "transport: it must contain " + self._report_expectation +
                    ". Correct only the report-channel syntax and publish again; "
                    "your investigation context and private scratch are unchanged.",
                    2,
                )
                # 词法校验失败：报告已保存但未被接受，提示只改报告通道语法重发，
                # 调查上下文与私有 scratch 保持不变。
            return _trace("Verifier report published to the host harness.")
            # 发布成功。

        if language.startswith("py"):
            interpreter = "python3"
            extension = "py"
        elif language in {"bash", "sh"}:
            interpreter = "bash"
            extension = "sh"
        else:
            return _trace(
                "Verifier Program language is unsupported. Use Python, Bash, Look, "
                "or verifier-report.",
                126,
            )
            # 不支持的语言：返回 126。

        ready = self._ensure_workspace(timeout=min(max(int(timeout), 30), 180))
        if ready is not None:
            return ready
            # 确保隔离工作区就绪；失败则返回该基础设施错误 Trace。
        result = self._run_isolated(
            interpreter, extension, code or "", timeout=max(1, int(timeout)))
        return result
        # 进入所选保护边界执行模型代码。

    def _ensure_workspace(self, timeout):
        # 确保私有隔离工作区（挂载命名空间 + tmpfs scratch）就绪；返回 None 表示就绪。
        if self._closed:
            return _trace(
                "Verifier isolation workspace is already closed.", 125,
                infra_fail=True)
            # 执行器已关闭：拒绝新动作。
        if self._initialized:
            # A target Verifier can be idle while the Actor works for hours.  A
            # cached PID is not proof that its private mount namespace still
            # exists.  Avoid an extra guest round trip for immediately adjacent
            # trusted operations, but revalidate after any real idle interval.
            # （既有英文注释：缓存 PID 不能证明私有挂载命名空间仍存在；相邻受信操作
            # 免一次往返，真实空闲间隔后必须重验。）
            if time.monotonic() - self._last_namespace_check < 5.0:
                return None
                # 5 秒内的近期健康记录直接放行。
            return self._check_workspace(timeout)
            # 否则重新校验命名空间健康。
        if self._init_reason:
            return _trace(self._init_reason, 125, infra_fail=True)
            # 曾初始化失败：直接复现该错误。

        if self._rollback_transaction is not None:
            try:
                self._rollback_transaction.begin()
                # 回滚镜像：先保存 QEMU 检查点（此后回滚是强制义务）。
                hidden = self._quarantine_rollback_private_paths(timeout)
                if hidden is not None:
                    self._init_reason = hidden.stdout
                    return hidden
                    # 镜像内隔离 Actor 私有路径失败：中止。
            except Exception as exc:  # noqa: BLE001 - trusted boundary failure
                self._init_reason = (
                    "Verifier rollback-mirror preflight failed; model-authored code "
                    f"was not executed. {type(exc).__name__}: {exc}")
                return _trace(self._init_reason, 125, infra_fail=True)
                # 受信边界失败按基础设施处理（fail-closed）。

        workspace = self._workspace
        if not self._sudo_password_b64:
            self._init_reason = (
                "Verifier execution-boundary preflight failed: the VM transport did "
                "not expose its harness-held privilege credential. Model-authored "
                "code was not executed.")
            return _trace(self._init_reason, 125, infra_fail=True)
            # 无提权凭据则边界无法建立：拒绝执行模型代码。
        self._workspace_attempted = True
        token = secrets.token_hex(12)
        keeper_path = f"/tmp/rsiagent_verifier_keeper_{token}.sh"
        status_path = f"/tmp/rsiagent_verifier_keeper_{token}.status"
        log_path = f"/tmp/rsiagent_verifier_keeper_{token}.log"
        # keeper 脚本/状态/日志的随机化路径。
        keeper = r'''#!/bin/bash
set -eu
umask 077
workspace_path=$1
status_path=$2
uid_value=$3
gid_value=$4
cleanup() {
  mountpoint -q "$workspace_path" && umount -l "$workspace_path" || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT HUP
mount --make-rprivate /
mount -t tmpfs \
  -o nosuid,nodev,mode=0700,uid="$uid_value",gid="$gid_value" \
  rsiagent-verifier-scratch "$workspace_path"
test -w "$workspace_path"
printf 'READY %s\n' "$$" > "$status_path"
while :; do sleep 3600; done
'''
        # keeper 脚本语义：在私有挂载命名空间内把根设为 rprivate，于工作区路径挂载
        # 属主为桌面用户的 0700 tmpfs（nosuid,nodev）；确认可写后把 "READY <pid>"
        # 写入状态文件，然后永久驻留（其生命即命名空间的生命；退出时惰性卸载）。
        keeper_b64 = base64.b64encode(keeper.encode("utf-8")).decode("ascii")
        # keeper 源码 base64 编码以便安全嵌入。
        script = f"""\
set -eu
workspace_path={workspace!r}
keeper_path={keeper_path!r}
status_path={status_path!r}
log_path={log_path!r}
keeper_payload={keeper_b64!r}
sudo_password_b64={self._sudo_password_b64!r}
uid_value=$(id -u user)
gid_value=$(id -g user)
run_privileged() {{
  printf %s "$sudo_password_b64" | base64 -d | \
    sudo -S -k -p '' -- "$@"
}}
clear_privilege_cache() {{ sudo -K >/dev/null 2>&1 || true; }}
trap clear_privilege_cache EXIT
command -v unshare >/dev/null
command -v nsenter >/dev/null
command -v setpriv >/dev/null
if [ -L "$workspace_path" ] || {{ [ -e "$workspace_path" ] && [ ! -d "$workspace_path" ]; }}; then
  echo 'workspace path is not a directory'
  exit 125
fi
if mountpoint -q "$workspace_path"; then
  echo 'Verifier scratch unexpectedly exists in the Actor-visible mount namespace'
  exit 125
fi
run_privileged install -d -m 0000 -o root -g root "$workspace_path"
printf %s "$keeper_payload" | base64 -d > "$keeper_path"
chmod 0700 "$keeper_path"
rm -f -- "$log_path"
run_privileged nohup unshare --mount -- bash "$keeper_path" \
  "$workspace_path" "$status_path" "$uid_value" "$gid_value" \
  >"$log_path" 2>&1 </dev/null &
for _attempt in $(seq 1 300); do
  run_privileged test -s "$status_path" && break
  sleep 0.1
done
if ! run_privileged test -s "$status_path"; then
  echo 'private mount-namespace keeper did not become ready'
  cat "$log_path" 2>/dev/null || true
  exit 125
fi
keeper_status=$(run_privileged cat "$status_path")
namespace_pid=${{keeper_status#READY }}
case "$namespace_pid" in
  ''|*[!0-9]*) echo 'private mount-namespace keeper returned an invalid PID'; exit 125 ;;
esac
run_privileged kill -0 "$namespace_pid"
run_privileged nsenter --target "$namespace_pid" --mount -- \
  mountpoint -q "$workspace_path"
run_privileged nsenter --target "$namespace_pid" --mount -- \
  test -w "$workspace_path"
if mountpoint -q "$workspace_path" || run_privileged setpriv \
    --reuid="$uid_value" --regid="$gid_value" --clear-groups \
    test -r "$workspace_path"; then
  echo 'Verifier scratch leaked into the Actor-visible mount namespace'
  run_privileged kill "$namespace_pid" 2>/dev/null || true
  exit 125
fi
printf 'RSIAGENT_VERIFIER_NAMESPACE=%s:%s:%s\n' \
  "$namespace_pid" "$uid_value" "$gid_value"
run_privileged rm -f -- "$status_path"
rm -f -- "$keeper_path" "$log_path"
"""
        # 引导脚本语义（f-string 模板，插值均为 harness 生成的 token/base64）：
        # 校验工作区路径可用且未在 Actor 可见命名空间中挂载 → root 建立挂载点(0000)
        # → 落盘并以 nohup+unshare --mount 启动 keeper → 轮询 READY 状态拿到
        # 命名空间 PID → 用 nsenter 验证挂载与可写 → 再从 Actor 侧验证不可见/不可读
        # （泄漏即杀 keeper 退出 125）→ 打印 RSIAGENT_VERIFIER_NAMESPACE 标记行并清理。
        result = self._run_harness_script(
            "bash", script, timeout=timeout, cap=0)
        # 执行引导脚本（cap=0 无损输出）。
        if result.exit_code != 0 or result.infra_fail:
            self._init_reason = (
                "Verifier private-workspace preflight failed; model-authored code was "
                "not executed.\n" + (result.stdout or "(no diagnostic output)"))
            return _trace(self._init_reason, 125, infra_fail=True)
            # 预检失败：模型代码未执行，按基础设施错误返回。
        marker = re.search(
            r"(?m)^RSIAGENT_VERIFIER_NAMESPACE=(\d+):(\d+):(\d+)$",
            result.stdout or "")
        if marker is None:
            self._init_reason = (
                "Verifier private-workspace preflight failed: the private mount "
                "namespace did not publish its identity.\n" +
                (result.stdout or "(no diagnostic output)"))
            return _trace(self._init_reason, 125, infra_fail=True)
            # 缺少命名空间身份标记：同样判失败。
        self._namespace_pid = int(marker.group(1))
        self._desktop_uid = int(marker.group(2))
        self._desktop_gid = int(marker.group(3))
        # 记录命名空间 keeper PID 与桌面 uid/gid。
        self._initialized = True
        self._last_namespace_check = time.monotonic()
        # 标记初始化完成并记录健康检查时刻。
        if self._scratch_archive is not None:
            restored = self._restore_scratch(self._scratch_archive)
            if restored is not None:
                self._initialized = False
                self._init_reason = restored.stdout
                return restored
                # 恢复先前 scratch 失败：回退初始化状态。
            self._scratch_archive = None
            # 恢复成功即清除归档引用。
        return None
        # 就绪。

    def _quarantine_rollback_private_paths(self, timeout):
        """Remove Actor-private files from the disposable mirror before Programs.

        A mount-only mask can be bypassed through another process's ``/proc/PID/root``
        when process observation is intentionally enabled.  Moving the paths into a
        root-only directory after the QEMU checkpoint hides the bytes from every
        guest process; rollback restores their exact names and contents afterward.
        """
        # （docstring 大意：开启进程观察时，仅挂载遮罩可被其他进程的 /proc/PID/root
        # 绕过；检查点之后把路径搬进 root-only 目录可对一切客户机进程隐藏字节；
        # 回滚会精确恢复原名与内容。）
        if self._rollback_private_paths_hidden or not self._private_paths:
            self._rollback_private_paths_hidden = True
            return None
            # 幂等：已隔离或无私有路径则直接返回。
        encoded = base64.b64encode(
            "\n".join(self._private_paths).encode("utf-8")).decode("ascii")
        # 私有路径清单 base64 编码后传入脚本。
        token = secrets.token_hex(16)
        script = f"""\
set -eu
sudo_password_b64={self._sudo_password_b64!r}
hidden_payload={encoded!r}
quarantine=/root/.rsiagent_verifier_hidden_{token}
run_privileged() {{
  printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- "$@"
}}
trap 'sudo -K >/dev/null 2>&1 || true' EXIT
run_privileged install -d -m 0700 -o root -g root "$quarantine"
index=0
printf %s "$hidden_payload" | base64 -d | while IFS= read -r hidden_path \
    || [ -n "$hidden_path" ]; do
  [ -n "$hidden_path" ] || continue
  index=$((index + 1))
  if [ -e "$hidden_path" ] || [ -L "$hidden_path" ]; then
    run_privileged mv -- "$hidden_path" "$quarantine/$index"
    run_privileged chown -R root:root "$quarantine/$index"
    run_privileged chmod -R go-rwx "$quarantine/$index"
  fi
done
printf RSIAGENT_ROLLBACK_PRIVATE_PATHS_HIDDEN
"""
        # 隔离脚本语义：root 建立随机名 0700 隔离目录；逐条把存在的私有路径 mv 进去
        # 并 chown/chmod 给 root（任何人都不可访问）；成功后打印哨兵。
        trace = self._run_harness_script(
            "bash", script, timeout=max(30, int(timeout)), cap=0)
        if (trace.exit_code != 0 or trace.infra_fail
                or "RSIAGENT_ROLLBACK_PRIVATE_PATHS_HIDDEN" not in trace.stdout):
            return _trace(
                "Verifier rollback mirror could not quarantine Actor-private "
                "paths.\n" + trace.stdout, 125, infra_fail=True)
            # 隔离未验证成功：中止（fail-closed）。
        self._rollback_private_paths_hidden = True
        return None
        # 完成。

    def _check_workspace(self, timeout):
        """Revalidate the cached keeper PID and private scratch mount."""
        # 重验缓存的 keeper PID 与私有 scratch 挂载（空闲后的例行健康检查）。
        if not self._namespace_pid:
            return _trace(
                "Verifier private mount namespace has no keeper identity.", 125,
                infra_fail=True)
            # 无身份记录：无法重验。
        marker = "RSIAGENT_VERIFIER_NAMESPACE_HEALTHY"
        wrapper = f"""\
set -eu
workspace_path={self._workspace!r}
sudo_password_b64={self._sudo_password_b64!r}
run_privileged() {{
  printf %s "$sudo_password_b64" | base64 -d | \
    sudo -S -k -p '' -- "$@"
}}
trap 'sudo -K >/dev/null 2>&1 || true' EXIT
run_privileged kill -0 {int(self._namespace_pid)}
run_privileged nsenter --target {int(self._namespace_pid)} --mount -- \
  mountpoint -q "$workspace_path"
run_privileged nsenter --target {int(self._namespace_pid)} --mount -- \
  test -w "$workspace_path"
printf '{marker}\n'
"""
        # 健康探针脚本：keeper 进程存活 + 命名空间内挂载存在 + scratch 可写，
        # 全部通过才打印健康哨兵。
        result = self._run_harness_script(
            "bash", wrapper, timeout=timeout, cap=0)
        if (result.exit_code != 0 or result.infra_fail
                or marker not in (result.stdout or "")):
            return _trace(
                "Verifier persistent isolation namespace became unavailable; "
                "model-authored code was not executed and scratch persistence "
                "cannot be assumed. This is infrastructure, not candidate "
                "evidence.\n" + (result.stdout or "(no diagnostic output)"),
                125, infra_fail=True)
            # 命名空间失效：模型代码未执行，持久性不可假设——基础设施错误。
        self._last_namespace_check = time.monotonic()
        return None
        # 健康：刷新检查时刻。

    def _run_in_private_mount_namespace(
            self, lang, code, *, timeout=600, as_desktop=True):
        """Run trusted harness code where the private scratch mount exists.

        This helper is never exposed as an Agent action.  Model-authored programs
        still go through :meth:`_run_isolated`, which applies the selected candidate
        protection boundary. Keeping this transport primitive separate makes it
        impossible for the ordinary Actor namespace to resolve scratch contents
        between Verifier turns.
        """
        if not self._namespace_pid:
            return _trace(
                "Verifier private mount namespace is unavailable.", 125,
                infra_fail=True)
            # 命名空间未建立：拒绝。
        interpreter = "python3" if str(lang).startswith("py") else "bash"
        extension = "py" if interpreter == "python3" else "sh"
        token = secrets.token_hex(12)
        # This trusted helper must remain available for postmortem preservation
        # after the guest's ext4 root has remounted read-only. /dev/shm is a
        # separate writable tmpfs in the OSWorld guest; model-authored programs
        # still execute only inside the nested effect-isolation namespace.
        # （既有英文注释：该受信助手在 ext4 根只读后仍须可用于事后保全；/dev/shm 是
        # 独立可写 tmpfs；模型程序仍只在嵌套的效果隔离命名空间内执行。）
        script_path = f"/dev/shm/rsiagent_verifier_host_{token}.{extension}"
        # 脚本落盘到 /dev/shm（只读根时的可靠落点）。
        encoded = base64.b64encode((code or "").encode("utf-8")).decode("ascii")
        identity = ""
        if as_desktop:
            identity = (
                f"setpriv --reuid={int(self._desktop_uid)} "
                f"--regid={int(self._desktop_gid)} --clear-groups "
                "--bounding-set=-all --inh-caps=-all --ambient-caps=-all "
                "--no-new-privs ")
            # 以桌面身份降权运行：清附加组、剥夺全部能力位、禁止再提权。
        wrapper = f"""\
set -eu
script_path={script_path!r}
payload={encoded!r}
sudo_password_b64={self._sudo_password_b64!r}
cleanup() {{ rm -f -- "$script_path"; sudo -K >/dev/null 2>&1 || true; }}
trap cleanup EXIT
printf %s "$payload" | base64 -d > "$script_path"
chmod 0600 "$script_path"
if [ "$(id -u)" = 0 ]; then chown user:user "$script_path"; fi
printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- \
  nsenter --target {int(self._namespace_pid)} --mount -- \
  {identity}{interpreter} "$script_path"
"""
        # 包装语义：base64 解码落盘(0600, user 属主) → sudo+nsenter 进入私有挂载
        # 命名空间 → （可选 setpriv 降权后）运行解释器执行脚本；退出即删脚本并清凭据。
        result = self._run_harness_script(
            "bash", wrapper, timeout=timeout, cap=0)
        if not result.infra_fail and result.exit_code == 0:
            self._last_namespace_check = time.monotonic()
            # 执行成功即证明命名空间仍健康。
        return result

    def _restore_scratch(self, archive):
        """Restore a prior private workspace without trusting archive paths.

        The archive was produced by :meth:`export_scratch`, but its contents were
        ultimately influenced by Verifier-authored programs.  Restore only ordinary
        relative files/directories and reject links, devices, traversal, and absolute
        members before writing anything.
        """
        # （docstring 大意：归档虽由 export_scratch 生成，但内容受 Verifier 程序影响；
        # 只恢复普通相对文件/目录，写盘前拒绝链接/设备/穿越/绝对成员。）
        if not archive:
            return None
            # 空归档无事可做。
        push_file = getattr(self._vm, "push_file", None)
        if not callable(push_file):
            return _trace(
                "Verifier scratch restore is unavailable on this VM transport.",
                125, infra_fail=True)
            # 传输不支持推送：无法恢复。
        local = tempfile.NamedTemporaryFile(
            prefix="rsiagent-verifier-scratch-", suffix=".tar", delete=False)
        # 宿主侧临时 tar 承载归档字节。
        try:
            local.write(archive)
            local.flush()
            os.fsync(local.fileno())
            local.close()
            # 写盘并 fsync 确保完整。
            # Stage the transport bundle on the ordinary guest filesystem, not
            # inside the private tmpfs it is about to populate.  A lossless
            # restore necessarily contains the same bytes as the destination;
            # putting both copies on one tmpfs made large, valid Verifier
            # evidence fail with ENOSPC before extraction could begin.
            # （既有英文注释：传输包先落在普通客户机文件系统而非它要填充的私有
            # tmpfs；同一 tmpfs 放两份相同字节曾让大体积合法证据在解压前 ENOSPC。）
            guest_archive = (
                "/tmp/rsiagent_verifier_restore_" + secrets.token_hex(12) + ".tar")
            ok, reason = push_file(local.name, guest_archive)
            if not ok:
                return _trace(
                    "Verifier scratch restore upload failed: " + str(reason),
                    125, infra_fail=True)
                # 上传失败即中止。
            code = f'''import os, pathlib, tarfile
root = pathlib.Path({self._workspace!r}).resolve()
archive = pathlib.Path({guest_archive!r})
with tarfile.open(archive, "r:") as tf:
    members = tf.getmembers()
    for member in members:
        name = member.name
        target = (root / name).resolve()
        if (name.startswith("/") or target == root.parent
                or root not in target.parents
                or not (member.isdir() or member.isfile())):
            raise RuntimeError("unsafe Verifier scratch archive member")
    tf.extractall(root, members=members, filter="data")
archive.unlink(missing_ok=True)
'''
            # 解包脚本：先把全部成员解析并校验（拒绝绝对路径/逃逸根/非普通文件目录），
            # 再以 data 过滤器整体解包；最后删除归档。
            trace = self._run_in_private_mount_namespace(
                "python", code, timeout=600)
            if trace.exit_code != 0 or trace.infra_fail:
                return _trace(
                    "Verifier scratch restore failed:\n" + trace.stdout,
                    125, infra_fail=True)
                # 解包失败即中止。
            return None
        finally:
            if 'guest_archive' in locals():
                self._vm.run_command(
                    "rm -f -- " + shlex.quote(guest_archive),
                    timeout=120, cap=0)
                # 无论成败都清理客户机侧归档。
            try:
                local.close()
            except Exception:
                pass
            try:
                os.unlink(local.name)
            except FileNotFoundError:
                pass
            # 宿主侧清理尽力而为。

    def _export_regular_tree(self, source_path, *, archive_prefix=""):
        """Return one lossless regular-file tree as tar bytes.

        The tar is staged outside ``source_path``.  This is a transport detail,
        not a content policy: every regular file and directory below the source
        is included, with no byte/count ceiling or semantic filtering.
        """
        # （docstring 大意：tar 暂存在源路径之外；这是传输细节而非内容策略——源下
        # 全部普通文件与目录都包含，无字节/数量上限、无语义过滤。）
        # This privileged transport is restricted to the private tmpfs created
        # by this executor. It must never become a general root file reader.
        # （既有英文注释：该特权传输仅限本执行器创建的私有 tmpfs，绝不得变成
        # 通用 root 文件读取器。）
        if source_path != self._workspace:
            raise AgenticVerifierInfrastructureError(
                "Verifier export source escaped its private workspace")
            # 源只能是私有工作区本身。
        prefix_path = pathlib.PurePosixPath(archive_prefix)
        if prefix_path.is_absolute() or ".." in prefix_path.parts:
            raise AgenticVerifierInfrastructureError(
                "unsafe Verifier export archive prefix")
            # 归档前缀不得是绝对路径或含 .. 穿越。
        archive_token = secrets.token_hex(12)
        failures = []
        # Prefer the ordinary guest filesystem because it may have substantially
        # more free space. If ext4 has failed read-only, retry the same read-only
        # export through the independent /dev/shm tmpfs. This is a transport
        # fallback, not a byte or file-count cap.
        # （既有英文注释：优先普通客户机文件系统（空间大）；ext4 只读失败时改用
        # 独立的 /dev/shm tmpfs 重试同一只读导出——传输回退，非字节/数量上限。）
        for transport_root in ("/tmp", "/dev/shm"):
            archive_path = (
                transport_root + "/rsiagent_verifier_export_" +
                archive_token + ".tar")
            code = f'''import os, pathlib, stat, tarfile
root = {source_path!r}
archive = {archive_path!r}
prefix = {archive_prefix!r}
directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
root_fd = os.open(root, directory_flags)
root_device = os.fstat(root_fd).st_dev

def info(name, metadata, directory=False):
    item = tarfile.TarInfo(name)
    item.mode = stat.S_IMODE(metadata.st_mode)
    item.uid, item.gid = metadata.st_uid, metadata.st_gid
    item.mtime = metadata.st_mtime
    item.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    item.size = 0 if directory else metadata.st_size
    return item

def visit(tf, directory_fd, relative):
    for name in sorted(os.listdir(directory_fd)):
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        is_directory = stat.S_ISDIR(before.st_mode)
        if not (is_directory or stat.S_ISREG(before.st_mode)):
            continue
        flags = directory_flags if is_directory else (
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            actual = os.fstat(fd)
            if ((actual.st_dev, actual.st_ino, stat.S_IFMT(actual.st_mode)) !=
                    (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
                    or actual.st_dev != root_device):
                raise RuntimeError("Verifier scratch changed during export")
            child = "/".join(part for part in (relative, name) if part)
            archive_name = "/".join(part for part in (prefix, child) if part)
            if is_directory:
                tf.addfile(info(archive_name, actual, directory=True))
                visit(tf, fd, child)
            else:
                # Every hard-linked regular file is stored as ordinary bytes.
                # Descriptor-relative, no-follow opens keep concurrent link
                # replacement from redirecting this trusted reader elsewhere.
                with os.fdopen(os.dup(fd), "rb") as content:
                    tf.addfile(info(archive_name, actual), content)
        finally:
            os.close(fd)

try:
    archive_fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW, 0o600)
    with os.fdopen(archive_fd, "wb") as outgoing:
        # Allow the desktop controller to remove even a partially written tar.
        os.fchown(outgoing.fileno(), {int(self._desktop_uid)}, {int(self._desktop_gid)})
        with tarfile.open(fileobj=outgoing, mode="w") as tf:
            if prefix:
                tf.addfile(info(prefix, os.fstat(root_fd), directory=True))
            visit(tf, root_fd, "")
        outgoing.flush()
        os.fsync(outgoing.fileno())
finally:
    os.close(root_fd)
'''
            # 归档脚本语义（root 身份、描述符相对、全程 nofollow）：以 O_NOFOLLOW 打开
            # 源根并记录其设备号；递归遍历（目录/普通文件之外一律跳过）；每个条目先
            # lstat 再 fstat 比对 (dev,ino,类型)，且必须仍在根设备上——并发替换/跨设备
            # 即报“导出期间 scratch 已变化”；硬链接普通文件按普通字节存档；tar 以
            # 0600 建于 O_EXCL，并 chown 给桌面控制器以便失败也能清掉；结束后 fsync。
            # Unzip and similar tools can legitimately leave mode-000 files or
            # non-searchable directories in the Verifier's private scratch.
            # Archive their exact bytes using the confined trusted reader;
            # model-authored Programs retain their existing desktop identity.
            # （既有英文注释：unzip 等工具会合法留下 000 权限文件/不可搜索目录；
            # 用受限受信读取器归档其精确字节；模型程序的桌面身份不变。）
            trace = self._run_in_private_mount_namespace(
                "python", code, timeout=3600, as_desktop=False)
            # 以 root（非桌面）身份在私有命名空间内运行归档脚本。
            if trace.exit_code == 0 and not trace.infra_fail:
                try:
                    archive, reason = self._vm.fetch_file(
                        archive_path, max_bytes=None)
                    if archive is not None:
                        return archive
                        # 成功取回 tar 字节即返回（max_bytes=None 无上限）。
                    failures.append(
                        f"{transport_root}: fetch failed: {reason}")
                    # 取回失败记录原因。
                finally:
                    self._vm.run_command(
                        "rm -f -- " + shlex.quote(archive_path),
                        timeout=120, cap=0)
                    # 无论成败清理客户机侧 tar。
            else:
                failures.append(
                    f"{transport_root}: " +
                    (trace.stdout or "archive helper failed without output"))
                self._vm.run_command(
                    "rm -f -- " + shlex.quote(archive_path),
                    timeout=120, cap=0)
                # 脚本失败也记录输出并清理。
                # A healthy /tmp failure such as a missing source will fail the
                # same way in /dev/shm. Only transport/filesystem failures warrant
                # consuming the emergency tmpfs.
                # （既有英文注释：健康的 /tmp 失败（如源缺失）在 /dev/shm 也会同样
                # 失败；只有传输/文件系统类失败才值得动用应急 tmpfs。）
                diagnostic = (trace.stdout or "").lower()
                if not any(marker in diagnostic for marker in (
                        "read-only file system", "no space left on device",
                        "permission denied", "staging fallback")):
                    break
                    # 非传输类失败：不再尝试 /dev/shm。
        raise AgenticVerifierInfrastructureError(
            "could not archive persistent Verifier data: " +
            "\n".join(failures))
        # 两个传输根都失败：抛基础设施错误并汇总全部失败原因。

    def export_scratch(self):
        """Return a lossless archive of private regular files/directories.

        No candidate path is included, no semantic selection is performed, and no
        file-size ceiling is imposed. Symlinks and special files are intentionally
        omitted so a model-influenced archive cannot escape the next private root.
        """
        # （docstring 大意：无损归档私有普通文件/目录；不含候选路径、无语义筛选、
        # 无大小上限；刻意省略符号链接与特殊文件，防模型影响的归档逃逸到下一个
        # 私有根。）
        if not self._initialized or self._closed:
            return self._scratch_archive
            # 未初始化或已关闭：返回调用方传入的原归档（或 None）。
        ready = self._ensure_workspace(timeout=180)
        if ready is not None:
            raise AgenticVerifierInfrastructureError(ready.stdout)
            # 工作区未就绪：抛基础设施错误。
        return self._export_regular_tree(self._workspace)
        # 导出当前私有工作区树。

    def _fetch_private_file(self, path, max_bytes=None):
        """Bridge one regular scratch file to the trusted guest controller.

        The controller intentionally lives outside the Verifier-only mount
        namespace. A fixed harness helper copies only a resolved regular file
        below the private root to a randomized transport path, which is erased
        immediately after the lossless fetch.
        """
        # （docstring 大意：控制器刻意位于 Verifier 专属命名空间之外；固定的 harness
        # 助手只把私有根下已解析的普通文件复制到随机传输路径，无损取走后立即删除。）
        requested = posixpath.normpath(str(path))
        root = posixpath.normpath(self._workspace)
        if not (requested == root or requested.startswith(root + "/")):
            return None, "private Verifier fetch path escaped its workspace"
            # 路径必须落在私有工作区内。
        token = secrets.token_hex(16)
        staged = f"/tmp/rsiagent_verifier_fetch_{token}.bin"
        # 随机化中转文件路径。
        code = f'''import os, pathlib, shutil, stat
root = pathlib.Path({root!r}).resolve()
source = pathlib.Path({requested!r})
if source.is_symlink():
    raise RuntimeError("Verifier Look source may not be a symbolic link")
resolved = source.resolve(strict=True)
if root not in resolved.parents:
    raise RuntimeError("Verifier Look source escaped private scratch")
if not stat.S_ISREG(resolved.stat().st_mode):
    raise RuntimeError("Verifier Look source is not a regular file")
destination = pathlib.Path({staged!r})
with resolved.open("rb") as incoming, destination.open("xb") as outgoing:
    shutil.copyfileobj(incoming, outgoing, length=1 << 20)
    outgoing.flush()
    os.fsync(outgoing.fileno())
destination.chmod(0o600)
'''
        # 桥接脚本：拒绝符号链接；resolve(strict) 后确认仍在私有根下；必须是普通
        # 文件；以排他方式复制到中转路径并 fsync、chmod 0600。
        trace = self._run_in_private_mount_namespace(
            "python", code, timeout=600)
        if trace.exit_code != 0 or trace.infra_fail:
            return None, "could not bridge private Verifier pixels: " + trace.stdout
            # 桥接失败：返回原因。
        try:
            return self._vm.fetch_file(staged, max_bytes=max_bytes)
            # 从中转路径无损取回字节（受 max_bytes 显式策略约束）。
        finally:
            self._vm.run_command(
                "rm -f -- " + shlex.quote(staged), timeout=120, cap=0)
            # 无论成败立即删除中转文件。

    def _run_isolated(self, interpreter, extension, code, timeout):
        # 模型程序执行分发：回滚镜像走 _run_rollback_mirror；
        # effect_isolated 构建嵌套命名空间包装（下方）。
        from env.qemu_rollback import ROLLBACK_MIRROR

        if self._execution_mode == ROLLBACK_MIRROR:
            return self._run_rollback_mirror(
                interpreter, extension, code, timeout)
            # 回滚镜像模式：完整活动状态可见，事务回滚兜底。
        action_token = secrets.token_hex(12)
        ready_token = "__RSIAGENT_VERIFIER_SANDBOX_READY_" + action_token + "__"
        program_path = f"{self._workspace}/program_{action_token}.{extension}"
        encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
        # 本次动作的随机令牌、就绪哨兵、程序在 scratch 内的落盘路径与 base64 载荷。

        # All interpolated values are harness-generated tokens or base64 data. The
        # model program is decoded to a file and never parsed as wrapper source.
        # （既有英文注释：全部插值都是 harness 生成的 token 或 base64 数据；模型程序
        # 被解码到文件，绝不作为包装脚本源码解析。）
        wrapper = f"""\
set -eu
workspace_path={self._workspace!r}
program_path={program_path!r}
payload={encoded!r}
sudo_password_b64={self._sudo_password_b64!r}
uid_value=$(id -u user)
gid_value=$(id -g user)
cleanup_program() {{
  printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- \
    nsenter --target {int(self._namespace_pid)} --mount -- \
    rm -f -- "$program_path" >/dev/null 2>&1 || true
  sudo -K >/dev/null 2>&1 || true
}}
trap cleanup_program EXIT
printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- \
  nsenter --target {int(self._namespace_pid)} --mount -- \
  unshare \
  --mount --pid --fork --kill-child=KILL --mount-proc \
  --net --ipc --uts \
  bash -ceu '
    printf %s "$8" | base64 -d > "$6"
    chown "$2:$3" "$6"
    chmod 0600 "$6"
    mount --make-rprivate /
    # A private network namespace starts with loopback down. Enable only loopback
    # so the Verifier can exercise a server it starts in this same Program. There
    # is no external interface or route, and Actor localhost lives in a
    # different namespace.
    if ! command -v ip >/dev/null; then
      echo "ip command unavailable"
      exit 125
    fi
    ip link set dev lo up
    # The unified loop treats durable memory as private Actor Agent policy,
    # not candidate evidence. Mask it only inside the private Verifier Program
    # mount namespace; the live Actor filesystem is untouched.
    printf %s "$7" | base64 -d | while IFS= read -r hidden_path; do
      [ -n "$hidden_path" ] || continue
      if [ -L "$hidden_path" ]; then
        echo "Actor-private path is a symbolic link: $hidden_path"
        exit 125
      fi
      if [ -d "$hidden_path" ]; then
        mount -t tmpfs -o mode=000,nosuid,nodev,noexec tmpfs "$hidden_path"
      elif [ -e "$hidden_path" ]; then
        mount --bind /dev/null "$hidden_path"
        mount -o remount,bind,ro "$hidden_path"
      fi
    done
    # Hide filesystem-backed IPC before freezing the remaining mount tree. The
    # OSWorld util-linux predates mount_setattr(2) recursive read-only support,
    # so every visible pre-existing mount is bind-remounted independently below.
    mount -t tmpfs -o mode=1777,nosuid,nodev tmpfs /tmp
    mount -t tmpfs -o mode=0755,nosuid,nodev,noexec tmpfs /run
    mkdir -p "/run/user/$2"
    chown "$2:$3" "/run/user/$2"
    chmod 0700 "/run/user/$2"
    if mountpoint -q /dev/shm; then
      mount -t tmpfs -o mode=1777,nosuid,nodev,noexec tmpfs /dev/shm
    fi
    declare -A seen_mounts=()
    while IFS= read -r target; do
      if [[ ${{seen_mounts[$target]+present}} == present ]]; then
        continue
      fi
      seen_mounts[$target]=1
      case "$target" in
        "$1"|/tmp|/tmp/*|/run|/run/*|/proc|/proc/*|/dev/shm|/dev/shm/*) continue ;;
      esac
      mountpoint -q "$target" || continue
      if ! mount -o remount,bind,ro "$target" 2>/tmp/remount_error; then
        # Stacked autofs/pseudo mounts occasionally reject a bind remount. Detach
        # every layer in this private namespace; the read-only parent then becomes
        # the only reachable filesystem at that path.
        while mountpoint -q "$target"; do
          umount -l "$target" >/dev/null 2>&1 || break
        done
      fi
    done < <(findmnt -Rrn --raw -o TARGET / | tac)
    while IFS= read -r target; do
      case "$target" in
        "$1"|/tmp|/tmp/*|/run|/run/*|/proc|/proc/*|/dev/shm|/dev/shm/*) continue ;;
      esac
      resolved_options=$(findmnt -T "$target" -n -o VFS-OPTIONS 2>/dev/null || true)
      case ",$resolved_options," in
        *,rw,*) echo "writable mount remained visible: $target"; exit 125 ;;
      esac
    done < <(findmnt -Rrn --raw -o TARGET /)
    case ",$(findmnt -T /home/user -n -o OPTIONS)," in
      *,ro,*) ;;
      *) echo "candidate filesystem did not become read-only"; exit 125 ;;
    esac
    case ",$(findmnt -T "$1" -n -o OPTIONS)," in
      *,rw,*) ;;
      *) echo "Verifier scratch did not remain writable"; exit 125 ;;
    esac
    mkdir -p "$1/home"
    set -o pipefail
    {{ chown "$2:$3" /proc/self/fd/1 || exit 125
       printf "%s\\n" "$5"
       exec setpriv \
      --reuid="$2" --regid="$3" --clear-groups \
      --bounding-set=-all --inh-caps=-all --ambient-caps=-all \
      --no-new-privs \
      env -i \
        HOME="$1/home" USER=user LOGNAME=user \
        LANG=C.UTF-8 LC_ALL=C.UTF-8 \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        TMPDIR=/tmp XDG_RUNTIME_DIR="/run/user/$2" VERIFIER_SCRATCH="$1" \
        "$4" "$6"; }} 2>&1 | cat
  ' rsiagent-verifier \
    "$workspace_path" "$uid_value" "$gid_value" \
    {interpreter!r} {ready_token!r} "$program_path" \
    {base64.b64encode(chr(10).join(self._private_paths).encode()).decode()!r} \
    "$payload"
"""
        # effect_isolated 包装语义（bash 内嵌脚本，逐段）：
        # 1) 把 base64 程序解码到 scratch 内(0600, user 属主)；
        # 2) root+nsenter 进入私有挂载命名空间后 unshare 全套隔离（mount/pid/fork/
        #    kill-child/mount-proc/net/ipc/uts）；
        # 3) 内层：只拉起 lo（可自起服务自测，无外部路由）；用 tmpfs/bind-ro 遮罩
        #    Actor 私有路径；/tmp、/run、/dev/shm 换成一次性 tmpfs；对根下其余全部
        #    现存挂载逐个 bind 重挂为只读（失败则惰性卸载该栈），随后校验不存在任何
        #    rw 挂载、/home/user 确为只读、scratch 仍可写，任一不满足即 exit 125；
        # 4) 修复 stdout 属主并打印就绪哨兵，最终 setpriv 全降权 + env -i 最小环境
        #    运行解释器执行程序（VERIFIER_SCRATCH 指向私有 tmpfs），输出合并回传。
        result = self._run_harness_script(
            "bash", wrapper, timeout=timeout, cap=0)
        # 经受信传输执行包装（cap=0 无损）。
        visible_output = (getattr(result, "context_stdout", None)
                          or result.stdout or "")
        if ready_token not in visible_output:
            return VerifierTrace(
                stdout=(
                    "[verifier effect-isolation readiness was not confirmed; "
                    "program execution is unknown; output may be incomplete]\n" +
                    (result.stdout or "(no diagnostic output)")),
                exit_code=125,
                secs=result.secs,
                timed_out=result.timed_out,
                infra_fail=True,
            )
            # 未见到本动作专属的就绪哨兵：无法证明隔离边界成立、程序是否执行未知——
            # 按基础设施错误处理（防把无保护执行当证据）。
        self._last_namespace_check = time.monotonic()
        # 哨兵出现即证明命名空间健康。
        if getattr(result, "context_stdout", None) is not None:
            result.context_stdout = result.context_stdout.replace(
                ready_token + "\n", "", 1)
            result.context_stdout = result.context_stdout.replace(
                ready_token, "", 1)
            # 从上下文输出中剥离控制面哨兵（两种形态）。
        else:
            result.stdout = result.stdout.replace(ready_token + "\n", "", 1)
            # 无外置化时从归档输出剥离。
        return result

    def _run_rollback_mirror(self, interpreter, extension, code, timeout):
        """Run model code with full live-state visibility inside QEMU rollback."""
        # 在 QEMU 回滚事务内以完整活动状态可见性运行模型代码（回滚镜像模式）。
        action_token = secrets.token_hex(12)
        ready_token = "__RSIAGENT_VERIFIER_MIRROR_READY_" + action_token + "__"
        program_path = f"{self._workspace}/program_{action_token}.{extension}"
        encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
        # 同 effect_isolated：随机令牌、专属就绪哨兵、程序路径与 base64 载荷。
        wrapper = f"""\
set -eu
workspace_path={self._workspace!r}
program_path={program_path!r}
payload={encoded!r}
sudo_password_b64={self._sudo_password_b64!r}
uid_value=$(id -u user)
gid_value=$(id -g user)
cleanup_program() {{
  printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- \\
    nsenter --target {int(self._namespace_pid)} --mount -- \\
    rm -f -- "$program_path" >/dev/null 2>&1 || true
  sudo -K >/dev/null 2>&1 || true
}}
trap cleanup_program EXIT
printf %s "$sudo_password_b64" | base64 -d | sudo -S -k -p '' -- \\
  nsenter --target {int(self._namespace_pid)} --mount -- \\
  bash -ceu '
    printf %s "$8" | base64 -d > "$6"
    chown "$2:$3" "$6"
    chmod 0600 "$6"
    set -o pipefail
    {{ chown "$2:$3" /proc/self/fd/1 || exit 125
       printf "%s\\n" "$5"
       exec setpriv \\
      --reuid="$2" --regid="$3" --clear-groups \\
      --bounding-set=-all --inh-caps=-all --ambient-caps=-all \\
      --no-new-privs \\
      env -i \\
        HOME=/home/user USER=user LOGNAME=user \\
        LANG=C.UTF-8 LC_ALL=C.UTF-8 \\
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \\
        DISPLAY=:0 XAUTHORITY=/home/user/.Xauthority \\
        XDG_RUNTIME_DIR="/run/user/$2" \\
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$2/bus" \\
        TMPDIR=/tmp VERIFIER_SCRATCH="$1" \\
        "$4" "$6"; }} 2>&1 | cat
  ' rsiagent-verifier-mirror \\
    "$workspace_path" "$uid_value" "$gid_value" \\
    {interpreter!r} {ready_token!r} "$program_path" ignored "$payload"
"""
        # 镜像包装语义：解码程序到 scratch(0600) → root+nsenter 进入私有挂载命名
        # 空间（不做任何只读化——镜像内允许完整观察与测试）→ 修复 stdout 属主并打印
        # 就绪哨兵 → setpriv 全降权 + env -i 最小环境（含 DISPLAY/XAUTHORITY/
        # DBUS 会话总线，可观察 GUI）运行解释器；安全性由事务回滚兜底。
        result = self._run_harness_script(
            "bash", wrapper, timeout=timeout, cap=0)
        visible_output = (getattr(result, "context_stdout", None)
                          or result.stdout or "")
        if ready_token not in visible_output:
            return VerifierTrace(
                stdout=(
                    "[verifier rollback-mirror readiness was not confirmed; "
                    "program execution is unknown; output may be incomplete]\n" +
                    (result.stdout or "(no diagnostic output)")),
                exit_code=125,
                secs=result.secs,
                timed_out=result.timed_out,
                infra_fail=True,
            )
            # 哨兵缺失：镜像执行未知，按基础设施错误处理。
        self._last_namespace_check = time.monotonic()
        if getattr(result, "context_stdout", None) is not None:
            result.context_stdout = result.context_stdout.replace(
                ready_token + "\n", "", 1).replace(ready_token, "", 1)
            # 从上下文输出剥离哨兵。
        else:
            result.stdout = result.stdout.replace(
                ready_token + "\n", "", 1).replace(ready_token, "", 1)
            # 从归档输出剥离哨兵。
        return result

    def fetch_look_image(self, path):
        """Fetch pixels while routing any document renderer through the sandbox.

        Direct image reads and live-screen capture are trusted harness operations.
        Office/PDF rendering normally starts guest processes and writes a profile;
        a small VM facade sends that command through this executor and rewrites the
        renderer's temporary directory into the private persistent tmpfs.
        """
        # （docstring 大意：取像素时把文档渲染也路由进沙箱——直接读图与实时截屏是
        # 受信 harness 操作；Office/PDF 渲染通常会在客户机起进程并写 profile，
        # 用一个小 VM 门面把该命令送进本执行器，并把渲染器临时目录改写到私有
        # 持久 tmpfs。）
        from core.imagery import fetch_look_image

        ready = self._ensure_workspace(timeout=180)
        if ready is not None:
            raise AgenticVerifierInfrastructureError(ready.stdout)
            # 先确保隔离工作区就绪。

        if str(path).lower().startswith("screen:"):
            # Screen capture must reach the real display, so it is a fixed trusted
            # harness action rather than model code. Candidate pixels are not
            # private Verifier state, so use a randomized transport directory in
            # the desktop namespace and erase it immediately.
            # （既有英文注释：截屏必须触达真实显示器，属固定受信 harness 动作而非
            # 模型代码；候选像素不是 Verifier 私有状态，用桌面命名空间内随机传输
            # 目录并立即删除。）
            trusted_vm = self._vm
            capture_dir = (
                "/tmp/rsiagent_verifier_screen_" + secrets.token_hex(16))
            # 随机化截屏传输目录。

            class _VerifierScreenVM:
                # VM 门面：把渲染器默认的 /tmp/rsiagent_render 重定向到随机目录。
                def run_command(self, command, timeout=30, cap=4000):
                    isolated = command.replace("/tmp/rsiagent_render", capture_dir)
                    return trusted_vm.run_command(
                        isolated, timeout=timeout, cap=cap)
                    # 普通命令通道执行（截屏无需隔离）。

                def fetch_file(self, target, max_bytes=None):
                    return trusted_vm.fetch_file(target, max_bytes=max_bytes)
                    # 取文件直通。

            try:
                return fetch_look_image(_VerifierScreenVM(), path)
                # 执行受信截屏。
            finally:
                self._vm.run_command(
                    "rm -rf -- " + shlex.quote(capture_dir),
                    timeout=120, cap=0)
                # 无论成败立即删除截屏目录。

        normalized_look = posixpath.normpath(str(path))
        private_root = posixpath.normpath(self._workspace)
        look_is_private_scratch = (
            normalized_look == private_root
            or normalized_look.startswith(private_root + "/"))
        # 判定 Look 目标是否已在私有 scratch 内。
        if self._private_paths and not look_is_private_scratch:
            quoted = shlex.quote(str(path))
            resolved = self._vm.run_command(
                "resolved=$(readlink -f -- " + quoted
                + ") || exit 2; printf 'RSIAGENT_RESOLVED=%s' \"$resolved\"",
                timeout=30, cap=8192) or ""
            # 非私有目标：在客户机内 readlink -f 解析真实路径（防符号链接绕过）。
            marker = "RSIAGENT_RESOLVED="
            actual = resolved.split(marker, 1)[1].strip() \
                if marker in resolved else ""
            if not actual:
                return None, "Verifier Look path could not be resolved safely."
                # 解析失败：拒绝 Look。
            if _is_private_path(actual, self._private_paths):
                return None, "That path is private to the Actor Agent."
                # 解析后落在 Actor 私有路径上：拒绝。

        isolated_executor = self
        render_dir = self._workspace + "/look_render"
        # 文件 Look：渲染命令经本执行器的隔离边界执行，临时目录改到私有 scratch。

        class _VerifierLookVM:
            # VM 门面：把渲染命令送进隔离执行，取文件按归属分流。
            def run_command(self, command, timeout=30, cap=4000):
                isolated = command.replace("/tmp/rsiagent_render", render_dir)
                trace = isolated_executor(
                    "bash", isolated, timeout=timeout, cap=0)
                return trace.stdout
                # 渲染命令在效果隔离/回滚镜像边界内执行（cap=0 无损）。

            def fetch_file(self, target, max_bytes=None):
                normalized = posixpath.normpath(str(target))
                private_root = posixpath.normpath(
                    isolated_executor._workspace)
                if (normalized == private_root
                        or normalized.startswith(private_root + "/")):
                    return isolated_executor._fetch_private_file(
                        normalized, max_bytes=max_bytes)
                    # 私有 scratch 内的产物走受控桥接取回。
                return isolated_executor._vm.fetch_file(
                    normalized, max_bytes=max_bytes)
                # 其他目标直通普通传输。

        return fetch_look_image(_VerifierLookVM(), path)
        # 执行文件 Look。

    def close(self):
        """Destroy private state and, in mirror mode, restore the scored VM."""
        # 关闭执行器：销毁私有状态；镜像模式下恢复（回滚）计分 VM。
        if self._closed:
            return
            # 幂等。
        self._closed = True
        script = f"""\
set -u
workspace_path={self._workspace!r}
namespace_pid={str(self._namespace_pid or '')!r}
sudo_password_b64={self._sudo_password_b64!r}
run_privileged() {{
  printf %s "$sudo_password_b64" | base64 -d | \
    sudo -S -k -p '' -- "$@"
}}
if [ -n "$namespace_pid" ]; then
  run_privileged kill -TERM "$namespace_pid" 2>/dev/null || true
  for _attempt in $(seq 1 50); do
    run_privileged kill -0 "$namespace_pid" 2>/dev/null || break
    sleep 0.1
  done
  run_privileged kill -KILL "$namespace_pid" 2>/dev/null || true
fi
run_privileged rmdir "$workspace_path" 2>/dev/null || true
sudo -K >/dev/null 2>&1 || true
"""
        # 清理脚本：先 TERM 后 KILL keeper 进程（命名空间随之消失），再删挂载点
        # 目录，最后清空 sudo 凭据缓存。
        try:
            if self._workspace_attempted:
                self._run_harness_script("bash", script, timeout=120, cap=0)
                # 曾尝试建工作区才需要清理。
        except Exception:
            # The QEMU rollback below is the authoritative cleanup in mirror mode;
            # in the historical namespace mode the randomized mount disappears with
            # the VM.  Cleanup transport errors must not replace a completed report.
            # （既有英文注释：镜像模式下方的 QEMU 回滚才是权威清理；命名空间模式下
            # 随机挂载随 VM 消失；清理传输错误不得顶替已完成的报告。）
            pass
        finally:
            if self._rollback_transaction is not None:
                try:
                    self._rollback_transaction.rollback()
                    # 镜像模式：强制回滚到检查点（恢复计分状态）。
                except Exception as exc:  # noqa: BLE001
                    raise AgenticVerifierInfrastructureError(
                        "Verifier rollback failed; the candidate is not safe to "
                        f"grade. {type(exc).__name__}: {exc}") from exc
                    # 回滚失败：候选不安全评分——必须抛基础设施错误阻止评分。
