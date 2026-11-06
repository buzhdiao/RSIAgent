"""Interpret process and grading state without mistaking retries for progress."""
# 进程与评分状态的解读器：不要把"重试中"误读成"有进展"。
# 返回 (状态, 标记)：标记为 None 表示无需注意，否则给出需要注意的原因。
from __future__ import annotations


def attention_state(*, status, process_alive, process_state=None,
                    grading=None, seconds_since_agent_activity=None):
    # 综合状态、进程存活、进程态、评分状态与静默时长，判定当前是否需要注意。
    grading = grading or {}
    # 评分信息缺省为空 dict。
    if status == "complete":
        # 已完成：无需注意。
        return status, None
        # 原样返回状态。
    if grading.get("status") == "recovered":
        # 评分器曾中断但已恢复：标记为"评分恢复"（值得注意但非错误）。
        return "grading_recovered", None
        # 状态改写为 grading_recovered，无需人工介入。
    programming_errors = {"AttributeError", "TypeError", "NameError",
                          "ImportError", "SyntaxError", "AssertionError"}
    # 典型编程错误类型集合：这类阻塞不是重试能解决的。
    if grading.get("status") == "blocked" or (
            grading.get("status") == "pending"
            and grading.get("error_type") in programming_errors
            and grading.get("attempt", 0) >= 3):
        # 评分器明确阻塞，或待定且编程错误已重试 3 次以上：判定为真阻塞。
        return "grading_blocked", "evaluator_error"
        # 标记为评估器错误。
    if process_state in {"T", "t"}:
        # 进程处于停止态（如被 SIGSTOP/调试器暂停）：是操作员主动暂停。
        return "paused", "operator_paused"
        # 标记为操作员暂停。
    if status == "running" and not process_alive:
        # 状态仍是 running 但进程已不在：需要人工复查。
        return "needs_review", "process_exited"
        # 标记为进程退出。
    if grading.get("status") == "pending":
        # 评分器仍在重试中：这不算进展也不算失败。
        return "grading_retry", "evaluator_retry"
        # 标记为评估器重试。
    if (status == "running" and seconds_since_agent_activity is not None
            and seconds_since_agent_activity >= 900):
        # 运行中但 Agent 已静默 15 分钟以上：仅提示复查（quiet_15min_review_only）。
        return status, "quiet_15min_review_only"
        # 状态不变，附静默标记。
    return status, None
    # 其余情况：状态原样返回，无需注意。
