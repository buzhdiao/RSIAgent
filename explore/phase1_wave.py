# 【中文注释·模块定位】Phase 1（BRS：Broad Recursive Self-exploration，广度递归自我
# 探索）的"智能体自著并行波次"实现。核心流程：Curriculum Agent 决定每波（wave）的
# 宽度（项目数）与项目内容 → 每个分支从同一份不可变的"波前记忆快照"出发，在独立
# 全新 VM 上并行执行 Actor→Verifier 循环 → 只有当所有分支都拿到 Verifier 终态判定
# 后，才按自著顺序逐个"串行"恢复同一批 Actor 上下文，把各自经验整合进唯一扁平规范
# 记忆库（canonical memory bank）。这个串行提交边界就是论文中的 wave memory barrier
# （波次记忆屏障）：既防止兄弟分支互相观察，又避免丢失更新竞争，还不强制记忆模式。
"""Agent-authored parallel waves for Phase-1 environment exploration.

The Curriculum Agent chooses each wave's width and projects.  Every branch reads
the same immutable pre-wave memory snapshot and runs in an isolated fresh VM.
Only after every branch has a terminal Verifier verdict are the *same* branch
Actor contexts resumed, one at a time, to reconcile their experience into the
single flat canonical memory bank.  The serialized commit boundary prevents
sibling observation and lost-update races without imposing a memory schema.
"""

# from __future__ import annotations：延迟注解求值，允许 tuple[...] 等新式注解写法。
from __future__ import annotations

# ThreadPoolExecutor/as_completed：并行执行波内分支（每个分支独立 VM 与线程）。
from concurrent.futures import ThreadPoolExecutor, as_completed
# copy：深拷贝角色配置（actor_cfg 等），确保并行分支互不共享可变配置。
import copy
# dataclasses：asdict() 把结果对象转 JSON 可序列化字典；replace() 派生改字段配置。
import dataclasses
from dataclasses import dataclass
# hashlib：对 target_direction 做 SHA-256 摘要，写入状态文件供审计比对。
import hashlib
# json：读写状态、决策、审计 JSON。
import json
# Path：谱系（lineage）目录树操作。
from pathlib import Path
# re：项目 ID 白名单校验。
import re
# shlex.quote：把路径安全地嵌入 shell 命令（防注入，含空格/特殊字符）。
import shlex
# shutil.move：基础设施重试时归档未完成尝试的产物。
import shutil
# Any/Callable：类型注解。
from typing import Any, Callable

# PLAIN_JSON_TRANSPORT_NOTE：向模型声明"动作通道是正文纯 JSON"的澄清文本。
from core.actor import PLAIN_JSON_TRANSPORT_NOTE
# ArtifactSink：Verifier 的产物落盘器（trace、findings 无损归档）。
from core.trace import ArtifactSink
# Curriculum 端的三份提示词：波次章程、Actor 自进化章程、记忆蒸馏/整合消息。
from explore.charter import (
    phase1_wave_curriculum_charter,
    self_evolving_actor_charter,
    self_evolving_actor_memory_distillation_msg,
    self_evolving_actor_memory_reconciliation_msg,
)
# practice_loop 是 Phase 2（DRS）的主循环模块，Phase 1 复用其全部底层设施。
# 【中文注释】导入清单分组解读：ACTOR_EXECUTION_EVIDENCE/ACTOR_HANDOFF/PROJECT_ROOT
# 是客户机内路径哨兵（执行证据/交接文件/项目工作目录）；PracticeBoundaryError 是
# 边界违规异常（如越权访问目标查询），PracticeInfrastructureError 是基础设施错误
# （不计分、可重试），PracticeHooks 是宿主钩子集（审计/记忆/捕获等），PracticeResult
# 是阶段结果对象；_ACTOR_TOKEN 是 Actor 交接令牌正则，_actor_runtime_contract 是
# Actor 运行时契约文本；_atomic_json/_atomic_text 是原子写盘（临时文件+rename，
# 防半写）；_audit_agent_artifacts/_audit_prompt 做越界/泄漏审计；
# _build_actor_execution_evidence 构建执行证据包；_capture_owned_tree 捕获"自有
# 目录树"回宿主；_continuation_after_transport 生成传输失败后的同上下文续跑提示；
# _fresh_vm 把 VM 复位到干净边界；_listing/_manifest 渲染记忆清单与文件清单；
# _outcome_text 把结果记录渲染为 markdown；_phase_cfg 取阶段配置（含上限）；
# _pull_terminal_memory/_push_canonical_memory/_push_actor_execution_evidence
# 负责记忆与证据在宿主与客户机间的搬运；_read_memory_tree 读记忆树为
# {相对路径: bytes}；_remove_guest_file 删除客户机文件；_replay 把候选/夹具回灌
# 进 VM；_run_free_phase/_run_handoff_phase 驱动自由回合/交接回合；
# _scope_prior_curriculum_visuals 清理 Curriculum 历史中的旧视觉内容；
# _verify_actor_execution_evidence/_verify_candidate 校验证据与候选可重放性。
from explore.practice_loop import (
    ACTOR_EXECUTION_EVIDENCE,
    ACTOR_HANDOFF,
    PROJECT_ROOT,
    PracticeBoundaryError,
    PracticeHooks,
    PracticeInfrastructureError,
    PracticeResult,
    _ACTOR_TOKEN,
    _actor_runtime_contract,
    _atomic_json,
    _atomic_text,
    _audit_agent_artifacts,
    _audit_prompt,
    _build_actor_execution_evidence,
    _capture_owned_tree,
    _continuation_after_transport,
    _fresh_vm,
    _listing,
    _manifest,
    _outcome_text,
    _phase_cfg,
    _pull_terminal_memory,
    _push_actor_execution_evidence,
    _push_canonical_memory,
    _read_memory_tree,
    _remove_guest_file,
    _replay,
    _run_free_phase,
    _run_handoff_phase,
    _scope_prior_curriculum_visuals,
    _verify_actor_execution_evidence,
    _verify_candidate,
)


# 客户机内路径：Curriculum 起草波次项目的草稿根目录。
WAVE_ROOT = "/home/user/evolution_wave"
# 客户机内路径：Curriculum 发布的波次交接信封（JSON）。
WAVE_HANDOFF = "/home/user/phase1_wave.json"
# 客户机内路径：往波 Curricular 证据（上一波结果）在客户机的存放点。
CURRICULUM_EVIDENCE = "/home/user/.phase1_wave_outcomes"
# 项目 ID 白名单：小写字母/数字开头，随后可为小写字母数字下划线连字符，最长 64。
# fullmatch 语义（\Z 结尾锚点）杜绝 "x/../y" 之类的路径注入。
_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
# Mechanical liveness only: an exhausted retry allowance is infrastructure,
# never Curriculum saturation or a project verdict. Productive segments reset it.
# 【中文注释】连续"无动作段"上限只作机械活性检测：连续多段既无 Program 也无 Look
# 说明传输/解码出了问题（属基础设施故障），绝不是 Curriculum 的饱和（SATURATED）
# 判定或项目判定。有产出的段会把计数清零。
_MAX_ACTIONLESS_WAVE_SEGMENTS = 3


# frozen=True：不可变数据类——项目一经 Curriculum 定稿不可再改（可哈希、可审计）。
@dataclass(frozen=True)
class WaveProject:
    # 单个波次项目：机器可读 id + 自然语言指令。
    project_id: str
    instruction: str


# 波次决策：Curriculum 对本波的裁决——继续 WAVE / 饱和 SATURATED / 停滞 STALLED。
@dataclass(frozen=True)
class WaveDecision:
    decision: str
    rationale: str
    projects: tuple[WaveProject, ...]
    raw: str


# 一个分支的可恢复运行时：并行执行后仍保留 Actor 历史，供串行提交阶段复用。
@dataclass
class _BranchRuntime:
    project_index: int
    project: WaveProject
    episode_dir: Path
    desktop: Any
    vm: Any
    actor_history: list[dict[str, Any]]
    actor_handoff: str
    verifier_report: str
    terminal_outcome: str
    wave_memory: dict[str, bytes]


def parse_wave_handoff(text: str) -> WaveDecision | None:
    """Parse the Curriculum's lossless transport envelope, not its strategy."""

    # 【中文注释】解析 Curriculum 的"无损传输信封"——只验证传输格式合法性，不解释
    # 策略内容。整体 try/except 把非字符串与坏 JSON 都归为解析失败。
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    # 信封必须是 JSON 对象。
    if not isinstance(value, dict):
        return None
    decision = value.get("decision")
    rationale = value.get("rationale")
    projects = value.get("projects")
    # 决策必须是三种合法值之一；rationale 必须非空字符串；projects 必须是列表。
    if (decision not in {"WAVE", "SATURATED", "STALLED"}
            or not isinstance(rationale, str) or not rationale.strip()
            or not isinstance(projects, list)):
        return None
    # 非 WAVE 决策（SATURATED/STALLED）不允许携带项目——携带即非法。
    if decision != "WAVE":
        if projects:
            return None
        return WaveDecision(decision, rationale.strip(), (), text)
    # WAVE 决策必须至少声明一个项目。
    if not projects:
        return None
    parsed: list[WaveProject] = []
    seen: set[str] = set()
    # 逐个校验项目记录：id 必须过白名单正则且不重复；instruction 必须非空且显式
    # 包含 PROJECT_ROOT 路径哨兵（保证项目落在约定工作目录内，便于夹具捕获）。
    for record in projects:
        if not isinstance(record, dict):
            return None
        project_id = record.get("id")
        instruction = record.get("instruction")
        if (not isinstance(project_id, str)
                or not _PROJECT_ID.fullmatch(project_id)
                or project_id in seen
                or not isinstance(instruction, str)
                or not instruction.strip()
                or PROJECT_ROOT not in instruction):
            return None
        seen.add(project_id)
        parsed.append(WaveProject(project_id, instruction.strip()))
    # 返回决策对象；raw 保留原始文本供审计落盘。
    return WaveDecision(decision, rationale.strip(), tuple(parsed), text)


def _remove_wave_publication(vm) -> None:
    # 清除客户机上的波次发布面：草稿根目录与交接信封文件。
    # shlex.quote 保证路径被安全引用；末尾用 test ! -e 验证两者确实不存在。
    quoted_root = shlex.quote(WAVE_ROOT)
    quoted_handoff = shlex.quote(WAVE_HANDOFF)
    out = vm.run_command(
        f"rm -rf -- {quoted_root}; rm -f -- {quoted_handoff}; "
        f"test ! -e {quoted_root} && test ! -e {quoted_handoff}; "
        "echo PHASE1_WAVE_CLEAR_RC=$?", timeout=120) or ""
    # 清除失败（验证不通过）属基础设施错误，向上抛出，不静默继续。
    if "PHASE1_WAVE_CLEAR_RC=0" not in out:
        raise PracticeInfrastructureError("could not clear the Curriculum wave surface")


def _validate_wave_fixtures(vm, decision: WaveDecision) -> bool:
    # 校验信封声明的每个项目夹具树在客户机上真实存在且非空：
    # test -d 检查目录存在，find -mindepth 1 -print -quit 非空即输出一行。
    for project in decision.projects:
        source = f"{WAVE_ROOT}/{project.project_id}/evolution_project"
        quoted = shlex.quote(source)
        out = vm.run_command(
            f"test -d {quoted} && "
            f"test -n \"$(find {quoted} -mindepth 1 -print -quit)\"; "
            "echo PHASE1_WAVE_FIXTURE_RC=$?", timeout=60) or ""
        if "PHASE1_WAVE_FIXTURE_RC=0" not in out:
            return False
    return True


def _curriculum_continuation(detail: str) -> str:
    # 同上下文续跑提示：上一段没有发布可重放的波次。措辞刻意限定为"通用传输/工作区
    # 观察"——不是评分、不是搜索指令、更不是收敛信号（这些只能由 Curriculum 自己
    # 基于证据决定）。草稿夹具与私有文件保留在同一台机器上，供其在同一上下文中
    # 继续完善；重新发布后只有信封里声明的项目会被重放。
    return f"""CURRICULUM AGENT — SAME-CONTEXT WAVE CONTINUATION.

The prior segment did not commit a replayable wave: {detail}. This is a generic
transport/workspace observation, not a grade, search instruction, or convergence
signal. The incomplete handoff was cleared, but your draft fixtures at {WAVE_ROOT}
and other private working files remain on the same machine. Inspect and continue
that work in your same context. Publish a fresh complete handoff when ready; only
its declared projects will be replayed. You remain free to choose WAVE, SATURATED,
or STALLED based on your evidence.

{PLAIN_JSON_TRANSPORT_NOTE}"""


def _author_wave(
        *, hooks: PracticeHooks, vm, cfg, target_direction: str,
        previous_wave_outcomes: str, history: list[dict[str, Any]],
        sink_root: Path, target_query_conditioned: bool) \
        -> tuple[WaveDecision, list[dict[str, Any]]]:
    """Keep draft work across retries without resetting transport ceilings."""

    # 【中文注释】驱动 Curriculum 写作波次：跨重试保留草稿工作（同一 VM、同一对话
    # 上下文），且不清空已消耗的传输预算（累计结算），防止无限重试绕过上限。
    # 复制历史（避免原地改调用方列表）。
    current_history = list(history)
    # 波次章程提示词：包含目标方向、上一波结果、交接路径与草稿根路径。
    prompt = phase1_wave_curriculum_charter(
        target_direction,
        previous_wave_outcomes=previous_wave_outcomes,
        handoff_path=WAVE_HANDOFF,
        wave_root=WAVE_ROOT,
        target_query_conditioned=target_query_conditioned)
    # 是否"续跑"：有历史即在同一对话中继续（不重置上下文）。
    continuing = bool(current_history)
    # 段计数：每次 run_attempt 为一段。
    segment = 0
    # 取阶段配置（迭代/墙钟上限）。
    phase_cfg = _phase_cfg(cfg, WAVE_HANDOFF)
    # This role uses content JSON, including when a provider advertises no native
    # tools. Pin the distinction above the accumulated same-context narration.
    # 【中文注释】该角色使用"正文 JSON"动作通道，即使供应商宣称有原生工具也一样。
    # 把这一澄清钉在累积叙述之前，防止模型误用原生工具信封。
    phase_cfg.system_extra += "\n\n" + PLAIN_JSON_TRANSPORT_NOTE
    # 累计消耗的迭代数 / 墙钟秒数 / 连续无动作段计数（跨段累计结算）。
    used_iters = 0
    used_wall = 0.0
    actionless_segments = 0
    # 作者状态报告：schema_version 便于未来演进；各计数与上限一并记录供审计。
    report: dict[str, Any] = {
        "schema_version": 1, "status": "authoring", "segments": 0,
        "iters": 0, "wall_secs": 0.0, "actionless_segments": 0,
        "limits": {"iters": phase_cfg.max_iters,
                   "wall_secs": phase_cfg.wall_clock_secs,
                   "actionless_segments": _MAX_ACTIONLESS_WAVE_SEGMENTS},
    }

    def blocked(reason: str) -> None:
        # 基础设施阻塞处理：归档未发布输入仅供检查。
        # Archive unpublished inputs for inspection only. These bytes are never
        # promoted to project fixtures or Actor memory by a failed authoring turn.
        # 【中文注释】这些字节永远不会因失败的写作回合被提升为项目夹具或 Actor 记忆。
        report.update(status="infra", reason=reason)
        _atomic_json(sink_root / "authoring_state.json", report)
        try:
            # 捕获草稿树（只读归档，不进入学习环）。
            draft = hooks.capture_project(
                vm, "unpublished-curriculum-wave",
                str(sink_root / "blocked_draft"),
                guest_dirs=[Path(WAVE_ROOT).name], attempts=1)
            report["draft_capture"] = draft
        except Exception as exc:  # preserve the primary failure if capture fails
            # 捕获失败不掩盖主故障：只记录，随后仍抛原 reason。
            report["draft_capture"] = {"ok": False, "error": str(exc)}
        _atomic_json(sink_root / "authoring_state.json", report)
        raise PracticeInfrastructureError(reason)

    # A fresh wave starts empty. Retries within that wave clear only the stale
    # terminal envelope, so multi-segment construction does not erase itself.
    # 【中文注释】新波从空开始；波内重试只清除"过期的终态信封"，多段构建的草稿
    # （WAVE_ROOT 下）不会被误删——这是"跨段保留草稿"的关键。
    _remove_wave_publication(vm)
    while True:
        # 剩余预算 = 上限 - 已用；任一耗尽即基础设施阻塞（不是饱和判定）。
        remaining_iters = phase_cfg.max_iters - used_iters
        remaining_wall = phase_cfg.wall_clock_secs - used_wall
        if remaining_iters <= 0 or remaining_wall <= 0:
            blocked("Curriculum wave exhausted its cumulative transport ceiling")
        # 审计提示词（目标方向越界检查）。
        _audit_prompt(hooks, prompt, target_direction)
        # 每段独立的产物目录（segment_000、segment_001…冲突则递增）。
        sink_dir = sink_root / f"segment_{segment:03d}"
        while sink_dir.exists():
            segment += 1
            sink_dir = sink_root / f"segment_{segment:03d}"

        def publication_ready() -> bool:
            # 终态就绪判据：交接信封可解析，且（非 WAVE，或 WAVE 夹具校验通过）。
            # run_attempt 会轮询此判据决定"模型是否可被终止"。
            decision = parse_wave_handoff(
                hooks.read_guest_text(vm, WAVE_HANDOFF))
            return decision is not None and (
                decision.decision != "WAVE"
                or _validate_wave_fixtures(vm, decision))

        # 每段独立 ArtifactSink；带余量的预算与既有历史传入 run_attempt。
        sink = hooks.sink_factory(str(sink_dir))
        result, current_history = hooks.run_attempt(
            prompt, vm, phase_cfg, sink,
            iters_budget=remaining_iters, wall_budget=remaining_wall,
            initial_history=current_history, continue_context=continuing,
            terminal_handoff_ready=publication_ready)
        # 结果转字典归档（dataclasses.asdict 递归转换 dataclass）。
        sink.save_result(dataclasses.asdict(result))
        # 累计结算预算；wall_secs 已排除供应商传输暂停。
        used_iters += result.iters
        # LoopResult.wall_secs already excludes provider transport pauses.
        # 【中文注释】（注）LoopResult.wall_secs 已剔除供应商传输暂停时间。
        used_wall += result.wall_secs
        # 活性计数：本段有任何 Program 或 Look 就清零，否则加一。
        actionless_segments = (
            0 if result.programs_run or result.looks else actionless_segments + 1)
        # 更新并原子落盘作者状态。
        report.update(
            segments=report["segments"] + 1, last_segment=sink_dir.name,
            last_segment_status=result.status, iters=used_iters,
            wall_secs=used_wall, actionless_segments=actionless_segments)
        _atomic_json(sink_root / "authoring_state.json", report)
        try:
            # 审计本段智能体产物；越界（边界违规）则隔离并抛错。
            _audit_agent_artifacts(hooks, str(sink_dir), target_direction)
        except PracticeBoundaryError as exc:
            report.update(status="quarantined", reason=str(exc))
            try:
                _atomic_json(sink_root / "authoring_state.json", report)
            except OSError:
                pass  # The original quarantine remains authoritative.
                # 【中文注释】（注）状态回写失败可容忍：最初的隔离结论仍然权威。
            raise
        # 读回交接信封文本并做目标边界审计（防 Curriculum 越权引用目标查询）。
        status = str(getattr(result, "status", "infra"))
        text = hooks.read_guest_text(vm, WAVE_HANDOFF)
        if text and hooks.audit_text(
                text, mode="practice",
                authorized_instruction=target_direction):
            raise PracticeBoundaryError(
                "Curriculum wave handoff failed the target boundary")
        # 解析信封并按三种情形归类失败细节。
        decision = parse_wave_handoff(text)
        if decision is not None and status == "done":
            # 信封合法且模型回合完成：非 WAVE 直接发布；WAVE 需夹具校验通过。
            if decision.decision != "WAVE" or _validate_wave_fixtures(
                    vm, decision):
                report.update(status="published", decision=decision.decision)
                _atomic_json(sink_root / "authoring_state.json", report)
                # 返回决策与（延续的）Curriculum 历史。
                return decision, current_history
            detail = "one or more declared fixture trees were absent or empty"
        elif decision is None:
            detail = "the JSON handoff was missing, malformed, or incomplete"
        else:
            detail = "the publication preceded the Agent's completed segment"

        # 终态必须可重试（done/stalled/...）才允许续段；infra 类状态直接阻塞。
        if status not in {"done", "stalled", "stalled_quiescent", "budget"}:
            blocked(
                "Curriculum wave phase ended at infrastructure status " + status)
        # 机械活性：连续无动作段达到上限 → 基础设施问题，非收敛决策。
        if actionless_segments >= _MAX_ACTIONLESS_WAVE_SEGMENTS:
            blocked(
                "Curriculum wave produced no Program or Look actions in "
                f"{actionless_segments} consecutive segments; retrying requires "
                "runtime review, not a convergence decision")
        # 只删除过期信封，保留草稿；用"续跑提示"进入下一段。
        _remove_guest_file(vm, WAVE_HANDOFF)
        prompt = _curriculum_continuation(detail)
        continuing = True
        segment += 1


def _capture_wave_fixtures(
        *, hooks: PracticeHooks, vm, decision: WaveDecision,
        assignments: tuple[tuple[int, WaveProject, Path], ...],
        target_direction: str) -> None:
    # 把 Curriculum 声明的每个项目夹具树"舞台化"（stage）到约定工作目录，
    # 再捕获回宿主形成权威副本（供分支 VM 重放）。
    for project_index, project, episode_dir in assignments:
        source = f"{WAVE_ROOT}/{project.project_id}/evolution_project"
        # 宿主拥有的拷贝是幂等的（先删后拷）；不重放任何模型动作。
        command = (
            f"rm -rf -- {shlex.quote(PROJECT_ROOT)}; "
            f"cp -a -- {shlex.quote(source)} {shlex.quote(PROJECT_ROOT)}; "
            "echo PHASE1_WAVE_STAGE_RC=$?")
        out = vm.run_command(command, timeout=120) or ""
        if "PHASE1_WAVE_STAGE_RC=0" not in out:
            # 首次失败：落盘失败输出供审计，然后原样重试一次。
            _atomic_text(episode_dir / "fixture_stage_failure.txt", out)
            # Host-owned copy is idempotent; no model action is replayed.
            # 【中文注释】（注）宿主自有拷贝幂等；不会重放任何模型动作。
            out = vm.run_command(command, timeout=120) or ""
        if "PHASE1_WAVE_STAGE_RC=0" not in out:
            # 两次都失败 → 基础设施错误（尾部 1000 字符进异常消息）。
            raise PracticeInfrastructureError(
                f"could not stage Curriculum fixture {project.project_id}: {out[-1000:]}")
        # 捕获夹具树回宿主 episodes 目录（"自有目录树"按目标边界审计）。
        fixture_dir = episode_dir / "fixtures"
        _capture_owned_tree(
            hooks, vm, f"phase1-project-{project_index:03d}",
            str(fixture_dir), target_direction)
        # 项目指令落盘为 project.md（夹具的权威说明）。
        _atomic_text(episode_dir / "project.md", project.instruction)


def _branch_control_config(base_cfg):
    # 分支级 Verifier 控制配置：在基础配置上叠加"Phase 1 分支"专用开关。
    # 私有路径集合去重合并（dict.fromkeys 保序去重），加入 Actor 交接与执行证据——
    # Verifier 不可读取 Actor 私有推理（Verifier 隔离原则）。
    private_paths = tuple(dict.fromkeys(
        tuple(getattr(base_cfg, "verifier_private_paths", ()) or ())
        + (ACTOR_HANDOFF, ACTOR_EXECUTION_EVIDENCE)))
    # dataclasses.replace：以 base_cfg 为原型派生新配置（不可变更新的标准做法）。
    # 【中文注释】开关逐项解读：evolve_route=False 分支内不让 Verifier 走进化路线；
    # local_verdict_only=False 允许完整 agentic 判定；unverified_evidence=False
    # 不接受未验证证据；failure_starts_evolution=False 分支失败不触发进化；
    # hide_actor_memory=True 隐藏 Actor 记忆（Verifier 隔离核心）；
    # stage_lifecycle=False 不做多阶段生命周期；persist_scratch=False 不持久化
    # Verifier 草稿；private_paths 合并后的私有路径集合。
    return dataclasses.replace(
        base_cfg,
        verifier_evolve_route=False,
        verifier_local_verdict_only=False,
        verifier_unverified_evidence=False,
        verifier_failure_starts_evolution=False,
        verifier_hide_actor_memory=True,
        verifier_stage_lifecycle=False,
        verifier_persist_scratch=False,
        verifier_private_paths=private_paths)


def _execute_branch(
        *, vm_factory: Callable[[], tuple[Any, Any]], hooks: PracticeHooks,
        project_index: int, project: WaveProject, episode_dir: Path,
        wave_memory_dir: Path, actor_cfg, verifier_control_cfg,
        target_direction: str) -> _BranchRuntime:
    """Run one isolated Actor -> Verifier branch, leaving its Actor resumable."""

    # 【中文注释】执行一个隔离的 Actor→Verifier 分支，结束时保持 Actor 可恢复
    # （历史保留在返回的 _BranchRuntime 中，供串行提交阶段复用）。
    desktop = None
    try:
        # 建独立 VM（工厂返回 desktop 与 vm 句柄）。
        desktop, vm = vm_factory()
        # 读波前记忆快照（所有分支共享同一份不可变快照）。
        wave_memory = _read_memory_tree(str(wave_memory_dir))
        # 重放项目夹具到 PROJECT_ROOT，再推送波前记忆。
        _replay(hooks, vm, str(episode_dir / "fixtures"))
        _push_canonical_memory(hooks, vm, str(wave_memory_dir))

        actor_history: list[dict[str, Any]] = []
        # Actor 章程：项目指令 + 波前记忆清单 + 交接路径 + 运行时契约。
        actor_prompt = self_evolving_actor_charter(
            project.instruction, _listing(wave_memory), ACTOR_HANDOFF) \
            + _actor_runtime_contract()
        actor_continuation = False
        while True:
            # Actor 交接阶段循环：要求模型发布交接文件并捕获候选；传输类失败
            # （夹具缺失/不可重放）时用续跑提示重试，历史不重置。
            actor_decision, actor_history, _ = _run_handoff_phase(
                hooks=hooks, vm=vm, cfg=actor_cfg, prompt=actor_prompt,
                role="ACTOR", handoff_path=ACTOR_HANDOFF,
                token_pattern=_ACTOR_TOKEN,
                sink_root=str(episode_dir / "actor" / "attempt_001"),
                target=target_direction, history=actor_history,
                continuation=actor_continuation)
            actor_continuation = True
            # 候选捕获目录：本分支第一验证周期的候选。
            candidate_dir = episode_dir / "candidates/cycle_001"
            try:
                # 捕获候选树回宿主；成功则退出 Actor 阶段。
                _capture_owned_tree(
                    hooks, vm, f"phase1-candidate-{project_index:03d}",
                    str(candidate_dir), target_direction)
                break
            except PracticeInfrastructureError:
                # 捕获失败（PROJECT_ROOT 缺失/为空/不可重放）→ 续跑提示再试。
                actor_prompt = _continuation_after_transport(
                    "ACTOR", ACTOR_HANDOFF,
                    f"{PROJECT_ROOT} was absent, empty, or not replayable")

        # Actor 交接文本落盘（handoffs/actor_001.md）。
        _atomic_text(
            episode_dir / "handoffs/actor_001.md", actor_decision.text)
        # 构建执行证据包（Actor 自证"程序确实跑过"的证据）。
        execution_evidence = _build_actor_execution_evidence(
            episode_dir / "actor", 1,
            episode_dir / "actor_execution_evidence/cycle_001")

        # Verifier 阶段：全新 VM（回滚掉 Actor 的副作用与工作期记忆编辑），
        # 只重放候选——Verifier 独立调查候选，看不见 Actor 私有推理。
        _fresh_vm(hooks, vm, target_direction)
        _replay(hooks, vm, str(candidate_dir))
        # 函数级延迟导入 core.verifier：分支启动路径更轻，也避免循环导入。
        from core.verifier import VerifierSession, verify_agentic

        # 应用分支级 Verifier 控制配置（隐藏 Actor 私有面等）。
        control_cfg = _branch_control_config(verifier_control_cfg)
        session = VerifierSession()
        try:
            # agentic 验证：独立检查候选是否符合项目指令；context 明确告知
            # "Actor 私有记忆/交接/推理/执行日志已被宿主隐藏，请独立调查"。
            verdict, findings = verify_agentic(
                project.instruction, vm, control_cfg,
                sink=ArtifactSink(str(episode_dir / "verifier/cycle_001")),
                turn_no=project_index,
                context=(
                    f"The candidate project is {PROJECT_ROOT}. Actor-private "
                    "memory, handoff prose, reasoning, and execution logs are "
                    "hidden by the harness. Investigate the actual candidate "
                    "independently."),
                session=session, wall_budget=control_cfg.wall_clock_secs)
        finally:
            # 无论验证成败都关闭执行器（释放会话资源）。
            session.close_executor()
        # Phase 1 分支必须有终态判定：pass/wrong 之外的任何结果都是基础设施错误
        # （例如 unresolved），不允许带着模糊结论进入记忆提交。
        if verdict not in {"pass", "wrong"}:
            raise PracticeInfrastructureError(
                "Phase-1 Verifier Agent ended without PASS/FAIL: "
                + str(findings))
        # 映射为论文术语：pass→PASS，wrong→FAIL。
        terminal_outcome = "PASS" if verdict == "pass" else "FAIL"
        verifier_report = str(findings)
        # Verifier 报告落盘。
        _atomic_text(
            episode_dir / "handoffs/verifier_001.md", verifier_report)

        # 第三段：恢复 Actor 视角做记忆准备——把执行证据推回客户机、校验候选与
        # 证据完整性（防证据与候选不一致）。
        _push_actor_execution_evidence(hooks, vm, execution_evidence)
        _verify_candidate(hooks, vm, str(candidate_dir))
        _verify_actor_execution_evidence(vm, execution_evidence)

        # Throw away Verifier effects and work-phase memory edits, then replay the
        # exact submitted candidate.  The parent later replaces this branch's
        # pre-wave memory with the current canonical bank at its serialized turn.
        # 【中文注释】丢弃 Verifier 副作用与工作期记忆编辑，重放"提交的那份候选"。
        # 父流程稍后会在本分支的串行回合把波前记忆替换为当时的规范库——保证
        # 提交阶段从"与执行时相同的起点"重新出发。
        _fresh_vm(hooks, vm, target_direction)
        _replay(hooks, vm, str(candidate_dir))
        _push_canonical_memory(hooks, vm, str(wave_memory_dir))
        # 返回可恢复的分支运行时（VM 保持打开，供提交阶段续用）。
        return _BranchRuntime(
            project_index=project_index, project=project,
            episode_dir=episode_dir, desktop=desktop, vm=vm,
            actor_history=actor_history, actor_handoff=actor_decision.text,
            verifier_report=verifier_report,
            terminal_outcome=terminal_outcome, wave_memory=wave_memory)
    except Exception:
        # 分支任何异常：关闭本分支的 VM（不影响其他分支），再向上抛。
        if desktop is not None:
            desktop.close()
        raise


def _execute_branch_with_infra_retry(**kwargs) -> _BranchRuntime:
    """One bounded clean retry for broken machines before a terminal verdict.

    Candidate state cannot be faithfully resumed after a failed guest filesystem.
    Archive that unscored attempt and restart from the same immutable fixtures.
    Completed verdicts, memory work, and boundary violations are never retried.
    """
    # 【中文注释】分支级基础设施重试包装：客户机文件系统损坏后，候选状态无法忠实
    # 恢复，因此把"未计分尝试"归档，从同一份不可变夹具重启；只允许一次干净重试。
    # 已完成判定、记忆工作与边界违规永不重试（防止把失败洗成成功）。
    episode = kwargs["episode_dir"]
    try:
        return _execute_branch(**kwargs)
    except PracticeBoundaryError:
        # 边界违规直接上抛（不重试）。
        raise
    except Exception as exc:
        # 已有 Verifier 报告 → 判定已完成，重试会重写历史，直接上抛。
        if (episode / "handoffs/verifier_001.md").exists():
            raise
        # 物理故障启发式：超时/连接错误，或异常文本命中已知基础设施标记。
        physical = isinstance(exc, (TimeoutError, ConnectionError)) or any(
            marker in str(exc) for marker in (
                "fresh VM boundary failed", "Read timed out", "port_allocation"))
        # 扫描本 episode 的 trace_meta.json：标记 infra_fail 且 trace 文本命中
        # 只读文件系统/通道错误/回滚镜像失败等物理标记，也认定为物理故障。
        for trace in episode.glob("**/trace_meta.json"):
            if "infra_attempts" in trace.parts:
                continue
            metadata = json.loads(trace.read_text())
            output = trace.with_name("trace.txt")
            if metadata.get("infra_fail") and output.is_file():
                text = output.read_text(errors="replace").lower()
                physical |= any(marker in text for marker in (
                    "read-only file system", "channel error",
                    "rollback-mirror setup failed"))
        # 非物理故障（模型/协议问题）不重试，如实上抛。
        if not physical:
            raise
        # 建归档目录（exist_ok=False 防重复重试），搬走除夹具/项目说明/归档区外的
        # 一切产物，附重试回执（记录原因、模式、未提升记忆、最多 1 次重试）。
        archive = episode / "infra_attempts/attempt_001"
        archive.mkdir(parents=True, exist_ok=False)
        for path in list(episode.iterdir()):
            if path.name not in {"fixtures", "project.md", "infra_attempts"}:
                shutil.move(str(path), archive / path.name)
        _atomic_json(archive / "retry_receipt.json", {
            "reason": f"{type(exc).__name__}: {exc}",
            "mode": "restart_unscored_branch_from_immutable_fixtures",
            "prior_memory_promoted": False, "maximum_retries": 1})
    # 从不可变夹具重新执行整个分支。
    return _execute_branch(**kwargs)


def _memory_changes(before: dict[str, bytes], after: dict[str, bytes]) \
        -> dict[str, list[str]]:
    # 计算记忆树差异（按文件名集合）：created/deleted/modified/unchanged 四类，
    # 修改判定基于字节级比较（bytes 相等性）。结果各表排序保证确定性。
    before_names = set(before)
    after_names = set(after)
    return {
        "created": sorted(after_names - before_names),
        "deleted": sorted(before_names - after_names),
        "modified": sorted(
            name for name in before_names & after_names
            if before[name] != after[name]),
        "unchanged": sorted(
            name for name in before_names & after_names
            if before[name] == after[name]),
    }


def _commit_branch_memory(
        *, branch: _BranchRuntime, hooks: PracticeHooks, memory_cfg,
        canonical_memory_dir: Path, journal_dir: Path, corpus_path: str,
        target_direction: str) -> dict[str, Any]:
    """Resume the same branch Actor and serialize its update into one bank."""

    # 【中文注释】wave memory barrier 的落地：恢复同一分支 Actor，把它的记忆更新
    # 串行化进唯一记忆库。调用方按自著顺序逐分支调用本函数，天然避免并发写冲突。
    # 提交前的记忆基线（宿主侧读取）。
    before = _read_memory_tree(str(canonical_memory_dir))
    # 双写基线留档：episode 目录下保存 before 副本供审计。
    hooks.install_memory(
        str(branch.episode_dir / "memory_before_commit"), before)
    # 把"当时的规范库"推到本分支 VM（替代波前快照——分支执行期间规范库未变，
    # 因为提交是串行的）。
    _push_canonical_memory(hooks, branch.vm, str(canonical_memory_dir))
    # 第一阶段：记忆蒸馏——基于终态判定（PASS/FAIL）与 Verifier 报告提炼经验。
    # 延续分支的 Actor 历史（同一上下文，无需重新侦察）。
    history, _ = _run_free_phase(
        hooks=hooks, vm=branch.vm, cfg=memory_cfg,
        prompt=self_evolving_actor_memory_distillation_msg(
            branch.terminal_outcome, branch.verifier_report),
        role="ACTOR",
        sink_root=str(branch.episode_dir / "memory_distillation"),
        target=target_direction, history=branch.actor_history)
    # 第二阶段：记忆整合（reconciliation）——与既有记忆库对照合并，避免重复与矛盾。
    history, _ = _run_free_phase(
        hooks=hooks, vm=branch.vm, cfg=memory_cfg,
        prompt=self_evolving_actor_memory_reconciliation_msg(),
        role="ACTOR",
        sink_root=str(branch.episode_dir / "memory_reconciliation"),
        target=target_direction, history=history)
    # 从客户机拉取终态记忆候选（Actor 整理后的记忆树）。
    candidate = _pull_terminal_memory(hooks, branch.vm)
    # 记忆审计：必须引用语料（require_corpus=True），越界条目进 reject 日志。
    accepted = hooks.audit_memory(
        candidate, corpus_path,
        str(canonical_memory_dir.parent / "audit_rejects.jsonl"),
        authorized_instruction=target_direction, require_corpus=True)
    # 审计若删改了候选（accepted != candidate）→ 边界违规，整批拒绝。
    if accepted != candidate:
        raise PracticeBoundaryError("terminal memory failed the target boundary")
    # 写记忆日志（journal）：记录分支序号、来源种类与终态判定，供恢复与追溯。
    hooks.journal_memory(
        str(journal_dir), branch.project_index, accepted,
        {"kind": "phase1-parallel-terminal-memory",
         "terminal_outcome": branch.terminal_outcome})
    # 原子安装进规范库；并在 episode 留档 after 副本。
    hooks.install_memory(str(canonical_memory_dir), accepted)
    hooks.install_memory(
        str(branch.episode_dir / "memory_after_commit"), accepted)
    # 返回 before/after 清单与差异，供 outcome 记录与下一波 Curriculum 参考。
    return {
        "before": _manifest(before),
        "after": _manifest(accepted),
        "changes": _memory_changes(before, accepted),
    }


def evolve_parallel_phase1(
        *, root: str, target_direction: str, actor_cfg,
        curriculum_cfg, memory_cfg, verifier_control_cfg,
        vm_factory: Callable[[], tuple[Any, Any]], corpus_path: str,
        hooks: PracticeHooks | None = None,
        event_sink: Callable[..., Any] | None = None,
        project_budget: int | None = None,
        checkpoint_projects: tuple[int, ...] = (),
        max_parallel: int = 4,
        target_query_conditioned: bool = False,
        resume_completed_boundary: bool = False,
        park_verified_branches: bool = False) -> PracticeResult:
    """Run persistent-Curriculum parallel waves until Agent saturation.

    ``project_budget`` is an undisclosed emergency boundary checked only between
    complete Agent-authored waves. It never truncates a wave or defines semantic
    convergence. ``max_parallel`` is host scheduling capacity, not wave width.
    ``park_verified_branches`` releases verified VMs while retaining exact
    candidate archives and Actor contexts, then restores them for memory commits.
    """

    # 【中文注释】Phase 1 主循环：持久 Curriculum + 并行波次，直到 Agent 自宣饱和。
    # project_budget：未公开的应急算力边界——只在"完整波"之间检查，绝不截断波内
    # 项目，也绝不充当语义收敛判据。max_parallel：宿主调度容量，不等于波宽
    # （波宽由 Curriculum 决定）。park_verified_branches：已验证分支先释放 VM
    # （保留候选档案与 Actor 上下文），到记忆提交时再恢复。
    # 参数校验：预算必须是非负整数或 None（type(x) is int 拒绝 bool 混入）。
    if project_budget is not None and (
            type(project_budget) is not int or project_budget < 0):
        raise ValueError("project_budget must be nonnegative or None")
    if type(max_parallel) is not int or max_parallel < 1:
        raise ValueError("max_parallel must be a positive integer")
    # 检查点序号去重排序（set 去重 + sorted 稳定顺序）。
    checkpoint_projects = tuple(sorted(set(checkpoint_projects)))
    if any(type(value) is not int or value < 0
           for value in checkpoint_projects):
        raise ValueError("checkpoint_projects must be nonnegative integers")

    # 钩子缺省构造；root 为谱系根目录。
    hooks = hooks or PracticeHooks()
    lineage = Path(root)
    # 谱系目录布局：memory（规范记忆库）、memory_journal（提交日志）、
    # episodes（分支记录）、waves（波次记录）、checkpoints（检查点）、state.json。
    memory_dir = lineage / "memory"
    journal_dir = lineage / "memory_journal"
    episodes_dir = lineage / "episodes"
    waves_dir = lineage / "waves"
    checkpoints_dir = lineage / "checkpoints"
    state_path = lineage / "state.json"
    # 建目录（parents=True 递归，exist_ok 幂等）。
    for path in (memory_dir, journal_dir, episodes_dir, waves_dir):
        path.mkdir(parents=True, exist_ok=True)

    project_index = 0
    wave_index = 0
    curriculum_history: list[dict[str, Any]] = []
    previous_wave_outcomes = ""
    if resume_completed_boundary:
        # 断点恢复（边界恢复模式）：验证已完成边界并归档未决残留，
        # 恢复项目/波次计数、Curriculum 历史与上一波结果文本。
        from explore.phase1_boundary_recovery import validate_boundary, archive_pending
        plan = validate_boundary(
            lineage, target_direction, project_budget=project_budget,
            checkpoint_projects=checkpoint_projects, max_parallel=max_parallel,
            target_query_conditioned=target_query_conditioned)
        archive_pending(lineage, plan)
        project_index, wave_index = plan["projects"], plan["wave"]
        curriculum_history = plan["history"]
        previous_wave_outcomes = plan["outcomes"]

    def emit(event_type: str, **payload: Any) -> None:
        # 事件发射闭包：event_sink 存在时转发；默认状态 in_progress，并附带
        # 三个"打开句柄"标志（供外部恢复器判断是否有未完成工作）。
        if event_sink is not None:
            event_sink(
                event_type, status=payload.pop("status", "in_progress"),
                state={"project_open": False, "memory_phase_open": False,
                       "recovery_open": False}, payload=payload)

    def checkpoint(index: int) -> None:
        # 检查点闭包：指定序号命中时，把当前记忆库快照安装到 checkpoints 目录。
        if index not in checkpoint_projects:
            return
        destination = checkpoints_dir / f"project_{index:03d}/memory"
        hooks.install_memory(
            str(destination), _read_memory_tree(str(memory_dir)))

    def finish(status: str, reason: str, terminal_text: str = "") -> PracticeResult:
        # 终态收尾闭包：写 state.json（schema v2，含 SHA-256 与记忆清单）并返回
        # PracticeResult。target_sha256 把目标方向摘要化，便于跨运行比对一致性。
        payload = {
            "schema_version": 2,
            "status": status,
            "projects": project_index,
            "waves": wave_index,
            "last_project": project_index,
            "reason": reason,
            "terminal_text": terminal_text,
            "phase1_parallel_waves": True,
            "target_query_conditioned": target_query_conditioned,
            "project_budget": project_budget,
            "max_parallel": max_parallel,
            "checkpoint_projects": list(checkpoint_projects),
            "target_sha256": hashlib.sha256(
                target_direction.encode("utf-8")).hexdigest(),
            "memory_manifest": _manifest(
                _read_memory_tree(str(memory_dir))),
        }
        _atomic_json(state_path, payload)
        return PracticeResult(
            status=status, projects=project_index, root=str(lineage),
            memory_dir=str(memory_dir), reason=reason,
            terminal_text=terminal_text, last_project=project_index)

    # active_branches：已验证但尚未提交记忆的分支（异常时需要统一关 VM）。
    active_branches: list[_BranchRuntime] = []
    try:
        # 目标方向必须非空；语料必须通过校验；目标提示词过审计。
        if not target_direction.strip():
            raise PracticeInfrastructureError("target direction is empty")
        hooks.validate_corpus(corpus_path)
        _audit_prompt(hooks, target_direction, target_direction)
        if not resume_completed_boundary:
            # 新鲜运行：先做 project_000 检查点（初始记忆）。
            checkpoint(0)
        else:
            # 恢复运行：发射"边界已恢复"事件。
            emit("PHASE1_WAVE_BOUNDARY_RESUMED", wave=wave_index, projects=project_index)
        while True:
            # 应急预算检查：只在完整波之间生效（循环顶部即波间边界）。
            if project_budget is not None and project_index >= project_budget:
                return finish(
                    "budget_exhausted",
                    "external complete-wave compute boundary reached")

            # ===== 波次作者阶段（Curriculum 在专用 VM 上写波）=====
            wave_index += 1
            wave_dir = waves_dir / f"wave_{wave_index:03d}"
            curriculum_sink = lineage / "curriculum" / \
                f"wave_{wave_index:03d}"
            curriculum_desktop, curriculum_vm = vm_factory()
            try:
                # 把规范记忆推给 Curriculum VM：它基于当前记忆与上一波结果选题。
                _push_canonical_memory(hooks, curriculum_vm, str(memory_dir))
                # 驱动写作直到发布合法信封。
                decision, curriculum_history = _author_wave(
                    hooks=hooks, vm=curriculum_vm, cfg=curriculum_cfg,
                    target_direction=target_direction,
                    previous_wave_outcomes=previous_wave_outcomes,
                    history=curriculum_history, sink_root=curriculum_sink,
                    target_query_conditioned=target_query_conditioned)
                # 原始决策文本落盘（无损审计）。
                _atomic_text(wave_dir / "curriculum_decision.json", decision.raw)
                if decision.decision in {"SATURATED", "STALLED"}:
                    # 终态决策：SATURATED（饱和）或 STALLED（停滞）——落盘
                    # curriculum_terminal.json 并收尾；STALLED 与"成功收敛"
                    # 在状态上可区分（论文要求两者可辨别）。
                    _atomic_text(
                        lineage / "curriculum_terminal.json", decision.raw)
                    return finish(
                        decision.decision.lower(),
                        "Curriculum Agent terminal decision", decision.raw)

                # ===== 波前快照与分支指派 =====
                wave_memory_dir = wave_dir / "memory_snapshot"
                wave_memory = _read_memory_tree(str(memory_dir))
                # 安装本波记忆快照：所有分支共享的同一份不可变起点。
                hooks.install_memory(str(wave_memory_dir), wave_memory)
                # 指派表：(全局序号, 项目, episode 目录) 三元组；
                # enumerate(…, 1) 从 1 起编号（offset）。
                assignments = tuple(
                    (project_index + offset, project,
                     episodes_dir / f"ep{project_index + offset:03d}")
                    for offset, project in enumerate(decision.projects, 1))
                # 舞台化并捕获全部夹具。
                _capture_wave_fixtures(
                    hooks=hooks, vm=curriculum_vm, decision=decision,
                    assignments=assignments,
                    target_direction=target_direction)
            finally:
                # 作者 VM 用完即关（分支 VM 由分支自己管理）。
                curriculum_desktop.close()

            # 发射波开始事件：附波前记忆清单与调度容量。
            emit(
                "PHASE1_WAVE_STARTED", wave=wave_index,
                projects=len(assignments),
                memory_manifest=_manifest(wave_memory),
                max_parallel=max_parallel)
            completed: dict[int, _BranchRuntime] = {}
            branch_errors: list[Exception] = []
            # ===== 并行分支执行：线程池容量 = min(max_parallel, 波宽) =====
            with ThreadPoolExecutor(
                    max_workers=min(max_parallel, len(assignments)),
                    thread_name_prefix=f"phase1-wave-{wave_index}") as pool:
                # 字典推导建立 {future: 项目序号} 映射；每个分支深拷贝配置，
                # 确保并行分支不共享可变配置状态。
                futures = {
                    pool.submit(
                        _execute_branch_with_infra_retry, vm_factory=vm_factory, hooks=hooks,
                        project_index=index, project=project,
                        episode_dir=episode_dir,
                        wave_memory_dir=wave_memory_dir,
                        actor_cfg=copy.deepcopy(actor_cfg),
                        verifier_control_cfg=copy.deepcopy(
                            verifier_control_cfg),
                        target_direction=target_direction): index
                    for index, project, episode_dir in assignments
                }
                # as_completed：按完成顺序收集（不按提交顺序）。
                for future in as_completed(futures):
                    try:
                        branch = future.result()
                    except Exception as exc:
                        branch_errors.append(exc)
                        # Persist immediately: a productive sibling can take
                        # hours to finish after this future has already failed.
                        # This event does not commit memory or decide a verdict.
                        # 【中文注释】立即持久化失败事件：已经失败的未来之后，
                        # 有产出的兄弟分支可能还要跑数小时。该事件不提交记忆、
                        # 也不作出任何判定。
                        emit(
                            "PHASE1_BRANCH_EXCEPTION", wave=wave_index,
                            project=futures[future],
                            error_type=type(exc).__name__, error=str(exc),
                            classification=("boundary" if isinstance(
                                exc, PracticeBoundaryError) else "infra"))
                        continue
                    # 成功分支：登记入 completed 与 active_branches。
                    completed[branch.project_index] = branch
                    active_branches.append(branch)
                    if park_verified_branches:
                        # 停放已验证分支：关闭 VM 释放资源（候选档案与 Actor
                        # 上下文仍在 _BranchRuntime 中），提交时再恢复。
                        branch.desktop.close()
                        branch.desktop = None
                        branch.vm = None
                    emit(
                        "PHASE1_BRANCH_VERIFIED", wave=wave_index,
                        project=branch.project_index,
                        project_id=branch.project.project_id,
                        terminal_outcome=branch.terminal_outcome)

            # Drain every future so successful siblings retain an owned handle
            # for cleanup even when another branch fails. Never commit a partial
            # wave merely because the failing future happened to return first.
            # 【中文注释】排空所有 future：即使某分支失败，成功的兄弟分支也保留
            # 自己的句柄以便清理。绝不因为失败分支恰好先返回就提交半个波——
            # wave memory barrier 要求"全部有效验证完成后"才能进入提交阶段。
            if branch_errors:
                boundary = next((e for e in branch_errors
                                 if isinstance(e, PracticeBoundaryError)), None)
                # 边界违规优先上抛（语义更严重）；否则抛第一个错误。
                raise boundary or branch_errors[0]

            # ===== 串行记忆提交阶段（wave memory barrier 的"屏障侧"）=====
            rendered_outcomes: list[str] = []
            for index, _project, _episode_dir in assignments:
                # 按自著顺序（assignments 顺序）逐个提交。
                branch = completed[index]
                if park_verified_branches:
                    # 恢复被停放的分支：新建 VM 并重放其候选（保持可复现）。
                    branch.desktop, branch.vm = vm_factory()
                    _replay(hooks, branch.vm,
                            str(branch.episode_dir / "candidates/cycle_001"))
                # 串行提交该分支的记忆（蒸馏→整合→审计→入账→安装）。
                memory_record = _commit_branch_memory(
                    branch=branch, hooks=hooks, memory_cfg=memory_cfg,
                    canonical_memory_dir=memory_dir,
                    journal_dir=journal_dir, corpus_path=corpus_path,
                    target_direction=target_direction)
                # 结果记录：波次/项目索引、判定、交接与报告、波前快照清单、
                # 记忆前后清单与差异——全部落盘供审计。
                record = {
                    "schema_version": 2,
                    "wave_index": wave_index,
                    "project_index": index,
                    "project_id": branch.project.project_id,
                    "project": branch.project.instruction,
                    "terminal_outcome": branch.terminal_outcome,
                    "verification_cycles": 1,
                    "actor_handoffs": [branch.actor_handoff],
                    "verifier_reports": [branch.verifier_report],
                    "wave_memory_snapshot": _manifest(branch.wave_memory),
                    "memory_before": memory_record["before"],
                    "memory_after": memory_record["after"],
                    "memory_changes": memory_record["changes"],
                }
                _atomic_json(branch.episode_dir / "outcome.json", record)
                # 渲染 markdown 版结果（供下一波 Curriculum 证据）。
                rendered = _outcome_text(record) + "\n\nMEMORY CHANGES:\n" \
                    + json.dumps(
                        memory_record["changes"], ensure_ascii=False,
                        indent=2, sort_keys=True)
                _atomic_text(branch.episode_dir / "outcome.md", rendered)
                rendered_outcomes.append(rendered)
                # 推进项目索引并按需做检查点。
                project_index = index
                checkpoint(project_index)
                # 分支 VM 用完即关，并移出活跃表。
                branch.desktop.close()
                active_branches.remove(branch)
                emit(
                    "PHASE1_BRANCH_MEMORY_COMMITTED", status="completed",
                    wave=wave_index, project=project_index,
                    project_id=branch.project.project_id,
                    terminal_outcome=branch.terminal_outcome,
                    memory_changes=memory_record["changes"])

            # 下一波的证据：Curriculum 理由 + 全部分支渲染结果拼接。
            previous_wave_outcomes = (
                f"WAVE {wave_index} CURRICULUM RATIONALE:\n"
                f"{decision.rationale}\n\n" + "\n\n---\n\n".join(
                    rendered_outcomes))
            _atomic_text(wave_dir / "outcomes.md", previous_wave_outcomes)
            # 清理 Curriculum 历史中的旧视觉（多模态上下文瘦身）。
            curriculum_history, _ = _scope_prior_curriculum_visuals(
                curriculum_history)
            # 运行中状态落盘（含记忆清单），供崩溃后恢复。
            _atomic_json(state_path, {
                "schema_version": 2,
                "status": "running",
                "waves": wave_index,
                "projects": project_index,
                "last_project": project_index,
                "target_sha256": hashlib.sha256(
                    target_direction.encode("utf-8")).hexdigest(),
                "memory_manifest": _manifest(
                    _read_memory_tree(str(memory_dir))),
            })
            emit(
                "PHASE1_WAVE_COMPLETED", status="completed",
                wave=wave_index, projects=len(assignments),
                total_projects=project_index)
    except KeyboardInterrupt:
        # 外部中断：以 infra 状态收尾（不算失败也不算收敛）。
        return finish("infra", "KeyboardInterrupt: external interruption")
    except PracticeBoundaryError as exc:
        # 边界违规：隔离收尾（记忆已提交部分保留，未提交的不合并）。
        return finish("quarantined", str(exc))
    except Exception as exc:  # noqa: BLE001 - persist inspectable infra state
        # 其他异常：infra 收尾（noqa 注释表明"必须留下可检查状态"是有意为之）。
        return finish("infra", f"{type(exc).__name__}: {exc}")
    finally:
        # 统一清理：所有仍活跃的分支 VM 尽力关闭（异常也吞掉，优先完成清理）。
        for branch in active_branches:
            try:
                if branch.desktop is not None:
                    branch.desktop.close()
            except Exception:
                pass


# 模块公开 API：外部（benchmarks/osworld/phase1.py 等）只应使用这些名字。
__all__ = [
    "WAVE_HANDOFF",
    "WAVE_ROOT",
    "WaveDecision",
    "WaveProject",
    "evolve_parallel_phase1",
    "parse_wave_handoff",
]
