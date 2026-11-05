# Python 高级技法与重要库 100 讲

以 RSIAgent 仓库真实代码为例，每讲一个 Python 技法或重要库的用法，全部给出 `文件:行号` 可对照验证。行号以当前工作区（含中文注释版）为准。

## 一、并发与进程管理

1. **ThreadPoolExecutor + as_completed**：BRS 并行分支用线程池调度独立子进程，`as_completed` 按完成顺序而非提交顺序收集结果——`explore/phase1_wave.py:945`（`futures` 字典建立 future→序号映射，随后 `for future in as_completed(futures)`）。
2. **future→参数反向映射**：`{pool.submit(...): index for ...}` 字典推导把 future 映射回项目序号，失败时能报告是哪个分支——`explore/phase1_wave.py:932`。
3. **线程局部存储 threading.local**：并发分支各自持有最近的思考通道内容，pop 时绝不串线——`llm/client.py:61`（`_LAST_REASONING = threading.local()`）、`llm/client.py:340`（`pop_last_reasoning`）。
4. **asyncio 驱动长流程**：ALE 的准备/冒烟/运行是异步实现的，入口 `asyncio.run(prepare(args))`——`run_ale.py:96`、`run_ale.py:103`；异步容量池见 `benchmarks/ale/capacity.py:21`（`async def acquire`）。
5. **asyncio.run 的入口模式**：同步动作（report）与异步动作（prepare/smoke/run）在同一入口分流——`run_ale.py:83`（`main` 内按 `args.action` 分派）。
6. **信号处理 → finally 清理**：`signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))` 让 SIGTERM 触发 SystemExit，从而走 finally 关闭 VM——`benchmarks/osworld/task.py:27`、`benchmarks/osworld/phase1.py:54`。
7. **子进程逐阶段执行与 fail-fast**：批处理为每个任务顺序起子进程，`check=False` 拿退出码自行判定，某步非零即 break 停止后续阶段——`run_osworld.py:211`（`subprocess.run(..., check=False)`）与 `run_osworld.py:224`（`if code: break`）。
8. **sys.executable 保证同环境子进程**：所有内部阶段命令都以 `sys.executable -u -m 模块名` 启动，避免 PATH 漂移；`-u` 关缓冲保证日志实时——`run_osworld.py:69`、`run_osworld.py:107`。
9. **异常不掩盖**：finally 中释放资源失败时，若存在主失败异常则记录事件而不覆盖主异常；释放是唯一异常才上抛——`core/self_evolving_loop.py:384`（`primary_failure = sys.exc_info()[1]`）。
10. **守护式归档清理**：异常路径上对仍活跃分支尽力关闭 VM，单个失败吞掉异常优先完成整体清理——`explore/phase1_wave.py:1078`（`finally: for branch in active_branches: try: ... except Exception: pass`）。

## 二、数据类与状态建模

11. **frozen dataclass 不可变记录**：波次项目与决策一经定稿不可改（可哈希、可审计）——`explore/phase1_wave.py:135`（`@dataclass(frozen=True) class WaveProject`）。
12. **`__post_init__` 构造期校验/归一化**：frozen 对象里用 `object.__setattr__` 完成字符串→枚举的合法修正——`core/self_evolving_loop.py:76`（`TargetVerification.__post_init__`）。
13. **str 混入枚举（可序列化状态机）**：`class Phase2StopPolicy(str, Enum)` 让成员可直接比较/序列化字符串——`core/self_evolving_loop.py:46`；`TargetVerdict` 同理（`:60`）。
14. **dataclasses.replace 派生配置**：以旧配置为原型生成"改了几个开关"的新配置，不动原对象——`explore/phase1_wave.py:458`（`_branch_control_config`）、`explore/target_learning.py:489`。
15. **dataclasses.asdict 转可序列化**：结果对象整体转字典落盘——`explore/phase1_wave.py:352`（`sink.save_result(dataclasses.asdict(result))`）。
16. **field(default_factory=list)**：可变默认值的规范写法——`core/actor.py:836`（`checks: List[dict] = field(default_factory=list)`）。
17. **@property 提供只读便捷视图**：`Look.path` 把多路径拼成逗号串仅供日志展示——`core/actor.py:880`。
18. **dataclass 当"钩子表"**：`PracticeHooks` 用字段默认值绑定真实实现，测试可整体替换（依赖倒置）——`explore/practice_loop.py:1052`。
19. **dataclass 字段即注释的遥测对象**：`LoopResult` 的每个字段带行内语义注释，构成一次尝试的完整遥测——`core/loop.py:480`。
20. **类型窄化 `type(x) is int`**：显式排除 bool 混入整数参数——`core/self_evolving_loop.py:117`（`type(self.projects) is not int`）、`explore/phase1_wave.py:757`。

## 三、路径与文件系统

21. **pathlib 锚定仓库根**：`Path(__file__).resolve().parent` 使脚本从任何 CWD 启动行为一致——`run_osworld.py:41`（`ROOT`）、`run_ale.py:14`。
22. **parents[N] 取上级目录**：`Path(__file__).resolve().parents[1]` 得到仓库根——`config/runtime_paths.py:9`、`explore/practice_loop.py:65`（DEFAULT_CORPUS）。
23. **环境变量覆盖 + 默认兄弟目录**：`resolve_osworld_root` 优先 `OSWORLD_ROOT`，缺省取 `repo.parent / "OSWorld-V2"`——`config/runtime_paths.py:33`。
24. **resolve(strict=False) 宽松规范化**：允许目标尚未创建——`config/runtime_paths.py:12`（`_resolve`）。
25. **expanduser 处理 ~ 前缀**：客户机路径如 `~/.memory` 在宿主侧同样先 expanduser 再解析——`config/runtime_paths.py:51`（`resolve_path`）。
26. **os.replace 同文件系统原子换名**：记忆安装先换旧到备份、再换新进位，失败换回——`explore/practice_loop.py:385`（`os.replace(destination, backup)`）与 `:384`。
27. **tempfile.mkdtemp 建暂存目录**：`prefix=f".{destination.name}.stage-"` 且指定父目录保证同文件系统 rename——`explore/practice_loop.py:373`。
28. **os.fsync 逐层刷盘**：先文件、再目录（reverse 自底向上）、最后根目录，断电不丢页——`explore/practice_loop.py:340`（`_fsync_tree`）。
29. **symlink 防御**：`destination.is_symlink() or (exists and not is_dir())` 拒绝把记忆根装到链接上——`explore/practice_loop.py:370`；shell 侧 `test ! -L` 同理（`explore/target_learning.py:130`）。
30. **shutil.rmtree 收尾清理**：finally 中清理暂存与备份（`ignore_errors=True` 容忍竞态）——`explore/practice_loop.py:402`。

## 四、序列化与归档

31. **JSONDecoder.raw_decode 容错抽取**：扫描文本中所有"平衡的顶层 JSON 值"，成功即跳跃 i=end——`llm/client.py:621`（`json_values`）。
32. **json.dumps 规范化哈希**：`sort_keys=True, separators=(",",":"), ensure_ascii=False, allow_nan=False` 产出确定字节——`explore/memory_hash.py:32`（`_canonical_bytes`）。
33. **JSONL 追加式日志**：journal 与审计拒绝记录都用"一行一 JSON"——`explore/commit.py:202`（journal.jsonl）、`explore/commit.py:370`（reject_log）。
34. **原子写 JSON**：先写临时文件再 rename，杜绝半写状态——`explore/practice_loop.py:279`（`_atomic_json`）。
35. **tarfile 内存归档**：BytesIO + `mode="w:gz"` 在内存打包记忆树，`arcname` 去掉宿主绝对路径——`explore/commit.py:50`（`push_memory`）。
36. **手工构造 TarInfo**：不经磁盘直接从内存字节流添加成员（`info.size = len(data)`）——`explore/commit.py:205`。
37. **tar 成员过滤**：`m.isfile()` 只收普通文件、剥离 `.memory/` 前缀、剔除 `__pycache__`/`.pyc`——`explore/commit.py:139`。
38. **base64 文本化传输二进制**：tar.gz → base64 → 分块经 shell 写入 → 客户机 `base64 -d` 还原——`explore/commit.py:54`。
39. **冻结快照的树哈希**：{相对路径: 文件 SHA-256} 规范化后取根摘要，任何字节变化都会被发现——`explore/memory_hash.py:61`（`tree_sha256`）、`explore/target_learning.py:110`（`_memory_tree_sha256`）。
40. **清单（manifest）模式**：记忆/材料的 `{name: {bytes, sha256}}` 清单作为 before/after 证据——`explore/practice_loop.py:385`（`_manifest`）。

## 五、正则与文本工程

41. **命名分组解析复杂信封**：`(?P<tool>...)`/`(?P<body>...)` 提取供应商原生工具调用——`core/actor.py:751`（`_NATIVE_TOOL_CALL_RE`）。
42. **DOTALL 跨行匹配 + 非贪婪**：`re.DOTALL` 让 `.*?` 跨行捕获完整调用体——`core/actor.py:749`。
43. **MULTILINE 行首锚点**：`^` 配合 `re.MULTILINE` 抓取所有 PLAN 行——`core/actor.py:669`（`_PLAN_RE`）。
44. **fullmatch 做整串白名单**：项目 ID `[a-z0-9][a-z0-9_-]{0,63}\Z` 杜绝路径注入——`explore/phase1_wave.py:120`；`re.fullmatch` 处理 `screen:` 前缀（`core/actor.py:739`）。
45. **词形 shingle 集合交差**：8 连词滑窗 `{ ... for i in range(len(words)-_N+1) }` 做泄漏指纹——`explore/commit.py:239`（`_shingles`）。
46. **正则替换做语料归一化**：剔除运行期生成的 Overleaf 凭据，语料哈希才能稳定——`explore/commit.py:246`（`normalize_instruction_for_corpus`）。
47. **finditer 迭代取最后匹配**：循环到结束让 `m` 停在最后一个 PLAN 行——`core/actor.py:705`（`extract_plan`）。
48. **括号平衡启发式**：尾部 6000 字符开括号多于闭括号即判"程序被切半"——`llm/client.py:612`（`_looks_cut`）。
49. **多正则分支的令牌族**：同一 DECISION 令牌按生命周期变体编译成四个 Pattern——`explore/practice_loop.py:85-92`。
50. **rfind 剥离内联思考**：`txt.rfind("</think>")` 截掉思考模型的内联推理——`llm/client.py:534`。

## 六、网络与系统边界

51. **socket 监听 + accept 循环**：过滤网关的多线程 accept/relay——`explore/target_isolation.py:496`（`FilteringTcpGateway`，`_accept_loop` 在 `:596`）。
52. **TLS 拦截的自签身份**：短命 CA + 精确主机名叶子证书，仅宿主持有——`explore/target_isolation.py:376`（`GatewayIdentity`）、`:392`（`_new_gateway_identity`）。
53. **手工解析 TLS ClientHello SNI**：逐字节读取握手体提取 SNI——`explore/target_isolation.py:322`（`parse_tls_client_hello_sni`）。
54. **HTTP Host 头验证**：连接前导不足/畸形/越界分别对应三种异常——`explore/target_isolation.py:111`（`validate_http_preface`）。
55. **上下文管理器管理监听套接字**：`__enter__/__exit__` 保证网关确定关闭——`explore/target_isolation.py:593`（`__exit__`）。
56. **QEMU HMP 事务**：计分状态是 QEMU checkpoint，Verifier 效果全部落在其后并可回滚——`env/qemu_rollback.py:136`（`QemuRollbackTransaction`）、`:252`（`begin`）。
57. **Docker 网络探测**：`_docker_container_ip` 拿容器 IP 直连推送文件——`env/vm.py:507`。
58. **urllib.parse.urlsplit 解析 URL 权威段**：出口白名单只认规范主机名——`explore/target_isolation.py:21`（导入）与 `:83` 附近的 authority 解析。
59. **iptables 链编排**：NAT/FORWARD/INPUT/OUTPUT 四条自定义链构成重定向与封印——`explore/target_isolation.py:48-53`（链名常量）、`:761`（`_firewall_script`）。
60. **HTTP 反向中继 + 只读证据舞台化**：网关自己解析并连接上游，客户机选不了 IP——`explore/target_isolation.py:496`（模块头注释）；对照证据只读化见 `explore/target_learning.py:121`（`chmod -R a-w`）。

## 七、健壮性与恢复工程

61. **可恢复性分类**：`LLMTransportError.recoverable` 区分"暂停可救"与"立即失败"，4xx 配置错误不无限等——`llm/client.py:228`、`:297`（`_recoverable_transport`）。
62. **语义级传输暂停**：供应商异常不是助手消息——原样重试并把暂停从工作时钟扣除——`core/loop.py:518`（`_call_with_transport_pause`）、`time.monotonic()` 计时在 `:528`。
63. **线性退避重试**：`time.sleep(2 * (attempt + 1))` 的 4 次尝试循环——`llm/client.py:483`。
64. **HTTP 200 后的错误信封哨兵**：`_CompletionResponseError` 保留 id/provider/内嵌错误码，走统一重试策略——`llm/client.py:249`。
65. **截断恢复递归**：token 触顶且内容空/被切 → 递归调用自身加大预算、换推理档位——`llm/client.py:565`（`return chat(..., max_tokens=retry_tokens, ...)`）。
66. **fail-closed 的恢复校验**：state.json 必须真实存在、status=infra、计数自洽，否则拒绝恢复——`explore/practice_loop.py:1079`（`_resume_completed_phase1_boundary`）。
67. **一次干净重启 + 回执**：物理故障归档未计分尝试，写 retry_receipt（记录原因/模式/未提升记忆/最多 1 次）——`explore/phase1_wave.py:604`。
68. **幂等宿主拷贝重试**：`rm -rf` + `cp -a` 天然幂等，失败重试不重放模型动作——`explore/phase1_wave.py:428`。
69. **拉取重试与防抹除**：pull_memory 三次重试（教训：一次空拉取曾丢整个回合的记忆增量）——`explore/commit.py:110`。
70. **预算累计结算**：跨段重试不清空已耗预算（iters/wall 分段累计），防无限重试——`explore/phase1_wave.py:345`（`used_iters += result.iters`）。

## 八、配置与工程化

71. **dataclass Config + YAML 覆盖层**：`yaml.safe_load` 的键值逐个 setattr 到 Config——`config/settings.py:505`；Config 定义在 `:6`。
72. **.env 解析（不引第三方）**：环境变量优先，逐行 `startswith` + `split("=", 1)` 读密钥——`llm/client.py:34`（`_api_key`）。
73. **环境变量注入子进程**：`dict(os.environ, RSIAGENT_ROOT=..., OSWORLD_ROOT=...)` 叠加式构造——`run_osworld.py:185`。
74. **argparse 互斥语义与校验**：choices、`parser.error` 做参数合法性（并发≥1、VM 上限 1-16）——`run_ale.py:44`、`run_osworld.py:289`。
75. **UTC 时间戳默认名**：`datetime.now(timezone.utc).strftime("batch_%Y%m%d_%H%M%S")`——`run_osworld.py:275`。
76. **`__all__` 声明公开 API**：`explore/phase1_wave.py:1089`、`core/self_evolving_loop.py:400`——限定外部可用的名字集合。
77. **模块级 logger 命名空间**：`logging.getLogger("rsiagent.explore.commit")` 便于按模块过滤——`explore/commit.py:26`、`llm/client.py:336`。
78. **函数内延迟导入**：重依赖/易循环的模块在函数内 import（verifier、yaml、shutil、recovery 模块）——`core/verifier.py:881`、`config/settings.py:505`、`explore/commit.py:179`。
79. **frozenset 常量集合**：执行证据文件名集合不可变且可做成员快查——`explore/practice_loop.py:65`。
80. **基础锁文件（版本冻结）**：`baseline.lock.json` 冻结基准版本/任务数/种子，批处理与校验工具共读——`run_osworld.py:44`、`run_osworld.py:83`。

## 九、Shell 互操作

81. **shlex.quote 防注入**：所有路径嵌入 shell 命令前必须引用——`explore/phase1_wave.py:196`（`_remove_wave_publication`）。
82. **printf %s 分块写入**：`printf %s '<chunk>' >> /tmp/mem_in.b64` 规避参数长度上限——`explore/commit.py:57`（配合 `_CHUNK = 3000`，`:38`）。
83. **退出码回显哨兵**：`echo SYNC_RC=$?` 把 shell 结果编码进输出，Python 侧字符串判定——`explore/commit.py:59`、`explore/phase1_wave.py:199`。
84. **find -print -quit 的存在性检查**：`test -n "$(find ... -mindepth 1 -print -quit)"` 找到第一个即退出——`explore/phase1_wave.py:214`。
85. **复合命令做"删除并取证"**：`rm -rf X; ls X | head -3` 的输出即缺席证明——`explore/commit.py:96`（`rm_guest_dir`）。
86. **set -eu 的多行脚本**：一行失败立即中止，配合哨兵回显判定成功——`explore/target_learning.py:127`（`_stage_original_practice_fixtures`）。
87. **shell 变量做布尔计数**：`bad=0; ... || bad=1; echo RC=$bad` 汇总多项检查——`explore/target_learning.py:173`（`_require_owned_project_shape`）。
88. **cp -a 保属性拷贝 / chmod -R a-w 只读化**：证据对照视图的只读保障——`explore/target_learning.py:129-131`。
89. **管道合并 stderr**：`2>&1 | head -3`、`2>/dev/null; echo TAR_RC=$?` 控制取证内容——`explore/commit.py:99`、`:125`。
90. **rm -rf -- 双横线终止符**：防御以连字符开头的路径名——`explore/phase1_wave.py:426`（stage 命令）。

## 十、智能体协议与提示词工程

91. **str.format 槽位拼装系统提示词**：`_SYSTEM_TEMPLATE.format(core=..., look_bullet=..., ask_bullet=..., discipline=...)` 按运行配置组装——`core/actor.py:226`（`build_system`）。
92. **提示词模板双花括号转义**：模板里的 JSON 示例写成 `{{"program": ...}}` 防 format 吞花括号——`core/actor.py:325`。
93. **动作解析的优先级链**：`result = program or look or ask or done` 实现 Program>Look>Ask>Done 的保守优先级——`core/actor.py:962`（`parse_turn` 内）。
94. **退化检测计数（dup/extras）**：解析器统计同类动作出现次数，>=2 判为解码退化回合——`core/actor.py:966`（`result.dup = max(...)`）。
95. **无图消息保持普通 dict**：`DURABLE_IMAGES_FIELD` 只在有附件时写入，wire 边界再重构多模态消息——`llm/client.py:73`、`:119`（`durable_user_message`）、`:192`（`_wire_message`）。
96. **路由来源统计快照**：`_PROVIDER_COUNTS` 按 (model, provider) 计数，`provider_counts()` 返回深拷贝防外部改内部状态——`llm/client.py:493`、`:334`。
97. **walrus 运算符解析流式缓冲**：`while (newline := pending.find(b"\n", scan)) >= 0:` 边扫边切行——`benchmarks/ale/transport.py:62`。
98. **contextmanager 包装资源生命周期**：`benchmarks/osworld/provider.py:16`（`from contextlib import contextmanager`）——VM 供给的 with 语义。
99. **functools.wraps 保留装饰器元信息**：评估器更正的显式包装——`benchmarks/osworld/evaluator_corrections.py:4`（`from functools import wraps`）。
100. **路径归一化后再比较（ntpath/posixpath）**：Windows 侧用 `ntpath.normcase(ntpath.normpath(x))` 归一后与实际值比对——`benchmarks/ale/runtime.py:145`（海象运算符 `p := ...` 一次赋值一处使用）。

---

*以上 100 讲全部取自仓库当前代码。建议按节阅读：一~四节对应"批处理与记忆"的工程底盘，五~九节对应"传输与边界"的健壮性设计，第十节对应三智能体协议本身。*
