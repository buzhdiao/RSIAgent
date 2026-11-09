# 【中文注释·模块定位】ALE 公开文本（public task card）冲突的录得案例回归，
# 全部经绑定学习钩子（bound learning hooks）走通（benchmarks/ale.practice.Audit
# + benchmarks/ale.host.public_task_audit + benchmarks/ale.worker 装配 +
# benchmarks/ale.learning.phase2 + explore.phase1_wave 钩子传播 +
# explore.commit 的 shingle 原语）。被钉死的承诺：
# 1) 公开任务卡文本只有绑定到它的授权指令后才放行；未绑定/盲探时一律命中
#    other-task-instruction；换一条授权指令直接 ValueError；
# 2) 公开卡里的相对路径措辞与本地化绝对路径等价（含 Windows 反斜杠与反引号
#    别名）；依赖文件名对按"出现域"限定（外来前缀/.backup 后缀/裸词形仍命中）；
# 3) 相邻的自有文件清单不授权其他任务路径；宽可见祖先根也不能掩盖外来命中；
# 4) 通用环境搭建命令只认完整的真实本地 shell 语法（残缺语法/裸词袋命中）；
# 5) 私有来源（仓库 URL/eval_time/归档密码）即使出现在公开卡里也必须拒绝；
# 6) 审计三个入口（transcripts/captured/memory）共享同一公开绑定；
# 7) 宿主绑定沿 worker→Phase-1 钩子、learning.phase2→目标学习/课程两层传播，
#    且公开任务提示词绝不进入任何模型提示词（system_extra）；
# 8) 旧版未绑定审计与不受影响文本的结果保持不变（向后兼容）。
"""Recorded ALE public-text collisions, exercised through bound learning hooks."""
# 模块 docstring：录得的 ALE 公开文本冲突案例，经绑定的学习钩子走通。

import io
# 导入 io：构造内存字节流写 tar 成员。
import json
# 导入 json：写语料/转写/worker spec。
import tarfile
# 导入 tarfile：构造捕获归档（materials.tgz）。
from pathlib import Path
# 导入 Path：盘上路径构造。
from types import SimpleNamespace
# 导入 SimpleNamespace：伪造 VM/池等轻量对象。

import pytest
# 导入 pytest：参数化与异常断言。

from benchmarks.ale.practice import Audit
# 导入被测的练习审计器（语料相似度 + 公开任务绑定）。
from explore.commit import _shingles
# 导入 shingle 分词器（与主实现共享的相似度原语）。


TASK = "health_medicine/example_analysis"
# 录得案例的基准任务 ID。
ROOT = "/media/user/data/agenthle/" + TASK + "/base"
# 该任务的 guest 数据根（base 目录）。
FOREIGN = "Segment every individual cell in the provided microscope image series."
# 外来任务（另一个公开任务）的指令文本——任何场景下都应命中。


def make_audit(tmp_path, prompt, instruction, *, collision="", roots=None, task=TASK):
    """构造绑定公开任务卡的 Audit：语料含提示词/指令/冲突文本/外来文本。"""
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps(sorted(
        _shingles(prompt) | _shingles(instruction) | _shingles(collision) | _shingles(FOREIGN)
    )))
    # 语料 = 四种文本的 shingle 并集（模拟"曾在别处见过"的内容库）。
    public_task = {
        "task_id": task, "task_prompt": prompt, "instruction": instruction,
        "visible_roots": roots if roots is not None else [ROOT + "/input", ROOT + "/output"],
    }
    # 公开任务绑定卡：任务 ID、公开提示词、授权指令、可见根（默认 base 下
    # 的 input/output）。
    return Audit(corpus, public_task=public_task), public_task
    # 返回（绑定审计器, 绑定卡）。


def test_own_public_task_card_is_authorized_only_for_its_bound_instruction(tmp_path):
    """自有公开任务卡只对其绑定的授权指令放行（未绑定/换指令都拒绝）。"""
    # Sibo TCGA 案例：公开卡措辞不在 TaskLoader 更短的指令文本里。
    prompt = "Fit Kaplan-Meier, log-rank and Cox proportional hazards analyses."
    # 公开卡上的任务措辞。
    instruction = "Analyze the supplied survival cohort."
    # 实际下发的授权指令。
    audit, _ = make_audit(tmp_path, prompt, instruction)
    # 构造绑定审计器。
    assert Audit(audit.path).text(prompt, authorized_instruction=instruction)
    # 对照组：未绑定公开卡的旧版审计把自有提示词判为其他任务指令（命中）。
    assert audit.text(prompt, authorized_instruction=instruction) == []
    # 绑定后：公开提示词对授权指令零命中（放行）。
    assert audit.text(prompt) == [{"kind": "other-task-instruction"}]
    # 未授权（盲探）时同一文本必须命中 other-task-instruction。
    with pytest.raises(ValueError, match="bound instruction"):
        audit.text(prompt, authorized_instruction="A different task.")
        # 提示词只绑定它自己的指令——换授权指令直接报错（fail-closed）。
    assert audit.text(FOREIGN, authorized_instruction=instruction)
    # 外来任务文本始终命中。


@pytest.mark.parametrize("windows", [False, True])
def test_declared_relative_and_localized_output_paths_are_equivalent(tmp_path, windows):
    """公开卡的相对输出路径措辞与本地化绝对路径等价（含 Windows 反斜杠）。"""
    task = "psychology_neuro/example_coding"
    # 录得案例的任务 ID。
    root = ("E:/agenthle/" if windows else "/media/user/data/agenthle/") + task + "/base"
    # 按参数化选择 Windows/POSIX 数据根。
    prompt = "Save the completed workbook exactly to base/output/annotations.xlsx."
    # 公开卡措辞：相对 base/ 的输出路径。
    instruction = prompt.replace("base/", root + "/")
    # 授权指令：同一句话但路径已本地化为绝对路径。
    if windows:
        prompt, instruction = prompt.replace("/", "\\"), instruction.replace("/", "\\")
        # Windows 变体：正斜杠全部换成反斜杠。
    alias = "Save the completed workbook exactly to output/annotations.xlsx."
    # 第三种措辞：去掉 base/ 的相对路径别名。
    audit, _ = make_audit(tmp_path, prompt, instruction, task=task, collision=alias,
                         roots=[root + "/input", root + "/output"])
    # 语料里特意放入别名文本（确保别名单独出现在其他任务里也会命中）。
    assert Audit(audit.path).text(alias, authorized_instruction=instruction)
    # 对照组：未绑定审计把别名判为其他任务指令。
    assert audit.text(prompt, authorized_instruction=instruction) == []
    # 公开卡原措辞放行。
    assert audit.text(instruction, authorized_instruction=instruction) == []
    # 本地化后的授权指令自身放行。
    assert audit.text(alias, authorized_instruction=instruction) == []
    # 相对别名在同一绑定下同样放行（三种措辞等价）。


def test_humanoid_relative_output_phrase_matches_its_public_path(tmp_path):
    """人形任务卡的相对输出短语与公开路径等价（含反引号包裹的别名）。"""
    prompt = "Provide a visible motion demo artifact per case under /output/visual_demos/."
    # 公开卡措辞：相对根 /output/ 的产物路径。
    instruction = prompt.replace("/output/", ROOT + "/output/")
    # 授权指令：路径本地化。
    alias = "Provide a visible motion demo artifact per case under `output/visual_demos/`."
    # 别名：去斜杠 + 反引号包裹。
    audit, _ = make_audit(tmp_path, prompt, instruction, collision=alias)
    # 语料含别名。
    assert Audit(audit.path).text(alias, authorized_instruction=instruction)
    # 对照组：未绑定审计命中别名。
    assert audit.text(alias, authorized_instruction=instruction) == []
    # 绑定后别名放行（相对短语与公开路径等价）。


def test_own_dependency_filename_pair_is_occurrence_scoped(tmp_path):
    """自有依赖文件名对按"出现域"限定：改写形态/外来前缀/后缀都算命中。"""
    prompt = "Use base/input/runtime_env/pyproject.toml. Install from base/input/runtime_env/uv.lock."
    # 公开卡措辞：两个依赖文件的 base/ 相对路径。
    instruction = prompt.replace("base/", ROOT + "/")
    # 授权指令：路径本地化。
    pair = "runtime_env/pyproject.toml, runtime_env/uv.lock"
    # 别名：文件名对（不带 base/ 前缀）。
    bare = "runtime env pyproject toml runtime env uv lock"
    # 裸词形：无标点无斜杠。
    audit, _ = make_audit(tmp_path, prompt, instruction, collision=pair)
    # 语料含文件名对。
    assert Audit(audit.path).text(pair, authorized_instruction=instruction)
    # 对照组：未绑定审计命中文件名对。
    assert audit.text(pair, authorized_instruction=instruction) == []
    # 绑定后原形态放行。
    assert audit.text(pair + "\n" + bare, authorized_instruction=instruction)
    # 与裸词形同现：出现域改变，必须命中（裸词形不受授权）。
    assert audit.text("foreign/" + pair, authorized_instruction=instruction)
    # 外来路径前缀：命中。
    assert audit.text(pair + ".backup", authorized_instruction=instruction)
    # 后缀篡改（.backup 副本）：命中。


def test_adjacent_own_file_listing_does_not_authorize_other_task_paths(tmp_path):
    """相邻的自有文件清单不授权其他任务路径；宽可见根也不能掩盖外来命中。"""
    instruction = "Analyze the files supplied under " + ROOT + "/input."
    # 授权指令：分析本任务 input 下的文件。
    listing = ROOT + "/input/runtime_env/a.py\n" + ROOT + "/input/runtime_env/b.py"
    # 自有文件清单（逐行两个本任务文件）。
    foreign = listing.replace(TASK, "life_sciences/foreign_analysis")
    # 外来清单：同样的清单但任务 ID 换成别的任务。
    audit, _ = make_audit(tmp_path, instruction, instruction, collision=listing + "\n" + foreign)
    # 语料同时含自有清单与外来清单。
    assert Audit(audit.path).text(listing, authorized_instruction=instruction)
    # 对照组：未绑定审计命中自有清单。
    assert audit.text(listing, authorized_instruction=instruction) == []
    # 绑定后自有清单放行。
    assert audit.text(foreign, authorized_instruction=instruction)
    # 外来任务路径必须命中（相邻的自有清单不连带授权）。
    # CP searched outside its task; a broad visible ancestor must not hide that.
    wide, _ = make_audit(tmp_path, instruction, instruction,
                         collision=foreign, roots=["/home/user", "/media/user/data"])
    # 第二个审计器：可见根放宽到 /home/user 与 /media/user/data（宽祖先）。
    assert wide.text(foreign, authorized_instruction=instruction)
    # 关键：宽可见根绝不能把外来路径"覆盖"成可见——越界仍被报。


def test_generic_environment_setup_requires_real_local_shell_syntax(tmp_path):
    """通用环境搭建只认完整真实本地 shell 语法；裸词袋/残缺语法命中。"""
    # Sibo MERFISH 案例：普通搭建命令与另一个公开任务冲突。
    setup = "python3 -m venv .venv && uv pip install --python .venv/bin/python starfish cellpose"
    # 完整的搭建命令（真实 shell 语法）。
    gram = "venv uv pip install python venv bin python"
    # 裸词袋：同样的词、无语法。
    instruction = "Create a synthetic microscopy exercise."
    # 授权指令。
    audit, _ = make_audit(tmp_path, instruction, instruction, collision=gram)
    # 语料含裸词袋。
    assert Audit(audit.path).text(setup, authorized_instruction=instruction)
    # 对照组：未绑定审计命中搭建命令。
    assert audit.text(setup, authorized_instruction=instruction) == []
    # 绑定后完整语法放行。
    assert audit.text(gram, authorized_instruction=instruction)
    # 裸词袋命中（无真实语法不授权）。
    assert audit.text(setup + "\n" + gram, authorized_instruction=instruction)
    # 与裸词袋同现：命中。
    assert audit.text(setup + "\n" + FOREIGN, authorized_instruction=instruction)
    # 与外来文本同现：命中。
    assert audit.text("İ\n" + setup, authorized_instruction=instruction) == []
    # Unicode 前缀（土耳其 İ）+ 换行不影响语法解析：放行。
    assert audit.text(setup.replace("venv .venv", "venv other/.venv"),
                      authorized_instruction=instruction)
    # 换掉 venv 目标目录：不再是同一搭建语法，命中。
    # Only this complete setup grammar qualifies, not a command suffix quoted
    # without creating the same environment first.
    assert audit.text(".venv && uv pip install --python .venv/bin/python",
                      authorized_instruction=instruction)
    # 残缺语法（缺 venv 创建）：命中。
    assert audit.text(setup)  # No bound task authorization in blind exploration.
    # 盲探（无授权指令）时自有搭建命令也命中。


@pytest.mark.parametrize("text", [
    "https://github.com/rdi-berkeley/agents-last-exam/tree/main/tasks",
    "secret/eval_time", "ALE_REFERENCE_ARCHIVE_PASSWORD",
])
def test_private_sources_remain_rejected_even_if_a_public_card_mentions_them(tmp_path, text):
    """私有来源即使出现在公开卡里也必须拒绝（绑定不豁免私有来源）。"""
    audit, _ = make_audit(tmp_path, text, "Do the task.")
    # 公开卡提示词本身就是被参数化的私有来源文本。
    assert {hit["kind"] for hit in audit.text(text, authorized_instruction="Do the task.")} == {
        "benchmark-private-source"
    }
    # 命中类型必须是 benchmark-private-source（而不是被绑定放行）。


def test_all_audit_entrypoints_share_the_public_binding(tmp_path):
    """审计三个入口（转写/捕获/记忆）共享同一公开绑定。"""
    prompt = "Fit Kaplan-Meier, log-rank and Cox proportional hazards analyses."
    # 公开卡措辞。
    instruction = "Analyze the cohort."
    # 授权指令。
    audit, _ = make_audit(tmp_path, prompt, instruction)
    # 绑定审计器。
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "transcript.json").write_text(json.dumps({
        "messages": [{"role": "user", "content": prompt}]
    }))
    # 捕获转写：一条用户消息就是公开提示词。
    with tarfile.open(artifacts / "materials.tgz", "w:gz") as archive:
        data = prompt.encode()
        member = tarfile.TarInfo("notes.md")
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
        # 捕获归档：notes.md 内容也是公开提示词。
    assert audit.transcripts(artifacts, authorized_instruction=instruction) == []
    # 转写入口：绑定生效，零命中。
    assert audit.captured(artifacts, authorized_instruction=instruction) == []
    # 捕获归档入口：零命中。
    assert audit.memory({"notes.md": prompt.encode()}, audit.path, "unused",
                        authorized_instruction=instruction) == {"notes.md": prompt.encode()}
    # 记忆入口：绑定放行且内容原样通过（返回值与输入一致）。


def test_host_binding_reaches_worker_phase1_hooks_without_entering_any_model_prompt(tmp_path, monkeypatch):
    """宿主绑定经 worker 装配到达 Phase-1 钩子，且公开提示词不进任何模型提示。"""
    from benchmarks.ale import host, learning, worker
    # 导入宿主绑定、Phase-1 学习编排与 worker 入口。
    from explore import phase1_wave
    # 导入 Phase-1 并行进化（被 monkeypatch 的调用点）。

    prompt = "Fit Kaplan-Meier, log-rank and Cox proportional hazards analyses."
    # 公开卡措辞。
    instruction = "Analyze the cohort."
    # 授权指令。
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps(sorted(_shingles(prompt) | _shingles(FOREIGN))))
    # 语料：公开提示词 + 外来文本的 shingle 集。
    public_task = host.public_task_audit(
        TASK, {"taskId": TASK, "taskPrompt": prompt}, instruction, [ROOT + "/input"]
    )
    # 宿主侧构造绑定卡（taskId 身份字段 + 公开提示词 + 授权指令 + 可见根）。
    class Pool:
        """伪干净沙箱池：本测试只验证装配，绝不真正获取 VM。"""
        vms = [SimpleNamespace(is_windows=False, home="/home/user")]
        def __init__(self, *args, **kwargs): pass
        def acquire(self): raise AssertionError("no VM work in wiring test")
        def close(self): pass

    monkeypatch.setattr(learning, "CleanPool", Pool)
    # 替换沙箱池。
    def evolve(**kwargs):
        # 伪 Phase-1 并行进化：校验绑定传播与提示词隔离。
        assert kwargs["target_direction"] == instruction
        # 目标方向就是授权指令。
        for name in ("actor_cfg", "curriculum_cfg", "memory_cfg", "verifier_control_cfg"):
            assert prompt not in kwargs[name].system_extra
            # 关键：公开提示词绝不进入四个角色的系统提示词。
        hooks = kwargs["hooks"]
        assert hooks.audit_text(prompt, authorized_instruction=instruction) == []
        # 钩子上的审计绑定生效：公开提示词放行。
        assert hooks.audit_text(prompt)  # Caller has not authorized target content.
        # 未授权时仍命中（绑定不放松盲探）。
        assert hooks.audit_text(FOREIGN, authorized_instruction=instruction)
        # 外来文本命中。
        memory = tmp_path / "result-memory"
        memory.mkdir()
        return SimpleNamespace(memory_dir=str(memory), status="saturated", projects=0, reason="done")
        # 返回饱和收尾结果。
    monkeypatch.setattr(phase1_wave, "evolve_parallel_phase1", evolve)
    # 替换 Phase-1 进化入口。
    work = tmp_path / "phase1"
    work.mkdir()
    spec = tmp_path / "worker-spec.json"
    spec.write_text(json.dumps({
        "phase": "phase1",
        "work_dir": str(work), "practice_sandboxes": [{}], "reset_endpoint": "unused",
        "corpus": str(corpus), "instruction": instruction, "audit_public_task": public_task,
    }))
    # worker spec：绑定卡随 spec 下发（这就是"经 worker 传播"的通道）。
    monkeypatch.setattr(worker.sys, "argv", ["worker", str(spec)])
    # 伪造命令行参数。
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)
    # 屏蔽信号注册（测试环境）。
    assert worker.main() == 0
    # worker 正常退出。
    result = json.loads((work / "worker-result.json").read_text())
    # 读取 worker 结果。
    assert result["transition_ready"]
    # 就绪进入下一阶段。
    assert result["official_evaluator_calls"] == 0
    # 零官方评分调用。


@pytest.mark.parametrize("identity", ["taskId", "task_id"])
def test_host_accepts_both_existing_card_identity_fields(tmp_path, identity):
    """宿主绑定卡同时接受两种历史身份字段（taskId/task_id），冲突即拒绝。"""
    from benchmarks.ale.host import public_task_audit
    # 导入宿主侧绑定卡构造函数。

    assert public_task_audit(TASK, {identity: TASK, "taskPrompt": "Public task."},
                             "Issued task.", []) == {
        "task_id": TASK, "task_prompt": "Public task.", "instruction": "Issued task.",
        "visible_roots": [],
    }
    # 两种身份字段都归一为 task_id；可见根为空列表。
    with pytest.raises(ValueError, match="selected ALE task"):
        public_task_audit(TASK, {"taskId": TASK, "task_id": "other/task",
                                "taskPrompt": "Public task."}, "Issued task.", [])
        # 两个身份字段互相冲突 → 报"选定的 ALE 任务"错误。


def test_phase2_target_learning_and_curriculum_receive_bound_audit(tmp_path, monkeypatch):
    """Phase-2 把绑定审计同时传给目标学习与课程两层，且提示词不进系统提示。"""
    from dataclasses import replace
    from benchmarks.ale import host, learning
    from benchmarks.ale.protocol import memory_record
    from core import loop
    from explore import target_learning, unified_evolution
    from explore.practice_loop import _atomic_install_memory
    # 导入被测装配链的各层。

    prompt = "Fit Kaplan-Meier, log-rank and Cox proportional hazards analyses."
    # 公开卡措辞。
    instruction = "Analyze the cohort."
    # 授权指令。
    audit, _ = make_audit(tmp_path, prompt, instruction)
    # 绑定审计器（语料由 make_audit 构造）。
    public_task = host.public_task_audit(
        TASK, {"task_id": TASK, "taskPrompt": prompt}, instruction, [ROOT + "/input"]
    )
    # 宿主侧绑定卡。
    initial = tmp_path / "initial"
    _atomic_install_memory(str(initial), {"seed.md": b"initial lesson"})
    # Phase-2 输入记忆（一颗种子）。
    work = tmp_path / "phase2"
    work.mkdir()
    # Phase-2 工作目录。
    seen = []
    # 记录两层钩子的到达顺序。

    class Pool:
        """伪干净沙箱池：acquire 返回可关闭沙箱与空 VM 形状。"""
        vms = [SimpleNamespace(is_windows=False, home="/home/user")]
        def __init__(self, *args, **kwargs): pass
        def acquire(self):
            return SimpleNamespace(close=lambda: None), SimpleNamespace()
        def close(self): pass

    monkeypatch.setattr(learning, "CleanPool", Pool)
    # 替换沙箱池。
    real_hooks = learning.hooks_for
    def hooks(pool, corpus, binding):
        # 保留真实的 Audit/hooks 构造，只替换 VM 上传（push_memory 恒真）。
        return replace(real_hooks(pool, corpus, binding), push_memory=lambda *a: True)
    monkeypatch.setattr(learning, "hooks_for", hooks)
    # 包装钩子工厂（绑定审计的真实构造路径被保留）。

    def actor(direction, vm, cfg, sink, **kwargs):
        # 伪 Actor 循环：校验指令与提示词隔离后直接 PASS 交接。
        assert direction == instruction and prompt not in cfg.system_extra
        # 目标方向是授权指令；公开提示词不进系统提示词。
        history = [{"role": "assistant", "content": prompt}]
        return loop.LoopResult(status="done", verifier_route="HANDOFF",
                               verifier_report="VERDICT: PASS\nAll rows checked"), history
        # PASS 报告 + 最小历史。
    monkeypatch.setattr(loop, "run_with_resume", actor)
    # 替换可续跑循环。

    def learn(**kwargs):
        # 伪目标学习：校验到达学习层的绑定审计。
        assert kwargs["target"] == instruction
        bound = kwargs["hooks"]
        assert bound.audit_transcripts(work, authorized_instruction=instruction) == []
        # 绑定审计在工作目录上放行（目录无捕获内容）。
        assert bound.audit_transcripts(work)
        # 未授权时同一调用命中（绑定确实在起作用）。
        assert bound.audit_text(FOREIGN, authorized_instruction=instruction)
        # 外来文本命中。
        seen.append("target-learning")
        return kwargs["before_memory"], "Ready for review", kwargs["actor_history"]
        # 记忆不变、诊断、历史原样。
    monkeypatch.setattr(target_learning, "_promote_learning", learn)
    # 替换学习晋升。

    def evolve(vm, root, direction, report, diagnosis, memory, *configs, **kwargs):
        # 伪统一进化：校验到达课程层的绑定审计与四配置隔离。
        assert direction == instruction and all(prompt not in cfg.system_extra for cfg in configs)
        # 指令正确；所有配置的系统提示词都不含公开提示词。
        assert kwargs["hooks"].audit_text(prompt, authorized_instruction=instruction) == []
        assert kwargs["hooks"].audit_text(prompt)
        # 授权放行 / 未授权命中的成对校验。
        seen.append("curriculum")
        destination = Path(root) / "memory"
        _atomic_install_memory(str(destination), memory)
        return SimpleNamespace(status="ready_for_retry", memory_dir=str(destination),
                               projects=0, reason="No further practice")
        # 落进化记忆并就绪。
    monkeypatch.setattr(unified_evolution, "evolve_until_ready", evolve)
    # 替换进化入口。
    result = learning.phase2({
        "work_dir": str(work), "input_memory": memory_record(initial), "sandbox": {},
        "practice_sandboxes": [{}], "reset_endpoint": "unused", "corpus": str(audit.path),
        "instruction": instruction, "audit_public_task": public_task,
    })
    # 执行 Phase-2（绑定卡随参数下发）。
    assert seen == ["target-learning", "curriculum"]
    # 绑定审计先到目标学习、再到课程（两层都拿到）。
    assert result["target_cycles"] == 1 and result["learning_updates"] == 1
    # 一个目标周期、一次学习更新。
    assert result["transition_ready"] and result["official_evaluator_calls"] == 0
    # 就绪进入 Phase-3，零官方评分调用。


def test_old_unbound_and_unaffected_text_keep_their_results(tmp_path):
    """旧版未绑定审计与不受影响文本的结果保持不变（向后兼容）。"""
    target = "Summarize this public data table and return a concise analysis."
    # 一条普通任务文本（绑定其自身）。
    audit, _ = make_audit(tmp_path, target, target)
    # 绑定审计器（提示词=指令）。
    legacy = Audit(audit.path)
    # 同语料的旧版（未绑定）审计器。
    for text in (target, FOREIGN, "Ordinary Python output: 12 rows.", "secret/eval_time"):
        assert audit.text(text, authorized_instruction=target) == legacy.text(
            text, authorized_instruction=target
        )
        # 四种文本（自有/外来/普通输出/私有来源）在绑定与未绑定审计下结果
        # 完全一致——绑定不改变这些文本的原有判定。
