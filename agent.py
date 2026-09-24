"""
agent.py — 主循环 + 钩子 + 四层压缩 + 错误恢复。从 s13 原版搬运。
留：四层压缩(L1-L4+reactive)、钩子系统、with_retry、续写、json 安全网
改：非流式 → 流式（stream=True），分片在 _accumulate_stream 里自己拼成 message dict
改：max_tokens=MAX_TOKENS(8192) 一步到位（删升级分支）；permission_hook 只留 bash 硬拒+破坏性确认
    （写入类由 safe_path 围栏兜底，不再弹窗）；常量全部从 config/tools import，单一出处
改：权限裁决搬去 permissions.decide（钩子只传话）；工具层加重复调用提醒（REPEAT_HINT_AFTER）
删：记忆(s09)、任务(s12)、后台(s13)、todo(s05) —— V1 回归
"""
import json, os, random, time
from datetime import datetime
from pathlib import Path
import openai
from openai import OpenAI
from config import (API_KEY, BASE_URL, MODEL, MAX_TOKENS, THRESHOLD, WORKDIR,
                    OUTPUT_DIR, TRANSCRIPTS_DIR, FALLBACK_MODEL,
                    BASE_DELAY_MS, MAX_RECOVERY_RETRIES, MAX_529_RETRIES, MAX_STEPS)
from tools import TOOLS, TOOL_HANDLERS
from permissions import decide
from system_prompt import get_system_prompt, update_context
from ui import tracker, warn, info, permission_prompt, StreamView

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)


def fatal(messages, reason):
    """所有放弃路径统一出口：日志 + 把原因写进对话。"""
    warn(reason)
    messages.append({"role": "assistant", "content": f"[Error] {reason}"})


def _message_has_tool_use(msg: dict) -> bool:
    return bool(msg.get("tool_calls"))


def _is_tool_result_message(msg: dict) -> bool:
    return msg.get("role") == "tool"


'''  工具调用：dict / SDK 对象两种形态的读取 + 流式分片合并  '''

def _tool_name(tool_call) -> str:
    """工具名。流式拼出来的是 dict，SDK 给的是对象，两种都认。"""
    if isinstance(tool_call, dict):
        return (tool_call.get("function") or {}).get("name") or ""
    return tool_call.function.name


def _tool_args(tool_call) -> dict:
    """工具参数。解析失败返回 {}，不让权限钩子把整轮对话打断。"""
    if isinstance(tool_call, dict):
        raw = (tool_call.get("function") or {}).get("arguments")
    else:
        raw = tool_call.function.arguments
    try:
        args = json.loads(raw or "{}")
    except Exception:
        return {}
    return args if isinstance(args, dict) else {}


def _accumulate_stream(stream, view) -> tuple[dict, str | None]:
    """把流式分片拼回一条完整的 assistant 消息（content + tool_calls）。

    要点：content 是一段段文本直接接起来；tool_calls 按 index 归位——
    id / name 只出现在第一个分片（取一次就好），arguments 是切碎的 JSON 字符串，必须逐片累加。
    """
    content_parts = []
    tool_calls: dict[int, dict] = {}
    finish_reason = None
    for chunk in stream:
        if not getattr(chunk, "choices", None):
            continue                      # 有些服务商会补发一个只有 usage 的空包
        choice = chunk.choices[0]
        if choice.finish_reason:
            finish_reason = choice.finish_reason
        delta = getattr(choice, "delta", None)
        if delta is None:
            continue
        if delta.content:
            content_parts.append(delta.content)
            view.on_text(delta.content)
        for tc in (getattr(delta, "tool_calls", None) or []):
            idx = tc.index if tc.index is not None else 0
            slot = tool_calls.setdefault(idx, {
                "id": "", "type": "function",
                "function": {"name": "", "arguments": ""},
            })
            if tc.id and not slot["id"]:
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if fn.name and not slot["function"]["name"]:
                    slot["function"]["name"] = fn.name
                if fn.arguments:
                    slot["function"]["arguments"] += fn.arguments
    message = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return message, finish_reason


def repair_dangling_tool_calls(messages, placeholder="[interrupted: tool result never returned]") -> int:
    """Ctrl+C 可能把对话停在「模型刚要调工具、结果没回来」上：补一条占位结果再存盘，
    否则恢复会话时 tool_calls 配不上 tool 结果，API 会直接 400。"""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "tool":
            continue
        if m.get("role") == "assistant" and m.get("tool_calls"):
            answered = {x.get("tool_call_id") for x in messages[i + 1:]
                        if x.get("role") == "tool"}
            missing = [tc for tc in m["tool_calls"] if (tc.get("id") or "") not in answered]
            for tc in missing:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id") or "",
                    "content": placeholder,
                })
            return len(missing)
        break
    return 0


'''
            L1：snip_compact —— 裁掉无关的旧对话
'''
def snip_compact(messages, max_messages=50):
    """L1: 消息（大于50条）太多时裁掉中间，保留头 3 + 尾 47。"""
    if len(messages) <= max_messages:
        return messages
    head_end, tail_start = 3, len(messages) - (max_messages - 3)
    if _message_has_tool_use(messages[head_end - 1]):
        while head_end < len(messages) and _is_tool_result_message(messages[head_end]):
            head_end += 1
    if tail_start < len(messages):
        if _is_tool_result_message(messages[tail_start]):
            while tail_start > head_end and _is_tool_result_message(messages[tail_start]):
                if _message_has_tool_use(messages[tail_start - 1]):
                    break
                tail_start -= 1
            tail_start -= 1  # 吞掉前面对应的 tool_use
    snipped = tail_start - head_end
    if snipped <= 0:
        return messages
    placeholder = {"role": "user", "content": f"[snipped {snipped} messages from conversation middle]"}
    return messages[:head_end] + [placeholder] + messages[tail_start:]


'''
           L2：micro_compact —— 旧工具结果占位
'''
KEEP_RECENT_TOOL_RESULTS = 3

def collect_tool_result_messages(messages):
    return [(i, m) for i, m in enumerate(messages) if m.get("role") == "tool"]


def micro_compact(messages):
    """L2: 只保留最近 3 条工具结果的完整内容，更旧的换成占位符。"""
    tool_results = collect_tool_result_messages(messages)
    if len(tool_results) <= KEEP_RECENT_TOOL_RESULTS:
        return messages
    for idx, msg in tool_results[:-KEEP_RECENT_TOOL_RESULTS]:
        if len(str(msg.get("content", ""))) > 120:
            msg["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


'''
            L3：tool_result_budget —— 大结果落盘(磁盘)
'''
def persist_large_output(tool_call_id: str, content: str):
    """把大输出写到磁盘，返回占位标记 + 前 2000 字符预览。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{tool_call_id}.txt"
    path.write_text(content, encoding="utf-8")
    preview = content[:2000]
    return (
        f"<persisted-output file={path}>\n{preview}\n</persisted-output>\n"
        "(完整内容已落盘，需要时用 read_file 重新读取。)"
    )


def tool_result_budget(messages, max_bytes=200_000):
    """L3: 工具结果总大小超限时，从最大的开始落盘。"""
    # enumerate(messages)
    # 遍历 messages（对话历史，是消息字典的列表），同时拿到下标 i 和消息对象 m。
    # 例如：(2, {'role': 'tool', 'content': 'main.py'})
    blocks = [(i, m) for i, m in enumerate(messages) if m.get("role") == "tool"]
    total = sum(len(str(m.get("content", ""))) for _, m in blocks)
    if total <= max_bytes:
        return messages
    ranked = sorted(blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True)
    for idx, msg in ranked:
        if total <= max_bytes:
            break
        msg["content"] = persist_large_output(msg.get("tool_call_id", str(idx)), str(msg["content"]))
        total = sum(len(str(m.get("content", ""))) for _, m in blocks)
    return messages


'''  L4：compact_history —— LLM 全量摘要  '''
def write_transcript(messages):
    """把完整对话存成 JSONL 文件，返回路径。"""
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPTS_DIR / f"transcript-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for m in messages:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    return path


# 为代码智能体总结本次对话。
# 需保留：(1) 当前目标，(2) 重要发现，
# (3) 已修改文件，(4) 剩余工作，(5) 用户约束条件。
# 仅使用文本回复。禁止调用任何工具。

SUMMARY_PROMPT = (
    "Summarize the conversation for a coding agent. "
    "Preserve: (1) the current goal, (2) important findings, "
    "(3) files modified, (4) remaining work, (5) user constraints. "
    "Respond with TEXT ONLY. Do NOT call any tools."
)


def summarize_history(messages) -> str:
    """调 LLM 生成对话摘要。失败或为空 → 返回 ""，由调用方兜底。"""
    try:
        payload= [] #用来存放要送去大模型做摘要的消息片段
        total = 0 #记录当前累计字符串长度
        # messages[-40:] 只处理最近40条消息 ; [::-1]：反转列表，从最新消息往旧消息遍历
        for m in messages[-40:][::-1]:
            s = json.dumps(m, ensure_ascii=False)
            if total + len(s) > 60000:
                break
            payload.append(m)
            total += len(s)
        if not payload:                    # ← 修：空 payload 直接放弃
            info("[compact] payload empty, fallback")
            return ""
        payload = payload[::-1]
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            stream=False,
            extra_body={"thinking": {"type": "disabled"}},
        )
        summary = response.choices[0].message.content or ""
        info(f"[compact] summary: {summary[:100]!r}")
        return summary.strip()
    except Exception as e:
        warn(f"[compact] summarize failed: {type(e).__name__}: {e}")
        return ""


# ① 空间经济学（最硬）：compact 是预防性的 —— 触发时已到 40000 阈值，
# 它的任务是把上下文压到最小、给后面的长路留足跑道。如果它保留尾部，压缩只腾出一点空间，
# 马上又到阈值（防抖只隔 6 条！），每几轮就花一次 LLM 摘要的 token，压缩失去意义。
# reactive 不需要买跑道 ——它只求这一次重试能塞进窗口，缩到刚过线即可，保留 5 条的成本极低。
# reactive 在模型已经输出 / 正在执行时触发—— 请求已经被拒，
# 重试必须和刚做的动作一致（least-change 原则：最小改动，别让模型重来一遍）。
# 保留尾部 = 重试时 "还是刚才那个模型，只是记忆小了一点"。

def compact_history(messages):
    """L4: 完整对话落盘 + LLM 摘要。摘要为空 → 回退确定性压缩。"""
    transcript_path = write_transcript(messages)
    summary = summarize_history(messages)
    system_msgs = [m for m in messages if m.get("role") == "system"]  # ← 关键修复：system 不压掉
    if not summary:
        info("[compact] summary empty, fallback to snip_compact")
        return snip_compact(messages)
    info(f"[compact_history] transcript saved to {transcript_path}")
    return system_msgs + [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


'''
        应急：reactive_compact
'''
MAX_REACTIVE_RETRIES = 1

def reactive_compact(messages):
    """应急压缩：摘要 + 保留尾部最近几条消息。"""
    transcript_path = write_transcript(messages)
    summary = summarize_history(messages)
    system_msgs = [m for m in messages if m.get("role") == "system"]
    tail_start = max(0, len(messages) - 5)
    # 不能拆散 工具调用/结果 对
    while tail_start < len(messages) and _is_tool_result_message(messages[tail_start]):
        j = tail_start
        while j > 0 and _is_tool_result_message(messages[j - 1]):
            j -= 1
        if j > 0 and _message_has_tool_use(messages[j - 1]):
            tail_start = j - 1
        else:
            tail_start = j + 1
    info("[reactive compact]")
    info(f"[auto compact] transcript saved to {transcript_path}")
    return system_msgs + [{"role": "user", "content": f"[Reactive compact]\n\n{summary}"}, *messages[tail_start:]]


# ---------- s04: 钩子系统 ----------
HOOKS = {
    "UserPromptSubmit": [],   # 用户输入后、进模型前
    "PreToolUse": [],         # 工具执行前
    "PostToolUse": [],        # 工具执行后
    "Stop": [],               # 循环退出前
}

def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    """触发某个事件的所有钩子。第一个返回非 None 的钩子说了算。"""
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None

# 上下文注钩子
def context_inject_hook(query: str):
    info(f"working in {WORKDIR}")
    return None

register_hook("UserPromptSubmit", context_inject_hook)


# ---------- 节点②：工具执行前（权限）----------
# 这里只剩「传话」：真正的裁决（谁能读、谁能写、要不要问、bash 怎么识别路径）
# 全在 permissions.py —— 那是唯一出处，别在这里加判断，否则又开始漂移。


def permission_hook(tool_call):
    """权限钩子：ask 弹窗、deny 变拒绝理由，其余放行。裁决本身在 permissions.decide。"""
    try:
        verdict, title, detail = decide(_tool_name(tool_call), _tool_args(tool_call))
    except Exception as e:                        # 权限场景保持 fail-closed
        warn(f"permission check failed: {type(e).__name__}: {e}")
        return f"Permission denied: permission check failed ({type(e).__name__})"
    if verdict == "allow":
        return None                               # 放行
    if verdict == "deny":
        return detail or "Permission denied"
    if not permission_prompt(title, detail):      # ask
        return "Permission denied by user"
    return None


def _args_summary(tool_call) -> str:
    """从 tool_call 提取一行参数摘要（截断到 40 字符）。"""
    args = _tool_args(tool_call)
    for key in ("command", "path", "pattern", "file_path"):
        if key in args:
            v = str(args[key]).replace("\n", " ")
            return v[:40]
    return ""


def log_hook(tool_call):
    tracker.start(_tool_name(tool_call), _args_summary(tool_call))
    return None

register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)


# ---------- 节点③：工具执行后 ----------
def large_output_hook(tool_call, output: str, failed: bool | None = None):
    """输出超过 2000 字符就提醒一下（真正的落盘由 L3 做）。"""
    if len(str(output)) > 2000:
        warn(f"large output ({len(str(output))} chars) from {_tool_name(tool_call)}")
    return None

def finish_hook(tool_call, output: str, failed: bool | None = None):
    tracker.finish(output, failed=failed)
    return None

# 顺序有意义：先收盘（停掉 Live），再打印警告，免得日志和 spinner 抢屏幕
register_hook("PostToolUse", finish_hook)
register_hook("PostToolUse", large_output_hook)


# ---------- 节点④：循环退出前 ----------
def summary_hook(messages: list):
    tool_count = sum(1 for m in messages if m.get("role") == "tool")
    info(f"Stop: session used {tool_count} tool calls")
    return None

register_hook("Stop", summary_hook)


# ---------- s11: 错误恢复 ----------

# 续写提示词
# 已达到输出token上限。直接继续——无需致歉，不重述之前的内容。
# 若中断发生在思考过程中，则从断点处接续。将剩余任务拆分成更小的片段。

CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly — no apology, no recap of "
    "what you were doing. Pick up mid-thought if that is where the cut "
    "happened. Break remaining work into smaller pieces."
)


class RecoveryState:
    """记录哪些恢复动作已经用过 + 当前模型。"""
    def __init__(self):
        self.has_attempted_reactive_compact = False
        self.recovery_count = 0
        # consecutive 连续的
        self.consecutive_529 = 0
        self.current_model = MODEL


def retry_delay(attempt, retry_after=None):
    """指数退避 + 抖动。attempt 从 0 开始：0.5s, 1s, 2s, 4s... 上限 32s。"""
    if retry_after:
        return retry_after
    # BASE_DELAY_MS = 500  # 0.5s 初始延迟避时间
    base = min(BASE_DELAY_MS * (2 ** attempt), 32000) / 1000
    return base + random.uniform(0, base * 0.25)


def is_overloaded(e):
    """529 / 503：服务端过载，短时间后可能恢复。"""
    return isinstance(e, openai.APIStatusError) and e.status_code in (529, 503)


def is_rate_limit(e):
    """429：SDK 封装的限流异常。"""
    return isinstance(e, openai.RateLimitError)


def with_retry(fn, state, max_retries=10):
    """瞬态错误（429/529）专用：整个 fn 调用重试，最多 max_retries 次。"""
    for attempt in range(max_retries):
        try:
            return fn()
        except openai.RateLimitError:
            delay = retry_delay(attempt)
            info(f"[retry] 429 attempt {attempt+1}/{max_retries}, delay {delay:.2f}s")
            time.sleep(delay)
            state.consecutive_529 = 0
        except openai.APIStatusError as e:
            if not is_overloaded(e):
                raise
            delay = retry_delay(attempt)
            info(f"[retry] {e.status_code} attempt {attempt+1}/{max_retries}, delay {delay:.2f}s")
            time.sleep(delay)
            state.consecutive_529 += 1
            # MAX_529_RETRIES = 3
            if state.consecutive_529 >= MAX_529_RETRIES and FALLBACK_MODEL:
                info(f"[model] switching to {FALLBACK_MODEL}")
                state.current_model = FALLBACK_MODEL
    raise RuntimeError("Max retries exceeded")


def is_prompt_too_long_error(e):
    """OpenAI/DeepSeek 的 400 错误，消息里带 context / length。"""
    return (isinstance(e, openai.BadRequestError)
            and "context" in str(e).lower() and "length" in str(e).lower())


def estimate_token_count(messages) -> int:
    """用字符数估算 token 数（教学版简化）。"""
    return sum(len(str(m.get("content", ""))) for m in messages)


# ---------- 主循环 ----------
_ACTIVE = {"view": None}   # 当前轮次的流式视图；Ctrl+C 收尾时要靠它保住已生成的内容

# 重复调用止损：同一轮里同一个 (工具, 参数) 第 N 次出现 → 在结果后面附一句提醒。
# 只提醒、不拦截：重读有时候是有意的（旧结果刚被 L2 压成占位符，本来就该重读），
# 但"一模一样调 5 次"多半是模型在原地打转 —— 提醒它一句，比让它撞满 MAX_STEPS 便宜。
REPEAT_HINT_AFTER = 5


def _repeat_hint(counts: dict, tool_name: str, args: dict) -> str:
    """登记本次调用；重复到阈值就返回要附在工具结果后面的提醒（否则返回空串）。"""
    try:
        key = f"{tool_name}:{json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)}"
    except Exception:
        key = f"{tool_name}:{args!r}"
    counts[key] = counts.get(key, 0) + 1
    n = counts[key]
    if n < REPEAT_HINT_AFTER:
        return ""
    return (f"\n\n[重复调用] 本轮第 {n} 次用完全相同的参数调用 {tool_name}。"
            f"你已经调用过这个，换个做法：换参数、换工具，或者直接把结论写出来。")


# 半截回答的收尾标记。只进 messages，不进屏幕：用户知道自己按了 Ctrl+C，
# 需要被提醒的是模型 —— 上面那句是我自己没写完的，不是我说完了。
# 没有这个标记，模型看到自己一句断掉的话，会想「把它补完」，
# 而不是「用户打断了，重新计划」。这正好和用户按 Ctrl+C 的意图相反。
PARTIAL_MARK = "\n\n[user interrupted mid-response; the text above is truncated and incomplete]"


def _abort_turn(messages) -> None:
    """Ctrl+C 的统一收尾：停 Live / spinner、保住已生成的内容、补齐没回来的工具结果。
    和 fatal() 一个思路——所有放弃路径都走同一个出口，别把状态留在半路。"""
    # 顺序有意义：先封口，再追加半截文本。
    # repair_dangling_tool_calls 是从尾往前找「最后一条非 tool 消息」然后 break，
    # 先把半截 assistant 塞进去的话，它会被当成对话真正的结尾，悬空的工具调用就漏封了。
    # （实际跑起来这两件事互斥：commit() 会把 buffer 清空，所以有悬空调用时 partial 必为空。
    #   但依赖这种巧合太脆，顺序摆对就永远不用管。）
    repair_dangling_tool_calls(messages, placeholder="[interrupted by user]")
    view = _ACTIVE["view"]
    if view is not None:
        partial = view.interrupt()            # 停 Live + 把半截回答打印出来（屏幕上不带标记）
        if partial:
            messages.append({"role": "assistant", "content": partial + PARTIAL_MARK})
    tracker.stop()
    warn("^C 已中断本轮（可以继续提问）")


def agent_loop(messages, context):
    """对外入口：Ctrl+C 在这一层吃掉——只中断本轮，程序不退。

    返回值：正常跑完是 None；本轮被 Ctrl+C 打断时返回 "interrupted"。
    调用方看到它就该跳过「事后动作」（提炼记忆之类）——
    半截对话没有结论，不该拿它去下判断。
    """
    _ACTIVE["view"] = None
    try:
        return _agent_loop_inner(messages, context)
    except KeyboardInterrupt:
        _abort_turn(messages)
        return "interrupted"


def _agent_loop_inner(messages, context):
    last_compact_count = 0
    state = RecoveryState()
    repeat_counts = {}             # 本轮 (工具, 参数) 的调用计数，用于重复调用提醒
    max_tokens = MAX_TOKENS        # 改：直接 8192，不再 1000 起步
    step = 0
    while True:
        step += 1
        if step > MAX_STEPS:
            return fatal(messages, f"max steps ({MAX_STEPS}) exceeded, stop to avoid infinite loop")
        # s10：每轮开始用最新 context 组装 system，整体替换 messages[0]
        messages[0] = {"role": "system", "content": get_system_prompt(context)}
        # ===== 上下文预算：先量，再决定要不要动刀 =====
        # 关键改动：压缩不再是「每一步的例行公事」，而是「真的超预算才做」。
        # 以前 L2 每步都跑：读过的文件只要再过 3 次工具调用就被就地覆盖成
        # "[Earlier tool result compacted. Re-run if needed.]"，模型只能重读，
        # 重读又踢掉别人 —— 旋转门就是这么转起来的。
        est_before = estimate_token_count(messages)
        if est_before > THRESHOLD and len(messages) - last_compact_count >= 6:
            # 顺序不能换：先落盘（可 read_file 回读），再裁中间，最后才换占位符
            messages[:] = tool_result_budget(messages)   # L3: 大输出落盘 + 预览 + 文件路径
            messages[:] = snip_compact(messages)         # L1: 裁中间（不可恢复）
            messages[:] = micro_compact(messages)        # L2: 旧工具结果换占位符（不可恢复）
            est_after = estimate_token_count(messages)
            if est_after > THRESHOLD:                    # 压完还超 → 花一次 API 做 LLM 摘要
                messages[:] = compact_history(messages)
                last_compact_count = len(messages)
            info(f"[compact] {est_before} → {estimate_token_count(messages)} chars (阈值 {THRESHOLD})")

        view = StreamView()
        _ACTIVE["view"] = view          # 交给 Ctrl+C 收尾用（见 _abort_turn）

        def stream_once():
            """建流 + 收流。with_retry 会整段重跑，所以每次开新流前先把半截预览擦掉。"""
            view.start()
            try:
                with client.chat.completions.create(
                    model=state.current_model,
                    messages=messages,
                    tools=TOOLS,
                    max_tokens=max_tokens,
                    stream=True,
                    extra_body={"thinking": {"type": "disabled"}},
                ) as stream:
                    return _accumulate_stream(stream, view)
            except KeyboardInterrupt:
                raise                 # 中断：保住已收到的内容，交给下面收尾
            except BaseException:
                view.abort()           # 重试前擦干净，否则同一段会显示两遍
                raise

        try:
            msg_dict, finish_reason = with_retry(stream_once, state)
        except Exception as e:
            view.abort()
            # 上下文超限很多服务商返回 400，尝试 reactive_compact
            if is_prompt_too_long_error(e):
                if not state.has_attempted_reactive_compact:
                    info("[reactive] compacting and retrying once...")
                    messages[:] = reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    continue
                return fatal(messages, "context still too long after reactive compact")
            return fatal(messages, f"{type(e).__name__}: {e}")

        is_final = not msg_dict.get("tool_calls")
        # 中途解说（后面还要调工具）和续写片段都不画分隔线：它们和上一段是同一轮回答
        view.commit(rule=is_final and state.recovery_count == 0)

        # finish_reason 回答 "模型这一轮为什么停下来了"，流式下由最后一个分片带出来。
        # "length"的含义很直白：生成还没写完，撞上了 max_tokens 上限，被服务端强制掐断。
        # "stop"	 模型自然说完了（遇到结束标记）	  直接收下，正常结束
        # "tool_calls"   模型决定调用工具，这一轮是 "工具调用请求"	执行工具，结果塞回 messages，循环继续
        # "length"	 单次输出达到 max_tokens（你的 8192），没说完被硬截断	412 行续写分支接住

        # 截断检查——必须在 append 之前
        if finish_reason == "length":
            messages.append(msg_dict)
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                info(f"[max_tokens] continuation {state.recovery_count}/{MAX_RECOVERY_RETRIES}")
                continue
            return fatal(messages, "still truncated after 3 continuations")

        messages.append(msg_dict)

        if is_final:   # 任务完成
            trigger_hooks("Stop", messages)  # 打印工具统计；返回 None → 放行退出 
            break

        for tool in msg_dict["tool_calls"]:
            args = _tool_args(tool)
            hint = _repeat_hint(repeat_counts, _tool_name(tool), args)
            permission = trigger_hooks("PreToolUse", tool)
            # 返回 None 放行；不为 None 就是拒绝理由
            if permission:
                tracker.denied(_tool_name(tool), _args_summary(tool), str(permission))
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool.get("id") or "",
                    "content": str(permission) + hint,
                })
                continue
            tool_name = _tool_name(tool)
            handler = TOOL_HANDLERS.get(tool_name)
            failed = None
            try:
                if handler is None:
                    output = f"Unknown tool: {tool_name}"
                    failed = True
                else:
                    output = handler(**args)   # ← 包进 try：非法 JSON / TypeError 都不崩
                    if not isinstance(output, str):   # 安全网：非字符串一律转 JSON 字符串
                        output = json.dumps(output, ensure_ascii=False, default=str)
            except Exception as e:
                output = f"Error: tool call failed: {type(e).__name__}: {e}"
                failed = True
                # messages.append({"role": "tool", "content": output}) 时，content 必须是字符串，OpenAI API 才认。
            output += hint                     # 重复到阈值就附一句提醒（通常是空串）
            trigger_hooks("PostToolUse", tool, output, failed)
            messages.append({
                "role": "tool",
                "tool_call_id": tool.get("id") or "",
                "content": output,
            })

    # 在python中只有#是注释
    # if not isinstance(output, str)  判断 output 是否为字符串类型
    # json 模块的两个核心函数构成一对互逆操作，界定于对象域（Python 原生容器）与文本域（JSON 文档）之间：
    # json.loads(s) — 反序列化（deserialization）
    # 将 JSON 文本解析为 Python 原生对象（dict / list / str / int / float / bool / None）。其作用是将传输或存储格式还原为内存中的可操作数据结构。
    # json.dumps(obj, **kwargs) — 序列化（serialization）
    # 将 Python 对象编码为 JSON 文本。本项目中使用的两个参数语义如下：
    # - ensure_ascii=False：禁用 ASCII 强制转义。默认（True）下非 ASCII 字符被输出为 `\uXXXX` 转义序列；置 False 后以 UTF-8 字面量输出，保证中文可读性。
    # - default=str：fallback 钩子。当对象图含不可直接序列化的类型（如 datetime、Path 等）时，调用 str() 兜底转换，避免抛出 TypeError。
    # model_dump 不是 JSON—— 它只是 Pydantic 对象 → dict，
    # 要真转字符串得用 model_dump_json()。你代码里 model_dump(exclude_none=True) 是为了把 OpenAI 返回的 message 对象变成普通 dict 塞进 messages 列表。
