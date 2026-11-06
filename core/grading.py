"""Post-Agent grading transport that preserves completed work across outages.

This module knows nothing about benchmark tasks, graders, scores, or candidate state.
It only retries an opaque operation after the Actor/Verifier lifecycle has ended.
Keeping the retry here makes the no-abandonment policy testable without importing the
sealed benchmark boundary from :mod:`benchmarks.osworld.task`.
"""
from __future__ import annotations

# 仅用于默认的重试间隔睡眠函数
import time


# 评分被阻断异常：评审器重复出现同类缺陷、需要人工修复时抛出（而非无限盲目重试）
class GradingBlocked(RuntimeError):
    """A repeated evaluator defect needs repair; preserve the completed VM."""

    def __init__(self, attempt, error):
        # 记录失败尝试次数，供调用方写入状态记录
        self.attempt = attempt
        # 保存最后一次异常对象，便于排查
        self.error = error
        # 把阻断摘要（第几次尝试、错误内容）写入异常消息
        super().__init__(f"grading blocked after {attempt} attempts: {error}")


# 视为“编程错误”的异常类型：确定性缺陷，重复出现说明评审器代码本身坏了
_PROGRAMMING_ERRORS = (AttributeError, TypeError, NameError, ImportError,
                       SyntaxError, AssertionError)


# 反复运行评审操作直至成功：瞬态故障不设次数上限，重复编程错误转为 GradingBlocked
def run_until_success(operation, *, on_error=None, retry_delay=30.0,
                      sleep_fn=time.sleep, max_identical_programming_errors=3):
    """Run ``operation`` until it returns, preserving state after every exception.

    Transient evaluator/provider outages have no attempt ceiling. Repeated identical
    programming errors raise ``GradingBlocked``; callers must preserve completed
    state and expose the blockage to operators. ``KeyboardInterrupt`` and
    ``SystemExit`` remain operator-controlled terminal signals because they are not
    ``Exception`` subclasses.
    """
    # 尝试计数器（每失败一次加 1）
    attempt = 0
    # 上一次错误的指纹（类型+消息），用于识别“连续相同的错误”
    previous_error = None
    # 连续相同错误计数
    identical_errors = 0
    # 无限循环：直到操作成功返回，或编程错误达到阈值抛出阻断
    while True:
        try:
            # 执行不透明的评审操作；成功即把结果返回给调用方
            return operation()
        except Exception as exc:  # noqa: BLE001 - opaque external evaluator boundary
            # 失败计数 +1（KeyboardInterrupt/SystemExit 不是 Exception，不会被吞掉）
            attempt += 1
            # 若调用方提供了回调，则通知它本次失败（如持久化重试状态）
            if on_error is not None:
                on_error(attempt, exc)
            # 以（异常类型, 异常消息）作为错误指纹
            error_key = (type(exc), str(exc))
            # 与上一次指纹相同则累加，不同则重新计数
            identical_errors = (identical_errors + 1
                                if error_key == previous_error else 1)
            # 记录本次指纹，供下一轮比较
            previous_error = error_key
            # 属于编程类错误且连续重复达到阈值：判定评审器需要人工修复
            if (isinstance(exc, _PROGRAMMING_ERRORS)
                    and identical_errors >= max_identical_programming_errors):
                # 抛出阻断异常；调用方必须保留已完成的 VM/候选并向运维暴露
                raise GradingBlocked(attempt, exc) from exc
            # 睡眠指定延迟后重试（负值钳为 0）
            sleep_fn(max(0.0, float(retry_delay)))
