#!/usr/bin/env python3
"""Recover a final, uncommitted parallel practice wave from durable artifacts.

This intentionally supports only a stopped wave that will reach the existing
project ceiling when closed. It never chooses projects or retries from grades.
Completed reports and Actor conversations are reused. Missing candidate capture
may be retried from the same fixture only before any verification; an interrupted
Verifier may continue only if all its attempted programs failed before execution.
The previous infrastructure result remains archived, and memory is written only
by the normal continuing Actor distillation/reconciliation phases.
"""
# 中文注释（模块概览）：本工具从持久化工件中恢复“最后一波尚未提交的并行练习波次”（practice wave，
# 练习波次：RSIAgent 自我改进循环中一批并行执行的项目）。仅支持已停在基础设施边界（infrastructure
# boundary）且收尾后恰好触及既有项目上限（project ceiling）的波次；绝不重新挑选项目、绝不依据评分重试。
# 已完成的报告与 Actor（行动者：生成候选解的一方）会话直接复用；缺失的候选捕获（candidate capture，
# 候选捕获：把候选解及其机器产物打包归档）可在任何验证开始前用同一夹具（fixture）重试；被打断的
# Verifier（验证者：检验候选解的一方）仅当其所有已尝试程序都在执行前失败时方可续跑。此前的基础设施
# 结果保持归档；记忆（memory）只允许由后续正常的 Actor 蒸馏（distillation）/对账（reconciliation）阶段写入。
# 启用 PEP 563 延迟注解求值，类型注解不在定义时求值
from __future__ import annotations

# 解析命令行参数
import argparse
# 线程池：并行恢复各分支并逐个收取结果
from concurrent.futures import ThreadPoolExecutor, as_completed
# 将 dataclass 配置转为字典以便与启动快照比对
import dataclasses
# 记录恢复计划的 UTC 时间戳
from datetime import datetime, timezone
# 文件锁：防止多个恢复进程并发执行
import fcntl
# 计算文件/文本的 SHA256 摘要，校验工件未漂移
import hashlib
# 读写 JSON 工件（state/result/manifest 等）
import json
# 日志基础配置
import logging
# 读取环境变量（如 OSWorld 缓存目录）
import os
# 面向对象的文件路径操作
from pathlib import Path
# 复制恢复前的关键工件作为存档
import shutil
# 恢复完成后调用常规流水线做校验收尾
import subprocess
# 调整 sys.path 并在子进程中引用解释器
import sys
# 用属性风格包装恢复计划上下文
from types import SimpleNamespace

# 定位仓库根目录（tools/ 的上一级）
REPO = Path(__file__).resolve().parents[1]
# 将仓库根加入 sys.path，使脚本能直接导入 benchmarks/core/explore 等包
sys.path.insert(0, str(REPO))


def load(path):
    # 读取指定 JSON 工件并解析为 Python 对象（恢复校验中最常用的读取入口）
    return json.loads(Path(path).read_text())


def digest(path):
    # 计算文件的 SHA256 摘要，用于证明工件自停止以来未被改动
    # 新建 SHA256 哈希对象
    h = hashlib.sha256()
    # 以二进制方式打开文件
    with Path(path).open('rb') as stream:
        # 每次读 1MiB 数据块，直到读空
        for block in iter(lambda: stream.read(1 << 20), b''):
            # 将数据块喂入哈希
            h.update(block)
    # 返回十六进制摘要字符串
    return h.hexdigest()


def require(condition, message):
    # 硬性断言辅助：任一恢复前提不满足都立即失败，绝不带病继续
    if not condition:
        # 以明确原因抛错终止
        raise RuntimeError(message)


def actor_history(episode):
    # 读取并校验该 episode（单项目执行目录）最后一次 Actor 会话的完整对话记录
    # 收集所有分段转写并按序排序
    paths = sorted((episode / 'actor').glob('attempt_*/segment_*/transcript.json'))
    # 必须存在 Actor 转写，否则无法恢复
    require(bool(paths), f'Missing Actor transcript: {episode}')
    # 取最后一段转写的消息列表（user/assistant 交替）
    history = load(paths[-1])['messages']
    # 会话必须非空且成对收尾（以 assistant 结束才算完整对话）
    require(bool(history) and len(history) % 2 == 0,
            f'Incomplete Actor conversation: {episode}')
    # 逐条校验角色交替合法
    for index, message in enumerate(history):
        # 偶数位必须是 user，奇数位必须是 assistant
        require(message.get('role') == ('user' if index % 2 == 0 else 'assistant'),
                f'Invalid Actor conversation roles: {episode}')
    # 返回完整对话历史，供恢复后的 Actor 运行时复用
    return history


def classify_branch(episode):
    """Recovery selection depends on durable boundaries, never verdict quality."""
    # 恢复分支的判定只依赖持久化边界（哪些工件已落盘），绝不依据质量评判（verdict）结果选路
    # 复用 Agentic Verifier（智能体式验证器）报告的解析函数
    from core.verifier import _parse_agentic_verifier_report
    # 候选解捕获归档（打包候选程序与机器产物）
    candidate = episode / 'candidates/cycle_001/materials.tgz'
    # Verifier 已发布的报告
    report = episode / 'handoffs/verifier_001.md'
    # 已写出 outcome 即波次部分提交，禁止恢复
    require(not (episode / 'outcome.json').exists(), 'Wave already partly committed')
    # 记忆蒸馏阶段已开始则不可恢复
    require(not (episode / 'memory_distillation').exists(), 'Memory phase already began')
    # 记忆对账阶段已开始同样不可恢复
    require(not (episode / 'memory_reconciliation').exists(), 'Memory phase already began')
    # 情形一：Verifier 报告已存在
    if report.exists():
        # 已评级候选的归档必须仍在
        require(candidate.is_file(), 'Graded candidate is missing')
        # 从报告解析终局判定
        verdict = _parse_agentic_verifier_report(report.read_text())
        # 仅 pass/wrong 两种终局可复用
        require(verdict in {'pass', 'wrong'}, 'Saved Verifier report is not terminal')
        # 模式：直接复用既有判定，不再重跑 Verifier
        return 'reuse_verdict'
    # 情形二：候选已捕获但尚未出具 Verifier 报告
    if candidate.exists():
        # 收集 Verifier 已留下的执行痕迹元数据
        traces = list((episode / 'verifier').rglob('trace_meta.json'))
        # 逐份检查每个被打断的验证尝试
        for trace in traces:
            # 读取该尝试的元数据
            meta = load(trace)
            # 必须是纯基础设施失败且程序未执行（机器副作用已归档）才允许续跑
            require(meta.get('infra_fail') is True and meta.get('secs') == 0,
                    'Cannot restore a Verifier whose machine effects are unarchived')
        # 存在 Look 状态则需单独的恢复审计，不可自动续跑
        require(not list((episode / 'verifier').rglob('look.json')),
                'Verifier Look state requires a separate recovery audit')
        # 不允许覆盖已发布的报告
        require(not list((episode / 'verifier').rglob('verifier_report.md')),
                'Do not replace an already published report')
        # 模式：在同一虚拟机上续跑 Verifier
        return 'resume_verifier'
    # 候选缺失时不得有任何 Verifier 痕迹
    require(not (episode / 'verifier').exists(), 'Missing candidate has Verifier evidence')
    # 候选缺失时不得有已提交的交接文档
    require(not (episode / 'handoffs').exists(), 'Missing candidate has committed handoffs')
    # 候选目录必须为空，否则属于含义不明的部分捕获
    require(not any(p.is_file() for p in (episode / 'candidates').rglob('*')),
            'Ambiguous partial candidate capture')
    # 模式：Actor 记录已随故障丢失，用同一夹具整体重跑该分支
    return 'retry_capture_lost_actor'


def validate_archive(directory):
    # 校验目录与 materials.tgz 归档完全一致（文件清单与符号链接均匹配），确保恢复输入未被篡改
    # 复用供应（provisioning）模块的清单/符号链接读取函数
    from explore.provisioning import _inventory_from_tgz, read_manifest, read_symlinks
    # 从 tgz 字节流解析文件清单与符号链接表
    manifest, links = _inventory_from_tgz((directory / 'materials.tgz').read_bytes())
    # 归档清单必须非空且与目录实际清单一致
    require(bool(manifest) and manifest == read_manifest(str(directory)),
            f'Archive manifest mismatch: {directory}')
    # 归档符号链接与目录实际一致
    require(links == read_symlinks(str(directory)), f'Archive symlink mismatch: {directory}')


def next_recovery_dir(root):
    """Preserve failed recovery attempts; never replay an open or completed one."""
    # 分配新的恢复审计目录：保留历史失败恢复，绝不重放进行中或已完成的恢复
    # 从 001 号开始探测
    index = 1
    # 逐个编号向后找
    while True:
        # 候选审计目录名（如 infrastructure_recovery_001）
        path = root / f'infrastructure_recovery_{index:03d}'
        # 不存在且非符号链接即可使用
        if not path.exists() and not path.is_symlink():
            # 返回全新的恢复目录
            return path
        # 已存在的必须是真实目录而非符号链接
        require(path.is_dir() and not path.is_symlink(), 'Unsafe recovery archive')
        # 已完成的恢复不可重放
        require(not (path / 'completion.json').exists(), 'A recovery already completed')
        # 上次恢复必须已停止（写有 failure.json）
        require((path / 'failure.json').is_file(), 'Previous recovery is still open')
        # 上次恢复必须也停在基础设施边界
        require(load(path / 'failure.json').get('status') == 'infra',
                'Previous recovery did not stop at an infrastructure boundary')
        # 编号递增继续探测
        index += 1


def build_plan(protocol_path):
    # 构建恢复计划：先全面校验谱系（lineage）与工件完整性，再为每个分支确定恢复模式
    # 复用 phase1 运行器的路径/配置/工件函数
    import benchmarks.osworld.phase1 as runner
    # 安装 phase1 运行所需的 sys.path 与依赖
    runner._install_paths()
    # 复用并行波次模块的解析/提交/执行函数
    from explore import phase1_wave as wave
    # 复用记忆清单与记忆树读取工具
    from explore.practice_loop import _manifest, _read_memory_tree

    # 读取协议文件（整个实验运行的配置契约）
    protocol = load(protocol_path)
    # 取出 phase1 段配置
    phase = protocol['phase1']
    # 由运行名推出 phase1 运行根目录
    root = runner._safe_phase_root(protocol['run_name'])
    # 根目录旁的协议副本必须与传入协议逐字节一致（协议自启动后未被改动）
    require((root.parent / 'protocol.json').read_bytes() == protocol_path.read_bytes(),
            'Protocol changed after launch')
    # phase2/phase3 工件不存在才允许恢复 phase1
    require(not any((root.parent / name).exists() for name in ('phase2', 'phase3')),
            'Later phase artifacts already exist')
    # 依次加载运行清单、停止时状态、结果三份工件
    manifest, state, result = (load(root / name) for name in
                               ('manifest.json', 'state.json', 'result.json'))
    # 状态与结果都必须表明谱系停在基础设施边界（infra）
    require(state['status'] == result['status'] == 'infra', 'Lineage is not infrastructure-stopped')
    # 协议与清单都必须声明并行波次（parallel waves）模式
    require(phase['parallel_waves'] is True and manifest['parallel_waves'] is True,
            'Not a parallel wave lineage')
    # 按协议重建各角色（Actor/Verifier/Verifier 控制/Curriculum/记忆）的配置对象及配置文件路径
    configs, paths = runner._load_configs(SimpleNamespace(**{
        name: phase[name] for name in ('actor_config', 'verifier_config',
        'verifier_control_config', 'curriculum_config', 'memory_config')}))
    # 逐角色比对配置与启动快照
    for role, config in configs.items():
        # 启动时记录的该角色配置快照
        old = manifest['configs'][role]
        # 配置文件字节摘要必须一致
        require(digest(paths[role]) == old['sha256'], f'Changed {role} config bytes')
        # 经 JSON 规整后的生效配置也必须一致
        require(json.loads(json.dumps(dataclasses.asdict(config))) == old['effective'],
                f'Changed {role} effective config')
    # 解析目标方向（target direction）分布文件的路径
    distribution_path = runner._resolve_input_path(phase['distribution_file'])
    # 读取目标方向文本并去除首尾空白
    target = distribution_path.read_text().strip()
    # 分布文件摘要须与清单一致（防漂移）
    require(digest(distribution_path) == manifest['distribution_sha256'], 'Distribution drift')
    # 目标文本摘要须与停止状态一致
    require(hashlib.sha256(target.encode()).hexdigest() == state['target_sha256'], 'Target drift')
    # 默认语料库摘要须与清单一致
    require(digest(runner.DEFAULT_CORPUS) == manifest['corpus_sha256'], 'Corpus drift')
    # 恢复前后都不允许调用官方评测器（official evaluator）
    require(result['official_evaluator_calls'] == 0, 'Unexpected evaluator call')
    # 已提交完成的项目数（停止时的边界）
    count = state['projects']
    # 状态/结果的项目数须一致且为正
    require(count == result['projects'] and count > 0, 'Inconsistent project boundary')
    # 项目预算（project budget）在协议/状态/结果三处必须一致
    require(phase['project_budget'] == state['project_budget'] == result['project_budget'], 'Budget drift')
    # 并行度在协议/状态/清单三处必须一致
    require(phase['parallelism'] == state['max_parallel'] == manifest['max_parallel'], 'Parallelism drift')
    # 检查点（checkpoint）项目集合在三处必须一致
    require(phase['checkpoints'] == state['checkpoint_projects'] == result['checkpoints'], 'Checkpoint drift')
    # 当前波次编号
    wave_number = state['waves']
    # 当前波次目录
    wave_dir = root / 'waves' / f'wave_{wave_number:03d}'
    # 波次总结未写出（未收尾）才可恢复
    require(not (wave_dir / 'outcomes.md').exists(), 'Stopped wave already closed')
    # 解析 Curriculum（课程）已提交的波次决策
    decision = wave.parse_wave_handoff((wave_dir / 'curriculum_decision.json').read_text())
    # 必须存在已提交的 WAVE 决策
    require(decision is not None and decision.decision == 'WAVE', 'Missing committed wave decision')
    # 仅支持“收尾即跨过预算上限”的最后一波（已提交数 < 预算 ≤ 已提交数 + 本波项目数）
    require(count < phase['project_budget'] <= count + len(decision.projects),
            'Recovery supports only the final budget-crossing wave')
    # 计算规范记忆（canonical memory）树清单
    memory = _manifest(_read_memory_tree(str(root / 'memory')))
    # 与状态/结果记录的记忆清单一致
    require(memory == state['memory_manifest'] == result['memory_manifest'], 'Canonical memory drift')
    # 冻结记忆副本须与规范记忆一致
    require(memory == _manifest(_read_memory_tree(str(root / 'memory_frozen'))), 'Frozen memory drift')
    # 波次记忆快照须与规范记忆一致
    require(memory == _manifest(_read_memory_tree(str(wave_dir / 'memory_snapshot'))), 'Wave snapshot drift')
    # 已提交项目的 outcome 文件（按序）
    completed = sorted((root / 'episodes').glob('ep*/outcome.json'))
    # 项目台账必须从 ep001 起连续无缺口
    require([p.parent.name for p in completed] == [f'ep{i:03d}' for i in range(1, count + 1)],
            'Committed project ledger is not contiguous')
    # 末位项目的“记忆后”状态须等于当前规范记忆
    require(load(completed[-1])['memory_after'] == memory, 'Last committed memory differs')
    # 初始化分支任务表、计划摘要行、输入文件摘要表
    assignments, rows, inputs = [], [], {}
    # 遍历波次决策中的每个待执行项目
    for offset, project in enumerate(decision.projects, 1):
        # 该项目的全局序号（接续已提交项目）
        index = count + offset
        # 对应的 episode 目录
        episode = root / 'episodes' / f'ep{index:03d}'
        # 项目指令须与决策一致（防漂移）
        require((episode / 'project.md').read_text() == project.instruction, 'Project instruction drift')
        # 校验夹具（fixture）归档完整一致
        validate_archive(episode / 'fixtures')
        # 判定该分支的恢复模式
        mode = classify_branch(episode)
        # 校验 Actor 会话记录完整合法
        actor_history(episode)
        # 除整体重试模式外，都要求候选与交接工件齐全
        if mode != 'retry_capture_lost_actor':
            # 校验候选归档完整一致
            validate_archive(episode / 'candidates/cycle_001')
            # 必须存在 Actor 交接（handoff）文档
            require((episode / 'handoffs/actor_001.md').is_file(), 'Missing Actor handoff')
        else:
            # 整体重试的前提：必须是先前诊断出的绝对链接归档故障，才允许丢弃重试
            require(state['reason'].startswith('AbsoluteLinkError:'),
                    'Uncaptured Actor recovery requires the diagnosed archive fault')
        # 登记该分支的恢复任务
        assignments.append((index, project, episode, mode))
        # 计划摘要：项目号/项目ID/恢复模式
        rows.append({'project': index, 'project_id': project.project_id, 'recovery': mode})
        # 遍历 episode 下所有文件以登记恢复输入摘要
        for path in episode.rglob('*'):
            # 登记规则：转写与项目说明，以及 fixtures/candidates/handoffs 目录下的所有文件
            if path.is_file() and (path.name in {'transcript.json', 'project.md'}
                                  or 'fixtures' in path.parts or 'candidates' in path.parts
                                  or 'handoffs' in path.parts):
                # 记录路径→SHA256 映射
                inputs[str(path)] = digest(path)
    # 汇总恢复计划：UTC 时间戳、运行根、波次号、已提交项目数、各分支恢复模式、项目预算、
    # 收尾后总数、恢复前记忆清单、全部输入文件摘要、脚本自身摘要；官方评测调用数恒为 0
    plan = {'time': datetime.now(timezone.utc).isoformat(), 'root': str(root),
            'wave': wave_number, 'committed_projects': count, 'branches': rows,
            'budget': phase['project_budget'], 'complete_wave_total': count + len(rows),
            'memory_before': memory, 'input_sha256': inputs,
            'script_sha256': digest(__file__), 'official_evaluator_calls': 0}
    # 以属性风格打包全部上下文（根目录/协议/配置/状态/结果/目标/决策/波次目录/分支任务/计划/运行器）
    return SimpleNamespace(root=root, protocol=protocol, configs=configs, state=state,
                           result=result, target=target, decision=decision, wave_dir=wave_dir,
                           assignments=assignments, plan=plan, runner=runner)


def restore_branch(context, assignment, factory, hooks):
    # 在线程池工作线程中恢复单个分支：按恢复模式重建候选与验证状态，返回可统一提交的分支运行时
    # 复用波次模块的分支执行与提交函数
    from explore import phase1_wave as wave
    # 复用记忆/回放/推送/候选校验等内部工具
    from explore.practice_loop import _read_memory_tree, _replay, _push_canonical_memory, _verify_candidate
    # 复用 Agentic Verifier 会话、验证入口与解析工具
    from core.verifier import VerifierSession, verify_agentic, _agentic_substantive_checkpoint, _parse_agentic_verifier_report
    # 痕迹落盘接收器：续跑验证时保存审计痕迹
    from core.trace import ArtifactSink
    # 解包分支任务：项目序号、项目定义、episode 目录、恢复模式
    index, project, episode, mode = assignment
    # 波次开始时的记忆快照目录（恢复时的记忆基线）
    snapshot = context.wave_dir / 'memory_snapshot'
    # 模式一：候选从未捕获，Actor 记录已丢失，整体重跑
    if mode == 'retry_capture_lost_actor':
        # 失败捕获尝试的归档目录
        archive = episode / 'infrastructure_attempts/capture_failed_001'
        # 创建归档目录（必须不存在，防止覆盖）
        archive.mkdir(parents=True, exist_ok=False)
        # 把残缺的 actor 目录移入归档，保留证据
        (episode / 'actor').rename(archive / 'actor')
        # 直接调用常规波次执行函数，从零运行该分支（捕获→验证→交接）
        return wave._execute_branch(
            vm_factory=factory, hooks=hooks, project_index=index, project=project,
            episode_dir=episode, wave_memory_dir=snapshot,
            actor_cfg=context.configs['actor'], verifier_control_cfg=context.configs['verifier_control'],
            target_direction=context.target)
    # 其余模式需要虚拟机：创建桌面环境与 VM 包装
    desktop, vm = factory()
    # 确保异常时虚拟机资源被释放
    try:
        # 候选解目录
        candidate = episode / 'candidates/cycle_001'
        # 把候选归档回放到虚拟机上，重建机器状态
        _replay(hooks, vm, str(candidate))
        # Verifier 报告的目标路径
        report_path = episode / 'handoffs/verifier_001.md'
        # 模式二：续跑被打断的 Verifier
        if mode == 'resume_verifier':
            # 新建验证会话（管理对话与执行器）
            session = VerifierSession()
            # 收集已有验证尝试的转写
            transcripts = sorted((episode / 'verifier').rglob('transcript.json'))
            # 若存在先前尝试的转写，则从“实质性检查点”恢复会话状态
            if transcripts:
                # 取最后一次尝试的转写
                transcript = transcripts[-1]
                # 从转写消息中提炼有效对话前缀与待处理观察/截图/轮次
                history, observation, images, turn = _agentic_substantive_checkpoint(
                    [], load(transcript)['messages'])
                # 恢复会话的消息历史
                session[:] = history
                # 收集该尝试的逐轮执行痕迹
                trace_paths = sorted(transcript.parent.glob('iter_*/trace.txt'))
                # 挂起观察：优先用提炼结果，否则回退到最后一条执行痕迹
                session.pending_observation = observation or (trace_paths[-1].read_text() if trace_paths else '')
                # 恢复挂起的截图与轮次号
                session.pending_images, session.pending_turn = images, turn
            # 取该分支的验证器控制配置（含时间预算）
            control = wave._branch_control_config(context.configs['verifier_control'])
            # 验证结束后必须释放执行器
            try:
                # 续跑 Agentic 验证：独立调查候选是否达成目标（Actor 私有记忆与推理被隐藏）
                verdict, report = verify_agentic(
                    project.instruction, vm, control,
                    sink=ArtifactSink(str(getattr(context, 'audit_dir', episode)
                                          / f'verifier_resume_ep{index:03d}')), turn_no=index,
                    context=(f'The candidate project is {wave.PROJECT_ROOT}. Actor-private '
                             'memory, handoff prose, reasoning, and execution logs are '
                             'hidden by the harness. Investigate the actual candidate independently.'),
                    session=session, wall_budget=control.wall_clock_secs)
            finally:
                # 无论结果如何都关闭验证执行器
                session.close_executor()
            # 续跑必须得到终局判定（pass 或 wrong）
            require(verdict in {'pass', 'wrong'}, f'Project {index} Verifier incomplete: {report}')
            # 原子写出验证报告
            wave._atomic_text(report_path, str(report))
            # 校验候选归档与虚拟机状态一致
            _verify_candidate(hooks, vm, str(candidate))
            # 将虚拟机重置为干净的目标初始状态
            wave._fresh_vm(hooks, vm, context.target)
            # 再次回放候选，为后续记忆提交准备一致环境
            _replay(hooks, vm, str(candidate))
        # 读取（可能刚续跑完成的）验证报告
        report = report_path.read_text()
        # 解析终局判定
        verdict = _parse_agentic_verifier_report(report)
        # 把波次记忆快照推送到虚拟机，作为 Actor 的私有记忆
        _push_canonical_memory(hooks, vm, str(snapshot))
        # 构造分支运行时：携带全部恢复产物交由主线程统一提交
        return wave._BranchRuntime(
            project_index=index, project=project, episode_dir=episode,
            desktop=desktop, vm=vm, actor_history=actor_history(episode),
            actor_handoff=(episode / 'handoffs/actor_001.md').read_text(),
            verifier_report=report, terminal_outcome='PASS' if verdict == 'pass' else 'FAIL',
            wave_memory=_read_memory_tree(str(snapshot)))
    # 任何异常都要先释放虚拟机资源
    except BaseException:
        # 关闭桌面环境
        desktop.close()
        # 原样向上抛出
        raise


def execute(context):
    # 执行恢复计划：并行恢复各分支 → 按序提交记忆与 outcome → 收尾波次并更新 state/result
    # 复用波次模块的原子写/提交/渲染函数
    from explore import phase1_wave as wave
    # 复用练习钩子与记忆工具
    from explore.practice_loop import PracticeHooks, _manifest, _read_memory_tree
    # 计算记忆树整体 SHA256
    from explore.target_learning import _memory_tree_sha256
    # 准备可检查点的 Docker 供应器
    from benchmarks.osworld.provider import prepare_checkpointable_docker_provider
    # OSWorld 桌面环境
    from desktop_env.desktop_env import DesktopEnv
    # 项目自研的 VM 包装层
    from env.vm import VM
    # 运行根目录与 phase1 运行器
    root, runner = context.root, context.runner
    # 按验证执行模式准备 Docker 供应器（与原运行保持一致）
    prepare_checkpointable_docker_provider(context.configs['verifier_control'].verifier_execution_mode)
    # 分配本次恢复的审计目录（保留历史失败恢复）
    audit = next_recovery_dir(root)
    # 创建审计目录（必须不存在）
    audit.mkdir(exist_ok=False)
    # 挂到上下文，供续跑验证的 ArtifactSink 使用
    context.audit_dir = audit
    # 备份恢复前的三份关键工件
    for name in ('state.json', 'result.json', 'manifest.json'):
        # 以 before_ 前缀复制进审计目录
        shutil.copy2(root / name, audit / ('before_' + name))
    # 原子写出恢复计划（审计凭证）
    wave._atomic_json(audit / 'plan.json', context.plan)
    # 创建练习循环钩子（负责记忆安装等副作用）
    hooks = PracticeHooks()

    def event(name, **payload):
        # 内部事件函数：向 events.jsonl 追加恢复事件并同步打印，便于实时观察
        # 追加事件：标注恢复正在进行（无项目/记忆阶段打开）
        runner._append_event(root / 'events.jsonl', name, status='in_progress',
                             state={'project_open': False, 'memory_phase_open': False,
                                    'recovery_open': True}, payload=payload)
        # 同步打印 JSON 事件行
        print(json.dumps({'event': name, **payload}), flush=True)

    def factory():
        # 虚拟机工厂：创建全新的 OSWorld Docker 桌面环境并复位，供各分支使用
        # 创建 Docker 桌面环境：pyautogui 动作空间、Ubuntu 无头、1920x1080、60GB 数据卷，缓存目录可由环境变量覆盖
        desktop = DesktopEnv(provider_name='docker', action_space='pyautogui', os_type='Ubuntu',
                             screen_size=(1920, 1080), headless=True, require_a11y_tree=False, volume_size=60,
                             cache_dir=os.environ.get('RSIAGENT_OSWORLD_CACHE_DIR', 'cache'))
        try:
            # 复位到干净快照（不加载具体任务）
            desktop.reset(task_config=None)
            # 返回桌面环境与 VM 包装
            return desktop, VM(desktop)
        # 创建/复位失败时释放资源
        except BaseException:
            # 关闭桌面环境
            desktop.close()
            # 原样抛出
            raise

    # 活跃分支列表、按项目号完成的分支表、错误收集
    active, completed, errors = [], {}, []
    # 发布恢复开始事件
    event('PHASE1_WAVE_RECOVERY_STARTED', wave=context.plan['wave'], branches=context.plan['branches'])
    # 整个恢复过程包裹在 try 中：任何失败都写入 failure.json
    try:
        # 按协议并行度创建线程池
        with ThreadPoolExecutor(max_workers=context.protocol['phase1']['parallelism']) as pool:
            # 为每个分支提交恢复任务，并建立 future→项目号 的映射以便报错定位
            futures = {pool.submit(restore_branch, context, item, factory, hooks): item[0]
                       for item in context.assignments}
            # 逐个收取完成的分支
            for future in as_completed(futures):
                try:
                    # 取分支运行时（异常会在此抛出）
                    branch = future.result()
                    # 记入活跃列表（虚拟机稍后统一释放）
                    active.append(branch)
                    # 按项目号登记完成分支
                    completed[branch.project_index] = branch
                    # 发布分支就绪事件：项目号与终局结果
                    event('PHASE1_RECOVERY_BRANCH_READY', project=branch.project_index,
                          terminal_outcome=branch.terminal_outcome)
                # 单分支失败不立即中断，先收集稍后统一判死
                except Exception as exc:
                    # 记录失败分支与异常信息
                    errors.append(f'project {futures[future]}: {type(exc).__name__}: {exc}')
        # 任一分支恢复失败即整体终止，避免部分提交
        require(not errors, '\n'.join(errors))
        # 各分支渲染后的 outcome 文本
        rendered = []
        # 按项目顺序提交每个已恢复分支
        for index, project, episode, mode in context.assignments:
            # 取对应分支运行时
            branch = completed[index]
            # 提交该分支的记忆蒸馏/对账：写规范记忆与日志，返回记忆前后状态与变更
            memory = wave._commit_branch_memory(
                branch=branch, hooks=hooks, memory_cfg=context.configs['memory_actor'],
                canonical_memory_dir=root / 'memory', journal_dir=root / 'memory_journal',
                corpus_path=str(runner.DEFAULT_CORPUS), target_direction=context.target)
            # 组装 outcome 记录：模式版本/波次号/项目序号与ID/项目指令/终局结果/验证轮数/
            # Actor 交接/Verifier 报告/波次记忆快照清单/记忆前后与变更/恢复审计信息
            record = {'schema_version': 2, 'wave_index': context.plan['wave'],
                      'project_index': index, 'project_id': project.project_id,
                      'project': project.instruction, 'terminal_outcome': branch.terminal_outcome,
                      'verification_cycles': 1, 'actor_handoffs': [branch.actor_handoff],
                      'verifier_reports': [branch.verifier_report],
                      'wave_memory_snapshot': _manifest(branch.wave_memory),
                      'memory_before': memory['before'], 'memory_after': memory['after'],
                      'memory_changes': memory['changes'],
                      'infrastructure_recovery': {'plan': str(audit / 'plan.json'), 'mode': mode}}
            # 原子写出 outcome 记录（项目正式完成凭证）
            wave._atomic_json(episode / 'outcome.json', record)
            # 渲染 Markdown 报告并附记忆变更明细
            text = wave._outcome_text(record) + '\n\nMEMORY CHANGES:\n' + json.dumps(memory['changes'], indent=2)
            # 原子写出 Markdown 报告
            wave._atomic_text(episode / 'outcome.md', text)
            # 收集供波次总结拼接
            rendered.append(text)
            # 检查点项目需另存一份记忆快照
            if index in context.protocol['phase1']['checkpoints']:
                # 在检查点目录安装当时规范记忆的副本
                hooks.install_memory(str(root / 'checkpoints' / f'project_{index:03d}' / 'memory'),
                                     _read_memory_tree(str(root / 'memory')))
            # 该分支虚拟机已用毕，释放资源
            branch.desktop.close()
            # 从活跃列表移除（finally 不再重复关闭）
            active.remove(branch)
            # 发布分支记忆已提交事件
            event('PHASE1_BRANCH_MEMORY_COMMITTED', wave=context.plan['wave'], project=index,
                  project_id=project.project_id, terminal_outcome=branch.terminal_outcome)
        # 原子写出波次总结：课程（Curriculum）理由 + 各分支 outcome 以分隔线连接
        wave._atomic_text(context.wave_dir / 'outcomes.md',
                          f"WAVE {context.plan['wave']} CURRICULUM RATIONALE:\n{context.decision.rationale}\n\n"
                          + '\n\n---\n\n'.join(rendered))
        # 读取提交全部记忆变更后的规范记忆树
        memory = _read_memory_tree(str(root / 'memory'))
        # 更新冻结记忆副本
        hooks.install_memory(str(root / 'memory_frozen'), memory)
        # 收尾后的项目总数（含本波全部项目）
        total = context.plan['complete_wave_total']
        # 项目台账必须恰好齐全（outcome 数等于总数）
        require(len(list((root / 'episodes').glob('ep*/outcome.json'))) == total,
                'Incomplete final project ledger')
        # 项目预算（缺省视为已达上限）
        budget = context.plan.get('budget', total)
        # 达到预算即“预算耗尽”收尾，否则仍标记基础设施停止
        status = 'budget_exhausted' if total >= budget else 'infra'
        # 停止原因文案：预算耗尽（外部完整波次算力边界）vs 需要 Curriculum 继续推进
        reason = ('external complete-wave compute boundary reached' if status == 'budget_exhausted'
                  else 'recovered complete wave; persistent Curriculum continuation required')
        # 更新 state：新状态、项目数、最后项目号、停止原因、记忆清单与恢复审计指针
        state = {**context.state, 'status': status, 'projects': total,
                 'last_project': total, 'reason': reason, 'memory_manifest': _manifest(memory),
                 'infrastructure_recovery': str(audit / 'plan.json')}
        # 更新 result：状态、项目数、原因、记忆统计（文件数/字节数/树摘要）与恢复审计指针
        result = {**context.result, 'status': status, 'projects': total,
                  'stop_reason': reason, 'memory_manifest': _manifest(memory),
                  'memory_files': len(memory), 'memory_bytes': sum(map(len, memory.values())),
                  'memory_tree_sha256': _memory_tree_sha256(memory),
                  'infrastructure_recovery': str(audit / 'plan.json')}
        # 原子写回 state.json
        wave._atomic_json(root / 'state.json', state)
        # 原子写回 result.json
        wave._atomic_json(root / 'result.json', result)
        # 发布波次完成事件（标记为恢复而来）
        event('PHASE1_WAVE_COMPLETED', wave=context.plan['wave'], total_projects=total,
              recovered=True)
        # 标记恢复完成并记录 result 摘要
        wave._atomic_json(audit / 'completion.json', {'status': 'complete', 'result_sha256': digest(root / 'result.json')})
    # 恢复失败：写 failure.json（状态恒为 infra）并向上抛出
    except BaseException as exc:
        # 记录失败原因，供下次恢复的前置校验使用
        wave._atomic_json(audit / 'failure.json', {'status': 'infra', 'reason': f'{type(exc).__name__}: {exc}'})
        # 原样向上抛出
        raise
    # 无论成败都要清理资源
    finally:
        # 兜底释放仍活跃分支的虚拟机
        for branch in active:
            # 关闭失败不影响主流程
            try:
                # 尽力关闭桌面环境
                branch.desktop.close()
            # 关闭失败不影响主流程
            except Exception:
                # 吞掉关闭异常
                pass


def main():
    # 命令行入口：先构建并打印恢复计划（默认 dry-run），加 --execute 后加锁执行恢复并触发常规校验收尾
    # 以模块 docstring 作为命令行描述
    parser = argparse.ArgumentParser(description=__doc__)
    # 必填：协议文件路径
    parser.add_argument('--protocol', type=Path, required=True)
    # 开关：仅规划（默认）或真正执行
    parser.add_argument('--execute', action='store_true')
    # 解析命令行参数
    args = parser.parse_args()
    # 解析协议绝对路径（必须真实存在）
    protocol_path = args.protocol.resolve(strict=True)
    # 构建恢复计划（含全部完整性校验）
    context = build_plan(protocol_path)
    # 打印计划概要（略去庞大的输入摘要与记忆清单两个字段）
    print(json.dumps({k: v for k, v in context.plan.items() if k not in {'input_sha256', 'memory_before'}}, indent=2), flush=True)
    # 仅在显式要求时执行恢复
    if args.execute:
        # 打开并行恢复锁文件
        guard = (context.root / '.parallel_recovery.lock').open('a')
        # 非阻塞独占加锁：已有恢复在跑则直接失败
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # 执行恢复
        execute(context)
        # Let the ordinary protocol wrapper validate the now-complete Phase 1
        # and record its completion event; it reuses the result without work.
        # 调用常规 phase1 流水线：校验已完成的 Phase 1 并补记完成事件（直接复用恢复结果，不做实际工作）
        subprocess.run([
            sys.executable, '-m', 'benchmarks.osworld.pipeline',
            '--protocol', str(protocol_path), '--phase', 'phase1',
            '--execute', 'RUN-RECURSIVE-IMPROVEMENT-PHASE1'], cwd=REPO, check=True)


# 作为脚本直接运行时的入口
if __name__ == '__main__':
    # 初始化根日志格式
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    # 执行主流程
    main()
