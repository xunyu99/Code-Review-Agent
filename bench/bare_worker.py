"""bench/bare_worker.py — 基线 worker：同一套记账，换掉 agent 实现。

它和 `bench/run_task.py` 的 worker **接口完全一样**（`--task-file` / `--result-file` /
照样写 `archives`、`sessions`、`record.json`、`report.md`），唯一的差别是中间那个循环：
这里只有一个最朴素的「问模型 → 有工具就执行 → 再问 → 不想调工具就收工」。

**为什么要有它**：项目里此前所有数字都是自比的（"我的 agent 从 X 变成 Y"），没有对照，
读者无法判断 Y 到底好不好。跑一次基线，才能回答那个最朴素的问题 ——
**这套框架相对于一个 60 行的 while 循环，多值了什么？**

剥掉了什么（**这些正是被测的东西**）：

    ✗ 系统提示词（只剩一句角色 + 输出格式约定，见 BARE_PROMPT）
    ✗ 技能注入 / 记忆播种的注入（`update_context()` 照旧被调用，但结果没人用）
    ✗ 四层上下文压缩（L1~L4）
    ✗ 重复调用提醒（REPEAT_HINT_AFTER）
    ✗ 步数预算提示 + 撞上限抢救 + 空回答重问（改动 1~4 那一整套）
    ✗ 报告长度约束（改动 5/6）
    ✗ 流式 UI、429/529 重试、截断续写（SDK 自带的 max_retries 还在）

保留了什么：

    ✓ 工具实现本身（`read_file` / `glob` / `bash` / `write_file` / `edit_file`）
    ✓ 这些工具**内部的路径围栏**（`guard()`）。剥了就得重写工具，那比的就变成
      "谁的工具实现更严"，而不是"谁的框架更值钱"
    ✓ `MAX_STEPS = 20` 的硬上限 —— 不给它同样的墙，对比就不公平

**口径**：跑出来的东西落在 `bench/runs-baseline/`，`record.json` 里的 `worker` 字段是
`bare_worker.py`。两棵树别混着算，也别混着报。
"""
import argparse
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
AGENT_DIR = BENCH.parent
for p in (str(AGENT_DIR), str(BENCH)):
    if p not in sys.path:
        sys.path.insert(0, p)

import agent                                                  # noqa: E402
import run_task                                               # noqa: E402
from config import MAX_STEPS, MAX_TOKENS, MODEL               # noqa: E402

# 基线能拿到的最小提示词：角色 + 输出格式。它不是"故意写烂"，而是"一个正常工程师
# 写 60 行循环时会顺手写的那点东西"——框架的额外价值（技能/记忆/压缩/预算）都不在里面。
BARE_PROMPT = (
    "You are a code-review agent. The repository under review is the current working directory.\n"
    "Use the tools to read it, then give your review as the final answer.\n"
    "Answer in Simplified Chinese. Report each issue as one bullet:\n"
    "- 严重|重要|次要  <文件>:<行号> — <问题>；依据：<证据>；建议：<修法>\n"
    "Always cite the file and line number of the code you are talking about."
)


class _Quiet:
    """`_accumulate_stream` 需要一个能收 delta 的 view。基线不留 UI，收下就不管。"""

    def on_text(self, chunk: str) -> None:
        pass


def bare_loop(messages, context):
    """最朴素的工具循环。返回值刻意和 `agent.agent_loop` 一致：None=跑完，其它=没跑完。"""
    messages[0] = {"role": "system", "content": BARE_PROMPT}
    step = 0
    while True:
        step += 1
        if step > MAX_STEPS:
            # 注意这里**没有抢救兜底**：撞墙就是撞墙，这正是基线该有的样子
            messages.append({"role": "assistant",
                             "content": f"[Error] max steps ({MAX_STEPS}) exceeded"})
            return "failed"
        stats: dict = {}
        try:
            with agent.client.chat.completions.create(
                model=MODEL, messages=messages, tools=agent.TOOLS, max_tokens=MAX_TOKENS,
                stream=True, stream_options={"include_usage": True},
                extra_body={"thinking": {"type": "disabled"}},
            ) as stream:
                msg, _finish = agent._accumulate_stream(stream, _Quiet(), stats)
        except Exception as e:                      # 没有重试层：错了就是这一轮错了
            messages.append({"role": "assistant",
                             "content": f"[Error] {type(e).__name__}: {e}"})
            return "failed"
        if stats.get("usage") or stats.get("model"):      # 记账口径和主 agent 一致
            agent.append_record(messages, {"type": "usage", "step": step,
                                           "model": stats.get("model"),
                                           "usage": stats.get("usage") or {}})
        messages.append(msg)
        if not msg.get("tool_calls"):
            return None
        for tc in msg["tool_calls"]:
            name = agent._tool_name(tc)
            args = agent._tool_args(tc)
            handler = agent.TOOL_HANDLERS.get(name)
            try:
                out = handler(**args) if handler else f"Unknown tool: {name}"
            except Exception as e:
                out = f"Error: {type(e).__name__}: {e}"
            if not isinstance(out, str):
                out = json.dumps(out, ensure_ascii=False, default=str)
            messages.append({"role": "tool", "tool_call_id": tc.get("id") or "",
                             "content": out})


def run_bare_session(task: str, *, verbose: bool = False, with_memory: bool = False) -> dict:
    """用裸循环跑一次。会话、archives、记账、summarize 全部复用 run_task，一行不改。"""
    saved = agent.agent_loop
    agent.agent_loop = bare_loop
    try:
        return run_task.run_session(task, verbose=verbose, with_memory=with_memory)
    finally:
        agent.agent_loop = saved


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="基线 worker（只由 bench/baseline.py 拉起）")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--task-file", help=argparse.SUPPRESS)
    ap.add_argument("--result-file", help=argparse.SUPPRESS)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--with-memory", action="store_true")
    args = ap.parse_args(argv)
    if not args.worker:
        ap.error("这是 worker 脚本；要跑基线请用 bench/baseline.py")
    task = Path(args.task_file).read_text(encoding="utf-8")
    result = run_bare_session(task, verbose=args.verbose, with_memory=args.with_memory)
    Path(args.result_file).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
