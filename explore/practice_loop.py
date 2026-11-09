# 【中文注释·模块定位】Agent 自有的练习生命周期（DRS 的骨架模块，同时向 Phase 1
# 波次与目标学习提供全部底层原语）。流程：持久 Curriculum → 一次全新 Actor 提交 →
# 一次全新 Verifier 判定（PASS 或 FAIL）→ 同一 Actor 起草并把终态经验整合进持久
# 记忆 → 持久 Curriculum 再次搜索。这里没有宿主评分、项目预检、奖励、基准评估器
# 或宿主书写的成功标准——每次交接只携带一个小的词法传输令牌（token），
# 一切语义决策都属于智能体（control split 的传输/语义二分）。
"""Agent 自有的练习生命周期（Agent-owned practice lifecycle）。

本模块协调当前的练习生命周期：

    持久 Curriculum（课程智能体）
      → 一次全新 Actor（执行智能体）提交
      → 一次全新 Verifier（验证智能体）判定（PASS 或 FAIL）
      → 同一 Actor 起草并把终态经验整合进持久记忆
      → 持久 Curriculum 再次搜索

这里没有宿主评分、项目专属预检、奖励、基准评测器，也没有宿主编写的
成功标准。每次交接只携带一个小的词法传输令牌（transport token）；
一切语义决策都属于智能体本身。
"""

from __future__ import annotations
# 启用 PEP 563 延迟注解求值：注解不在导入时求值，允许前向引用。

# 导入 copy：深拷贝角色配置（阶段开关在副本上改，绝不污染原始配置）。
import copy
# 导入 dataclasses：dataclasses.replace 生成 Verifier 控制面配置的改写副本。
import dataclasses
# 导入 dataclass 装饰器：声明交接/结果等纯数据容器。
from dataclasses import dataclass
# 导入 hashlib：项目、记忆清单、执行证据等内容的 sha256 指纹（内容寻址与防篡改）。
import hashlib
# 导入 json：状态文件、事件负载与结果记录的序列化。
import json
# 导入 os：原子替换（os.replace）、文件描述符与 fsync 等底层文件操作。
import os
# 导入 Path：路径对象处理（谱系目录、记忆树遍历）。
from pathlib import Path
# 导入 re：传输令牌等词法模式的编译与匹配。
import re
# 导入 shlex：客户机 shell 命令中路径的安全引用（防注入、防分词）。
import shlex
# 导入 shutil：暂存/备份目录的递归清理。
import shutil
# 导入 tempfile：原子写入的临时文件与暂存目录生成（同目录保证同文件系统）。
import tempfile
# 导入 time：备份名的时间戳唯一化。
import time
# 导入 Any/Callable：动态结构与钩子函数表的类型标注。
from typing import Any, Callable

# 导入纯 JSON 传输说明常量：续传提示中复用的统一传输约定文本。
from core.actor import PLAIN_JSON_TRANSPORT_NOTE
# 导入 run_attempt：底层单轮智能体执行循环（经 PracticeHooks 注入本生命周期）。
from core.loop import run_attempt
# 导入 ArtifactSink：轨迹工件落盘的接收器工厂（每段一个目录）。
from core.trace import ArtifactSink
# 导入 DURABLE_IMAGES_FIELD：消息中持久图像附件的字段名（视觉作用域裁剪用）。
from llm.client import DURABLE_IMAGES_FIELD
# 导入 explore.commit 并别名为 memory_transport：持久记忆的推拉、静默审计、日志与校验。
from explore import commit as memory_transport
# 导入各角色章程与消息构造器：Curriculum/Actor/Verifier 的提示词全部由 charter 生成。
from explore.charter import (
    self_evolving_actor_learning_diagnosis_msg,
    self_evolving_actor_charter,
    self_evolving_actor_memory_distillation_msg,
    self_evolving_actor_memory_reconciliation_msg,
    self_evolving_curriculum_charter,
    self_evolving_verifier_charter,
)
# 导入项目材料捕获/回放/校验/审计原语：项目树在客户机与宿主之间的搬运与验真。
from explore.provisioning import (
    audit_captured_materials,
    capture_project_materials,
    replay_project_materials,
    verify_project_materials,
)
# 导入防泄漏审计工具：对提示词与智能体轨迹做目标边界（target boundary）检测。
from tools.exam_fence import audit_text, audit_transcripts


# 【中文注释】客户机内路径哨兵与令牌约定（所有角色共用同一套"信封地址"）：
# PROJECT_ROOT 是练习/波次项目的工作目录；CURRICULUM_HANDOFF/CURRICULUM_NOTES 是
# Curriculum 的交接与笔记；ACTOR_HANDOFF/VERIFIER_REPORT/LEARNING_DIAGNOSIS 分别是
# Actor 交接、Verifier 报告与学习诊断；ACTOR_EXECUTION_EVIDENCE（点前缀=Agent 私有）
# 存放执行证据。DEFAULT_CORPUS 是宿主侧任务指令 8-gram 语料的默认位置（防泄漏审计）。
PROJECT_ROOT_NAME = "evolution_project"
# 客户机内项目根目录名：Curriculum 造题、Actor 解题的统一工作区。
PROJECT_ROOT = f"/home/user/{PROJECT_ROOT_NAME}"
# 项目根绝对路径：唯一的"求解器可见发布集"，快照/回放只针对它。
CURRICULUM_HANDOFF = "/home/user/project_next.md"
# Curriculum 交接文件路径（下一项目的自然语言描述 + DECISION 令牌）。
CURRICULUM_NOTES = "/home/user/curriculum_notes.md"
# Curriculum 笔记路径（跨项目持久的课程笔记）。
ACTOR_HANDOFF = "/home/user/actor_handoff.md"
# Actor 交接文件路径（含 STATUS: SUBMIT 令牌的解题交付）。
VERIFIER_REPORT = "/home/user/verifier_report.md"
# Verifier 报告路径（含 VERDICT: PASS|FAIL 令牌的判定报告）。
LEARNING_DIAGNOSIS = "/home/user/learning_diagnosis.md"
# Actor 学习诊断路径（自由格式的因果复盘，不套模式）。
ACTOR_EXECUTION_EVIDENCE = "/home/user/.practice_actor_execution"
# Actor 执行证据目录（点前缀=Agent 私有；宿主推送并逐字节校验的执行记录视图）。
DEFAULT_CORPUS = str(
    Path(__file__).resolve().parents[1] / "results/explore/corpus_shingles.json")
# 默认任务指令 8-gram 语料路径（防泄漏静默审计的对照语料）。

_ACTOR_EXECUTION_FILENAMES = frozenset({
    "program.py", "program.sh", "trace.txt", "trace_meta.json", "look.json",
})
# 执行证据白名单：只收编这五种确切产物（程序/轨迹/元数据/观察请求），
# 刻意排除模型轮次、工作日志与宿主判定。
_ACTOR_EXECUTION_MANIFEST = "SHA256SUMS"
# 执行证据清单文件名（sha256 清单，宿主与客户机双向校验的锚点）。

# 【中文注释】传输令牌（词法约束，不承载语义）：各角色的交接首行必须匹配其一。
# Curriculum 的四种变体对应四种生命周期（通用/Phase1 波次/目标/统一循环），可选词
# 各不相同；Actor 只有 SUBMIT；Verifier 只有 PASS|FAIL（练习判定二值）。令牌只证明
# "格式可解析"，绝不等于语义判定本身。
# 通用循环 Curriculum 令牌：PROJECT 继续造题 / CONVERGED 收敛 / STALLED 停滞。
_CURRICULUM_TOKEN = re.compile(
    r"^DECISION: (PROJECT|CONVERGED|STALLED)$")
# Phase 1 探索令牌：SATURATED 替代 CONVERGED——声明"探索分布饱和"而非任务收敛。
_PHASE1_CURRICULUM_TOKEN = re.compile(
    r"^DECISION: (PROJECT|SATURATED|STALLED)$")
# 目标条件化 Phase 1 令牌：READY_FOR_TARGET_TEST 表示准备好上真实目标。
_TARGET_CURRICULUM_TOKEN = re.compile(
    r"^DECISION: (PROJECT|READY_FOR_TARGET_TEST|STALLED)$")
# 统一循环令牌：READY_FOR_RETRY 表示准备好重试。
_UNIFIED_CURRICULUM_TOKEN = re.compile(
    r"^DECISION: (PROJECT|READY_FOR_RETRY|STALLED)$")
# Actor 令牌：只有 SUBMIT 一种（一次提交语义）。
_ACTOR_TOKEN = re.compile(r"^STATUS: (SUBMIT)$")
# Verifier 令牌：PASS|FAIL 二值判定。
_VERIFIER_TOKEN = re.compile(r"^VERDICT: (PASS|FAIL)$")


# 基础设施错误：传输、VM 边界或紧急看门狗阻止了"作出判定"（不计分、可恢复）。
class PracticeInfrastructureError(RuntimeError):
    """传输、VM 边界或紧急看门狗阻止了"作出判定"（infrastructure 状态，不计分、可恢复）。"""


# 边界违规错误：目标泄漏遏制边界拒绝了某个 Agent 表面（如越权引用任务信息）。
class PracticeBoundaryError(RuntimeError):
    """目标泄漏遏制边界拒绝了某个 Agent 表面（如越权引用任务信息），进入隔离（quarantined）。"""


def _scope_prior_curriculum_visuals(
        history: list[dict[str, Any]]) \
        -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """在项目边界处结束像素回放，但不压缩 Agent 的上下文。

    Curriculum Agent 在当前项目决策期间始终需要原生 Look 像素；但把每张历史
    图像回放进之后的每个项目，最终会突破服务方的图像载荷上限。完整图像无损
    留存在首次记录它们的原始轨迹中。这个"活动上下文投影"只在项目边界之后
    摘除私有线上附件；它保留每条消息与每个原始字符，并追加一条如实的取回
    说明，让 Agent 在旧视觉证据重新相关时可以自由地重新发起 Look。
    """

    scoped = []
    # scoped：投影后的新历史（保留全部文本，只摘除附件）。
    archived = []
    # archived：被归档附件的登记表（消息下标、张数、各图 sha256）。
    for index, original in enumerate(list(history or [])):
        # 逐条遍历历史消息（先拷贝列表，避免遍历中修改入参）。
        message = dict(original)
        # 浅拷贝消息字典：改动只落在投影副本上，不动原始轨迹。
        attachments = message.pop(DURABLE_IMAGES_FIELD, [])
        # 弹出持久图像附件字段（无附件时取空列表）。
        if attachments:
            # 只处理确实带图像附件的消息。
            if (not isinstance(attachments, list)
                    or any(not isinstance(item, dict)
                           or not isinstance(item.get("data"), str)
                           or not isinstance(item.get("sha256"), str)
                           for item in attachments)):
                # 结构校验 fail closed：附件不是"字典列表且 data/sha256 均为字符串"即报错。
                raise PracticeInfrastructureError(
                    "persistent Curriculum visual attachment is malformed")
            valid = attachments
            # 命名绑定：通过校验的附件集合（语义上等于原附件列表）。
            content = message.get("content", "")
            # 取消息正文（缺省为空串）。
            if not isinstance(content, str):
                # 正文必须是文本：带图消息若非文本则无法追加取回说明。
                raise PracticeInfrastructureError(
                    "persistent Curriculum visual observation is not text")
            message["content"] = (
                content
                + "\n\n[HARNESS PROJECT-BOUNDARY SENSORY ARCHIVE: "
                  f"{len(valid)} exact Look image(s) from the prior project remain "
                  "losslessly stored in that turn's raw transcript but are not "
                  "resent as pixels in this new project context. Reissue a Look "
                  "whenever you judge those pixels relevant.]"
            )
            # 在正文后追加如实的归档说明：图像仍在原始轨迹里，需要时重发 Look 即可取回。
            archived.append({
                "message_index": index,
                "images": len(valid),
                "sha256": [str(item.get("sha256", "")) for item in valid],
            })
            # 登记归档明细：哪条消息、几张图、各图的 sha256 指纹（仅哈希，不含内容）。
        scoped.append(message)
        # 投影消息进入新历史（文本一字不改，附件已摘除）。
    return scoped, archived
    # 返回（投影历史, 归档登记）二元组。


@dataclass(frozen=True)
class ParsedHandoff:
    """解析成功的一份交接：token 是词法令牌，body 是去掉令牌行的正文，text 是原文。"""

    token: str
    # 令牌捕获组（如 PROJECT / SUBMIT / PASS）。
    body: str
    # 去掉令牌行后的正文文本。
    text: str
    # 原始完整文本（供落盘存档原样保留）。


@dataclass
class PracticeResult:
    """练习生命周期的最终结果：终态、完成项目数、谱系根与记忆目录位置。"""

    status: str
    # 终态：done 类（converged/saturated/stalled 等）或 infra/quarantined/budget_exhausted。
    projects: int
    # 已完成（闭合边界）的项目数。
    root: str
    # 谱系根目录（state.json、episodes、curriculum 等所在）。
    memory_dir: str
    # 持久记忆目录路径。
    reason: str = ""
    # 终态原因说明（人类可读）。
    terminal_text: str = ""
    # 触发终态的原始交接文本（如 Curriculum 终局决定全文）。
    last_project: int = 0
    # 最后一个已开边界（可能未闭合）的项目编号。


def _reset_null_vm(vm, target_direction: str) -> dict[str, Any]:
    """跨越一次全新的空任务（null-task）边界，并只清除 RSI 自有的客户机表面。"""

    env = getattr(vm, "env", None)
    # 取 VM 适配层内部的 DesktopEnv 环境（可能不存在）。
    reset = getattr(env, "reset", None)
    # 取环境的 reset 边界（可能不存在）。
    if not callable(reset):
        # 没有 reset 边界：无法安全重置，报基础设施失败。
        return {"ok": False, "error": "VM environment has no reset boundary"}
    try:
        # RSIAgent 经客户机控制器在 DesktopEnv.step() 之外做变更；
        # 这里强制 DesktopEnv 的 reset 优化走真实快照路径。
        env.is_environment_used = True
        # 置"已使用"标志：迫使 reset 放弃跳过优化，真正执行快照恢复。
        reset(task_config=None)
        # 以 None 任务配置重置：回到与任何任务无关的干净基线。
    except Exception as exc:  # noqa: BLE001 - converted to infrastructure state
        # 重置失败统一转成基础设施状态（不计分，可恢复）。
        return {"ok": False, "error": f"fresh VM reset failed: {exc}"}
    if hasattr(vm, "_conda"):
        vm._conda = ""
        # 清空缓存的 conda 环境名：重置后客户机环境可能已变化，强制下次重新探测。
    # DesktopEnv.reset(None) 历史上不会清掉先前的任务/评测器（task/evaluator）字段。
    # TARGET 在"任务可见"与"空任务"两个阶段间交替，因此每次空任务边界都要
    # 显式擦除这些仅宿主侧的回调字段。
    for name, value in {
            "task_config": None, "instruction": None, "config": [],
            "user_simulator": None, "evaluator": None, "metric": None,
            "metric_conj": "and", "result_getter": None,
            "expected_getter": None, "metric_options": {}}.items():
        # 逐个把任务/评测器相关字段刷成中性值，防止上一任务的信息泄漏进下一阶段。
        try:
            setattr(env, name, value)
            # 逐字段覆写（字段集合随 DesktopEnv 版本可能变化，故逐个容错）。
        except Exception:  # noqa: BLE001 - reset boundary fails below if dirty
            # 擦不干净的脏字段会导致下方 reset 边界失败，这里先行报告。
            return {"ok": False,
                    "error": f"could not scrub stale task field {name}"}
    owned = " ".join((PROJECT_ROOT, CURRICULUM_HANDOFF, CURRICULUM_NOTES,
                      ACTOR_HANDOFF, VERIFIER_REPORT,
                      LEARNING_DIAGNOSIS, ACTOR_EXECUTION_EVIDENCE,
                      "/home/user/.practice_original_project",
                      "/home/user/practice_target_candidate",
                      "/home/user/.memory"))
    # 枚举全部 RSI 自有客户机路径（项目根、各交接文件、证据目录、原项目、候选与记忆）。
    out = vm.run_command(
        f"rm -rf {owned}; echo RSI_CLEAN_RC=$?", timeout=120) or ""
    # 在客户机内一次性递归删除这些路径，并回显退出码（RSI_CLEAN_RC）。
    if "RSI_CLEAN_RC=0" not in out:
        # 清理未成功确认：拒绝把残留状态带进下一阶段。
        return {"ok": False, "error": "could not clean RSI-owned guest state"}
    # 全部清理确认成功：新边界就绪。
    return {"ok": True}


def _read_guest_text(vm, path: str, max_bytes: int | None = None) -> str:
    """精确取回客户机文件的 UTF-8 字节；无隐式大小上限，也不截断。"""

    # DesktopEnv 的文件下载器对缺失文件会重试三次、每次停 5 秒。原子发布探测
    # 在每个完成的 Agent 程序之后都会运行，若直接走下载器，探测一份尚未发布
    # 的报告会拖慢每次调查动作约 15 秒。只有当在线客户机明确确认"不存在非空
    # 普通文件"时才跳过下载器；命令通道结果不确定时仍回退到带重试的取回——
    # 传输的不确定绝不能制造假的"未发布"结论、进而触发又一次随机模型轮次。
    quoted = shlex.quote(path)
    # 对路径做 shell 安全引用。
    state = vm.run_command(
        "if test -f " + quoted + " && test -s " + quoted + "; then "
        "printf 'RSI_FILE_STATE=PRESENT'; else "
        "printf 'RSI_FILE_STATE=ABSENT'; fi",
        timeout=10, cap=128) or ""
    # 客户机内快速探测：文件存在且非空输出 PRESENT，否则 ABSENT（10 秒超时、128 字节截断）。
    if state.strip() == "RSI_FILE_STATE=ABSENT":
        # 客户机明确确认不存在非空文件：直接返回空串，跳过昂贵的重试下载器。
        return ""

    fetch = getattr(vm, "fetch_file", None)
    # 取 VM 适配层的文件取回能力（可能不存在）。
    if not callable(fetch):
        # 传输层不支持取文件：无法读取交接，报基础设施错误。
        raise PracticeInfrastructureError("VM transport cannot fetch handoff files")
    try:
        value = fetch(path, max_bytes=max_bytes)
        # 首选带 max_bytes 上限的取回签名。
    except TypeError:
        # 旧签名不接受 max_bytes：退回单参数调用。
        value = fetch(path)
    data = value[0] if isinstance(value, tuple) else value
    # 兼容返回 (数据, 元信息) 元组或直接返回数据的两种适配层。
    if data is None:
        # 显式 None 视为无内容。
        return ""
    if isinstance(data, str):
        # 已经是文本：原样返回（传输层自行解码过）。
        return data
    try:
        return bytes(data).decode("utf-8", errors="strict")
        # 字节流按 UTF-8 严格解码（errors=strict 保证交接文本无损可读）。
    except (TypeError, UnicodeDecodeError):
        # 解码失败按"无有效文本"处理，交由上层提示续传。
        return ""


def _remove_guest_file(vm, path: str) -> None:
    """删除客户机上的单个文件，并确认它确实消失（fail closed）。"""

    quoted = shlex.quote(path)
    # shell 安全引用目标路径。
    out = vm.run_command(
        f"rm -f -- {quoted}; test ! -e {quoted}; echo RSI_REMOVE_RC=$?",
        timeout=30) or ""
    # rm -f 后紧跟 test ! -e：确认文件真的不存在，退出码经 RSI_REMOVE_RC 回传。
    if "RSI_REMOVE_RC=0" not in out:
        # 删除或确认失败：残留交接会被误读，必须报基础设施错误。
        raise PracticeInfrastructureError(f"could not clear stale handoff {path}")


def _remove_incomplete_curriculum_project(vm) -> None:
    """丢弃未提交（未 declare done）的 Curriculum 段所留下的造题夹具。"""

    # 刻意不做通用递归删除助手：Curriculum 恰好只拥有这一个有界客户机根，
    # 任何未解析的路径或通配符都不允许扩大删除目标。
    quoted = shlex.quote(PROJECT_ROOT)
    # 只引用唯一有界的 PROJECT_ROOT 常量（无变量拼接、无通配符）。
    out = vm.run_command(
        f"rm -rf -- {quoted}; test ! -e {quoted}; "
        "echo RSI_REMOVE_TREE_RC=$?",
        timeout=120) or ""
    # rm -rf 后确认目录确实不存在，退出码经 RSI_REMOVE_TREE_RC 回传。
    if "RSI_REMOVE_TREE_RC=0" not in out:
        # 清不干净就报错：半成品夹具绝不能泄漏进下一个项目阶段。
        raise PracticeInfrastructureError(
            f"could not discard incomplete Curriculum project {PROJECT_ROOT}")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写 JSON：同目录临时文件 + fsync + os.replace，断电也不留半截文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    # 先确保父目录存在。
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent))
    # 在目标同目录建临时文件（同目录保证 replace 是同文件系统原子操作）。
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            # 缩进 + 键排序序列化：产物确定、可 diff、可复现。
            handle.write("\n")
            # 末尾补换行（POSIX 文本文件惯例）。
            handle.flush()
            os.fsync(handle.fileno())
            # 刷用户态缓冲并强制落盘：掉电后临时文件也是完整的。
        os.replace(temporary, path)
        # 原子替换：读者要么看到旧文件、要么看到完整新文件，绝无中间态。
        directory_fd = os.open(path.parent, os.O_RDONLY)
        # 打开父目录句柄：rename 之后还要把目录项本身刷盘。
        try:
            os.fsync(directory_fd)
            # fsync 父目录：让"指向新文件的目录项"在掉电后依然成立。
        finally:
            os.close(directory_fd)
            # 无论刷盘成败都关闭目录句柄。
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
            # 任何路径上失败都清掉临时文件，不留垃圾。


def _atomic_text(path: Path, text: str) -> None:
    """原子写文本：与 _atomic_json 同一套"临时文件 + replace"纪律（不 fsync 目录）。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    # 先确保父目录存在。
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=str(path.parent))
    # 同目录临时文件，保证后续 replace 的原子性。
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            # 写入完整文本。
            handle.flush()
            os.fsync(handle.fileno())
            # 刷缓冲并落盘，防半截文件。
        os.replace(temporary, path)
        # 原子替换为目标路径。
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
            # 失败路径上清理临时文件。


def _safe_memory_name(name: str) -> bool:
    """记忆文件相对路径白名单校验：非空、非绝对路径、各段不含空/./..。"""

    path = Path(name)
    # 把名字当作相对路径解析。
    return bool(name) and not path.is_absolute() and all(
        part not in {"", ".", ".."} for part in path.parts)
    # 三重校验：名字非空、不是绝对路径、路径段全部合法（防越界写、防目录穿越）。


def _read_memory_tree(path: str) -> dict[str, bytes]:
    """把整棵持久记忆树读成 {相对路径: 字节内容}；拒绝符号链接（防绕过审计）。"""

    root = Path(path)
    # 记忆根目录。
    if not root.exists():
        # 根不存在视为空记忆库（首次运行是合法状态）。
        return {}
    if root.is_symlink() or not root.is_dir():
        # 根必须是真实目录：符号链接或其他类型都 fail closed。
        raise PracticeInfrastructureError("durable memory root is not a real directory")
    files: dict[str, bytes] = {}
    # 结果容器：相对路径 -> 原始字节。
    for candidate in sorted(root.rglob("*")):
        # 排序遍历全部条目（确定性：同样的树给出同样的顺序）。
        if candidate.is_symlink():
            # 出现符号链接立即报错：记忆树必须是普通文件的封闭集合。
            raise PracticeInfrastructureError(
                f"durable memory contains a symlink: {candidate}")
        if candidate.is_file():
            files[str(candidate.relative_to(root))] = candidate.read_bytes()
            # 只收普通文件，键为相对根的路径字符串。
    return files
    # 返回完整记忆快照（字节级精确）。


# 【中文注释】_fsync_tree：把整棵树的文件与目录句柄强制刷盘（fsync）。
# 先刷文件（rglob 排序保证确定性），再自底向上刷目录（reverse=True），
# 最后刷根目录——这是"记忆提交持久化"的物理保障：断电后不丢页。
def _fsync_tree(root: Path) -> None:
    """把整棵树的文件与目录强制刷盘：先文件、再自底向上目录、最后根目录。"""

    for candidate in sorted(root.rglob("*")):
        # 第一遍：排序遍历所有条目。
        if candidate.is_file():
            with candidate.open("rb") as handle:
                os.fsync(handle.fileno())
                # 对每个普通文件 fsync：数据页落盘。
    for candidate in sorted(
            (p for p in root.rglob("*") if p.is_dir()), reverse=True):
        # 第二遍：目录按逆序（自底向上）——子目录先于父目录落盘。
        fd = os.open(candidate, os.O_RDONLY)
        # 打开目录句柄。
        try:
            os.fsync(fd)
            # fsync 目录：目录项（新文件名）落盘。
        finally:
            os.close(fd)
            # 确保句柄关闭。
    fd = os.open(root, os.O_RDONLY)
    # 最后处理根目录本身。
    try:
        os.fsync(fd)
        # 根目录也刷盘，保证整棵树的持久化边界完整。
    finally:
        os.close(fd)
        # 关闭根目录句柄。


def _atomic_install_memory(path: str, files: dict[str, bytes]) -> None:
    # 【中文注释】原子安装完整记忆树（同文件系统 rename 恢复法）——记忆写入的
    # 关键防线。流程：路径安全校验 → 暂存目录写全部文件并 fsync →
    # os.replace(旧目录→备份) → os.replace(暂存→目标)；第二步失败则把备份换回去
    # （记忆永不出现"半新半旧"状态），最后 fsync 父目录并清理备份。
    """以同文件系统 rename 恢复法安装完整记忆树（记忆写入的关键防线）。"""

    if any(not _safe_memory_name(name) for name in files):
        # 路径安全校验前置：任何一个不安全名字都整体拒绝。
        raise PracticeInfrastructureError("candidate memory contains an unsafe path")
    destination = Path(path)
    # 目标记忆根目录。
    destination.parent.mkdir(parents=True, exist_ok=True)
    # 确保父目录存在。
    if destination.is_symlink() or (destination.exists()
                                    and not destination.is_dir()):
        # 目标若是符号链接或非目录：不可被替换，fail closed。
        raise PracticeInfrastructureError("durable memory root is not replaceable")
    stage = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.stage-", dir=str(destination.parent)))
    # 暂存目录：与目标同父目录（同文件系统），保证 rename 原子。
    backup = destination.parent / (
        f".{destination.name}.backup-{os.getpid()}-{time.time_ns()}")
    # 备份路径：带进程号与纳秒时间戳，避免并发冲突。
    moved_old = False
    # 记录旧目录是否已挪到备份位。
    try:
        for name, data in sorted(files.items()):
            target = stage / name
            # 在暂存目录内按相对路径建文件。
            target.parent.mkdir(parents=True, exist_ok=True)
            # 先建父级子目录。
            target.write_bytes(data)
            # 写入精确字节。
        _fsync_tree(stage)
        # 暂存树整体刷盘：之后 rename 的内容是持久的。
        if destination.exists():
            os.replace(destination, backup)
            # 旧记忆先原子挪到备份位（第一步）。
            moved_old = True
            # 标记：旧目录现在只在备份位。
        try:
            os.replace(stage, destination)
            # 暂存目录原子改名成目标（第二步）：记忆瞬间整体切换到新树。
        except Exception:
            # 第二步失败：把备份换回原位，恢复到安装前状态。
            if moved_old and backup.exists() and not destination.exists():
                os.replace(backup, destination)
            raise
            # 恢复后继续抛出：调用方按基础设施失败处理。
        parent_fd = os.open(destination.parent, os.O_RDONLY)
        # 打开父目录：把两次 rename 的目录项变化刷盘。
        try:
            os.fsync(parent_fd)
            # fsync 父目录：掉电后新记忆的目录结构依然成立。
        finally:
            os.close(parent_fd)
            # 关闭父目录句柄。
        if backup.exists():
            shutil.rmtree(backup)
            # 安装成功：删除旧树备份。
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)
            # 无论成败都清理暂存目录（成功时已被改名走，通常不存在）。
        # 安装目标已存在时残留的备份只是清理残渣；若目标缺失，
        # 备份就是恢复状态，必须保留。
        if backup.exists() and destination.exists():
            shutil.rmtree(backup, ignore_errors=True)
            # 只在"新树已就位"的前提下才删备份：目标缺失时保留备份供恢复。


def _manifest(files: dict[str, bytes]) -> dict[str, dict[str, Any]]:
    """为记忆树生成清单：每个文件的大小与 sha256（按名字排序）。"""

    return {
        name: {"bytes": len(data),
               "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(files.items())
    }
    # 排序遍历：清单结构确定，可逐字节比较两棵树是否一致。


def _listing(files: dict[str, bytes]) -> str:
    """把清单渲染成"名字  大小B"的多行文本（给 Actor 的记忆目录视图）。"""

    return "\n".join(f"{name}  {len(data)}B"
                     for name, data in sorted(files.items()))
    # 每行一个文件：名字 + 字节数；排序保证同一棵树渲染结果稳定。


def _parse_handoff(text: str, token_pattern: re.Pattern[str]) \
        -> ParsedHandoff | None:
    """解析"首行即令牌"的交接：第一非空行必须完整匹配令牌模式，否则拒绝。"""

    lines = text.splitlines()
    # 按行拆开原文。
    first = next((index for index, line in enumerate(lines)
                  if line.strip()), None)
    # 找第一个非空行的下标（全空文本则 None）。
    if first is None:
        # 空文本：没有交接。
        return None
    token_line = lines[first].strip()
    # 候选令牌行：去首尾空白。
    match = token_pattern.fullmatch(token_line)
    # fullmatch：整行必须恰好是令牌（防止"令牌藏在长句里"的伪交接）。
    if not match:
        # 首行不是合法令牌：解析失败。
        return None
    body = "\n".join(lines[first + 1:]).strip()
    # 正文 = 令牌行之后的全部内容（去首尾空白）。
    return ParsedHandoff(match.group(1), body, text)
    # 返回令牌捕获组、正文与原文。


def _parse_unique_handoff(text: str, token_pattern: re.Pattern[str]) \
        -> ParsedHandoff | None:
    """在 Markdown 代码围栏之外寻找唯一无歧义的独立令牌。

    Verifier 报告刻意是自由格式的。曾要求把 Agent 自有的判定移到自然标题
    之上，结果导致一份正确的终局报告被重新采样并悄悄反转。这个解析器只
    识别既有的词法令牌：不改一个字节，也不评判报告的语义。
    """

    lines = text.splitlines()
    # 逐行扫描。
    matches: list[tuple[int, re.Match[str]]] = []
    # 收集（行号, 匹配）——最终必须恰好一个才算无歧义。
    fence: str | None = None
    # 当前所处的代码围栏标记（None=不在围栏内）。
    for index, line in enumerate(lines):
        # 逐行处理。
        stripped = line.strip()
        # 行首尾空白剥离后的内容。
        if stripped.startswith(("```", "~~~")):
            # 围栏开/闭行：根据标记切换状态。
            marker = stripped[:3]
            # 取前三个字符作为围栏类型。
            if fence is None:
                fence = marker
                # 开围栏。
            elif marker == fence:
                fence = None
                # 同类型围栏闭合。
            continue
            # 围栏行本身绝不参与令牌匹配。
        if fence is not None or line.startswith(("\t", "    ")):
            continue
            # 围栏内或缩进代码块（4 空格/制表符）里的行不算令牌位置。
        match = token_pattern.fullmatch(stripped)
        # 该行整体匹配令牌模式？
        if match:
            matches.append((index, match))
            # 记录一个候选令牌。
    if len(matches) != 1:
        # 零个或多个令牌都视为歧义：拒绝解析（宁可不判定，也不猜）。
        return None
    index, match = matches[0]
    # 取唯一匹配。
    body = "\n".join(lines[:index] + lines[index + 1:]).strip()
    # 正文 = 除令牌行外的全部内容：令牌可以在报告任何位置，保持报告原貌。
    return ParsedHandoff(match.group(1), body, text)
    # 返回令牌、正文与原文。


def _phase_cfg(base_cfg, required_path: str = ""):
    """派生"练习阶段"配置副本：打开通用 Agent 自有传输的六个开关。"""

    cfg = copy.deepcopy(base_cfg)
    # 深拷贝基础配置：阶段开关只落在副本上，原始角色配置保持可复用。
    # 这些开关定义通用的 Agent 自有传输；模型身份、采样、工具访问与紧急上限
    # （watchdog）仍保持原配置不变。
    cfg.practice_mode = True
    # 练习模式：走本生命周期约定的交接/令牌传输。
    cfg.independent_verify = False
    # 关闭主循环内置的独立验证分支：判定完全交给外部 Verifier 角色。
    cfg.agent_decided_stop = True
    # 终止由 Agent 自己 declare done 决定（上限只作紧急看门狗）。
    cfg.practice_done_requires = required_path
    # declare done 时要求存在的发布路径（如交接文件），由各阶段传入。
    cfg.history_keep_pairs = 0
    # 不做历史对压缩：同一角色的上下文完整保留。
    cfg.max_resumes = 0
    # 禁止跨进程续跑：段间续传由本模块的同上下文续传机制接管。
    return cfg
    # 返回派生配置。


def _curriculum_runtime_contract() -> str:
    """Curriculum 的运行时所有权边界条款（追加在章程之后的纯文本提示）。"""

    # 界定 PROJECT_ROOT 的"发布集"语义：只造非解题输入不造答案、根必须存在
    # 且非空、持久交付物的目的地都必须在根内、临时中间物放 /tmp、宣布 PROJECT
    # 前自行清点并清走建筑垃圾。这是传输/隔离边界，不是成功标准；
    # 下面的 f-string 正文是提示词内容，一字不改。
    return f"""

GENERIC RUNTIME OWNERSHIP BOUNDARY:
For DECISION: PROJECT, create all non-solution inputs, raw materials, references,
and blank workspace for the proposed experience inside {PROJECT_ROOT}. The owned
root must exist and contain at least one file when you declare done, and the
natural-language project handoff must name {PROJECT_ROOT}. The root must not
contain your own completed implementation of the Actor Agent's requested
deliverable. Treat this root as the exact solver-visible publication set, not as
construction scratch space. Every persistent or final Actor Agent deliverable
required by the natural-language project, including any additional saved or
exported copy, must have its destination inside {PROJECT_ROOT}; never require a
persistent or final deliverable at any path outside that root. You remain free to
choose every filename and layout within the root. The harness snapshots and
replays only this root. Create disposable fixture-building intermediates outside
it (for example under /tmp). Before declaring PROJECT, recursively inspect the
root and use your judgment to remove or relocate accidental recipes, partial
assemblies, caches, probes, and other construction debris that the natural-language
project does not intentionally supply to the Actor Agent. This is a
transport/isolation boundary, not a success criterion.
"""


def _actor_runtime_contract() -> str:
    """Actor 的运行时所有权边界条款（追加在章程之后的纯文本提示）。"""

    # 界定 Actor 的项目树纪律：可回放的项目状态与交付物都在 PROJECT_ROOT 内
    # 且保持非空；一次性抽取/缓存/探针等放在 /tmp 并在终局交接前清走；不得删
    # 供给输入；工作期间 ~/.memory 是只读的规范经验，只有终局记忆蒸馏阶段才能
    # 晋升记忆。f-string 正文是提示词内容，一字不改。
    return f"""

GENERIC RUNTIME OWNERSHIP BOUNDARY:
The complete replayable project tree is {PROJECT_ROOT}. Keep supplied project
inputs, editable project state, required project-local dependencies, and final
deliverables inside that root and leave it nonempty. Put disposable application
extractions, caches, reference-frame dumps, probes, and parameter-sweep artifacts
outside it (for example under /tmp), and remove any such debugging debris you
accidentally created inside the root before your terminal handoff. Do not remove
supplied inputs or anything required to replay the submitted project. The harness
snapshots only this root; it does not judge the contents. During project work,
~/.memory is canonical read-only experience: any work-phase changes to it are
discarded. Only the later terminal memory-distillation phase can promote memory.
"""


def _continuation_after_transport(role: str, path: str, detail: str) -> str:
    """传输修复续传提示：交接缺失/不可解析时让同一上下文的 Agent 自行修正。"""

    # 用途：已判定 done 但目标路径上没有可用终局交接时的同上下文续传文本。
    # 关键语义：这不是正确性评分，不改变角色与决策，也没有固定重试次数；
    # f-string 正文是提示词内容，一字不改。
    return f"""{role} AGENT — SAME-CONTEXT TRANSPORT CONTINUATION.

The prior turn did not leave a usable terminal handoff at {path}: {detail}.
This is not a correctness grade and does not change your role or decision. Continue
in this same context, correct the transport/workspace issue, and declare done only
after the complete current handoff is ready. No fixed retry count applies."""


def _continue_after_stall(role: str, path: str = "") -> str:
    """停滞续传提示：上一段没有终局决定时，同上下文继续当前阶段。"""

    suffix = (f" and leave the complete terminal handoff at {path}"
              if path else "")
    # 可选后缀：有目标路径时提醒 Agent 顺带留下完整终局交接。
    # 关键语义：停滞不被当作提交/判定/收敛/学习失败；f-string 正文一字不改。
    return f"""{role} AGENT — SAME-CONTEXT CONTINUATION.

The previous segment ended without your terminal decision. This was not treated
as a submission, verdict, convergence, or learning failure. Continue your current
phase in the same context{suffix}; declare done when you judge the phase complete.

{PLAIN_JSON_TRANSPORT_NOTE}"""


def _continue_after_incomplete_curriculum(path: str) -> str:
    """Curriculum 专用的"未完成即作废"续传提示。"""

    # 与普通停滞续传的区别：Curriculum 段在宣布完成前停滞/耗尽预算时，其草稿
    # 交接与不完整夹具树都会被丢弃（上方 helper 已删除），Agent 须在同一上下文
    # 里重建夹具树并重新发布终局交接；f-string 正文一字不改。
    return f"""CURRICULUM AGENT — SAME-CONTEXT CONTINUATION.

The previous segment stalled or exhausted its transport budget before you
declared the phase complete. It was not accepted as a Curriculum decision.
Any draft handoff at {path} and incomplete fixture tree at {PROJECT_ROOT} were
discarded. Continue your investigation in the same context. Recreate the
fixture tree and publish the terminal handoff only when you judge the complete
project decision ready, then declare done.

{PLAIN_JSON_TRANSPORT_NOTE}"""


def _audit_prompt(hooks: "PracticeHooks", text: str, target: str,
                  mode: str = "practice") -> None:
    """对将要进入角色提示词的文本做目标边界审计，命中即抛边界错误。"""

    hits = hooks.audit_text(
        text, mode=mode, authorized_instruction=target)
    # 用防泄漏检测器扫描文本（授权指令=target，作为误报豁免的对照）。
    if hits:
        # 有命中：这段文本不得进入任何角色提示词。
        raise PracticeBoundaryError("an Agent prompt failed the target boundary")


def _audit_agent_artifacts(hooks: "PracticeHooks", sink_dir: str,
                           target: str, mode: str = "practice") -> None:
    """对 Agent 轨迹工件做目标边界审计；命中则落盘隔离报告并抛边界错误。"""

    hits = hooks.audit_transcripts(
        sink_dir, mode=mode, authorized_instruction=target)
        # 扫描该段落盘的完整轨迹（比单条提示更全面的事后审计）。
    if hits:
        # 检测器的确切记录只留在宿主轨迹旁；绝不能把命中内容放进角色
        # 提示词、记忆或异常消息（否则泄漏会被二次传播）。
        report_path = Path(sink_dir) / "boundary_audit.json"
        # 隔离报告的落盘位置（与轨迹同目录）。
        reason = "an Agent transcript failed the target boundary"
        # 统一的拒绝原因文本。
        try:
            _atomic_json(report_path, {
                "schema_version": 1, "status": "quarantined",
                "surface": "agent_transcripts", "mode": mode, "hits": hits,
            })
            # 原子写入隔离报告：记录命中的确切检测器结果（仅宿主侧可见）。
        except OSError as exc:
            # 可选诊断落盘失败不能把已检出的边界违规降级成可重试的
            # 基础设施结果——违规必须保持违规。
            raise PracticeBoundaryError(
                f"{reason}; host audit report could not be written "
                f"({type(exc).__name__})") from exc
            # 转成带原因链的边界错误（信息只含异常类型名，不含命中内容）。
        raise PracticeBoundaryError(f"{reason}; host audit report: {report_path}")
        # 正常路径：抛错并指出隔离报告位置，供基础设施层审阅。


def _run_handoff_phase(
        *, hooks: "PracticeHooks", vm, cfg, prompt: str, role: str,
        handoff_path: str, token_pattern: re.Pattern[str], sink_root: str,
        target: str, history: list[dict[str, Any]] | None = None,
        continuation: bool = False,
        handoff_parser: Callable[[str, re.Pattern[str]],
                                 ParsedHandoff | None] = _parse_handoff,
        commit_on_publish: bool = False,
        require_completed_publication: bool = False,
        bounded_transport: bool = False,
        audit_mode: str = "practice") \
        -> tuple[ParsedHandoff, list[dict[str, Any]], Any]:
    """一直运行，直到 Agent 自己留下一个有效的传输令牌（transport token）。

    这是生命周期的通用"交接阶段"执行器：反复跑 run_attempt 段，读回交接
    文件、审计并解析令牌；传输不通就同上下文续传，直到拿到合法令牌或触发
    基础设施守卫。宿主永远不替 Agent 做语义决定。
    """

    current_history = list(history or [])
    # 当前会话历史（拷贝入参，避免跨段共享可变列表）。
    current_prompt = prompt
    # 当前提示词（每轮按续传原因重写）。
    continuing = continuation or bool(current_history)
    # 是否为同上下文续传：显式传入，或已有历史即视为续传。
    segment = 0
    # 段编号（决定 sink 子目录名）。
    actionless_segments = 0
    # 连续"无可执行进展"的段数（无程序、无 Look）。
    used_iters = 0
    # 累计消耗的迭代预算。
    used_wall = 0.0
    # 累计消耗的墙钟预算（秒）。
    while True:
        # 主循环：每轮一段。退出条件只有三个——合法令牌、连续无进展、紧急状态。
        _remove_guest_file(vm, handoff_path)
        # 段开始前清掉旧交接：杜绝把上一段的残留文件误读为本段发布。
        _audit_prompt(hooks, current_prompt, target, mode=audit_mode)
        # 提示词先过边界审计：泄漏内容绝不进入角色上下文。
        sink_dir = os.path.join(sink_root, f"segment_{segment:03d}")
        # 本段轨迹目录（segment_000、segment_001…）。
        while os.path.exists(sink_dir):
            segment += 1
            sink_dir = os.path.join(sink_root, f"segment_{segment:03d}")
            # 目录已存在则顺延编号：断点续跑时不覆盖历史轨迹。
        phase_cfg = _phase_cfg(cfg, handoff_path)
        # 派生练习配置：done 时要求 handoff_path 上有发布物。
        budgets = {}
        # 预算参数包：默认不限（单段自由），受限模式下传剩余预算。
        if require_completed_publication or bounded_transport:
            # 受限传输模式（如 Curriculum 造题）：跨段共享总预算。
            remaining_iters = phase_cfg.max_iters - used_iters
            # 剩余迭代 = 上限 - 已用。
            remaining_wall = phase_cfg.wall_clock_secs - used_wall
            # 剩余墙钟 = 上限 - 已用。
            if remaining_iters <= 0 or remaining_wall <= 0:
                # 累计预算耗尽：这是传输预算（infrastructure），不是语义结论。
                raise PracticeInfrastructureError(
                    f"{role} handoff exhausted its cumulative transport budget")
            budgets = dict(iters_budget=remaining_iters,
                           wall_budget=remaining_wall)
            # 把剩余预算下传给 run_attempt，压住跨段总量。
        terminal_probe = None
        # 终局提交探针：默认无（run_attempt 按自身规则判定结束）。
        if commit_on_publish:
            terminal_probe = lambda: handoff_parser(
                hooks.read_guest_text(vm, handoff_path), token_pattern
            ) is not None
            # 探针：交接文件一旦出现可解析令牌即视为 Agent 已终局提交——
            # 原子发布先于状态判定，防止多采样一次把已发布的判定改写。
        result, current_history = hooks.run_attempt(
            current_prompt, vm, phase_cfg, hooks.sink_factory(sink_dir),
            initial_history=current_history,
            continue_context=continuing,
            terminal_handoff_ready=terminal_probe, **budgets)
        # 跑一段：注入提示词、VM、配置、轨迹接收器、历史与续传标志。
        if require_completed_publication or bounded_transport:
            # 受限模式下记账。
            used_iters += int(getattr(result, "iters", 0))
            # 累加本段消耗的迭代。
            used_wall += float(getattr(result, "wall_secs", 0.0))
            # 累加本段消耗的墙钟。
            actionless_segments = (
                0 if (getattr(result, "programs_run", 0)
                      or getattr(result, "looks", 0))
                else actionless_segments + 1)
            # 本段有程序执行或 Look 观察则清零计数，否则加一（防"空转循环"）。
            if dataclasses.is_dataclass(result):
                _atomic_json(Path(sink_dir) / "result.json",
                             dataclasses.asdict(result))
                # 把段结果原样落盘（供恢复工具与审计复现）。
        _audit_agent_artifacts(
            hooks, sink_dir, target, mode=audit_mode)
        # 段后轨迹审计：泄漏内容一旦落盘立即隔离。
        status = str(getattr(result, "status", "infra"))
        # 读取本段终态（缺省按基础设施处理）。
        # 先读已发布的交接、再解释循环状态：有效的规范发布是 Agent 的终局
        # 动作，即使同时撞上停滞/预算边界依然有效；不允许再采样一次把它改写。
        text = hooks.read_guest_text(vm, handoff_path)
        # 读回客户机上的交接文件（精确字节）。
        if text:
            hits = hooks.audit_text(
                text, mode=audit_mode, authorized_instruction=target)
            # 交接文本也要过边界审计。
            if hits:
                raise PracticeBoundaryError(
                    f"{role} handoff failed the target boundary")
                # 交接泄漏：拒绝接收，进入隔离。
        parsed = handoff_parser(text, token_pattern)
        # 按阶段选择的解析器解析令牌（普通交接或唯一令牌解析）。
        if (parsed is not None
                and (not require_completed_publication or status == "done")):
            return parsed, current_history, result
            # 成功出口：拿到合法令牌；受限模式还要求段状态确为 done
            # （草稿不能提交生命周期转换）。
        if (require_completed_publication or bounded_transport) and actionless_segments >= 3:
            # 连续三段无可执行进展：运行时必须介入恢复，不能空转烧预算。
            reason = (f"{role} handoff made no executable progress in "
                      f"{actionless_segments} consecutive segments; "
                      "runtime recovery is required")
            # 记录拒绝原因（含连续无进展段数）。
            _atomic_json(Path(sink_root) / "transport_stop.json", {
                "status": "infra", "reason": reason,
                "last_segment": Path(sink_dir).name,
                "actionless_segments": actionless_segments,
                "iters": used_iters, "wall_secs": used_wall,
                "semantic_decision": None,
            })
            # 落盘停止记录：semantic_decision 显式为 None（宿主未做语义判定）。
            raise PracticeInfrastructureError(reason)
            # 抛基础设施错误：交由上层按可恢复事件处理。
        if (require_completed_publication
                and status in {"done", "stalled", "stalled_quiescent",
                               "budget"}):
            # Curriculum 可能在夹具尚未就绪时就写好交接草稿。与原子发布的
            # Verifier 判定不同：除非 Agent 同时完成了本段，草稿不能提交
            # 生命周期转换。在干净发布工作区上、同一模型上下文内重试。
            _remove_guest_file(vm, handoff_path)
            # 清掉草稿交接。
            _remove_incomplete_curriculum_project(vm)
            # 清掉不完整的夹具树：下次续传从干净状态重建。
        if status == "done":
            current_prompt = _continuation_after_transport(
                role, handoff_path,
                "it was missing, non-UTF-8, or did not contain an allowed "
                "unambiguous transport token")
            # 段已 done 但交接不可用：发传输修复提示，同上下文再试。
        elif status in {"stalled", "stalled_quiescent", "budget"}:
            current_prompt = (
                _continue_after_incomplete_curriculum(handoff_path)
                if require_completed_publication
                else _continue_after_stall(role, handoff_path))
            # 停滞/静默停滞/预算：按是否受限模式选择对应续传提示。
        else:
            raise PracticeInfrastructureError(
                f"{role} phase ended at emergency/infrastructure status {status}")
            # 紧急/未知状态：不可恢复为传输事件，抛错上报。
        continuing = True
        # 之后的每一轮都是同上下文续传。
        segment += 1
        # 段号推进，进入下一轮。


def _run_free_phase(
        *, hooks: "PracticeHooks", vm, cfg, prompt: str, role: str,
        sink_root: str, target: str, history: list[dict[str, Any]],
        required_path: str = "", audit_mode: str = "practice") \
        -> tuple[list[dict[str, Any]], Any]:
    """运行一个不设上限的同上下文阶段：唯一出口是 Agent 自己 declare done。"""

    current_history = list(history)
    # 拷贝会话历史。
    current_prompt = prompt
    # 当前提示词（停滞续传时重写）。
    segment = 0
    # 段编号。
    while True:
        # 主循环：自由阶段没有令牌判定，只看段状态。
        _audit_prompt(hooks, current_prompt, target, mode=audit_mode)
        # 提示词过边界审计。
        sink_dir = os.path.join(sink_root, f"segment_{segment:03d}")
        # 本段轨迹目录。
        while os.path.exists(sink_dir):
            segment += 1
            sink_dir = os.path.join(sink_root, f"segment_{segment:03d}")
            # 目录已占用则顺延，不覆盖历史轨迹。
        result, current_history = hooks.run_attempt(
            current_prompt, vm, _phase_cfg(cfg, required_path),
            hooks.sink_factory(sink_dir), initial_history=current_history,
            continue_context=True, allow_noop_done=True)
        # 跑一段：始终同上下文续传；allow_noop_done 允许"无动作也判完成"
        # （记忆整理这类纯思考阶段可能不需要执行程序）。
        _audit_agent_artifacts(
            hooks, sink_dir, target, mode=audit_mode)
        # 段后轨迹审计。
        status = str(getattr(result, "status", "infra"))
        # 本段终态。
        if status == "done":
            return current_history, result
            # 唯一正常出口：Agent 自己判完成。
        if status in {"stalled", "stalled_quiescent", "budget"}:
            current_prompt = _continue_after_stall(role)
            segment += 1
            continue
            # 停滞/预算：换续传提示继续（阶段无上限，不算失败）。
        raise PracticeInfrastructureError(
            f"{role} phase ended at emergency/infrastructure status {status}")
        # 紧急状态：上报基础设施错误。


def _run_learning_diagnosis(
        *, hooks: "PracticeHooks", vm, cfg, terminal_outcome: str,
        verifier_report: str, sink_root: str, target: str,
        history: list[dict[str, Any]], audit_mode: str = "practice") \
        -> tuple[str, list[dict[str, Any]]]:
    """发布 Actor 的自由格式因果交接，不施加任何模式（schema）。"""

    current_history = list(history)
    # 拷贝会话历史。
    prompt = self_evolving_actor_learning_diagnosis_msg(
        terminal_outcome, verifier_report, LEARNING_DIAGNOSIS)
    # 由 charter 构造学习诊断提示（终局结果 + Verifier 报告 + 目标路径）。
    segment = 0
    # 尝试编号（决定 sink 子目录）。
    while True:
        # 循环直到拿到非空且过审的诊断文本。
        _remove_guest_file(vm, LEARNING_DIAGNOSIS)
        # 先清掉旧诊断文件，防误读上一轮残留。
        current_history, _ = _run_free_phase(
            hooks=hooks, vm=vm, cfg=cfg, prompt=prompt, role="ACTOR",
            sink_root=os.path.join(sink_root, f"attempt_{segment:03d}"),
            target=target, history=current_history,
            required_path=LEARNING_DIAGNOSIS, audit_mode=audit_mode)
        # 跑一个自由阶段：done 的前提是诊断文件已发布。
        diagnosis = hooks.read_guest_text(vm, LEARNING_DIAGNOSIS)
        # 读回诊断全文。
        if diagnosis.strip():
            # 非空才算有效产出。
            if hooks.audit_text(
                    diagnosis, mode=audit_mode,
                    authorized_instruction=target):
                raise PracticeBoundaryError(
                    "Actor learning diagnosis failed the target boundary")
                # 诊断泄漏：隔离，绝不进入记忆。
            return diagnosis, current_history
            # 成功：返回诊断文本与延续的会话历史。
        prompt = _continuation_after_transport(
            "ACTOR", LEARNING_DIAGNOSIS,
            "the free-form diagnosis was missing, empty, or non-UTF-8")
        # 诊断缺失/空/非 UTF-8：发传输修复提示再来一轮。
        segment += 1
        # 尝试编号推进。


def _fresh_vm(hooks: "PracticeHooks", vm, target: str) -> None:
    """把 VM 重置到全新空任务边界；失败即抛基础设施错误（fail closed）。"""

    result = hooks.reset_vm(vm, target)
    # 执行重置（经钩子注入，便于测试与恢复工具替换）。
    if not result.get("ok"):
        raise PracticeInfrastructureError(
            "fresh VM boundary failed: " + str(result.get("error", "unknown")))
        # 重置不成功绝不带病继续。


def _replay(hooks: "PracticeHooks", vm, snapshot_dir: str) -> None:
    """把宿主快照精确回放进新 VM；任何不一致都视为基础设施失败。"""

    result = hooks.replay_project(vm, snapshot_dir)
    # 执行回放（字节级校验由 provisioning 层负责）。
    if not result.get("ok"):
        raise PracticeInfrastructureError(
            "project snapshot replay failed: "
            + str(result.get("error") or result.get("mismatches") or "unknown"))
        # 回放失败（错误或逐文件不匹配清单）即抛错。


def _verify_candidate(hooks: "PracticeHooks", vm, snapshot_dir: str) -> None:
    """活性候选文件与 Actor 快照不一致时 fail closed。"""
    result = hooks.verify_project(vm, snapshot_dir)
    # 对照快照清单逐字节校验当前客户机状态。
    if not result.get("ok"):
        raise PracticeInfrastructureError(
            "candidate snapshot integrity verification failed: "
            + str(result.get("error") or result.get("mismatches") or
                  "unknown"))
        # 校验失败：判定依据可能被动过，必须报错而不是继续。


def _push_canonical_memory(hooks: "PracticeHooks", vm,
                           memory_dir: str) -> None:
    """把规范记忆推送到 VM 的只读证据位；推送失败即报错。"""

    if not hooks.push_memory(vm, memory_dir):
        raise PracticeInfrastructureError("canonical memory push failed")
        # 推送是角色观察记忆的唯一通道，失败必须显式暴露。


def _hashed_regular_tree(root: Path) -> dict[str, str]:
    """精确描述一棵普通文件/目录树（含哈希），绝不跟随符号链接。"""

    if root.is_symlink() or not root.is_dir():
        # 根必须是真实目录：符号链接或其他类型直接 fail closed。
        raise PracticeInfrastructureError("evidence view is not a real directory")
    files: dict[str, str] = {}
    # 结果：{带类型前缀的相对路径: 哈希或空}。
    for candidate in sorted(root.rglob("*")):
        # 排序遍历（确定性比较的基础）。
        if candidate.is_symlink():
            # 出现符号链接立即拒绝：证据视图必须是封闭的普通条目集合。
            raise PracticeInfrastructureError("evidence view contains a symlink")
        relative = candidate.relative_to(root).as_posix()
        # 相对路径统一 POSIX 风格。
        if candidate.is_file():
            files[f"file:{relative}"] = hashlib.sha256(
                candidate.read_bytes()).hexdigest()
            # 普通文件：记录 sha256。
        elif candidate.is_dir():
            files[f"dir:{relative}"] = ""
            # 目录：哈希位留空（只记录结构）。
        else:
            raise PracticeInfrastructureError(
                "evidence view contains a non-regular entry")
            # 其他类型（设备、套接字等）不允许出现在证据视图里。
    return files
    # 返回可比较的树指纹字典。


def _build_actor_execution_evidence(
        actor_root: str | Path, through_attempt: int,
        output_dir: str | Path) -> dict[str, Any]:
    """把可得的 Actor 动作记录精确复制成持久的宿主视图。

    该视图刻意排除模型轮次、轨迹、工作日志与宿主判定；只包含确切提交的
    程序、返回的 trace 及其传输元数据，外加 Look 请求。保留原始的
    attempt/segment/iteration 路径，让 Verifier 能自行推理时序，
    而不需要宿主编写的语义摘要。
    """

    if through_attempt < 1:
        # 至少要有一次尝试才有执行证据。
        raise PracticeInfrastructureError(
            "Actor execution evidence requires at least one attempt")
    source_root = Path(actor_root)
    # 源：episode 的 actor 工件根目录。
    destination = Path(output_dir)
    # 目的地：宿主侧证据目录。
    if source_root.is_symlink() or not source_root.is_dir():
        # 源必须是真实目录（防符号链接绕过白名单）。
        raise PracticeInfrastructureError(
            "Actor artifact root is not a real directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # 确保目的地父目录存在。
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.", dir=str(destination.parent)))
    # 同父目录暂存目录：先建好完整视图再原子切换。
    entries: list[tuple[str, str, int]] = []
    # 收集（相对路径, sha256, 字节数）。
    try:
        for attempt_index in range(1, through_attempt + 1):
            # 按时间顺序遍历 attempt_001..attempt_N（时序即证据）。
            attempt = source_root / f"attempt_{attempt_index:03d}"
            # 该次尝试的工件目录。
            if attempt.is_symlink() or not attempt.is_dir():
                # 缺失或不是真实目录：时间线断裂，fail closed。
                raise PracticeInfrastructureError(
                    f"Actor artifact attempt is missing: {attempt.name}")
            for walk_root, dirnames, filenames in os.walk(
                    attempt, followlinks=False):
                # 不跟随符号链接地遍历该尝试的全部文件。
                walk_path = Path(walk_root)
                # 当前遍历目录。
                for dirname in dirnames:
                    if (walk_path / dirname).is_symlink():
                        raise PracticeInfrastructureError(
                            "Actor artifacts contain a directory symlink")
                        # 目录符号链接：拒绝（防越界收集）。
                for filename in sorted(filenames):
                    # 逐个文件（排序保证清单稳定）。
                    source = walk_path / filename
                    # 源文件路径。
                    if source.is_symlink():
                        raise PracticeInfrastructureError(
                            "Actor artifacts contain a file symlink")
                        # 文件符号链接：拒绝。
                    if filename not in _ACTOR_EXECUTION_FILENAMES:
                        continue
                        # 不在白名单（程序/trace/元数据/look）里的文件一律跳过。
                    if not source.is_file():
                        raise PracticeInfrastructureError(
                            "Actor execution artifact is not a regular file")
                        # 白名单名字却不是普通文件：结构异常。
                    relative = source.relative_to(source_root)
                    # 保留 attempt/segment/iteration 的原始相对路径（时序信息）。
                    target = temporary / relative
                    # 暂存区中的对应位置。
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # 建好父级目录。
                    shutil.copyfile(source, target)
                    # 精确复制文件内容。
                    data = target.read_bytes()
                    # 读回副本字节（以副本为准计算指纹）。
                    entries.append((
                        relative.as_posix(), hashlib.sha256(data).hexdigest(),
                        len(data)))
                    # 登记：路径、sha256、大小。

        manifest = "".join(
            f"{digest}  {name}\n"
            for name, digest, _size in sorted(entries))
        # 生成 sha256sum 兼容格式的清单文本（按名字排序）。
        _atomic_text(temporary / _ACTOR_EXECUTION_MANIFEST, manifest)
        # 清单原子写入暂存区。
        _fsync_tree(temporary)
        # 暂存区整体刷盘。
        if os.path.lexists(destination):
            if _hashed_regular_tree(destination) != \
                    _hashed_regular_tree(temporary):
                raise PracticeInfrastructureError(
                    "existing Actor execution evidence does not match "
                    "the current attempt chronology")
            # 目的地已存在：新旧树指纹必须一致（同一时间线的重放），否则报错。
        else:
            os.replace(temporary, destination)
            # 目的地不存在：暂存区原子改名就位。
            parent_fd = os.open(destination.parent, os.O_RDONLY)
            # 打开父目录。
            try:
                os.fsync(parent_fd)
                # 刷父目录项：掉电后视图依然成立。
            finally:
                os.close(parent_fd)
                # 关闭句柄。
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
            # 任何路径上失败都清掉暂存区（成功时已被改名走）。

    manifest_bytes = (destination / _ACTOR_EXECUTION_MANIFEST).read_bytes()
    # 读取就位后的清单字节。
    return {
        "path": str(destination),
        # 视图路径（宿主侧）。
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        # 清单自身的 sha256（后续客户机校验的锚点）。
        "files": len(entries),
        # 收编文件数。
        "bytes": sum(size for _name, _digest, size in entries),
        # 总字节数。
    }


def _verify_actor_execution_evidence(
        vm, evidence: dict[str, Any],
        guest_path: str = ACTOR_EXECUTION_EVIDENCE) -> None:
    """客户机视图与宿主清单不一致时 fail closed（在客户机内跑哈希校验脚本）。"""

    expected = str(evidence["manifest_sha256"])
    # 期望值：宿主侧算好的清单 sha256。
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        # 格式校验：必须是 64 位小写十六进制，防止把畸形值传进客户机脚本。
        raise PracticeInfrastructureError(
            "Actor execution evidence manifest hash is malformed")
    verifier = """import hashlib
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
expected_manifest_sha = sys.argv[2]
manifest = root / "SHA256SUMS"
if root.is_symlink() or not root.is_dir():
    raise SystemExit(2)
if manifest.is_symlink() or not manifest.is_file():
    raise SystemExit(3)
raw = manifest.read_bytes()
if hashlib.sha256(raw).hexdigest() != expected_manifest_sha:
    raise SystemExit(4)

expected = {}
for line in raw.decode("utf-8", errors="strict").splitlines():
    if len(line) < 67 or line[64:66] != "  ":
        raise SystemExit(5)
    digest, name = line[:64], line[66:]
    relative = pathlib.PurePosixPath(name)
    if (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
            or not name or relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or name == "SHA256SUMS" or name in expected):
        raise SystemExit(6)
    expected[name] = digest

actual_files = {}
actual_dirs = set()
for item in root.rglob("*"):
    if item.is_symlink():
        raise SystemExit(7)
    name = item.relative_to(root).as_posix()
    if item.is_file():
        if name != "SHA256SUMS":
            actual_files[name] = hashlib.sha256(item.read_bytes()).hexdigest()
    elif item.is_dir():
        actual_dirs.add(name)
    else:
        raise SystemExit(8)

expected_dirs = set()
for name in expected:
    parent = pathlib.PurePosixPath(name).parent
    while str(parent) != ".":
        expected_dirs.add(parent.as_posix())
        parent = parent.parent
if actual_files != expected or actual_dirs != expected_dirs:
    raise SystemExit(9)
"""
    root = shlex.quote(guest_path)
    # 引用客户机上的证据根路径。
    out = vm.run_command(
        f"python3 -c {shlex.quote(verifier)} {root} "
        f"{shlex.quote(expected)}; "
        "echo RSI_ACTOR_EVIDENCE_RC=$?",
        timeout=120) or ""
    # 把校验脚本注入客户机执行：对照清单逐字节核对视图（退出码经哨兵回传）。
    if "RSI_ACTOR_EVIDENCE_RC=0" not in out:
        raise PracticeInfrastructureError(
            "Actor execution evidence failed guest hash verification")
        # 校验不过：客户机上的执行证据已不可信，判定失去依据。


def _push_actor_execution_evidence(
        hooks: "PracticeHooks", vm, evidence: dict[str, Any]) -> None:
    """传输宿主自有的执行证据视图到客户机，并做逐字节校验。"""

    if not hooks.push_dir(
            vm, str(evidence["path"]), ACTOR_EXECUTION_EVIDENCE):
        raise PracticeInfrastructureError("Actor execution evidence push failed")
        # 推送失败：Verifier 将缺少因果动作证据，必须显式报错。
    _verify_actor_execution_evidence(vm, evidence)
    # 推完即验：确认客户机上的副本与宿主清单逐字节一致。


def _capture_owned_tree(hooks: "PracticeHooks", vm, key: str,
                        host_dir: str, target: str) -> None:
    """捕获客户机上的自有项目根到宿主，并对其做目标边界审计。"""

    result = hooks.capture_project(
        vm, key, host_dir, guest_dirs=[PROJECT_ROOT_NAME])
    # 只捕获 PROJECT_ROOT_NAME 这一个有界目录（快照/审计的对象）。
    if not result.get("ok"):
        raise PracticeInfrastructureError(
            "owned project root was absent, empty, or could not be captured")
        # 根缺失/为空/捕获失败：发布集承诺被打破，按基础设施错误处理。
    findings = hooks.audit_captured(
        host_dir, authorized_instruction=target)
    # 对捕获下来的材料做防泄漏审计。
    if findings:
        raise PracticeBoundaryError("captured project tree failed the target boundary")
        # 命中泄漏：捕获物进入隔离，不得回放给下一角色。


def _pull_terminal_memory(hooks: "PracticeHooks", vm) -> dict[str, bytes]:
    """拉取终局记忆并多重校验（结构/路径安全/数量对账），宁可失败不猜。"""

    files = hooks.pull_memory(vm)
    # 从客户机拉回整个记忆树。
    if not isinstance(files, dict) or any(
            not isinstance(name, str) or not isinstance(data, bytes)
            for name, data in files.items()):
        raise PracticeInfrastructureError("terminal memory pull was malformed")
        # 结构校验：必须是 {str: bytes}，否则视为传输损坏。
    if any(not _safe_memory_name(name) for name in files):
        raise PracticeInfrastructureError("terminal memory contained an unsafe path")
        # 路径安全校验：防目录穿越进入宿主文件系统。
    # 区分"有意的空记忆库"与传输层历史上"失败返回空字典"的哨兵值。
    # 字节码文件的排除口径与 pull_memory 一致。
    out = vm.run_command(
        "n=$(find /home/user/.memory -type f "
        "! -path '*/__pycache__/*' ! -name '*.pyc' ! -name '*.pyo' "
        "2>/dev/null | wc -l); echo RSI_MEMORY_FILES=$n; "
        "test -d /home/user/.memory; echo RSI_MEMORY_DIR_RC=$?",
        timeout=60) or ""
    # 在客户机内独立清点记忆文件数并确认目录存在（与拉回结果对账）。
    match = re.search(r"RSI_MEMORY_FILES=(\d+)", out)
    # 解析客户机清点出的文件数。
    if ("RSI_MEMORY_DIR_RC=0" not in out or match is None
            or int(match.group(1)) != len(files)):
        raise PracticeInfrastructureError("terminal memory pull was incomplete")
        # 目录不存在/清点失败/数量对不上：拉取不完整，拒绝采信。
    return files
    # 返回经过校验的终局记忆。


def _outcome_text(record: dict[str, Any]) -> str:
    """构造给 Curriculum Agent 看的黑盒结果文本。

    无损的宿主档案会保留 Actor 交接与记忆清单以供研究/恢复，但这些内部
    表面不进入课程上下文。Curriculum 只观察它的项目、Verifier 的终局
    信号以及 Verifier 自己的证据。
    """
    pieces = [
        f"PROJECT {record['project_index']}",
        # 项目编号。
        "ORIGINAL NATURAL-LANGUAGE PROJECT (verbatim):\n" + record["project"],
        # 原始自然语言项目描述（逐字引用，不概括）。
        f"TERMINAL OUTCOME: {record['terminal_outcome']}",
        # 终局结果（PASS/FAIL）。
    ]
    for index, report in enumerate(record["verifier_reports"], 1):
        # 逐份附加 Verifier 报告（逐字引用）。
        pieces.append(f"VERIFIER REPORT {index} (verbatim):\n{report}")
        # 编号 + 原文。
    return "\n\n".join(pieces)
    # 空行分隔各块，形成结果渲染文本（恢复时用于一致性校验）。


@dataclass
# 【中文注释】PracticeHooks：生命周期全部外部能力的"依赖接缝"（dataclass 字段即
# 钩子表，默认绑到真实实现）。分组解读：run_attempt/sink_factory/reset_vm 是执行与
# 供给；read_guest_text/capture_project/replay_project/verify_project/audit_captured
# 是材料在客户机与宿主间的搬运与校验；push_memory/pull_memory/audit_memory/
# journal_memory/install_memory/validate_corpus 是记忆的推拉、静默审计、日志、原子
# 安装与语料校验；audit_text/audit_transcripts 是目标边界审计；push_dir 是通用目录
# 传输。恢复工具与测试可整体替换任意钩子做确定性回放（依赖倒置）。
class PracticeHooks:
    """生命周期测试与恢复工具的依赖接缝（每个字段都是可整体替换的钩子）。"""

    run_attempt: Callable[..., Any] = run_attempt
    # 执行钩子：跑一段智能体循环（默认绑到 core.loop.run_attempt）。
    sink_factory: Callable[[str], Any] = ArtifactSink
    # 轨迹接收器工厂：为每段建一个落盘目录。
    reset_vm: Callable[..., dict[str, Any]] = _reset_null_vm
    # VM 供给：重置到全新空任务边界。
    read_guest_text: Callable[..., str] = _read_guest_text
    # 客户机文本读取：精确字节、不截断。
    capture_project: Callable[..., dict[str, Any]] = capture_project_materials
    # 材料捕获：客户机项目树 -> 宿主快照。
    replay_project: Callable[..., dict[str, Any]] = replay_project_materials
    # 材料回放：宿主快照 -> 客户机（字节级一致）。
    verify_project: Callable[..., dict[str, Any]] = verify_project_materials
    # 候选完整性校验：活性文件对照快照清单。
    audit_captured: Callable[..., list[Any]] = audit_captured_materials
    # 捕获物审计：目标边界防泄漏检测。
    push_memory: Callable[..., bool] = memory_transport.push_memory
    # 记忆推送：宿主规范记忆 -> 客户机只读证据位。
    pull_memory: Callable[..., dict[str, bytes]] = memory_transport.pull_memory
    # 记忆拉取：客户机终局记忆 -> 宿主。
    audit_memory: Callable[..., dict[str, bytes]] = memory_transport.silent_audit
    # 记忆静默审计：逐条对照语料，拒绝项隔离而不惊动 Agent。
    journal_memory: Callable[..., None] = memory_transport.journal
    # 记忆日志：安装前先把接受的记忆写进 append-only 日志。
    install_memory: Callable[..., None] = _atomic_install_memory
    # 记忆安装：原子替换整棵记忆树。
    validate_corpus: Callable[..., dict[str, Any]] = \
        memory_transport.validate_instruction_corpus
    # 语料校验：防泄漏对照语料的完整性检查。
    audit_text: Callable[..., list[Any]] = audit_text
    # 文本边界审计（提示词与交接）。
    audit_transcripts: Callable[..., list[Any]] = audit_transcripts
    # 轨迹边界审计（整段工件）。
    push_dir: Callable[..., bool] = memory_transport.push_dir
    # 通用目录推送（执行证据视图的传输）。


# 【中文注释】Phase 1 断点恢复：只恢复"完全闭合"的项目边界。Agent 的工作从不在此
# 被重建或重新评分——持久记忆、已完成的结果记录与最后一份无损 Curriculum 轨迹
# 共同充当恢复证人。任何"打开中/半完成"的下一个 episode 一律 fail closed
# （拒绝恢复），绝不靠猜蒙混过关（state.json 的 status 必须是 infra 且计数自洽）。
def _resume_completed_phase1_boundary(
        lineage: Path, target_direction: str, *,
        project_budget: int | None, checkpoint_projects: tuple[int, ...],
        phase1_exploration: bool,
        phase1_target_conditioned: bool) -> dict[str, Any]:
    """只恢复"完全闭合"的 Phase 1 项目边界。

    Agent 的工作从不在这里被重建或重新评分。持久记忆、已完成的结果记录
    与最后一份无损 Curriculum 轨迹共同充当恢复证人。任何打开中/不完整的
    下一个 episode 一律 fail closed，绝不靠猜蒙混过关。
    """

    state_path = lineage / "state.json"
    # 基础设施状态文件路径。
    if state_path.is_symlink() or not state_path.is_file():
        raise PracticeInfrastructureError(
            "Phase-1 resume requires a real infrastructure state file")
        # 状态文件必须是真实普通文件（防符号链接伪造）。
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        # 读取并解析状态。
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise PracticeInfrastructureError(
            "Phase-1 resume state is unreadable") from exc
        # 读不了就拒绝恢复（不留模糊空间）。
    project_index = state.get("projects")
    # 已完成项目数。
    if (state.get("status") != "infra"
            or type(project_index) is not int or project_index < 0
            or state.get("last_project") != project_index
            or state.get("phase1_exploration") is not phase1_exploration
            or state.get("phase1_target_conditioned")
            is not phase1_target_conditioned
            or state.get("project_budget") != project_budget
            or state.get("checkpoint_projects")
            != list(checkpoint_projects)
            or state.get("target_sha256") != hashlib.sha256(
                target_direction.encode("utf-8")).hexdigest()):
        raise PracticeInfrastructureError(
            "Phase-1 resume state does not match the requested protocol")
        # 逐项核对协议指纹：状态必须精确匹配本次请求（模式、预算、检查点、目标方向），
        # 任何不一致都拒绝恢复——绝不把别的协议进度接到这次运行上。

    memory = _read_memory_tree(str(lineage / "memory"))
    # 读取当前持久记忆树。
    if state.get("memory_manifest") != _manifest(memory):
        raise PracticeInfrastructureError(
            "Phase-1 resume memory drifted from the infrastructure state")
        # 记忆与状态文件记录的清单不一致：记忆已漂移，拒绝恢复。

    episodes = lineage / "episodes"
    # episodes 目录：每个项目一个 epN 段。
    project_summaries: list[str] = []
    # 恢复出的项目摘要（喂回 Curriculum 的历史）。
    latest_outcome = ""
    # 最近一次结果的渲染文本。
    for index in range(1, project_index + 1):
        # 逐个验证 1..N 的每个已完成 episode。
        episode = episodes / f"ep{index:03d}"
        # 该 episode 的目录。
        outcome_path = episode / "outcome.json"
        # 机器可读结果记录。
        rendered_path = episode / "outcome.md"
        # 渲染后的结果文本。
        if (episode.is_symlink() or outcome_path.is_symlink()
                or rendered_path.is_symlink()
                or not outcome_path.is_file() or not rendered_path.is_file()):
            raise PracticeInfrastructureError(
                f"Phase-1 resume is missing closed episode {index}")
            # 任何文件缺失/是符号链接：该边界不算闭合。
        try:
            record = json.loads(outcome_path.read_text(encoding="utf-8"))
            rendered = rendered_path.read_text(encoding="utf-8")
            # 读取记录与渲染文本。
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise PracticeInfrastructureError(
                f"Phase-1 resume episode {index} is unreadable") from exc
            # 读不了即拒绝。
        if (record.get("project_index") != index
                or record.get("terminal_outcome") not in {"PASS", "FAIL"}
                or rendered != _outcome_text(record)):
            raise PracticeInfrastructureError(
                f"Phase-1 resume episode {index} is not a closed outcome")
            # 三重自洽校验：编号对、结果是二值终态、渲染文本与记录逐字一致。
        project_summaries.append(
            f"project {index}: {record['terminal_outcome']}; "
            "one Verifier investigation")
        # 摘要只含最少的黑盒事实（结果 + 一次 Verifier 调查）。
        latest_outcome = rendered
        # 记下最近一次的渲染结果（下一轮 Curriculum 提示的输入）。

    partial = episodes / f"ep{project_index + 1:03d}"
    # 下一个（可能半开）episode 的目录。
    if partial.exists() or partial.is_symlink():
        try:
            has_entries = partial.is_symlink() or any(partial.iterdir())
            # 检查是否已有内容（符号链接视为有内容）。
        except OSError as exc:
            raise PracticeInfrastructureError(
                "Phase-1 resume cannot inspect the next episode boundary") from exc
            # 检查不了就拒绝：边界状态不明时绝不猜。
        if has_entries:
            raise PracticeInfrastructureError(
                "Phase-1 resume refuses a partially opened next episode")
            # 半开的下一 episode：拒绝恢复（它没有被承认的结果）。

    curriculum_root = lineage / "curriculum"
    # 持久 Curriculum 的轨迹根。
    turns: list[tuple[int, Path]] = []
    # 收集（轮次号, 目录）。
    if curriculum_root.is_dir() and not curriculum_root.is_symlink():
        for candidate in curriculum_root.iterdir():
            match = re.fullmatch(r"turn_(\d{3})", candidate.name)
            # 名字必须精确匹配 turn_NNN。
            if match and candidate.is_dir() and not candidate.is_symlink():
                turns.append((int(match.group(1)), candidate))
                # 只收真实目录。
    curriculum_turn = max((number for number, _ in turns), default=0)
    # 最大轮次号 = Curriculum 已做过的决定数（没有则 0）。
    transcripts: list[Path] = []
    # 收集所有段的 transcript.json。
    for _number, turn in turns:
        transcripts.extend(
            path for path in turn.glob("segment_*/transcript.json")
            if path.is_file() and not path.is_symlink())
        # 逐轮收集真实普通文件的轨迹。
    curriculum_history: list[dict[str, Any]] = []
    # 恢复出的 Curriculum 会话历史。
    scoped_visuals: list[dict[str, Any]] = []
    # 被归档的视觉附件登记。
    if transcripts:
        transcript = max(transcripts, key=lambda path: path.stat().st_mtime_ns)
        # 取修改时间最新的轨迹（最新上下文即当前活动上下文）。
        try:
            stored = json.loads(transcript.read_text(encoding="utf-8"))
            # 解析轨迹 JSON。
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            raise PracticeInfrastructureError(
                "Phase-1 Curriculum transcript is unreadable") from exc
            # 读不了即拒绝恢复。
        messages = stored.get("messages") if isinstance(stored, dict) else None
        # 取消息列表。
        if (not isinstance(messages, list)
                or any(not isinstance(item, dict) for item in messages)):
            raise PracticeInfrastructureError(
                "Phase-1 Curriculum transcript is malformed")
            # 结构校验失败即拒绝：绝不带着坏上下文续跑。
        curriculum_history, scoped_visuals = \
            _scope_prior_curriculum_visuals(messages)
            # 做视觉作用域裁剪：文本全保留，边界后的像素改为归档说明。
    elif curriculum_turn:
        raise PracticeInfrastructureError(
            "Phase-1 resume lost the persistent Curriculum context")
        # 有轮次却找不到轨迹：持久上下文丢了，拒绝恢复。

    return {
        "project_index": project_index,
        # 恢复到的项目编号。
        "curriculum_turn": curriculum_turn,
        # 恢复到的 Curriculum 轮次。
        "curriculum_history": curriculum_history,
        # 恢复的会话历史（视觉已裁剪）。
        "project_summaries": project_summaries,
        # 项目摘要列表。
        "latest_outcome": latest_outcome,
        # 最近一次结果渲染文本。
        "scoped_visuals": scoped_visuals,
        # 归档视觉登记（供事件负载统计）。
    }


def evolve_practice(
        vm, root: str, target_direction: str, actor_cfg, verifier_cfg,
        curriculum_cfg, memory_cfg, *, corpus_path: str = DEFAULT_CORPUS,
        hooks: PracticeHooks | None = None,
        event_sink: Callable[..., Any] | None = None,
        project_budget: int | None = None,
        checkpoint_projects: tuple[int, ...] = (),
        phase1_exploration: bool = False,
        phase1_target_conditioned: bool = False,
        agentic_verifier_cfg=None,
        resume_completed_boundary: bool = False) -> PracticeResult:
    """运行 Agent 自有的外层搜索，直到 Curriculum 宣布 CONVERGED/STALLED。

    ``target_direction`` 只暴露给 Curriculum 提示词。官方评测器刻意不是
    参数，也不读取或产生任何基准分数。配置的迭代/墙钟上限只是紧急的
    基础设施看门狗，绝不会被解释为收敛或项目结果。

    ``phase1_exploration`` 选择分布引导的 Phase 1 词表：Curriculum 可以
    宣布 ``SATURATED``（探索饱和）而不声称任务收敛。
    ``phase1_target_conditioned`` 保留该探索生命周期，但给 Curriculum 一个
    确切查询，用于派生多样的非目标项目。``project_budget`` 是完整项目
    数上的编排预算，绝不是 Agent 轮次上限或语义成功条件。记忆检查点是
    精确的、无上限的字节树，只在已提交的记忆阶段之后捕获。所有参数
    默认为历史上的 RSI 行为。
    """

    hooks = hooks or PracticeHooks()
    # 钩子表缺省绑真实实现（测试/恢复可整体替换）。
    lineage = Path(root)
    # 谱系根目录：本次生命周期全部持久状态的根。
    memory_dir = lineage / "memory"
    # 持久记忆目录（宿主侧规范副本）。
    journal_dir = lineage / "memory_journal"
    # 记忆日志目录（append-only，安装前留底）。
    episodes_dir = lineage / "episodes"
    # episodes 目录：每个项目一个 epN。
    checkpoints_dir = lineage / "checkpoints"
    # 记忆检查点目录。
    state_path = lineage / "state.json"
    # 基础设施状态文件。
    lineage.mkdir(parents=True, exist_ok=True)
    # 建谱系根。
    memory_dir.mkdir(parents=True, exist_ok=True)
    # 建记忆目录。
    journal_dir.mkdir(parents=True, exist_ok=True)
    # 建日志目录。
    episodes_dir.mkdir(parents=True, exist_ok=True)
    # 建 episodes 目录。

    if (project_budget is not None
            and (type(project_budget) is not int or project_budget < 0)):
        raise ValueError("project_budget must be a nonnegative integer or None")
        # 预算参数校验：必须是非负整数或 None（bool 也不算 int 之外的类型）。
    if phase1_target_conditioned and not phase1_exploration:
        raise ValueError(
            "phase1_target_conditioned requires phase1_exploration")
        # 目标条件化是探索模式的子选项：不能单独开启。
    if any(type(index) is not int or index < 0 for index in checkpoint_projects):
        raise ValueError("checkpoint_projects must contain nonnegative integers")
        # 检查点编号校验：必须是非负整数。
    checkpoint_projects = tuple(sorted(set(checkpoint_projects)))
    # 去重并排序：保证检查点集合规范。
    if (project_budget is not None
            and any(index > project_budget for index in checkpoint_projects)):
        raise ValueError("a checkpoint cannot exceed project_budget")
        # 检查点不得超出预算：否则永远轮不到触发。

    project_index = 0
    # 已闭合项目计数（从 0 开始）。
    curriculum_turn = 0
    # Curriculum 决定轮次计数。
    curriculum_history: list[dict[str, Any]] = []
    # 持久 Curriculum 的会话历史（跨项目保持）。
    project_summaries: list[str] = []
    # 黑盒项目摘要（喂回 Curriculum）。
    latest_outcome = ""
    # 最近一次结果渲染文本。
    curriculum_notes = ""
    # Curriculum 的持久笔记。
    resume_notice = ""
    # 恢复说明文本（只在恢复后的第一次提示里附加一次）。
    # 恢复尝试不得改写它还在校验的持久状态。这在"校验本身失败"时至关重要：
    # 若用尚未恢复的零值计数器去调 ``finish``，会抹掉一个有效的已完成项目边界。
    # 恢复是否已获准：非恢复模式一开始就算"已 admit"（finish 可正常写状态）。
    resume_admitted = not resume_completed_boundary

    def checkpoint(project: int) -> None:
        """在指定项目边界捕获精确记忆检查点（只记录，不改变规范记忆）。"""

        if project not in checkpoint_projects:
            return
            # 不在检查点集合里：直接跳过。
        memory = _read_memory_tree(str(memory_dir))
        # 读取当前完整记忆树（字节级精确）。
        destination = checkpoints_dir / f"project_{project:03d}"
        # 检查点目标目录。
        if destination.exists() or destination.is_symlink():
            raise PracticeInfrastructureError(
                f"Phase-1 checkpoint already exists: {destination}")
            # 已存在即报错：检查点一次性，覆盖会破坏历史。
        destination.mkdir(parents=True)
        # 建检查点目录。
        hooks.install_memory(str(destination / "memory"), memory)
        # 原子安装整棵记忆树副本。
        manifest = _manifest(memory)
        # 生成清单。
        _atomic_json(destination / "manifest.json", {
            "schema_version": 1,
            "kind": "phase1_memory_checkpoint",
            "project": project,
            "memory_manifest": manifest,
            "memory_manifest_sha256": hashlib.sha256(
                json.dumps(
                    manifest, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        })
        # 落盘清单与清单哈希（检查点自描述、可独立校验）。
        emit(
            "MEMORY_CHECKPOINT_COMMITTED", status="completed",
            project_open=False, memory_phase_open=False,
            payload={
                "project": project,
                "files": len(memory),
                "bytes": sum(len(data) for data in memory.values()),
                "memory_manifest_sha256": hashlib.sha256(
                    json.dumps(
                        manifest, sort_keys=True, separators=(",", ":")
                    ).encode("utf-8")
                ).hexdigest(),
            },
        )
        # 发出检查点事件（只含哈希/计数，不含 Agent 内容）。

    def emit(event_type: str, *, status: str,
             project_open: bool, memory_phase_open: bool,
             payload: dict[str, Any]) -> None:
        """在挂接了台账（ledger）时持久化保守的阶段边界事件。

        回调刻意只做传输：事件名标识活动的 Agent 席位，三个通用标志描述
        恢复义务。Agent 内容只用哈希/令牌表示。
        """
        if event_sink is None:
            return
            # 未挂接台账：事件静默丢弃（事件是可选设施）。
        event_sink(
            event_type, status=status,
            state={
                "project_open": project_open,
                # 项目边界是否处于打开中（恢复义务的依据）。
                "memory_phase_open": memory_phase_open,
                # 记忆阶段是否打开中。
                "recovery_open": False,
                # 恢复义务恒为 false（本模块不打开恢复窗口）。
            },
            payload=payload,
        )
        # 转发给外部事件台账。

    def finish(status: str, reason: str = "", terminal_text: str = "") \
            -> PracticeResult:
        """原子落盘终态并返回结果：状态文件是唯一权威的边界记录。"""

        payload = {
            "schema_version": 1,
            # 状态文件模式版本。
            "status": status,
            # 终态（converged/saturated/stalled/infra/quarantined/budget_exhausted…）。
            "projects": project_index,
            # 已闭合项目数。
            "last_project": project_index,
            # 最后一个项目编号（闭合时刻两者相等）。
            "reason": reason,
            # 终态原因。
            "terminal_text": terminal_text,
            # 触发终态的原始文本。
            "phase1_exploration": phase1_exploration,
            # 协议模式指纹：Phase 1 探索。
            "phase1_target_conditioned": phase1_target_conditioned,
            # 协议模式指纹：目标条件化。
            "project_budget": project_budget,
            # 协议模式指纹：项目预算。
            "checkpoint_projects": list(checkpoint_projects),
            # 协议模式指纹：检查点集合。
            "target_sha256": hashlib.sha256(
                target_direction.encode("utf-8")).hexdigest(),
            # 目标方向的 sha256（内容指纹，非内容本身）。
            "memory_manifest": _manifest(_read_memory_tree(str(memory_dir))),
            # 终态时刻的精确记忆清单（断点恢复的校验锚）。
        }
        _atomic_json(state_path, payload)
        # 原子写状态文件。
        return PracticeResult(
            status=status, projects=project_index, root=str(lineage),
            memory_dir=str(memory_dir), reason=reason,
            terminal_text=terminal_text, last_project=project_index)
        # 构造并返回结果对象。

    def preserve_unadmitted_resume(reason: str) -> PracticeResult:
        """报告恢复失败，但不改写未获准（未验证）的检查点状态。"""

        persisted_projects = 0
        # 持久化的项目数（读不到时保守取 0）。
        persisted_last_project = 0
        # 持久化的最后项目号（读不到时保守取 0）。
        try:
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            # 读取磁盘上已存在的状态文件。
            if type(persisted.get("projects")) is int:
                persisted_projects = persisted["projects"]
                # 只采信 int 类型的 projects。
            if type(persisted.get("last_project")) is int:
                persisted_last_project = persisted["last_project"]
                # 只采信 int 类型的 last_project。
        except (OSError, UnicodeError, ValueError, TypeError):
            pass
            # 读不到就保持 0：不伪造计数。
        return PracticeResult(
            status="infra", projects=persisted_projects, root=str(lineage),
            memory_dir=str(memory_dir), reason=reason,
            last_project=persisted_last_project)
        # 返回 infra 结果：持久状态原样保留，等待下一次恢复。

    try:
        # 主流程：任何异常都会在下方 except 中被归入可检视的终态。
        if not target_direction.strip():
            raise PracticeInfrastructureError("target direction is empty")
            # 目标方向不能是空白：它是唯一的任务侧输入。
        hooks.validate_corpus(corpus_path)
        # 校验防泄漏对照语料的完整性（缺失/损坏即拒绝开跑）。
        _audit_prompt(hooks, target_direction, target_direction)
        # 目标方向自身也要过边界审计（授权指令=它自己）。

        if resume_completed_boundary:
            # 恢复分支：只接受完全闭合的 Phase 1 边界。
            if not phase1_exploration:
                raise PracticeInfrastructureError(
                    "completed-boundary resume is only defined for Phase 1")
                # 闭合边界恢复只在 Phase 1 有定义。
            recovered = _resume_completed_phase1_boundary(
                lineage, target_direction,
                project_budget=project_budget,
                checkpoint_projects=checkpoint_projects,
                phase1_exploration=phase1_exploration,
                phase1_target_conditioned=phase1_target_conditioned)
            # 读取并校验持久状态（失败会抛错，不落任何写操作）。
            resume_admitted = True
            # 校验通过：恢复已获准，finish 此后可以安全写状态。
            project_index = recovered["project_index"]
            # 恢复项目计数。
            curriculum_turn = recovered["curriculum_turn"]
            # 恢复 Curriculum 轮次。
            curriculum_history = recovered["curriculum_history"]
            # 恢复会话历史（视觉已裁剪）。
            project_summaries = recovered["project_summaries"]
            # 恢复项目摘要。
            latest_outcome = recovered["latest_outcome"]
            # 恢复最近结果。
            resume_notice = f"""

HARNESS RECOVERY NOTICE:
Infrastructure or transport interrupted the next project after project
{project_index} had already closed. The partial project produced no admitted
correctness outcome and changed no durable memory; its transient environment was
discarded. Continue in your same persistent context: recreate that proposed
experiment or choose another one using your own judgment. This notice is transport
state, not a grade or convergence signal.
"""
            # 恢复说明（附加到下一次 Curriculum 提示）：半成品项目没有产生任何
            # 被承认的结果、未动持久记忆；是否重做由 Curriculum 自行判断。
            # 这是传输状态说明，不是评分或收敛信号。
            emit(
                "PHASE_RESUMED", status="in_progress",
                project_open=False, memory_phase_open=False,
                payload={
                    "after_project": project_index,
                    "recovered_curriculum_turn": curriculum_turn,
                    "archived_visual_messages": len(
                        recovered.get("scoped_visuals", [])),
                    "archived_visual_images": sum(
                        item["images"]
                        for item in recovered.get("scoped_visuals", [])),
                },
            )
            # 发出"已恢复"事件（记录恢复点与归档视觉统计）。
        else:
            checkpoint(0)
            # 全新运行：先拍 0 号基线检查点。
        if project_budget is not None and project_index >= project_budget:
            return finish(
                "budget_exhausted",
                "Phase-1 exploration project budget exhausted")
            # 项目预算已用尽：这是编排预算耗尽，不是语义结论。

        while True:
            # Curriculum 是一场持久对话，但每次外层决定都拿到一台全新的空任务
            # 机器。它可以在这台活 VM 上编写下一批非解题夹具；捕获发生在重置
            # 之前。规范记忆只以"用完即弃的证据副本"挂载：Curriculum 可以查阅
            # 它来选择经验，但只有结果之后的 Actor 记忆阶段才能改动宿主自有的
            # 语料。
            _fresh_vm(hooks, vm, target_direction)
            # 重置到全新空任务边界。
            _push_canonical_memory(hooks, vm, str(memory_dir))
            # 挂载规范记忆的只读证据副本。
            curriculum_turn += 1
            # 决定轮次 +1。
            curriculum_sink = lineage / "curriculum" / \
                f"turn_{curriculum_turn:03d}"
            # 本轮 Curriculum 轨迹目录。
            curriculum_prompt = self_evolving_curriculum_charter(
                target_direction,
                project_history="\n".join(project_summaries),
                latest_outcome=latest_outcome,
                curriculum_notes=curriculum_notes,
                handoff_path=CURRICULUM_HANDOFF,
                notes_path=CURRICULUM_NOTES,
                phase1_exploration=phase1_exploration,
                phase1_target_conditioned=phase1_target_conditioned,
            ) + _curriculum_runtime_contract() + resume_notice
            # 组装提示词：章程 + 黑盒历史/最近结果/笔记 + 运行时边界条款 +（一次性）恢复说明。
            resume_notice = ""
            # 恢复说明只附一次，随后清空。

            while True:
                # 内层循环：直到拿到合法 PROJECT 决定或终局令牌。
                decision, curriculum_history, _ = _run_handoff_phase(
                    hooks=hooks, vm=vm, cfg=curriculum_cfg,
                    prompt=curriculum_prompt, role="CURRICULUM",
                    handoff_path=CURRICULUM_HANDOFF,
                    token_pattern=(
                        _PHASE1_CURRICULUM_TOKEN
                        if phase1_exploration else _CURRICULUM_TOKEN),
                    sink_root=str(curriculum_sink), target=target_direction,
                    history=curriculum_history,
                    continuation=bool(curriculum_history),
                    require_completed_publication=True)
                # 跑 Curriculum 交接阶段：受限传输（跨段共享预算）+ 要求段完成才算决定。
                notes_candidate = hooks.read_guest_text(vm, CURRICULUM_NOTES)
                # 读回 Curriculum 笔记（可选产出）。
                if notes_candidate:
                    if hooks.audit_text(
                            notes_candidate, mode="practice",
                            authorized_instruction=target_direction):
                        raise PracticeBoundaryError(
                            "Curriculum notes failed the target boundary")
                        # 笔记泄漏：隔离。
                    curriculum_notes = notes_candidate
                    # 采纳为新的持久笔记。
                if decision.token in {"CONVERGED", "SATURATED", "STALLED"}:
                    # 终局决定：生命周期结束。
                    _atomic_text(lineage / "curriculum_terminal.md",
                                 decision.text)
                    # 终局决定原文落盘。
                    return finish(
                        decision.token.lower(),
                        "Curriculum Agent terminal decision", decision.text)
                    # 以小写令牌为终态收尾（converged/saturated/stalled）。
                if (not decision.body
                        or PROJECT_ROOT not in decision.body):
                    curriculum_prompt = _continuation_after_transport(
                        "CURRICULUM", CURRICULUM_HANDOFF,
                        f"a PROJECT must be nonempty and name {PROJECT_ROOT}")
                    continue
                    # PROJECT 正文为空或未指名项目根：传输缺陷，续传修正。
                candidate_index = project_index + 1
                # 候选项目编号。
                episode_dir = episodes_dir / f"ep{candidate_index:03d}"
                # 该项目的 episode 目录。
                fixture_dir = episode_dir / "fixtures"
                # 夹具快照目录。
                try:
                    _capture_owned_tree(
                        hooks, vm, f"ep{candidate_index:03d}-fixtures",
                        str(fixture_dir), target_direction)
                    # 在重置前捕获夹具树（含边界审计）。
                except PracticeInfrastructureError:
                    # 自有根缺失/为空是可修正的通用传输问题，
                    # 不是项目评分或收敛信号。
                    curriculum_prompt = _continuation_after_transport(
                        "CURRICULUM", CURRICULUM_HANDOFF,
                        f"{PROJECT_ROOT} was absent, empty, or not replayable")
                    continue
                    # 续传让 Curriculum 修好发布集。
                project_index = candidate_index
                # 采纳候选编号：项目边界从此打开。
                project = decision.body
                # 项目描述 = PROJECT 令牌后的正文。
                # 在写任何后续项目状态之前先保守地打开边界：
                # 此记录之后的任何中断都必须是明显可恢复的。
                emit(
                    "PROJECT_OPENED", status="in_progress",
                    project_open=True, memory_phase_open=False,
                    payload={
                        "project_index": project_index,
                        "project_sha256": hashlib.sha256(
                            project.encode("utf-8")).hexdigest(),
                    },
                )
                # 发"项目已开"事件（只含编号与描述哈希）。
                _atomic_text(episode_dir / "project.md", decision.text)
                # 项目决定原文落盘（可恢复的起点）。
                break
                # 跳出内层循环，进入 Actor 阶段。

            before_memory = _read_memory_tree(str(memory_dir))
            # 记录项目开始前的记忆基线（结果记录里要做前后对照）。
            _fresh_vm(hooks, vm, target_direction)
            # 换全新机器给 Actor。
            _replay(hooks, vm, str(fixture_dir))
            # 精确回放 Curriculum 的夹具快照。
            _push_canonical_memory(hooks, vm, str(memory_dir))
            # 挂载规范记忆只读副本。

            actor_history: list[dict[str, Any]] = []
            # Actor 的会话历史（本 episode 内累积）。
            actor_handoffs: list[str] = []
            # Actor 各次交接原文。
            verifier_reports: list[str] = []
            # Verifier 各轮报告原文。
            actor_prompt = self_evolving_actor_charter(
                project, _listing(before_memory), ACTOR_HANDOFF) \
                + _actor_runtime_contract()
            # Actor 提示词：项目 + 记忆目录视图 + 交接路径 + 运行时边界条款。
            actor_continuation = False
            # 首段不是续传。
            verification_cycle = 0
            # 验证轮次计数（每捕获一个候选 +1）。
            # 正确性判定只有一次 Actor 提交。外层循环只为在同上下文里修复
            # 畸形的传输/工作区发布而存在——在那唯一一次提交被接受之前。
            actor_attempt = 1
            # Actor 尝试编号恒为 1（一次正确性提交）。
            terminal_outcome = ""
            # 终局结果占位。

            emit(
                "ACTOR_PHASE_STARTED", status="in_progress",
                project_open=True, memory_phase_open=False,
                payload={
                    "project_index": project_index,
                    "actor_attempt": actor_attempt,
                    "verification_cycle": verification_cycle,
                },
            )
            # 发"Actor 阶段开始"事件。
            while True:
                # Actor 循环：提交 -> 捕获候选 -> 交 Verifier；传输问题就地续传。
                actor_decision, actor_history, _ = _run_handoff_phase(
                    hooks=hooks, vm=vm, cfg=actor_cfg,
                    prompt=actor_prompt, role="ACTOR",
                    handoff_path=ACTOR_HANDOFF,
                    token_pattern=_ACTOR_TOKEN,
                    sink_root=str(episode_dir / "actor" /
                                  f"attempt_{actor_attempt:03d}"),
                    target=target_direction, history=actor_history,
                    continuation=actor_continuation)
                # 跑 Actor 交接阶段直到出现 STATUS: SUBMIT。
                actor_continuation = True
                # 之后任何段都是同上下文续传。
                actor_handoffs.append(actor_decision.text)
                # 记录本次交接原文。

                candidate_number = verification_cycle + 1
                # 候选轮次号。
                candidate_dir = episode_dir / "candidates" / \
                    f"cycle_{candidate_number:03d}"
                # 候选快照目录。
                try:
                    _capture_owned_tree(
                        hooks, vm,
                        f"ep{project_index:03d}-candidate-"
                        f"{candidate_number:03d}",
                        str(candidate_dir), target_direction)
                    # 捕获候选（提交物）快照并审计。
                except PracticeInfrastructureError:
                    actor_prompt = _continuation_after_transport(
                        "ACTOR", ACTOR_HANDOFF,
                        f"{PROJECT_ROOT} was absent, empty, or not replayable")
                    actor_handoffs.pop()
                    continue
                    # 发布集缺失/为空：撤回交接记录，续传让 Actor 修正。
                verification_cycle = candidate_number
                # 采纳候选轮次号。
                _atomic_text(
                    episode_dir / "handoffs" /
                    f"actor_{verification_cycle:03d}.md",
                    actor_decision.text)
                # Actor 交接原文落盘。

                execution_evidence = _build_actor_execution_evidence(
                    episode_dir / "actor", actor_attempt,
                    episode_dir / "actor_execution_evidence" /
                    f"cycle_{verification_cycle:03d}")
                # 从 Actor 工件构建宿主自有的执行证据视图（白名单产物+清单）。
                candidate_manifest = \
                    candidate_dir / "materials.MANIFEST.sha256"
                # 候选快照的材料清单路径。
                if candidate_manifest.is_symlink() or \
                        not candidate_manifest.is_file():
                    raise PracticeInfrastructureError(
                        "candidate snapshot manifest is missing")
                    # 清单缺失：候选快照不完整，无法校验，报基础设施错误。
                candidate_manifest_sha256 = hashlib.sha256(
                    candidate_manifest.read_bytes()).hexdigest()
                # 清单内容哈希（事件与事后审计用）。

                # 给 Verifier 全新机器、精确的 Actor 快照、且没有 Actor 记忆。
                # 独立的宿主自有执行视图只暴露因果动作，不含 Actor 推理或宿主
                # 评分。Verifier 的改动与该视图都会在重置时被丢弃。
                emit(
                    "VERIFIER_PHASE_STARTED", status="in_progress",
                    project_open=True, memory_phase_open=False,
                    payload={
                        "project_index": project_index,
                        "verification_cycle": verification_cycle,
                        "actor_status": actor_decision.token,
                        "actor_handoff_sha256": hashlib.sha256(
                            actor_decision.text.encode("utf-8")).hexdigest(),
                        "actor_execution_manifest_sha256":
                            execution_evidence["manifest_sha256"],
                        "actor_execution_files": execution_evidence["files"],
                        "actor_execution_bytes": execution_evidence["bytes"],
                        "candidate_manifest_sha256":
                            candidate_manifest_sha256,
                    },
                )
                # 发"Verifier 阶段开始"事件（各输入只以哈希/计数表示）。
                _fresh_vm(hooks, vm, target_direction)
                # Verifier 拿到全新机器。
                _replay(hooks, vm, str(candidate_dir))
                # 回放候选快照（不含夹具之外的 Actor 痕迹）。
                if agentic_verifier_cfg is None:
                    # 历史 RSI 传输保持可复现。递归改进协议的 Phase 1 选择启用
                    # 下方"仅候选"的干净 Agentic Verifier 分支。
                    _push_actor_execution_evidence(
                        hooks, vm, execution_evidence)
                    # 推送执行证据到客户机（推送后逐字节校验）。
                    verifier_prompt = self_evolving_verifier_charter(
                        project, report_path=VERIFIER_REPORT,
                        actor_execution_path=ACTOR_EXECUTION_EVIDENCE)
                    # Verifier 提示词：项目 + 报告路径 + 证据路径。
                    verifier_decision, _, _ = \
                        _run_handoff_phase(
                            hooks=hooks, vm=vm, cfg=verifier_cfg,
                            prompt=verifier_prompt, role="VERIFIER",
                            handoff_path=VERIFIER_REPORT,
                            token_pattern=_VERIFIER_TOKEN,
                            sink_root=str(episode_dir / "verifier" /
                                          f"cycle_{verification_cycle:03d}"),
                            target=target_direction, history=[],
                            continuation=False,
                            handoff_parser=_parse_unique_handoff,
                            commit_on_publish=True)
                    # 跑 Verifier 交接阶段：全新历史（无前置上下文）、唯一令牌
                    # 解析（防报告内引用令牌造成歧义）、发布即提交。
                else:
                    from core.verifier import VerifierSession, verify_agentic
                    # Agentic Verifier 分支：延迟导入核心验证器（避免循环依赖）。

                    if not getattr(
                            agentic_verifier_cfg,
                            "agentic_verifier_config", ""):
                        raise PracticeInfrastructureError(
                            "Phase-1 Agentic Verifier control config is missing")
                        # 控制面配置缺失：无法启动 Agentic Verifier。
                    private_paths = tuple(dict.fromkeys(
                        tuple(getattr(
                            agentic_verifier_cfg,
                            "verifier_private_paths", ()) or ())
                        + (ACTOR_HANDOFF, ACTOR_EXECUTION_EVIDENCE)))
                    # 私有路径集合：配置自带路径 + Actor 交接与执行证据（去重保序）。
                    control_cfg = dataclasses.replace(
                        agentic_verifier_cfg,
                        verifier_evolve_route=False,
                        verifier_local_verdict_only=False,
                        verifier_unverified_evidence=False,
                        verifier_failure_starts_evolution=False,
                        verifier_hide_actor_memory=True,
                        verifier_stage_lifecycle=False,
                        verifier_persist_scratch=False,
                        verifier_private_paths=private_paths)
                    # 用 replace 派生 Phase 1 专用的控制面：关闭进化路由等主循环
                    # 专属行为，隐藏 Actor 记忆，私有路径如上。
                    verifier_session = VerifierSession()
                    # 建 Verifier 会话（管理执行器生命周期）。
                    try:
                        verdict, findings = verify_agentic(
                            project, vm, control_cfg,
                            sink=ArtifactSink(str(
                                episode_dir / "verifier" /
                                f"cycle_{verification_cycle:03d}")),
                            turn_no=project_index,
                            context=(
                                f"The candidate project is {PROJECT_ROOT}. "
                                "Actor-private memory, handoff prose, reasoning, "
                                "and execution logs are hidden by the harness. "
                                "Investigate the actual candidate independently."),
                            session=verifier_session,
                            wall_budget=control_cfg.wall_clock_secs)
                        # 执行 Agentic 验证：判定 + 发现；上下文明确告知 Actor
                        # 私有面已被隐藏，要求独立调查实际候选。
                    finally:
                        verifier_session.close_executor()
                        # 无论成败都关闭执行器（释放 VM 会话资源）。
                    if verdict not in {"pass", "wrong"}:
                        raise PracticeInfrastructureError(
                            "Phase-1 Verifier Agent ended without PASS/FAIL: "
                            + str(findings))
                        # 没有得出 pass/wrong 判定：按基础设施失败处理。
                    token = "PASS" if verdict == "pass" else "FAIL"
                    # 映射为练习协议的二值令牌。
                    report = str(findings)
                    # 报告文本 = 发现的字符串形式。
                    verifier_decision = ParsedHandoff(
                        token=token, body=report, text=report)
                    # 包装成与交接阶段同构的判定对象。
                    # 只在独立 Verifier Agent 终止之后，才为宿主审计保留
                    # 确切的执行档案（避免污染其调查环境）。
                    _push_actor_execution_evidence(
                        hooks, vm, execution_evidence)
                    # 推送执行证据（供宿主审计与后续校验）。
                _verify_candidate(hooks, vm, str(candidate_dir))
                # 判定后复核：候选快照仍与活性文件逐字节一致。
                _verify_actor_execution_evidence(vm, execution_evidence)
                # 复核客户机上的执行证据仍与宿主清单一致。

                verifier_reports.append(verifier_decision.text)
                # 记录 Verifier 报告原文。
                _atomic_text(
                    episode_dir / "handoffs" /
                    f"verifier_{verification_cycle:03d}.md",
                    verifier_decision.text)
                # Verifier 报告落盘。

                # 恢复 Actor 验证前确切的项目状态与项目前的规范记忆。
                # 这一步同时丢弃 Verifier 的改动与任何被禁止的工作期记忆变更。
                _fresh_vm(hooks, vm, target_direction)
                # 重置机器（丢弃 Verifier 的一切痕迹）。
                _replay(hooks, vm, str(candidate_dir))
                # 回放候选快照（回到提交时刻）。
                _push_canonical_memory(hooks, vm, str(memory_dir))
                # 重新挂载项目前的规范记忆（工作期记忆改动全部作废）。

                # 唯一的 Verifier 判定就是确切的终局结果（PASS 或 FAIL）。
                # 其后没有任何修复或确认步骤。
                terminal_outcome = verifier_decision.token
                # 终局结果 = Verifier 令牌。
                break
                # 跳出 Actor/验证循环，进入记忆阶段。

            # 只有此刻才允许记忆变化。使用同一 Actor 上下文，以及确切的
            # 终局项目状态加最终 Verifier 证据。
            emit(
                "MEMORY_PHASE_STARTED", status="in_progress",
                project_open=True, memory_phase_open=True,
                payload={
                    "project_index": project_index,
                    "terminal_outcome": terminal_outcome,
                    "verifier_report_sha256": hashlib.sha256(
                        verifier_reports[-1].encode("utf-8")).hexdigest(),
                },
            )
            # 发"记忆阶段开始"事件（记忆边界从此打开）。
            memory_prompt = self_evolving_actor_memory_distillation_msg(
                terminal_outcome, verifier_reports[-1])
            # 蒸馏提示：终局结果 + 最新 Verifier 报告。
            actor_history, _ = _run_free_phase(
                hooks=hooks, vm=vm, cfg=memory_cfg, prompt=memory_prompt,
                role="ACTOR", sink_root=str(episode_dir / "memory_distillation"),
                target=target_direction, history=actor_history)
            # 跑自由蒸馏阶段：同一 Actor 在同上下文里起草记忆更新。
            # 第一次更新仍是同一活 VM 里未晋升的草稿。让同一个 Actor
            # （带完整历史、记忆配置不变）在唯一的拉取/审计/日志/安装边界
            # 之前，专门获得一次整理全部语料的机会。
            reconciliation_prompt = \
                self_evolving_actor_memory_reconciliation_msg()
            # 整理提示词。
            actor_history, _ = _run_free_phase(
                hooks=hooks, vm=vm, cfg=memory_cfg,
                prompt=reconciliation_prompt, role="ACTOR",
                sink_root=str(episode_dir / "memory_reconciliation"),
                target=target_direction, history=actor_history)
            # 跑自由整理阶段：对整棵记忆库做最后一次自查与修正。
            candidate_memory = _pull_terminal_memory(hooks, vm)
            # 唯一一次拉取终局记忆（多重校验）。
            accepted_memory = hooks.audit_memory(
                candidate_memory, corpus_path,
                str(lineage / "audit_rejects.jsonl"),
                authorized_instruction=target_direction, require_corpus=True)
            # 静默审计：对照语料逐条检查，拒绝项写入隔离日志（不惊动 Agent）。
            # 静默审计一旦拒绝就隔离整个候选：绝不部分改写 Actor 的
            # 表意，也绝不把泄漏信号反馈给任何 Agent。
            if accepted_memory != candidate_memory:
                raise PracticeBoundaryError(
                    "terminal memory failed the target boundary")
                # 审计剔除了任何条目：整体隔离，不做"部分接受"。

            hooks.journal_memory(
                str(journal_dir), project_index, accepted_memory,
                {"kind": "practice-terminal-memory",
                 "terminal_outcome": terminal_outcome})
            # 先写 append-only 日志留底（安装前）。
            hooks.install_memory(str(memory_dir), accepted_memory)
            # 原子安装：规范记忆切换为本次接受的记忆树。

            checkpoint(project_index)
            # 若本项目在检查点集合里，拍下精确记忆检查点。

            record = {
                "schema_version": 1,
                # 结果记录模式版本。
                "project_index": project_index,
                # 项目编号。
                "project": project,
                # 原始项目描述全文（无损存档）。
                "terminal_outcome": terminal_outcome,
                # 终局结果（PASS/FAIL）。
                "verification_cycles": verification_cycle,
                # 验证轮次数。
                "actor_handoffs": actor_handoffs,
                # 全部 Actor 交接原文（研究/恢复用，不进课程上下文）。
                "verifier_reports": verifier_reports,
                # 全部 Verifier 报告原文。
                "memory_before": _manifest(before_memory),
                # 项目前记忆清单。
                "memory_after": _manifest(accepted_memory),
                # 项目后记忆清单。
            }
            _atomic_json(episode_dir / "outcome.json", record)
            # 结果记录原子落盘（闭合边界的凭证）。
            latest_outcome = _outcome_text(record)
            # 渲染黑盒结果文本。
            _atomic_text(episode_dir / "outcome.md", latest_outcome)
            # 渲染文本落盘（与 JSON 逐字一致，恢复时校验）。
            project_summaries.append(
                f"project {project_index}: {terminal_outcome}; "
                "one Verifier investigation")
            # 追加最简黑盒摘要（喂给下一轮 Curriculum）。
            _atomic_json(state_path, {
                "schema_version": 1,
                "status": "running",
                "projects": project_index,
                "last_project": project_index,
                "target_sha256": hashlib.sha256(
                    target_direction.encode("utf-8")).hexdigest(),
                "memory_manifest": _manifest(accepted_memory),
            })
            # 状态文件推进到"运行中、N 个闭合边界"（断点恢复的锚）。
            emit(
                "PROJECT_CLOSED", status="completed",
                project_open=False, memory_phase_open=False,
                payload={
                    "project_index": project_index,
                    "terminal_outcome": terminal_outcome,
                    "memory_manifest_sha256": hashlib.sha256(
                        json.dumps(
                            _manifest(accepted_memory), sort_keys=True,
                            separators=(",", ":")).encode("utf-8")
                    ).hexdigest(),
                },
            )
            # 发"项目已闭合"事件（边界全部关闭，恢复义务清零）。
            # 已闭合项目的完整 Curriculum 轨迹早已持久化。保留每个文本
            # 轮次与 Agent 的推理活性，只是在下一个独立项目决定之前终止像素自动回放。
            curriculum_history, scoped_visuals = \
                _scope_prior_curriculum_visuals(curriculum_history)
            # 视觉作用域裁剪：文本全留，像素归档。
            if scoped_visuals:
                emit(
                    "CURRICULUM_SENSORY_CONTEXT_SCOPED",
                    status="completed", project_open=False,
                    memory_phase_open=False,
                    payload={
                        "after_project": project_index,
                        "archived_visual_messages": len(scoped_visuals),
                        "archived_visual_images": sum(
                            item["images"] for item in scoped_visuals),
                    },
                )
                # 发"视觉上下文已归档"事件（统计口径）。
            if (project_budget is not None
                    and project_index >= project_budget):
                return finish(
                    "budget_exhausted",
                    "Phase-1 exploration project budget exhausted")
                # 达到项目预算：以预算耗尽收尾（回到外层 while 之前先退出）。

    except KeyboardInterrupt:
        # 外部操作停止属于基础设施事件。持久化最后已知边界，使
        # "闭合边界续跑"成为可能，同时尽快把控制权交还调用方。
        if resume_completed_boundary and not resume_admitted:
            return preserve_unadmitted_resume(
                "KeyboardInterrupt during resume validation")
            # 恢复校验期间被打断：不改写未获准的检查点。
        return finish("infra", "KeyboardInterrupt: external interruption")
        # 常规路径：以 infra 终态收尾（可恢复）。
    except PracticeBoundaryError as exc:
        if resume_completed_boundary and not resume_admitted:
            return preserve_unadmitted_resume(str(exc))
            # 恢复校验期发现边界违规：同样不动持久状态。
        return finish("quarantined", str(exc))
        # 运行期边界违规：以 quarantined 终态收尾。
    except PracticeInfrastructureError as exc:
        if resume_completed_boundary and not resume_admitted:
            return preserve_unadmitted_resume(str(exc))
            # 恢复校验期的基础设施失败：不动持久状态。
        return finish("infra", str(exc))
        # 运行期基础设施失败：以 infra 终态收尾（携带原因）。
    except Exception as exc:  # noqa: BLE001 - persist inspectable infra outcome
        # 兜底：任何未预期异常也持久化为可检视的 infra 终态，绝不静默丢失。
        if resume_completed_boundary and not resume_admitted:
            return preserve_unadmitted_resume(
                f"{type(exc).__name__}: {exc}")
            # 恢复校验期的意外异常：不动持久状态。
        return finish("infra", f"{type(exc).__name__}: {exc}")
        # 运行期意外异常：以 infra 终态收尾（异常类型 + 消息）。


__all__ = [
    "ACTOR_HANDOFF",
    "ACTOR_EXECUTION_EVIDENCE",
    "CURRICULUM_HANDOFF",
    "CURRICULUM_NOTES",
    "DEFAULT_CORPUS",
    "PracticeHooks",
    "PracticeResult",
    "PROJECT_ROOT",
    "PROJECT_ROOT_NAME",
    "VERIFIER_REPORT",
    "evolve_practice",
]
# 模块公开接口：路径常量、数据类、钩子表与主入口。
