"""Artifact persistence — every attempt is fully replayable from results/ alone.

Layout (one attempt):
  <root>/
    iter_01/turn.txt            raw model output
    iter_01/program.py|.sh|.txt|.md  the submitted program/action payload
    iter_01/trace.txt           full captured output
    iter_01/trace_meta.json     {exit_code, secs, timed_out}
    iter_01/checks.json         {accepted, rejections, results} (if that turn was a done)
    transcript.json             the full conversation (system prompt + every message)
    result.json                 final: status, iters, programs_run, score (sealed), ...
"""
import dataclasses
import json
import os


# 工件落盘器（ArtifactSink）：把一次运行的每一步持久化到 results/ 目录，
# 使任何一次尝试都能仅凭 results/ 完整重放（transcript + 每轮的输入/输出/评分）。
class ArtifactSink:
    def __init__(self, root: str):
        # 记录本次运行的工件根目录
        self.root = root
        # 立即创建目录（已存在则忽略）
        os.makedirs(root, exist_ok=True)

    def _iter_dir(self, n: int) -> str:
        # 第 n 轮的子目录名（iter_01、iter_02 …… 零填充两位）
        d = os.path.join(self.root, f"iter_{n:02d}")
        # 惰性创建该轮目录
        os.makedirs(d, exist_ok=True)
        return d

    def save_turn(self, n: int, text: str) -> None:
        # 保存第 n 轮模型的原始输出文本
        self._write(os.path.join(self._iter_dir(n), "turn.txt"), text)

    def save_program(self, n: int, lang: str, code: str) -> None:
        # 语言标签统一小写再分派扩展名
        language = (lang or "").lower()
        if language.startswith("py"):
            # Python 程序存为 program.py
            ext = "py"
        elif language == "verifier-report":
            # 验证者报告存为 Markdown
            ext = "md"
        else:
            # 其余（shell 命令/文本动作）一律存为 .sh
            ext = "sh"
        # 写入带正确扩展名的程序文件
        self._write(os.path.join(self._iter_dir(n), f"program.{ext}"), code)

    def save_trace(self, n: int, trace) -> None:
        # 定位第 n 轮目录
        d = self._iter_dir(n)
        # 保存该轮程序捕获的完整输出
        self._write(os.path.join(d, "trace.txt"), trace.stdout)
        # 同时写元数据 JSON：退出码、耗时、是否超时、基础设施故障标志、
        # 以及观测是否被外置（context_stdout 非空表示活上下文做过截断外置）
        self._json(os.path.join(d, "trace_meta.json"),
                   {"exit_code": trace.exit_code, "secs": round(trace.secs, 1),
                    "timed_out": trace.timed_out,
                    "infra_fail": bool(
                        getattr(trace, "infra_fail", False)),
                    "context_externalized": bool(
                        getattr(trace, "context_stdout", None) is not None)})

    def save_look(self, n: int, path: str, question: str, ok: bool) -> None:
        # 记录一次 look（看图）动作：目标路径、问题、是否成功
        self._json(os.path.join(self._iter_dir(n), "look.json"),
                   {"path": path, "question": question, "ok": ok})

    def save_ask(self, n: int, question: str, answer: str, ok: bool) -> None:
        # 记录一次 ask（问用户/用户模拟器）动作：问题、回答、是否成功
        self._json(os.path.join(self._iter_dir(n), "ask.json"),
                   {"question": question, "answer": answer, "ok": ok})

    def save_summary(self, n: int, text: str) -> None:
        """The model-maintained WORK LOG at fold n (summary-mode compaction) —
        persisted per fold so summary fidelity can be audited against the full
        transcript."""
        # 保存第 n 次折叠时的 WORK LOG（模型维护的工作日志），便于对照完整
        # transcript 审计摘要是否忠实
        self._write(os.path.join(self._iter_dir(n), "worklog.txt"), text)

    def save_verify2(self, n: int, verdict: str, findings: str,
                     transcript: list = None) -> None:
        # v34: never clobber — the stall-exit/forced inspection can run at the same
        # turn as a done-time inspection (22 archived runs lost the first artifact).
        # 首选文件名 verify2.json
        path = os.path.join(self._iter_dir(n), "verify2.json")
        # 冲突序号从 2 开始
        k = 2
        # 文件已存在则换名（verify2_2.json、verify2_3.json ……），绝不覆盖旧工件
        while os.path.exists(path):
            path = os.path.join(self._iter_dir(n), f"verify2_{k}.json")
            k += 1
        # 基础记录：验证者裁决 + 发现 + 完整探查 transcript
        rec = {"verdict": verdict, "findings": str(findings),
               "transcript": transcript or []}
        # E4-A1c: the structured per-requirement judgement, when the inspector
        # reported one — the artifact that makes convergence measurable across a
        # run's inspections (items met 5 -> 7 -> 9).
        # 若验证者给出了结构化的逐需求判定（items），逐条展开存入
        items = getattr(findings, "items", ())
        if items:
            rec["items"] = [{"req": r, "status": s, "evidence": e}
                            for r, s, e in items]
        # 若验证者附带了疑虑（doubts），也一并保存
        doubts = getattr(findings, "doubts", "")
        if doubts:
            rec["doubts"] = doubts
        # 写入最终的验证记录 JSON
        self._json(path, rec)

    def save_review(self, n: int, verdict: str, findings: str, reason: str,
                    model: str, transcript: list = None) -> None:
        """R2/v31: the doubt-gated SECOND inspection at the same done-turn — its own
        artifact so it never clobbers the first inspection's verify2.json. v32.1:
        carries the reviewer's probe transcript (it was discarded before — the AB-12
        forensics could not autopsy WHY reviewer probes failed)."""
        # 疑虑引发的二次检查单独落盘（review.json），不覆盖首次检查的 verify2.json
        self._json(os.path.join(self._iter_dir(n), "review.json"),
                   {"verdict": verdict, "findings": findings,
                    "reason": reason, "model": model,
                    "transcript": transcript or []})

    def save_checks(self, n: int, accepted, rejections, results) -> None:
        # 保存 done 声明时的检查记录：接受的检查、被拒原因、逐条执行结果
        self._json(os.path.join(self._iter_dir(n), "checks.json"), {
            "accepted": [dataclasses.asdict(c) for c in accepted],
            "rejections": rejections,
            "results": [dataclasses.asdict(r) for r in results],
        })

    def save_transcript(self, system: str, history: list) -> None:
        # 保存完整会话记录：系统提示词 + 每一条消息（无损留档）
        self._json(os.path.join(self.root, "transcript.json"),
                   {"system": system, "messages": history})

    def save_result(self, payload: dict) -> None:
        # 保存最终结果（状态、轮数、分数等汇总）
        self._json(os.path.join(self.root, "result.json"), payload)

    def save_grading_state(self, payload: dict) -> None:
        """Persist post-Agent evaluator retry state without exposing it upstream."""
        # 持久化 Agent 结束后的评审重试状态（不向 Agent 暴露）
        self._json(os.path.join(self.root, "grading.json"), payload)

    def save_recovery(self, payload: dict) -> None:
        """Persist a transport-recovery boundary beside the raw transcript."""
        # 在原始 transcript 旁持久化传输恢复边界信息（供运维接管恢复）
        self._json(os.path.join(self.root, "recovery.json"), payload)

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _write(path: str, text: str) -> None:
        # 写文本文件；None 视为空串
        with open(path, "w") as f:
            f.write(text or "")

    @staticmethod
    def _json(path: str, obj) -> None:
        # 写 JSON 文件（缩进 2 空格，便于人工审阅）
        with open(path, "w") as f:
            json.dump(obj, f, indent=2)
