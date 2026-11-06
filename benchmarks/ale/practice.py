"""Fresh guest leases and ALE-specific adapters for RSIAgent's RSI hooks."""
# 【中文注释·模块定位】ALE 侧的"全新客户机租约池"与 RSI 钩子适配：
# ①CleanPool——有界的客户机槽位池，任何复用租约都会经复位 RPC 换成全新 VM（练习纪律）；
# ②Audit——宿主侧的公开指令泄漏审计（不接触评分常数/分数），把 Agent 产物与冻结记忆
# 对照 67 任务指令 8-gram 语料检查；
# ③hooks_for——把上述能力装配成共享运行时期望的 PracticeHooks（Windows 还要翻译路径）。

# from __future__ import annotations：延迟求值类型注解，仓库统一写法。
from __future__ import annotations

# dataclasses：replace() 派生修改字段后的配置副本（Windows 路径翻译用）。
import dataclasses
# json：语料与 transcript 解析。
import json
# queue：租约池的有界队列（阻塞获取）。
import queue
# re：公开路径拼写与 venv 安装命令的正则。
import re
# tarfile：捕获材料归档（materials.tgz）的泄漏审计。
import tarfile
# threading：租约借还的并发控制。
import threading
# Path：审计文件路径。
from pathlib import Path

# runtime：VM 工厂、初始化与平台绑定。
from .runtime import bind_platform, initialize_vm, make_vm


class CleanPool:
    """Bounded guest slots; every reused lease gets a freshly provisioned VM."""
    # 【中文注释】有界客户机槽位池：构造时把全部沙箱初始化为可用 VM；关键纪律是
    # "复用即全新"——除首发租约外，每次 acquire 都先通过复位 RPC 换一台全新供应的 VM，
    # 保证练习分支之间互不残留状态（等价于全新 guest）。

    def __init__(self, sandboxes, *, practice, reset_endpoint):
        # 空闲 VM 的阻塞队列。
        self.available = queue.Queue()
        # 本池全部 VM（含租出中的）。
        self.vms = []
        # 借还操作的互斥锁。
        self._lock = threading.Lock()
        # 当前已租出的 VM id 集合。
        self.leased = set()
        # 尚未首次租出的 VM id 集合（首租免复位）。
        self.first = set()
        # 是否练习池：练习池的全新 VM 还要做载荷清理。
        self.practice = practice
        # 复位 RPC 的套接字端点。
        self.reset_endpoint = reset_endpoint
        # 逐个初始化沙箱为 VM。
        for sandbox in sandboxes:
            # 按沙箱描述构造 VM 对象（Windows/Linux 分派）。
            vm = make_vm(sandbox)
            # 初始化客户机（helper、依赖探测）。
            initialize_vm(vm)
            # 绑定平台钩子（快照/Look/提示词按 OS 分派）。
            bind_platform(vm)
            if practice:
                # 练习 VM：清除预置的基准任务载荷（防泄题）。
                clean_practice(vm)
            # 登记进池。
            self.vms.append(vm)
            # 标记为"首租"候选。
            self.first.add(id(vm))
            # 放入空闲队列。
            self.available.put(vm)

    # 取一个租约：首租直接给；复用租约先复位成全新 VM（失败则归还并抛错）。
    def acquire(self):
        # 阻塞等待空闲 VM，最长 1 小时（复位/启动可能很慢）。
        vm = self.available.get(timeout=3600)
        # 加锁登记为已租出。
        with self._lock:
            self.leased.add(id(vm))
        try:
            if id(vm) in self.first:
                # 首租：构造时已清理，直接可用。
                self.first.remove(id(vm))
            else:
                # 复用：先复位成全新 VM 再交付。
                self.reset(vm)
        except BaseException:
            # 复位失败：归还租约并把异常原样抛出。
            self.release(vm)
            raise
        # 返回租约句柄与 VM。
        return Lease(self, vm), vm

    # 复位：请求宿主 RPC 销毁重建沙箱，并把 VM 对象原位刷新为新沙箱。
    def reset(self, vm):
        # 延迟导入客户端封装（避免 worker 常驻依赖）。
        from .lifecycle import request_reset

        # 向宿主请求全新沙箱（旧沙箱销毁、新沙箱启动）。
        sandbox = request_reset(self.reset_endpoint, vm.sandbox["id"])
        # 保留旧 VM 上的任务可见根目录设置。
        visible = getattr(vm, "visible_roots", ())
        # 用新沙箱描述构造一个临时 VM 作为数据来源。
        replacement = make_vm(sandbox)
        # 关闭旧 docker 客户端连接。
        vm.client.close()
        # 原位替换实例属性：把新 VM 的全部状态搬进旧对象（外部引用保持有效）。
        vm.__dict__.clear()
        vm.__dict__.update(replacement.__dict__)
        # 重新初始化客户机。
        initialize_vm(vm)
        # 重新绑定平台钩子并恢复可见根。
        bind_platform(vm, visible)
        if self.practice:
            # 练习池：新 VM 同样要清除基准载荷。
            clean_practice(vm)
        # 返回成功标记（RPC 契约）。
        return {"ok": True}

    # 归还租约：只有确属本池已租出的 VM 才回队列（防重复归还）。
    def release(self, vm):
        # 加锁校验并移出已租集合。
        with self._lock:
            if id(vm) not in self.leased:
                return
            self.leased.remove(id(vm))
        # 放回空闲队列。
        self.available.put(vm)

    # 关池：关闭全部 VM 的 docker 连接。
    def close(self):
        # 逐个关闭。
        for vm in self.vms:
            vm.client.close()


# 租约句柄：close() 即归还。
class Lease:
    def __init__(self, pool, vm):
        # 记录所属池与 VM。
        self.pool, self.vm = pool, vm

    def close(self):
        # 归还 VM 到池。
        self.pool.release(self.vm)


# 只对显式标记的一次性练习 VM 清除预置基准载荷（防泄题）。
def clean_practice(vm):
    """Remove baked benchmark payloads only inside newly allocated practice VMs."""
    # 安全校验：沙箱元数据必须显式带 rsiagent_practice=True 才允许清理，
    # 防止误把真实评估沙箱的载荷删掉。
    if not vm.sandbox.get("metadata", {}).get("rsiagent_practice", False):
        raise RuntimeError(
            "Practice cleaning requires an explicitly marked disposable guest"
        )
    # 预置载荷根目录：Windows 在 E:/agenthle，Linux 在 /media/user/data/agenthle。
    roots = ["E:/agenthle"] if vm.is_windows else ["/media/user/data/agenthle"]
    if not vm.is_windows:
        # Linux：先用 sudo 免密递归删除目录（1800 秒超时）。
        result = vm.native_command(
            "sudo -n rm -rf -- /media/user/data/agenthle", timeout=1800
        )
        # 删除失败（返回码非 0）即中止。
        if result.get("return_code"):
            raise RuntimeError(
                "Could not remove baked task payloads from the disposable practice guest"
            )
    # 生成在客户机内执行的清理脚本：删除载荷根；再删除 home 下 RSI 过程文件
    # （.memory、波次/项目目录、交接与诊断文档、curriculum 笔记、波次 JSON）。
    # 对只读文件先 chmod 后重试删除；符号链接直接 unlink；删完断言不存在。
    code = f"""import pathlib,shutil,os,stat,json
roots={roots!r}
def retry(fn,path,exc):
 os.chmod(path,stat.S_IWRITE|stat.S_IREAD|stat.S_IEXEC);fn(path)
for name in roots:
 p=pathlib.Path(name)
 if p.is_symlink(): p.unlink()
 elif p.exists(): shutil.rmtree(p,onerror=retry)
 assert not p.exists(), name
home=pathlib.Path({vm.home!r})
for name in ['.memory','evolution_project','evolution_wave','actor_handoff.md','learning_diagnosis.md','project_next.md','curriculum_notes.md','phase1_wave.json']:
 p=home/name
 if p.is_symlink() or p.is_file(): p.unlink()
 elif p.exists(): shutil.rmtree(p,onerror=retry)
print(json.dumps({{'removed_payload_roots':roots}}))
"""
    # 以受信 Python 在客户机内执行清理（1800 秒超时）。
    vm.trusted_python(code, timeout=1800)


# 公开路径拼写正则：匹配文本中出现的绝对/相对路径样式（Windows 盘符可选），
# 前置断言排除已是更长路径一部分的片段。
_PUBLIC_PATH = re.compile(
    r"(?<![\w./\\-])(?:[A-Za-z]:)?[\\/]?(?:[\w.-]+[\\/]+)+[\w.-]*"
)
# 本地 venv 安装命令正则：识别 "python -m venv .venv && uv pip install --python .venv/bin/python"
# 这类普通环境搭建前缀（审计时仅豁免该句法前缀本身）。
_LOCAL_VENV_INSTALL = re.compile(
    r"(?<![\w./-])(?:python(?:3(?:\.\d+)?)?\s+-m\s+venv|uv\s+venv)"
    r"\s+(?P<venv>\.?venv)\s*&&\s*uv\s+pip\s+install\s+--python\s+"
    r"(?P=venv)/bin/python(?:3(?:\.\d+)?)?(?![\w./-])"
)


# 路径拼写归一化：统一分隔符、剔除句尾标点、拒绝相对跳转片段。
def _path_spelling(value):
    # 反斜杠与正斜杠统一为正斜杠。
    value = re.sub(r"[\\/]+", "/", value)
    # 含 "." 或 ".." 片段：无法安全归一，返回空串。
    if any(part in (".", "..") for part in value.split("/")):
        return ""
    # A sentence-ending period is not part of its quoted filename.
    # 【中文注释】句子末尾的句号不属于被引用的文件名，连同尾斜杠一并去掉。
    return value.rstrip("./")


class _PublicTaskText:
    """Task-bound public wording and declared path spellings, never agent prompts."""
    # 【中文注释】任务绑定的公开措辞集合：来自公开任务卡与正式指令本身（绝不是 Agent
    # 提示词）。维护：①本任务声明过的路径别名（variant/input/output 拼写互认）；
    # ②可见根目录；③公开文本的全部 8-gram（含四种路径拼写的等价变体）。
    # 审计时用于解释"Agent 文本中的哪些 8-gram 是公开知识，不算跨任务泄漏"。

    def __init__(self, record):
        # 延迟导入共享审计的分片函数与常量。
        from explore.commit import _shingles

        # 正式指令文本。
        self.instruction = record["instruction"]
        # 任务 ID。
        task = record["task_id"]
        # 公开任务卡的提示词。
        prompt = record["task_prompt"]
        # 绑定合法性：指令/提示词非空字符串，任务 ID 形如 <域>/<名>。
        if (not isinstance(self.instruction, str) or not self.instruction
                or not isinstance(prompt, str) or not prompt
                or not isinstance(task, str) or not re.fullmatch(r"[\w-]+/[\w-]+", task)):
            raise ValueError("Invalid ALE public task binding")
        # 参与别名推导的两段公开文本。
        texts = (prompt, self.instruction)
        # 从两段文本中抽取声明的路径拼写并归一化。
        declared = [_path_spelling(match.group()) for text in texts
                    for match in _PUBLIC_PATH.finditer(text)]
        # 显式可见根目录（归一化）。
        visible = [_path_spelling(path) for path in record["visible_roots"]]
        # 任务根匹配器：/media/user/data/agenthle/<task>/ 或 Windows 的 <盘>:/agenthle/<task>/ 下的首段子目录。
        task_root = re.compile(
            r"^(?:/media/user/data/agenthle/|[A-Za-z]:/agenthle/)"
            + re.escape(task) + r"/([^/]+)(?:/|$)"
        )
        # 路径根 → 变体名 的映射（仅认本任务自己的根）。
        self.roots = {}
        # 检查每个可见/声明的路径是否落在任务根之下。
        for path in visible + declared:
            match = task_root.match(path)
            if match:
                # 登记：根前缀 → 首段子目录（variant/input/output 名）。
                self.roots[path[:match.end()].rstrip("/")] = match.group(1)
        # A broad ancestor such as /home/user or /media/user/data grants nothing.
        # 【中文注释】宽泛祖先目录（如 /home/user）不授予任何可见性：只保留精确落在
        # 本任务根下的路径作为可见集合。
        self.visible = tuple(path for path in visible if task_root.match(path))
        # 别名集合：声明路径的全部等价拼写。
        self.aliases = set()
        for path in declared:
            self.aliases.update(self._aliases(path))
        # 公开 8-gram 集合：两段文本的 shingle + 每种路径拼写替换后的等价文本 shingle。
        self.grams = set()
        for text in texts:
            self.grams.update(_shingles(text))
            for spelling in range(4):
                # 把文本中的路径替换为第 spelling 种等价拼写再取 shingle，
                # 使任一拼写变体的提及都算作公开措辞。
                equivalent = _PUBLIC_PATH.sub(
                    lambda match: self._aliases(_path_spelling(match.group()))[spelling], text
                )
                self.grams.update(_shingles(equivalent))

    # 展开某声明路径的 4 种等价拼写（仅限本任务声明的变体/输入/输出语法）。
    def _aliases(self, path):
        """Expand only this task's declared variant/input/output path syntax."""
        # 空路径：原样返回。
        if not path:
            return (path,) * 4
        # 尝试把路径匹配到某个已登记的任务根或变体名下。
        for root, variant in self.roots.items():
            if path.startswith(root + "/"):
                suffix = path[len(root) + 1:]
            elif path.startswith(variant + "/"):
                suffix = path[len(variant) + 1:]
            elif path.lstrip("/").startswith(("input/", "output/")):
                suffix = path.lstrip("/")
            else:
                # 不属于任何已知根：跳过。
                continue
            # 若后缀以 input//output/ 开头，再剥掉该前缀得到最短拼写。
            short = suffix.split("/", 1)[1] if suffix.startswith(("input/", "output/")) else suffix
            # 四种等价拼写：完整根前缀 / 变体名前缀 / 裸后缀 / 最短拼写。
            return root + "/" + suffix, variant + "/" + suffix, suffix, short
        # 不匹配：返回自身重复 4 次。
        return (path,) * 4

    # 定位文本中"可解释"的路径提及与普通 venv 安装前缀的字符区间。
    def occurrence_spans(self, text):
        # 收集路径区间。
        paths = []
        for match in _PUBLIC_PATH.finditer(text):
            # 归一化每个匹配。
            path = _path_spelling(match.group())
            # 仅当它是已声明别名或位于可见根之下才收录。
            if path and (path in self.aliases or any(
                    path == root or path.startswith(root + "/") for root in self.visible)):
                paths.append((match.start(), match.end()))
        # Only the syntactic venv/install prefix is ordinary setup. Package names,
        # trailing commands, and the same words elsewhere still face the audit.
        # 【中文注释】只有句法意义上的 venv/安装前缀算普通环境搭建；包名、后续命令、
        # 以及出现在别处的相同字词仍要接受审计。
        setup = [(match.start(), match.end()) for match in _LOCAL_VENV_INSTALL.finditer(text)]
        # 返回两组区间。
        return paths, setup


class Audit:
    """Host-only public-instruction leakage audit; no grader constants or scores."""
    # 【中文注释】宿主专有的"公开指令泄漏"审计器：把 Agent 文本/产物/记忆与 67 任务
    # 指令的 8-gram 语料比对，捕获跨任务指令泄漏与基准私有信息。刻意不携带任何评分
    # 常数或分数——审计只管合法性，不管对错。

    def __init__(self, corpus, public_task=None):
        # 语料文件路径。
        self.path = Path(corpus)
        # 加载语料：排序后的 8-gram 列表 → 集合。
        self.grams = set(json.loads(self.path.read_text()))
        # 语料为空或含非字符串条目：视为缺失/损坏。
        if not self.grams or any(not isinstance(v, str) for v in self.grams):
            raise RuntimeError("ALE instruction corpus is missing or malformed")
        # 可选的公开任务绑定（仅 phase1/phase2 的正式任务提供）。
        self.public_task = _PublicTaskText(public_task) if public_task is not None else None

    # 语料一致性校验：路径与内容都必须与构造时一致（防中途替换）。
    def validate(self, path):
        # 解析后路径一致，且当前文件内容与内存中集合一致。
        if (
            Path(path).resolve() != self.path.resolve()
            or set(json.loads(self.path.read_text())) != self.grams
        ):
            raise RuntimeError("ALE instruction corpus drift")
        # 返回校验回执：基准名与分片数。
        return {"benchmark": "ALE", "shingles": len(self.grams)}

    # 单段文本审计：返回违规命中列表（空=通过）。
    def text(self, text, mode="practice", authorized_instruction="", **kwargs):
        # 延迟导入共享审计的常量与分片函数。
        from explore.commit import _N, _WORD, _shingles

        # 命中收集器。
        hits = []
        # 硬性私有信息：上游仓库地址、参考归档密码、评分时间文件——出现即违规。
        if re.search(
            r"github\.com[/\\]+rdi-berkeley[/\\]+agents-last-exam|ALE_REFERENCE_ARCHIVE_PASSWORD|secret[/\\]+eval_time",
            text,
            re.I,
        ):
            hits.append({"kind": "benchmark-private-source"})
        # 授权指令自身的 shingle（通常是本任务正式指令）。
        allowed = _shingles(authorized_instruction)
        # 有授权指令时启用公开任务绑定。
        binding = self.public_task if authorized_instruction else None
        if binding is not None:
            # 绑定一致性：审计的授权指令必须与绑定的指令逐字相同。
            if authorized_instruction != binding.instruction:
                raise ValueError("ALE audit does not match its bound instruction")
            # 公开措辞（任务卡 + 指令 + 路径拼写变体）计入允许集合。
            allowed |= binding.grams
        # 嫌疑 8-gram：文本 shingle ∩ 语料 − 允许集合。
        suspect = (_shingles(text) & self.grams) - allowed
        # 有绑定且存在嫌疑时，尝试用路径/搭建前缀区间解释掉。
        if binding is not None and suspect:
            # 取可解释区间。
            paths, setup = binding.occurrence_spans(text)
            # 折叠小写副本用于词元定位。
            folded = text.lower()
            if len(folded) != len(text):
                # Unicode lowercasing can change offsets (for example İ).
                # 【中文注释】某些字符小写化会改变长度（如 İ），区间端点需按折叠后文本重算。
                paths, setup = [
                    [(len(text[:start].lower()), len(text[:end].lower()))
                     for start, end in spans] for spans in (paths, setup)
                ]
            # 折叠文本的词元迭代器列表。
            words = list(_WORD.finditer(folded))
            # 是否存在无法解释的嫌疑 8-gram。
            unexplained = False
            # 滑窗检查每个 8-gram。
            for index in range(len(words) - _N + 1):
                # 当前窗口的词元。
                group = words[index:index + _N]
                # 不在嫌疑集合：跳过。
                if " ".join(word.group() for word in group) not in suspect:
                    continue
                # 完全落在 venv 安装前缀内：视为普通搭建，解释掉。
                if any(start <= group[0].start() and group[-1].end() <= end
                       for start, end in setup):
                    continue
                # 窗口内每个词都落在公开路径区间内：视为路径提及，解释掉。
                if all(any(start <= word.start() and word.end() <= end
                           for start, end in paths) for word in group):
                    continue
                # 存在无法解释的嫌疑：标记并退出。
                unexplained = True
                break
            # 全部解释掉：清空嫌疑集合。
            suspect = suspect if unexplained else set()
        # 仍有嫌疑：记为跨任务指令泄漏。
        if suspect:
            hits.append({"kind": "other-task-instruction"})
        # 返回命中列表。
        return hits

    # 审计谱系内全部 transcript.json（递归）。
    def transcripts(self, root, mode="practice", authorized_instruction="", **kwargs):
        # 命中收集器。
        findings = []

        # 递归检查 JSON 值：字符串直接审计，容器逐项下钻。
        def inspect(value):
            if isinstance(value, str):
                findings.extend(self.text(value, mode, authorized_instruction))
            elif isinstance(value, dict):
                for item in value.values():
                    inspect(item)
            elif isinstance(value, list):
                for item in value:
                    inspect(item)

        # 遍历谱系内每个 transcript.json。
        for path in Path(root).rglob("transcript.json"):
            # 解析并递归审计。
            inspect(json.loads(path.read_text()))
        # 返回全部命中。
        return findings

    # 审计捕获归档 materials.tgz：逐成员查文件名与文本内容。
    def captured(self, root, authorized_instruction=""):
        # 命中收集器。
        findings = []
        # 打开 gzip tar 归档。
        with tarfile.open(Path(root) / "materials.tgz", "r:gz") as tf:
            # 逐成员检查。
            for member in tf:
                # 成员路径名也要审计（可能内嵌指令词句）。
                findings.extend(
                    self.text(
                        member.name, authorized_instruction=authorized_instruction
                    )
                )
                if member.isfile():
                    # 普通文件成员：读取文本内容审计（解码失败容忍）。
                    findings.extend(
                        self.text(
                            tf.extractfile(member).read().decode("utf-8", "ignore"),
                            authorized_instruction=authorized_instruction,
                        )
                    )
        # 返回全部命中。
        return findings

    # 记忆入账审计：逐文件（文件名+内容）审计，命中即整文件剔除。
    def memory(
        self,
        files,
        corpus_path,
        reject_log,
        authorized_instruction="",
        require_corpus=False,
    ):
        # 先校验语料一致性。
        self.validate(corpus_path)
        # 过滤：文件名+换行+内容拼成文本审计；无命中的文件才允许写入记忆。
        return {
            name: data
            for name, data in files.items()
            if not self.text(
                name + "\n" + data.decode("utf-8", "ignore"),
                authorized_instruction=authorized_instruction,
            )
        }


# 装配 ALE 专用的 PracticeHooks：审计器 + VM 复位 + Windows 路径翻译。
def hooks_for(pool, corpus, public_task=None):
    # 延迟导入共享运行时（worker 进程专属环境）。
    from core.loop import run_attempt
    from explore.practice_loop import PracticeHooks

    # 构造审计器（可选绑定公开任务）。
    audit = Audit(corpus, public_task=public_task)
    # 以池中首个 VM 判断本进程的 OS 与 home 路径（一个 worker 只处理一种 OS）。
    windows = pool.vms[0].is_windows
    home = pool.vms[0].home
    if windows:
        # Windows：共享运行时模块里的常量路径都以 /home/user 写死，需要整体翻译成实际 home。
        from explore import (
            phase1_wave,
            practice_loop,
            target_learning,
            unified_evolution,
        )

        # 逐模块扫描大写常量。
        for module in (practice_loop, target_learning, phase1_wave, unified_evolution):
            for name, value in vars(module).copy().items():
                if (
                    name.isupper()
                    and isinstance(value, str)
                    and value.startswith("/home/user/")
                ):
                    # 把 /home/user 前缀替换为实际 home（只替换一次）。
                    setattr(module, name, value.replace("/home/user", home, 1))

    # Windows 版 run_attempt：先翻译提示词与配置中的路径。
    def attempt(prompt, vm, cfg, sink, **kwargs):
        if windows:
            # 提示词中的 /home/user 全部替换为实际 home。
            prompt = prompt.replace("/home/user", home)
            # 配置副本：system_extra 与 practice_done_requires 里的路径同步翻译。
            cfg = dataclasses.replace(
                cfg,
                system_extra=cfg.system_extra.replace("/home/user", home),
                practice_done_requires=cfg.practice_done_requires.replace(
                    "/home/user", home
                ),
            )
        # 调用原始尝试执行。
        return run_attempt(prompt, vm, cfg, sink, **kwargs)

    # 装配钩子集：尝试执行、VM 复位、语料校验、文本/transcript/捕获/记忆审计。
    return PracticeHooks(
        run_attempt=attempt,
        reset_vm=lambda vm, target: pool.reset(vm),
        validate_corpus=audit.validate,
        audit_text=audit.text,
        audit_transcripts=audit.transcripts,
        audit_captured=audit.captured,
        audit_memory=audit.memory,
    )
