#!/usr/bin/env python3
"""HOST-SIDE ONLY (grader-diagnostic class): build the P2 silent-audit corpus —
8-gram shingles over all 108 task instructions. The corpus file is consumed
exclusively by explore/commit.silent_audit (host process); it never enters any
agent context, and run_explore.py never imports THIS module or task_loader.
"""
# 仅限宿主侧（评分器诊断类工具）：构建 P2 静默审计语料——
# 对全部任务指令做 8-gram shingle（切词片段）指纹。
# 语料文件只被 explore/commit.silent_audit（宿主进程）消费；绝不进入任何
# Agent 上下文，run_explore.py 也绝不导入本模块或 task_loader。
import hashlib
# hashlib：语料文件整体 SHA-256（写入清单）。
import json
# json：读取 shingle 语料与写清单。
import os
# os：路径拼接、目录创建、文件大小。
import sys
# sys：调整模块搜索路径。
from pathlib import Path
# Path：定位仓库根。

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 把仓库根加入 sys.path，以便导入 config 与 explore 包。
from config.runtime_paths import resolve_root, resolve_osworld_root
# 运行时路径解析：仓库根与 OSWorld-V2 检出根。

V2 = str(resolve_osworld_root())
# OSWorld-V2 仓库根路径。
RSIAGENT = str(resolve_root())
# RSIAgent 仓库根路径。
for p in (V2, RSIAGENT):
    # 两个根都加入模块搜索路径。
    sys.path.insert(0, p)
    # 便于导入 task_loader（V2 内）与 explore（RSIAgent 内）。
os.chdir(V2)
# 切换工作目录到 OSWorld-V2（task_loader 以相对路径找 evaluation_examples）。

from task_loader import load_task_config, resolve_task_json_path   # noqa: E402
# 延迟导入任务加载器（在 sys.path 调整之后；保留原 noqa 注释）。
from explore.commit import (CORPUS_NORMALIZATION, build_corpus,    # noqa: E402
                            normalize_instruction_for_corpus)
# 语料构建工具：归一化说明、写语料、指令归一化。

OUT = os.path.join(RSIAGENT, "results", "explore", "corpus_shingles.json")
# 输出语料文件路径。

instructions, missing = [], []
# 指令列表与缺失任务 id 列表。
for i in range(1, 121):
    # 遍历全部 120 个任务槽位。
    tid = f"task_{i:03d}"
    # 任务 id（task_001..task_120）。
    try:
        # 单个任务缺失不应中断整个语料构建。
        cf = resolve_task_json_path(task_id=tid, base_dir="evaluation_examples",
                                    eval_version="v2")
        # 解析任务 JSON 路径。
        tc = load_task_config(cf, task_id=tid, base_dir="evaluation_examples",
                              eval_version="v2")
        # 加载任务配置。
        # Prefer a declared static base when present (026/041), then normalize
        # the labeled run-generated credential line used by 057/072. Secrets
        # are neither useful leakage shingles nor reproducible provenance.
        # （中文）优先取声明的静态基础指令（026/041），再归一化 057/072 中
        # "运行期生成的凭据行"标签——密钥既不是有用的泄漏指纹，也不具备可复现出处。
        ins = str(getattr(tc, "base_instruction", None)
                  or getattr(tc, "instruction", None)
                  or tc["instruction"])
        # 取指令文本：静态基础指令优先，其次属性指令，最后字典键。
        instructions.append(normalize_instruction_for_corpus(ins))
        # 归一化后加入语料。
    except Exception:                                  # noqa: BLE001
        # 任何加载失败都记为缺失（保留原 noqa 注释）。
        missing.append(tid)
        # 记录缺失任务 id。

os.makedirs(os.path.dirname(OUT), exist_ok=True)
# 确保输出目录存在。
build_corpus(OUT, instructions)
# 构建并写入 8-gram shingle 语料。
corpus_bytes = open(OUT, "rb").read()
# 读回语料字节。
shingles = json.loads(corpus_bytes)
# 解析 shingle 集合（用于计数）。
manifest = {
    "schema_version": 2,
    "benchmark": "OSWorld-V2",
    "evaluation_version": "v2",
    "normalization": CORPUS_NORMALIZATION,
    "instruction_count": len(instructions),
    "absent_task_ids": missing,
    "shingle_count": len(shingles),
    "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
}
# 组装语料清单：模式版本、基准名、归一化说明、数量、缺失 id 与语料哈希。
with open(OUT + ".MANIFEST.json", "w") as f:
    # 写清单文件。
    json.dump(manifest, f, indent=2, sort_keys=True)
    # 结构化保存。
    f.write("\n")
    # 结尾换行。
print(f"corpus built: {len(instructions)} instructions, "
      f"{len(missing)} ids absent, -> {OUT} "
      f"({os.path.getsize(OUT)} bytes, {len(shingles)} shingles, "
      f"sha256={manifest['corpus_sha256']})")
# 输出构建摘要：指令数、缺失数、路径、字节数、shingle 数与哈希。
