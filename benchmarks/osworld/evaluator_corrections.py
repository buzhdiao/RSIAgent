"""Explicit, versioned corrections applied only after Agent work terminates."""
# 【中文注释·模块定位】显式、带版本号的评测器修正（sealed evaluation，密封评测的修正环节）：
# 仅在智能体工作彻底终止之后，才允许对"冻结的评测器修订版"打上预先声明的补丁。
# 每个修正都有唯一 ID 与目标源码 sha256 绑定，杜绝在评测前悄悄改判分逻辑；
# 返回的溯源信息（provenance）必须伴随每个被修正的分数发布。
from __future__ import annotations
# 启用 PEP 563 延迟注解求值：注解不在导入时求值。

from functools import wraps
# 导入 wraps：包装函数时保留原函数的元信息（名字、文档等）。
import hashlib
# 导入 hashlib：校验冻结评测器源码与本修正实现的 sha256。
from pathlib import Path
# 导入 Path：定位评测器源码文件并读取其字节。
from types import SimpleNamespace
# 导入 SimpleNamespace：构造带 name 属性的轻量对象，模拟默认段落样式。


TASK102_STYLE_CORRECTION = "task102-missing-paragraph-style-v1"
# 本模块唯一登记的修正 ID：task_102 中"段落缺失 style 属性"问题的第 1 版修正。
TASK102_SOURCE_SHA256 = "1c6a608323d0e2561de75c8a304f830dac58e167f80f316c62e1fa0d0af49ef5"
# 该修正绑定的冻结评测器源码（task_102.py）sha256：换任何修订版都拒绝套用。


class _ParagraphWithDefaultStyle:
    """Supply an empty style name without changing the candidate document."""
    # 【中文注释】段落只读代理：为 style 为 None 的段落提供一个空样式名视图，
    # 使上游评测辅助函数可以正常取 .style.name；绝不修改候选文档本身。

    def __init__(self, paragraph):
        # 构造时保存被代理的原始段落对象。
        self._paragraph = paragraph
        # 内部持有真实段落，后续属性访问全部转发给它。
        self.style = SimpleNamespace(name="")
        # 暴露一个 name 为空串的样式对象，替代 None（修正"缺样式"这一评测崩溃点）。

    def __getattr__(self, name):
        # 除 style 外的其余属性一律转发给真实段落（仅当常规查找失败时才触发）。
        return getattr(self._paragraph, name)
        # 委托原段落返回同名属性，保证评测逻辑其余部分行为不变。


def apply_evaluator_correction(task, correction_id: str) -> dict:
    """Apply the selected correction to the exact frozen evaluator revision.

    Neither task source files nor the original benchmark locks are modified.
    The returned provenance must accompany every corrected score.
    """
    # 【中文注释】把选定的修正应用到"完全一致的冻结评测器修订版"上：
    # 不改任务源码文件、不动基准锁；返回的溯源字典必须随每个修正后的分数一起发布。
    if correction_id != TASK102_STYLE_CORRECTION:
        # 只认本模块登记的那个修正 ID，其他一概拒绝（显式、可审计）。
        raise ValueError(f"unknown evaluator correction: {correction_id}")
    namespace = type(task).evaluate.__globals__
    # 拿到该任务类 evaluate 方法所属模块的全局命名空间（评测器运行时的符号表）。
    source = Path(namespace["__file__"])
    # 由命名空间定位评测器源码文件路径。
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    # 计算评测器源码的 sha256，用于与冻结修订版比对。
    if source.name != "task_102.py" or source_hash != TASK102_SOURCE_SHA256:
        # 文件名或内容哈希任一不符，说明评测器版本漂移，拒绝修正。
        raise ValueError("evaluator correction does not match the frozen task source")
    names = ("_heading_level", "_list_kind")
    # 声明本修正要替换的两个评测辅助函数（标题级别判定、列表种类判定）。
    for name in names:
        # 逐个检查并包装目标辅助函数。
        original = namespace[name]
        # 取出命名空间中的原始函数。
        if getattr(original, "_rsiagent_correction", None) == correction_id:
            # 若已打过同一修正标记（幂等），跳过避免重复包装。
            continue

        def wrap(function):
            # 包装器工厂：闭包捕获 correction_id，给每个替换函数打上修正标记。
            @wraps(function)
            def corrected(paragraph):
                # 替换后的辅助函数：先修复 style 为 None 的段落再调用原逻辑。
                if paragraph.style is None:
                    # 命中修正目标场景：段落没有样式属性。
                    paragraph = _ParagraphWithDefaultStyle(paragraph)
                    # 用只读代理补一个空样式名，原始文档不受影响。
                return function(paragraph)
                # 调用原始辅助函数完成真正的评测计算。
            corrected._rsiagent_correction = correction_id
            # 在替换函数上留修正标记，供幂等检查与审计。
            return corrected
            # 返回打标后的替换函数。

        namespace[name] = wrap(original)
        # 用包装后的函数替换命名空间中的原函数（只影响本次进程内的评测执行）。
    # 返回修正溯源记录：随修正后的分数发布。字段含义：
    # id = 修正 ID；task_source_sha256 = 被修正评测器源码的 sha256；
    # implementation_sha256 = 本修正实现文件自身的 sha256（实现也可审计）；
    # changed_helpers = 被替换的辅助函数名单；scope = 作用域声明（仅密封评测器内部，
    # 永不触及候选/学习侧）；candidate_modified = 明确声明候选文档未被修改。
    return {
        "id": correction_id,
        "task_source_sha256": source_hash,
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "changed_helpers": list(names),
        "scope": "sealed_evaluator_only",
        "candidate_modified": False,
    }
