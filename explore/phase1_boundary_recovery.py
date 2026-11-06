"""Resume only a verified, fully committed parallel-wave prefix.

Uncommitted work is archived, never promoted. The last complete Curriculum
conversation, outcomes, memory and original total budget are preserved.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil

from explore.practice_loop import (
    PracticeInfrastructureError, _atomic_json, _manifest, _read_memory_tree,
    _scope_prior_curriculum_visuals,
)
# 复用练习主循环（practice_loop）的原子写 JSON、记忆树清单（manifest）等工具。


def _require(condition, detail):
    # 断言工具：条件不成立即抛基础设施错误（恢复准入 fail-closed）。
    if not condition:
        raise PracticeInfrastructureError("parallel-wave resume: " + detail)
        # 错误消息统一带 parallel-wave resume 前缀，便于定位恢复阶段。


def validate_boundary(lineage: Path, target: str, *, project_budget,
                      checkpoint_projects, max_parallel,
                      target_query_conditioned):
    # 准入校验：只允许从“已验证、完整提交的并行波（parallel-wave）前缀”恢复；
    # 未提交的工作一律归档，绝不晋升（promote）。
    from explore.phase1_wave import parse_wave_handoff

    state = json.loads((lineage / "state.json").read_text())
    # 读取谱系（lineage）状态文件。
    count = state.get("projects")
    # 已完成项目计数。
    # A rejected guest handoff may never have reached the saved transcripts.
    # Re-auditing those transcripts cannot establish that quarantine was false.
    # （既有英文注释：被拒绝的 guest 交接可能从未写入已保存的 transcript；重审计
    # 这些 transcript 也无法证明隔离（quarantine）不成立。）
    _require(state.get("status") != "quarantined",
             "quarantined lineages cannot resume from a completed boundary")
    # 被隔离的谱系不得从完成边界恢复。
    _require(state.get("status") == "infra",
             "lineage is not stopped at an infrastructure boundary")
    # 只接受停在基础设施边界上的谱系。
    _require(type(count) is int and count >= 0
             and state.get("last_project") == count, "invalid project counter")
    # 项目计数必须是自然数且与 last_project 一致（无半开项目）。
    expected = {
        "phase1_parallel_waves": True, "project_budget": project_budget,
        "checkpoint_projects": list(checkpoint_projects),
        "max_parallel": max_parallel,
        "target_query_conditioned": target_query_conditioned,
        "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
    }
    # 期望的协议字段快照：并行波开关、项目预算、检查点项目、最大并行度、
    # 目标查询条件化开关，以及目标的 SHA-256（目标身份）。
    _require(all(state.get(k) == v for k, v in expected.items()),
             "protocol fields drifted")
    # 全部协议字段必须逐一对上（防协议漂移）。
    _require(not any((lineage.parent / p).exists() for p in ("phase2", "phase3")),
             "a later phase already exists")
    # 不允许已存在后续阶段（phase2/phase3）的产物。
    memory = _manifest(_read_memory_tree(str(lineage / "memory")))
    # 计算当前规范记忆（canonical memory）树的清单。
    _require(memory == state.get("memory_manifest"), "canonical memory drifted")
    # 规范记忆必须与状态文件记录的清单一致。
    _require(memory == _manifest(_read_memory_tree(str(lineage / "memory_frozen"))),
             "frozen memory differs from stopped canonical memory")
    # 冻结记忆必须与停止时的规范记忆完全一致。

    records = []
    before = {}
    # 逐集（episode）审计记忆链与终止结果。
    for index in range(1, count + 1):
        episode = lineage / "episodes" / f"ep{index:03d}"
        record = json.loads((episode / "outcome.json").read_text())
        # 读取每集的结局记录。
        _require(record.get("project_index") == index
                 and record.get("terminal_outcome") in {"PASS", "FAIL"},
                 f"episode {index} is not closed")
        # 每集必须闭合（索引正确且终局为 PASS/FAIL 二值）。
        _require(record.get("memory_before") == before,
                 f"episode {index} memory chain drifted")
        # 记忆链连续：本集 memory_before 必须等于上一集 memory_after。
        _require((episode / "outcome.md").is_file(), "missing rendered outcome")
        # 渲染版结局文件必须存在。
        before = record["memory_after"]
        records.append(record)
        # 推进记忆链游标并收集记录。
    _require(before == memory, "memory includes a partial/uncommitted project")
    # 最终记忆必须等于最后闭合项目的记忆——证明没有半提交（partial/uncommitted）项目。

    last_wave = records[-1]["wave_index"] if records else 0
    # 最后一个完成波的编号（无记录则为 0）。
    _require(type(last_wave) is int and last_wave >= 0,
             "invalid completed wave counter")
    # 完成波计数合法性。
    _require(type(state.get("waves")) is int
             and state["waves"] in {last_wave, last_wave + 1}, "wave counter drifted")
    # 状态里的波计数只能是 last_wave 或 last_wave+1（+1 表示下一波刚开目录）。
    event_path = lineage / "events.jsonl"
    _require(not count or event_path.is_file(), "completed-wave event log is missing")
    # 有已完成项目就必须有事件日志。
    completed = [json.loads(line) for line in (event_path.read_text() if event_path.is_file() else "").splitlines()
                 if line.strip()]
    # 解析全部事件行。
    completed = [e for e in completed if e.get("event_type") == "PHASE1_WAVE_COMPLETED"]
    # 过滤出“波完成”事件。
    _require((not count and not completed) or (bool(completed)
             and completed[-1]["payload"]["total_projects"] == count
             and completed[-1]["payload"]["wave"] == last_wave),
             "counter is not the end of a completed wave")
    # 计数必须恰好落在某个完成波的末尾（最后一个波完成事件的 total_projects 与
    # wave 均与当前计数一致）。
    assignments = []
    for wave in range(1, last_wave + 1):
        directory = lineage / "waves" / f"wave_{wave:03d}"
        decision = parse_wave_handoff((directory / "curriculum_decision.json").read_text())
        # 解析每个完成波的 Curriculum 交接决策。
        _require(decision is not None and decision.decision == "WAVE",
                 "invalid completed wave decision")
        # 决策必须存在且类型为 WAVE。
        assignments.extend((wave, project.project_id) for project in decision.projects)
        # 收集 (波号, 项目ID) 分配序列。
        _require((directory / "outcomes.md").is_file(), "wave feedback is missing")
        # 波反馈文件必须存在。
    _require(assignments == [(r["wave_index"], r["project_id"]) for r in records],
             "completed waves differ from the ordered project ledger")
    # 分配序列必须与逐集账本（ledger）完全一致。

    history, outcomes = [], ""
    # 恢复所需的 Curriculum 上下文与最近波反馈。
    if count:
        transcripts = sorted((lineage / "curriculum" / f"wave_{last_wave:03d}").glob(
            "segment_*/transcript.json"))
        # 收集最后完成波的全部分段 transcript。
        _require(bool(transcripts), "completed Curriculum context is missing")
        # 完成的 Curriculum 上下文必须存在。
        last_transcript = max(transcripts, key=lambda p: int(p.parent.name.split("_")[1]))
        # 取编号最大的分段（最近一次对话）。
        messages = json.loads(last_transcript.read_text()).get("messages")
        # 读取其消息序列。
        _require(isinstance(messages, list) and bool(messages)
                 and len(messages) % 2 == 0, "incomplete Curriculum context")
        # 消息必须成对（user/assistant 完整轮）。
        _require(all(isinstance(message, dict)
                     and message.get("role") == ("user" if index % 2 == 0 else "assistant")
                     for index, message in enumerate(messages)),
                 "invalid Curriculum context roles")
        # 角色交替必须严格 user→assistant。
        history, _ = _scope_prior_curriculum_visuals(messages)
        # 裁剪出可恢复的历史上下文（含视觉引用的作用域处理）。
        outcomes = (lineage / "waves" / f"wave_{last_wave:03d}" / "outcomes.md").read_text()
        # 读取最近波的反馈文本。

    pending = []
    # 待归档的未提交产物。
    for directory, prefix, ceiling in (("episodes", "ep", count),
                                       ("waves", "wave_", last_wave),
                                       ("curriculum", "wave_", last_wave)):
        # 三类目录分别按各自的天花板（已完成计数）扫描超出部分。
        for path in sorted((lineage / directory).glob(prefix + "*")):
            suffix = path.name[len(prefix):]
            _require(suffix.isdigit() and path.name == f"{prefix}{int(suffix):03d}"
                     and path.is_dir() and not path.is_symlink(), "unexpected artifact path")
            # 产物路径必须是规范的三位数字目录且非符号链接（防夹带）。
            if int(suffix) <= ceiling:
                continue
                # 天花板以内的是已提交前缀，跳过。
            if directory == "episodes":
                _require(not (path / "outcome.json").exists()
                         and not (path / "memory_distillation").exists()
                         and not (path / "memory_reconciliation").exists(),
                         "the pending wave has already started learning")
                # 待定 episode 不得已开始学习（蒸馏/调和均不存在）。
            pending.append(path)
            # 记入待归档清单。
    return {"projects": count, "wave": last_wave, "history": history,
            "outcomes": outcomes, "pending": pending, "state": state}
    # 返回恢复计划：项目数、波号、历史上下文、反馈、待归档路径与状态快照。


def archive_pending(lineage: Path, plan):
    # 把未提交的工作整体归档（绝不晋升），并写回执。
    archive = lineage / "boundary_recovery" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    # 以 UTC 时间戳创建唯一归档目录。
    archive.mkdir(parents=True)
    for name in ("state.json", "result.json", "manifest.json", "events.jsonl"):
        if (lineage / name).is_file():
            shutil.copy2(lineage / name, archive / name)
            # 先复制顶层簿记文件（copy2 保留元数据）。
    receipt = {"status": "prepared", "resumed_projects": plan["projects"],
               "last_complete_wave": plan["wave"], "total_budget_unchanged": True,
               "archived_paths": [str(p.relative_to(lineage)) for p in plan["pending"]],
               "memory_manifest": plan["state"]["memory_manifest"]}
    # 回执初稿：状态 prepared、恢复的项目数、最后完成波、预算不变声明、
    # 待归档相对路径与记忆清单。
    _atomic_json(archive / "receipt.json", receipt)
    # 原子写入回执（先写 prepared 再改 archived，保证崩溃可判）。
    for path in plan["pending"]:
        destination = archive / path.relative_to(lineage)
        destination.parent.mkdir(parents=True, exist_ok=True)
        path.rename(destination)
        # 逐个把未提交产物移入归档（保留相对结构）。
    receipt["status"] = "archived_pending_wave"
    _atomic_json(archive / "receipt.json", receipt)
    # 回执终态：待定波已归档。
    return archive
    # 返回归档目录路径。
