# 【中文注释·模块定位】极简 OpenRouter 客户端：健壮 JSON 处理 + 持久多模态历史。
# RSIAgent 始终是 code-as-policy 智能体：原生 Look 附件以"精确图像字节"传输，并在
# 内部轨迹（transcript）中无损保留——后续回合与恢复的进程看到的是同样的像素，
# 不需要 Agent 自己转述。API 密钥来自 $OPENROUTER_API_KEY 或 .env 文件。
"""Minimal OpenRouter client with robust JSON and durable multimodal history.

RSIAgent remains a code-as-policy agent. Native Look attachments are transported as
exact image bytes and retained losslessly in the internal transcript so later turns
and recovered processes receive the same observations. The key comes from
``$OPENROUTER_API_KEY`` or a ``.env`` file.
"""
# base64：图像字节 → ASCII（嵌入 JSON 轨迹与 wire 消息）。
import base64
# hashlib：图像附件的 SHA-256 摘要（审计用，不筛选证据）。
import hashlib
# json：JSON 解码器（raw_decode 容错抽取）。
import json
# logging：传输层可观测性日志。
import logging
# os：读取环境变量中的 API 密钥。
import os
# threading：线程局部存储——并行分支各自的 reasoning 通道互不串线。
import threading
# time：重试退避 sleep。
import time

# OpenAI SDK：RSIAgent 通过 OpenAI 兼容协议访问 OpenRouter 网关。
from openai import APIConnectionError, APIError, APITimeoutError, OpenAI

# resolve_env_file：按运行时路径规则定位 .env 文件（支持 RSIAGENT_ENV_FILE 覆盖）。
from config.runtime_paths import resolve_env_file


def _api_key() -> str:
    # API 密钥解析：优先环境变量；为空则逐行扫描 .env 文件找 OPENROUTER_API_KEY=。
    k = os.environ.get("OPENROUTER_API_KEY", "")
    if not k:
        # 环境变量缺席时回退到 .env（resolve_env_file 已处理布局差异）。
        src = resolve_env_file()
        try:
            with src.open(encoding="utf-8") as handle:
                for line in handle:
                    # 只认行首精确前缀（split("=", 1) 只切第一个等号，值里可含等号）。
                    if line.startswith("OPENROUTER_API_KEY="):
                        k = line.split("=", 1)[1].strip()
                        break
        except FileNotFoundError:
            # .env 也不存在：返回空串（后续请求会得到 401，由调用方处理）。
            pass
    return k


# 模块级单例：OpenAI 客户端惰性构造（首次 chat() 才建连）。
_client = None
# Phase1 branches and delegated eyes issue completions concurrently. The next
# pop must retrieve this execution thread's response, never another branch's.
# 【中文注释】Phase 1 分支与受托视觉（eyes）会并发发起补全。下一次 pop 必须取回
# "本执行线程"的响应，绝不能拿到别的分支的——threading.local 提供线程隔离。
_LAST_REASONING = threading.local()
# 每进程的路由来源统计：model -> provider -> 成功响应计数（审计溯源用）。
_PROVIDER_COUNTS = {}              # model -> provider -> successful response count

# Internal transcript field for exact image bytes that a native-sighted agent has
# already received. The field is deliberately separate from ``content`` so existing
# text-history consumers and recovery validators keep their simple contract. ``chat``
# removes this private field and reconstructs an ordinary OpenAI multimodal message at
# the wire boundary. Transcripts therefore remain JSON-safe and lossless across turns
# and process recovery without asking the agent to summarize what it saw.
# 【中文注释】内部轨迹的专用字段：保存原生视觉 Agent 已收到的精确图像字节。
# 刻意与 content 分离——既有文本历史消费者与恢复校验器保持简单契约。chat() 在
# wire 边界移除该私有字段并重构普通 OpenAI 多模态消息。轨迹因此跨回合、跨进程
# 恢复都保持 JSON 安全且无损，无需让 Agent 摘述自己看到了什么。
DURABLE_IMAGES_FIELD = "_rsiagent_images"


def _image_mime(data: bytes) -> str:
    # 按魔数（magic bytes）识别图像 MIME 类型。
    if data.startswith(b"\xff\xd8"):
        # JPEG：FF D8 文件头。
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        # PNG：完整 8 字节签名。
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        # GIF：两个版本签名。
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        # WebP：RIFF 容器 + 偏移 8 的 WEBP 标记。
        return "image/webp"
    # Look preparation normally emits PNG/JPEG. Preserve the old PNG fallback for
    # an uncommon provider-supported payload whose signature is not recognized.
    # 【中文注释】Look 准备通常产出 PNG/JPEG；未识别签名的罕见载荷沿用旧的 PNG
    # 兜底（供应商多能容忍，避免直接丢弃）。
    return "image/png"


def _image_list(image) -> list[bytes]:
    # 图像参数归一化：None → 空列表；单值 → 单元素列表；统一成 bytes 列表。
    if image is None:
        return []
    # isinstance 判别单值/列表；再做逐元素类型归一。
    values = image if isinstance(image, list) else [image]
    out = []
    for value in values:
        if isinstance(value, str):
            # A few transport tests historically use a short string as fake bytes.
            # 【中文注释】部分传输测试历史上用短字符串当假字节——编码成 UTF-8 兼容。
            value = value.encode("utf-8")
        if isinstance(value, (bytearray, memoryview)):
            # 可变字节容器/内存视图统一转不可变 bytes。
            value = bytes(value)
        if not isinstance(value, bytes):
            # 仍然不是字节 → 参数错误（显式拒绝，不猜）。
            raise TypeError("image attachments must be bytes or a list of bytes")
        out.append(value)
    return out


def durable_user_message(content: str, image=None) -> dict:
    """Build a JSON-safe transcript message for one successfully sent user turn.

    Images are stored in full, without resizing, truncation, OCR, or interpretation.
    A digest makes the archived bytes auditable; it does not select or rank evidence.
    """
    # 构造"成功发送的用户回合"的 JSON 安全轨迹消息。
    # 【中文注释】图像全量存储：不缩放、不截断、不 OCR、不解释；SHA-256 摘要只让
    # 归档字节可审计，不做证据筛选或排序（保持观察的中立性）。
    message = {"role": "user", "content": content}
    attachments = []
    for data in _image_list(image):
        # 每张图：MIME 类型 + SHA-256 + base64 全量字节。
        attachments.append({
            "mime_type": _image_mime(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode("ascii"),
        })
    if attachments:
        # 有附件才写私有字段（保持无图消息与普通 dict 完全一致）。
        message[DURABLE_IMAGES_FIELD] = attachments
    return message


def has_durable_images(message: dict) -> bool:
    # 判定消息是否携带持久图像附件（bool() 做真值收敛）。
    return bool(isinstance(message, dict)
                and message.get(DURABLE_IMAGES_FIELD))


def durable_images(message: dict) -> list[dict]:
    """Return copied, schema-valid durable attachments from an internal message."""
    # 取出"拷贝且模式合法"的持久附件（防御式读取：坏结构直接跳过）。
    values = (message.get(DURABLE_IMAGES_FIELD, [])
              if isinstance(message, dict) else [])
    out = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        mime = item.get("mime_type")
        data = item.get("data")
        # mime 与 data 都必须是字符串才算合法条目。
        if isinstance(mime, str) and isinstance(data, str):
            copied = {"mime_type": mime, "data": data}
            if isinstance(item.get("sha256"), str):
                # sha256 可选携带。
                copied["sha256"] = item["sha256"]
            out.append(copied)
    return out


def with_durable_images(message: dict, attachments) -> dict:
    """Copy ``message`` and attach deduplicated archived observations."""
    # 拷贝消息并合并去重后的归档观察（按 sha256 或 (mime, data) 复合键去重）。
    result = dict(message)
    merged = []
    seen = set()
    for source in (durable_images(message), list(attachments or [])):
        for item in source:
            # 优先 sha256 指纹；缺省退回 (mime_type, data) 元组键。
            key = item.get("sha256") or (item.get("mime_type"), item.get("data"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(item))
    if merged:
        result[DURABLE_IMAGES_FIELD] = merged
    else:
        # 没有任何附件：移除该字段（保持消息形态干净）。
        result.pop(DURABLE_IMAGES_FIELD, None)
    return result


def _wire_message(message: dict) -> dict:
    """Translate one internal history message into the provider wire schema."""
    # 内部消息 → 供应商 wire 模式的翻译器（wire 边界剥离私有字段）。
    result = dict(message)
    attachments = durable_images(result)
    result.pop(DURABLE_IMAGES_FIELD, None)
    if attachments:
        # 有附件：content 重构为多模态 parts（文本 + data URL 图像）。
        text = result.get("content", "")
        if not isinstance(text, str):
            raise TypeError("durable image history requires textual user content")
        parts = [{"type": "text", "text": text}]
        for item in attachments:
            # data URL 格式：data:<mime>;base64,<payload>。
            parts.append({
                "type": "image_url",
                "image_url": {
                    "url": (f"data:{item['mime_type']};base64,"
                            f"{item['data']}")
                },
            })
        result["content"] = parts
    return result


def _request_messages(system: str, user: str, history=None, image=None) -> list:
    """Construct the provider request, rehydrating every durable observation."""
    # 构造完整请求消息序列：system → 历史（逐条重水化持久观察）→ 当前用户消息。
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(_wire_message(message) for message in history)
    messages.append(_wire_message(durable_user_message(user, image)))
    return messages


# 传输错误：补全请求在"Agent 回复存在之前"就失败了。
class LLMTransportError(RuntimeError):
    """A completion request failed before an agent reply existed.

    ``recoverable`` means an external-state change or provider recovery can make the
    exact same request valid later.  Agent runtimes pause on those failures without
    manufacturing an empty assistant message.  Non-recoverable request/configuration
    errors remain ordinary exceptions so a bad experiment cannot wait forever.
    """

    # 【中文注释】recoverable=True 表示"外部状态变化或供应商恢复能让同一请求稍后
    # 成功"——Agent 运行时对此暂停（pause）而不伪造空助手消息；不可恢复的请求/
    # 配置错误保持普通异常，让坏实验尽快失败而不是永远等待。
    def __init__(self, model: str, cause: Exception, *, recoverable: bool,
                 status_code: int = None, detail: str = ""):
        # 记录模型、根因、可恢复性与状态码/细节，供上层恢复逻辑分类。
        self.model = model
        self.cause = cause
        self.recoverable = bool(recoverable)
        self.status_code = status_code
        self.detail = detail
        # 组装人类可读消息：HTTP 状态与细节可选附加。
        status = f" HTTP {status_code}" if status_code is not None else ""
        suffix = f"{status}: {detail}" if detail else status
        super().__init__(f"{model} transport failure{suffix}")


# 成功 HTTP 交换中包含错误或无补全的哨兵异常（ValueError 子类）。
class _CompletionResponseError(ValueError):
    """A successful HTTP exchange contained an error or no completion.

    OpenRouter can send an error envelope after committing HTTP 200. Preserve
    that envelope and generation ID; its embedded code drives the same retry
    policy as ordinary HTTP errors. An unclassified empty response stays
    retryable, since it does not establish a malformed request.
    """

    # 【中文注释】OpenRouter 可能在提交 HTTP 200 之后才发错误信封——保留该信封与
    # 生成 ID；信封内嵌错误码驱动与普通 HTTP 错误相同的重试策略。未分类的空响应
    # 保持可重试（它并不能证明请求是坏的）。
    def __init__(self, response):
        error = getattr(response, "error", None)
        # 保留 id/provider/error 三元组（日志与重试分类用）。
        self.body = {"id": getattr(response, "id", None),
                     "provider": getattr(response, "provider", None),
                     "error": error}
        # 提取内嵌错误码并归一化为 int。
        value = error.get("code") if isinstance(error, dict) else None
        try:
            code = int(value)
        except (TypeError, ValueError):
            code = None
        # 只有 4xx/5xx 才视作 HTTP 语义状态码，否则 None。
        self.status_code = code if code is not None and 400 <= code <= 599 else None
        super().__init__("response contained a provider error" if error
                         else "response had no choices")


def _status_code(exc) -> int:
    """Best-effort status extraction across OpenAI SDK exception versions."""
    # 尽力提取 HTTP 状态码：兼容不同 SDK 版本（直接属性或 response.status_code）。
    value = getattr(exc, "status_code", None)
    if value is None:
        value = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _recoverable_transport(exc) -> bool:
    """Whether retrying the unchanged request later can reasonably succeed."""
    # 判定"稍后原样重试是否可能成功"（决定上层是暂停还是终止）。
    status = _status_code(exc)
    if status is None:
        # Do not turn a local programming/configuration error (for example a bad SDK
        # keyword raising TypeError) into an infinite infrastructure wait. The SDK's
        # connection/API family, ordinary socket failures, and our explicit malformed
        # response sentinel are the status-less conditions retrying can repair.
        # 【中文注释】无状态码时不把本地编程/配置错误（如错误关键字引发 TypeError）
        # 变成无限基础设施等待。可重试的无状态条件仅限：SDK 连接/API 异常族、普通
        # socket 失败、以及我们显式的坏响应哨兵。
        return (isinstance(exc, (_CompletionResponseError,
                                 APIConnectionError, APITimeoutError, APIError,
                                 ConnectionError, TimeoutError))
                or (isinstance(exc, ValueError)
                    and str(exc) == "response had no choices"))
    if status in {401, 402, 403, 408, 409, 425, 429}:
        # 401/402/403（密钥/额度/权限，外部可修复）、408/409/425/429（超时/冲突/
        # 过早/限流）→ 可恢复。
        return True
    if 500 <= status <= 599:
        # 5xx 服务端错误 → 可恢复。
        return True
    # 400/404/405/422 generally identify a bad model slug, unsupported parameter,
    # or malformed request. Pausing cannot repair the unchanged request.
    # 【中文注释】400/404/405/422 一般意味着模型名错误、参数不支持或请求畸形——
    # 暂停救不了原样的请求，视为不可恢复。
    return False


def reset_provider_counts() -> None:
    """Reset per-process routing provenance before one benchmark task starts."""
    # 在一个基准任务开始前清零路由来源统计（保证审计计数只属于本任务）。
    _PROVIDER_COUNTS.clear()


def provider_counts() -> dict:
    """Return a JSON-safe snapshot of providers observed in successful responses."""
    # 返回 JSON 安全快照（外层新 dict，内层拷贝——防止调用方改到内部状态）。
    return {model: dict(counts) for model, counts in _PROVIDER_COUNTS.items()}


def pop_last_reasoning() -> str:
    """Return-and-clear the thinking channel of the last chat() call. The official
    K3 protocol: 'add the COMPLETE assistant message to the next request. Do not
    keep only content' — the loop re-attaches this to history when
    cfg.reasoning_in_history is set."""
    # 取回并清空"本线程"最近一次 chat() 的思考通道内容。
    # 【中文注释】K3 官方协议要求"把完整助手消息加入下一请求，不能只留 content"；
    # cfg.reasoning_in_history 开启时，主循环把它重新附到历史里。
    # pop 语义（取出即清空）防止旧内容串进后续回合。
    r = getattr(_LAST_REASONING, "value", "")
    _LAST_REASONING.value = ""
    return r


def _c() -> OpenAI:
    # 惰性单例：首次调用才构造 OpenAI 客户端（base_url 指向 OpenRouter 网关）。
    global _client
    if _client is None:
        _client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=_api_key())
    return _client


def chat(model: str, system: str, user: str,
         max_tokens: int = 10000, temperature: float = 0.0,
         reasoning_effort: str = None, history: list = None,
         image=None, reasoning_max_tokens: int = 0, top_p: float = -1.0,
         provider_order=None, provider_allow_fallbacks: bool = True,
         provider_require_parameters: bool = False,
         json_object: bool = False) -> str:
    """One chat completion. ``history`` is the growing conversation (working memory —
    dropping it was anchor's biggest bug). ``image`` (png/jpg bytes, or a LIST of
    them — v13 auto-tiling attaches an overview plus native-resolution tiles) rides
    the current call. Callers record that exact attachment with
    :func:`durable_user_message`, allowing later calls and process recovery to see the
    same pixels without an agent-authored transcription. ``reasoning_effort`` is
    passed to the provider: "none" DISABLES reasoning where the model supports that;
    effort GRADATIONS (including model-specific ``max``) are honored by some models but
    IGNORED by MiniMax-M3 (verified 2026-07-06: low==high, M3 scales reasoning with
    problem difficulty, not the knob). GLM-5.3 does not support disabling thinking, so
    its truncation recovery uses ``low``. None omits the field. ``provider_order`` is
    an optional OpenRouter route preference; when present, it is never silently dropped
    after an API error because that would change the frozen experiment mid-run."""
    # 【中文注释】单次 chat 补全。history 是持续增长的工作记忆（丢弃它曾是最大的
    # bug）；image 支持字节或字节列表（v13 自动平铺：总览图 + 原生分辨率瓦片）；
    # 调用方用 durable_user_message 记录精确附件，后续调用与进程恢复看到同样像素。
    # reasoning_effort="none" 在支持的模型上禁用推理；分档在部分模型有效但被
    # MiniMax-M3 忽略（实测 low==high，M3 按题目难度伸缩推理）；GLM-5.3 不支持
    # 关闭思考，其截断恢复用 "low"；None 则省略该字段。provider_order 是可选路由
    # 偏好——出错时绝不静默丢弃（那会中途改变冻结的实验配置）。
    # 先构造请求消息（历史重水化 + 当前附件）。
    messages = _request_messages(system, user, history=history, image=image)
    # kwargs 收集可选 wire 参数；json_object 映射到 response_format。
    kwargs = {}
    if json_object:
        kwargs["response_format"] = {"type": "json_object"}
    if top_p is not None and top_p >= 0:
        kwargs["top_p"] = float(top_p)             # pinned explicitly (K3 report:
        #                                            "temperature = 1.0 and top-p = 1.0").
        #                                            -1 omits the field -> provider
        #                                            default, byte-identical to every
        #                                            run before 2026-08-09.
        # 【中文注释】top_p>=0 时显式钉住（K3 报告建议 temperature=1.0、top-p=1.0）；
        # -1 表示省略字段 → 供应商默认，与 2026-08-09 之前的所有运行逐字节一致。
    # extra_body 承载 OpenRouter 扩展字段（reasoning / provider 路由）。
    extra_body = {}
    if reasoning_effort:
        # "none" → {"enabled": False}（支持关闭的模型）；其余 → {"effort": ...}。
        extra_body["reasoning"] = ({"enabled": False} if reasoning_effort == "none"
                                   else {"effort": reasoning_effort})  # effort inert on M3
    elif reasoning_max_tokens:                     # v37 (: MAX reasoning, accuracy-only):
        # v37：不给档位但给思考 token 上限（k3 上 reasoning 是强制的，enabled:false
        # 会 400；只能调深度——封顶思考而非禁用。0 = 省略（GLM/M3 请求逐字节不变）。
        extra_body["reasoning"] = {"max_tokens": int(reasoning_max_tokens)}
        # k3: reasoning is MANDATORY (enabled:false -> 400); depth IS tunable — this caps
        # thinking high instead of disabling. 0 = omit (GLM/M3 requests byte-identical).
    order = provider_order
    if isinstance(order, str):
        # 字符串形式："a,b,c" → 列表（去空白、去空段）。
        order = [part.strip() for part in order.split(",") if part.strip()]
    elif order:
        order = list(order)
    else:
        order = []
    if order:
        # 有路由偏好：写入 provider 扩展（顺序、回退开关、参数一致性要求）。
        extra_body["provider"] = {
            "order": order,
            "allow_fallbacks": bool(provider_allow_fallbacks),
            "require_parameters": bool(provider_require_parameters),
        }
    if extra_body:
        kwargs["extra_body"] = extra_body
    # 模块级 logger（rsiagent.llm 命名空间）。
    log = logging.getLogger("rsiagent.llm")
    resp = None
    for attempt in range(4):                       # robust to TRANSIENT API errors (non-JSON body,
        try:                                       # 5xx, rate-limit): a flaky response must not kill a run
            # 【中文注释】最多 4 次尝试：对瞬态 API 错误（非 JSON 响应体、5xx、限流）
            # 健壮——一次抖动不能杀死整个运行。
            resp = _c().chat.completions.create(model=model, messages=messages,
                                                max_tokens=max_tokens, temperature=temperature, **kwargs)
            if (getattr(resp, "error", None)
                    or not getattr(resp, "choices", None)):
                # HTTP 200 但信封带错误或无 choices → 哨兵异常进入统一重试逻辑。
                raise _CompletionResponseError(resp)
            break
        except Exception as e:                     # noqa: BLE001
            # 统一异常处理：记录异常类名；哨兵异常额外记录信封细节。
            name = type(e).__name__
            if isinstance(e, _CompletionResponseError):
                error = e.body.get("error")
                metadata = error.get("metadata") if isinstance(error, dict) else None
                log.warning("%s completion response failure: id=%s provider=%s "
                            "embedded_code=%s error_type=%s", model,
                            e.body["id"], e.body["provider"], e.status_code,
                            metadata.get("error_type") if isinstance(metadata, dict) else None)
            if kwargs and not order and not isinstance(e, _CompletionResponseError):
                # A returned error/empty completion is not evidence that optional
                # fields are unsupported. Preserve the exact request on its retry.
                # 【中文注释】（v35.1 前身逻辑）出错时若请求带可选字段且未指定路由：
                # 一次错误不是"可选字段不被支持"的证据——但如果供应商持续拒绝，
                # 丢弃可选字段重试是恢复手段；此处保守地仅丢弃一次（kwargs={},
                # continue）。注意有 order 时绝不丢弃（会改变冻结实验）。
                log.warning("%s erred with optional request fields (%s); dropping them "
                            "and retrying", model, name)
                kwargs = {}
                continue
            body = getattr(e, "body", None)        # v35.1: surface the provider's actual
            detail = f" body={str(body)[:300]}" if body else ""  # complaint — a 400's
            if attempt == 3:                       # class name alone hides the cause
                # 最后一次尝试失败：按可恢复性分类，抛 LLMTransportError（不制造
                # Agent 回合）；detail 附上供应商原话（异常类名掩盖不了 400 的原因）。
                status = _status_code(e)
                recoverable = _recoverable_transport(e)
                log.warning("%s failed after retries (%s)%s; raising %s transport "
                            "failure (no agent turn created)", model, name, detail,
                            "recoverable" if recoverable else "fatal")
                raise LLMTransportError(
                    model, e, recoverable=recoverable, status_code=status,
                    detail=str(body)[:300] if body else name) from e
            # 非最终尝试：告警并线性退避后重试（2s、4s、6s）。
            log.warning("%s API error (%s)%s; retry %d after backoff", model, name, detail,
                        attempt + 1)
            time.sleep(2 * (attempt + 1))
    if resp is None or not getattr(resp, "choices", None):
        # Defensive fallback: the retry loop above should already have raised.
        # 【中文注释】防御性兜底：上面的重试循环本应已抛出；此分支理论不可达。
        cause = ValueError("response had no choices after retries")
        raise LLMTransportError(model, cause, recoverable=True,
                                detail=str(cause)) from cause
    # 路由来源统计：成功响应按 (model, provider) 计数。
    provider = _response_provider(resp)
    model_counts = _PROVIDER_COUNTS.setdefault(model, {})
    model_counts[provider] = model_counts.get(provider, 0) + 1
    log.info("%s provider: %s", model, provider)
    # v35.1 可观测性：逐调用 token 用量——没有它，跨模型上下文压缩的对等性无法检视。
    u = getattr(resp, "usage", None)               # v35.1 observability: per-call token
    if u is not None:                              # usage — cross-model context-compression
        det = getattr(u, "completion_tokens_details", None)   # parity is uninspectable
        rt = getattr(det, "reasoning_tokens", None) if det else None  # without it
        log.info("%s usage: in=%s out=%s%s", model,
                 getattr(u, "prompt_tokens", None), getattr(u, "completion_tokens", None),
                 f" reasoning={rt}" if rt is not None else "")
    # 取首个 choice：截断标志与文本内容。
    choice = resp.choices[0]
    truncated = getattr(choice, "finish_reason", None) == "length"
    txt = choice.message.content or ""
    native_calls = getattr(choice.message, "tool_calls", None) or []
    if native_calls:
        # RSIAgent requests text JSON actions, not native provider tools. Surface this
        # protocol drift explicitly instead of misdiagnosing an empty content field
        # as an ordinary decoder dry turn. Never execute an unsolicited native call.
        # 【中文注释】RSIAgent 要的是正文 JSON 动作而非原生工具调用。把这种协议
        # 漂移显式浮出（而不是把空 content 误诊为普通解码空转）；绝不执行未经
        # 请求的原生调用。只记录工具名列表供日志。
        names = []
        for call in native_calls:
            function = getattr(call, "function", None)
            names.append(str(getattr(function, "name", "unknown")))
        log.warning("%s returned %d unsolicited native tool call(s) (%s); "
                    "they were recorded as transport drift and not executed",
                    model, len(native_calls), ", ".join(names))
    # Keep the thinking channel, including empty-content turns, associated with
    # this caller until its own run loop appends the complete assistant message.
    # 【中文注释】把思考通道（包括空 content 回合）留给本调用线程，直到它自己的
    # 运行循环把完整助手消息附进历史——线程局部存储保证并发分支互不串线。
    _LAST_REASONING.value = getattr(choice.message, "reasoning", None) or ""
    if not txt.strip() and not truncated:          # v36.1: EMPTY-AT-STOP — the model ended
        rsn = getattr(choice.message, "reasoning", None) or ""   # its turn inside the hidden
        if rsn:                                    # channel. Log the tail for diagnosis
            log.warning("%s empty content at stop — reasoning tail: %r",   # (never execute
                        model, rsn[-300:])         # thinking; it holds rejected candidates)
        # 【中文注释】v36.1 空转于停止点：模型把回合终结在隐藏通道里。记录推理尾部
        # 300 字符供诊断（绝不执行思考内容——它装着被否决的候选）。
    cut = txt.rfind("</think>")               # drop inlined thinking from thinking models
    if cut >= 0:
        # 思考模型把推理内联进正文：丢弃 "</think>" 及其之前的全部内容。
        txt = txt[cut + len("</think>"):]
    txt = txt.strip()
    # 截断恢复的参数取决于"该模型能否关闭推理"。
    mandatory_reasoning = _reasoning_cannot_disable(model)
    retry_ceiling = 131072 if mandatory_reasoning else 40000
    if truncated and max_tokens < retry_ceiling and (not txt or _looks_cut(txt)):
        # TOKEN-LIMIT TRUNCATION: the reply hit the length cap with either NO visible
        # text (hidden reasoning ate the budget) or a program CUT MID-STREAM (its JSON
        # action never closed). A same-budget retry truncates identically — retry once
        # with a larger budget and cheaper reasoning. GLM-5.3 and Kimi K3 cannot
        # disable thinking, so they retry at their supported ``low`` effort up to a
        # 131072-token recovery ceiling; reasoning-optional models preserve the
        # reasoning-off recovery. (v16 extends the
        # v8.5 empty-reply retry to non-empty cut-off programs: one run wasted ~50 turns resubmitting a
        # program severed at the same byte.)
        # 【中文注释】token 上限截断恢复：可见文本为空（隐藏推理吃掉预算）或程序
        # 被切在半空（JSON 动作未闭合）时，同预算重试必然同样截断——因此用更大
        # 预算 + 更省的推理档位重试一次。GLM-5.3/Kimi K3 不能关思考，用其支持的
        # "low" 档且上限 131072；可关推理的模型沿用关闭推理恢复。（v16 把 v8.5 的
        # 空回复重试扩展到"非空但被切"的程序：一次运行曾浪费约 50 回合反复提交
        # 同一位置被切断的程序。）
        why = "empty reply" if not txt else "program cut mid-stream"
        retry_tokens = (min(max_tokens * 2, retry_ceiling)
                        if mandatory_reasoning else max_tokens * 2)
        retry_effort = "low" if mandatory_reasoning else "none"
        log.warning("%s truncated (%s at %d tokens) — retrying with %d + reasoning %s",
                    model, why, max_tokens, retry_tokens, retry_effort)
        # 递归调用自身完成"更大预算"重试（历史/附件原样传递，保持同一请求语义）。
        return chat(model, system, user, max_tokens=retry_tokens,
                    temperature=temperature, reasoning_effort=retry_effort,
                    history=history, image=image,
                    reasoning_max_tokens=reasoning_max_tokens, top_p=top_p,
                    provider_order=order,
                    provider_allow_fallbacks=provider_allow_fallbacks,
                    provider_require_parameters=provider_require_parameters,
                    json_object=json_object)
    if truncated:
        # 触顶但内容看似完整：告警"输出可能被截断"后原样返回。
        log.warning("%s hit the token limit; output may be cut", model)
    return txt


def _is_glm53(model: str) -> bool:
    """True for the canonical GLM-5.3 slug and dated/variant descendants."""
    # 规范 GLM-5.3 slug 及其带日期/变体后代的精确匹配。
    return model == "z-ai/glm-5.3" or model.startswith("z-ai/glm-5.3-")


def _reasoning_cannot_disable(model: str) -> bool:
    """Models in this harness whose APIs reject ``reasoning.enabled=false``."""
    # 本框架内"API 拒绝 reasoning.enabled=false"的模型集合：GLM-5.3 与 Kimi K3 系。
    return _is_glm53(model) or model == "moonshotai/kimi-k3" \
        or model.startswith("moonshotai/kimi-k3-")


def _response_provider(resp) -> str:
    """Extract OpenRouter's provider provenance without depending on SDK version."""
    # 跨 SDK 版本提取路由来源（provider）：
    # 1) 顶层属性；2) model_extra 字典；3) model_dump() 字典。
    value = getattr(resp, "provider", None)
    if not value:
        extra = getattr(resp, "model_extra", None)
        if isinstance(extra, dict):
            value = extra.get("provider") or extra.get("provider_name")
    if not value:
        try:
            dumped = resp.model_dump()
        except (AttributeError, TypeError):
            dumped = {}
        if isinstance(dumped, dict):
            value = dumped.get("provider") or dumped.get("provider_name")
    # 全部缺席 → "<unreported>"（审计里如实标注，不编造）。
    return str(value or "<unreported>")


def _looks_cut(txt: str) -> bool:
    """A truncated reply whose JSON action never closed — more open braces/brackets
    than close in the tail — is a program severed mid-stream, worth one bigger retry."""
    # 截断启发式：尾部 6000 字符里开括号多于闭括号 → JSON 动作未闭合，
    # 程序被切在半空，值得一次更大预算的重试。
    tail = txt[-6000:]
    return tail.count("{") + tail.count("[") > tail.count("}") + tail.count("]")


def json_values(text: str) -> list:
    """Every balanced top-level JSON value in ``text``, in order (tolerant of prose,
    markdown fences, and draft-then-correct replies)."""
    # 容错 JSON 抽取器：按顺序找出文本中所有"平衡的顶层 JSON 值"。
    # 对散文前导、markdown 围栏、草稿后修正式回复都健壮。
    dec = json.JSONDecoder()
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] in "[{":
            try:
                # raw_decode 从位置 i 尝试解析一个完整 JSON 值；成功则跳跃 i=end。
                val, end = dec.raw_decode(text, i)
                out.append(val)
                i = end
                continue
            except json.JSONDecodeError:
                # 此处不是合法 JSON 起点，前进一个字符继续扫描。
                pass
        i += 1
    return out


def parse_object(text: str):
    """The LAST well-formed JSON object (the model's final answer), or None."""
    # 取最后一个合法 JSON 对象（模型的最终答案）；没有则 None。
    # 列表推导过滤 dict 后取 [-1]（草稿后修正 → 最后者胜）。
    objs = [v for v in json_values(text) if isinstance(v, dict)]
    return objs[-1] if objs else None
