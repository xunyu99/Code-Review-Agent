"""步数预算提示：那道「隐形墙」必须能被模型看见。

背景（bench/runs 里数出来的）：6 次撞 MAX_STEPS 的运行，**6 次都是 0 字报告**。
根因之一是提示词和 skill 从头到尾没提过步数上限，模型按「小步走、分页读」的指示
一路勘察取证，到第 20 步被硬停，前面全作废。

这里锁住四件事：
  1) 每一步都带剩余步数，且措辞随剩余步数变硬；
  2) 提示只发给 API，不进工作副本 —— 否则会跟着存盘，污染 sessions/archives
     和 bench/replay 的离线统计；
  3) 真实循环里确实把提示发出去了（不是写了个没人调的函数）；
  4) 加了这条尾部消息之后，对话对 API 仍然是合法的（tool_call 都有结果配对）。
"""
import _bootstrap  # noqa: F401

from types import SimpleNamespace

import agent
import config


# ── 纯函数：措辞分档 ───────────────────────────────────────────────────

def test_every_step_carries_the_remaining_count():
    max_steps = agent.MAX_STEPS
    for step in range(1, max_steps + 1):
        note = agent.step_budget_notice(step, max_steps)
        remaining = max_steps - step
        assert f"{step}/{max_steps}" in note
        assert f"{remaining} 步" in note          # 剩余多少步必须说清楚
        assert "报告" in note                     # 且要指向「写报告」这个动作


def test_notice_hardens_as_the_budget_runs_out():
    """宽松 → 收尾 → 停手，三档措辞必须真的不一样。"""
    early = agent.step_budget_notice(1, 20)                    # 还剩 19 步
    warn = agent.step_budget_notice(20 - agent.STEP_WARN_REMAINING, 20)   # 正好剩 5 步
    last = agent.step_budget_notice(20, 20)                    # 最后一步，剩 0

    assert len({early, warn, last}) == 3
    assert "收手写报告" in early
    assert "停止新的勘察" in warn and "最终报告" in warn
    assert "停止一切工具调用" in last and "作废" in last


def test_report_length_budget_reaches_the_model():
    """6,292 / 8,310 字的报告来自「没人告诉它别写」，不是被 MAX_TOKENS 逼的。

    长度约束必须出现在模型真正看得到的三处：每轮的 system prompt、最后一步的收尾提示、
    以及撞上限后的抢救指令。任何一处漏了，那一次调用就没人管它写多长。
    """
    from system_prompt import assemble_system_prompt
    limit = str(config.REPORT_CHARS_LIMIT)

    assert limit in assemble_system_prompt({})            # system prompt（每轮都重建）
    assert limit in agent.step_budget_notice(20, 20)      # 只剩 0 步
    assert limit in agent.step_budget_notice(20 - agent.STEP_WARN_REMAINING, 20)
    assert limit in agent.BUDGET_EXHAUSTED_PROMPT         # 抢救那一次（不传 tools）


def test_length_rule_asks_for_compression_not_fewer_findings():
    """run-7 / run-8 的教训：字数砍到 1/3，召回从 6.5/8 掉到 3.5/8。

    当时的措辞是「最多 5 条，宁可丢掉弱的」—— 那是**授权模型少报**。而用户要的是
    「别啰嗦」，不是「少报」。所以这条用例钉的不是数字，是**方向**：压每一条的写法，
    不许砍掉有证据的发现。
    """
    from system_prompt import assemble_system_prompt
    text = assemble_system_prompt({})

    assert "do not drop a real issue" in text
    assert "Compress the wording instead of cutting the list" in text
    assert "Dropping a weak finding is better" not in text    # 旧措辞，别再回来
    assert "at most 5" not in text.lower()


def test_notice_never_enters_the_working_copy():
    """工作副本是要存盘的原文；提示只能活在这一次请求里。"""
    msgs = [{"role": "system", "content": "SYS"},
            {"role": "user", "content": "审查一下"}]
    snapshot = [dict(m) for m in msgs]

    api_messages = agent.with_step_budget(msgs, 3, 20)

    assert msgs == snapshot                        # 原列表一个字没动
    assert len(api_messages) == len(msgs) + 1
    assert api_messages[-1]["role"] == "user"
    assert "[步数预算]" in api_messages[-1]["content"]
    assert all("[步数预算]" not in str(m.get("content", "")) for m in msgs)


# ── 假客户端：形状照着 OpenAI 流式接口做（和 test_run_task 同款）────────

class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __enter__(self):
        return iter(self._chunks)

    def __exit__(self, *exc):
        return False


class _Completions:
    def __init__(self, scripts):
        self._scripts, self.seen = scripts, []

    def create(self, **kwargs):
        self.seen.append(kwargs)
        if not self._scripts:
            raise AssertionError("假客户端：响应序列用完了，说明循环没在该停的地方停")
        return _Stream(self._scripts.pop(0))


class _FakeClient:
    def __init__(self, scripts):
        self.completions = _Completions(list(scripts))
        self.chat = SimpleNamespace(completions=self.completions)

    @property
    def seen(self):
        return self.completions.seen


class _FakeView:
    """顶掉 ui.StreamView：测试不需要真往终端画东西。"""

    def start(self):
        pass

    def abort(self):
        pass

    def on_text(self, _text):
        pass

    def commit(self, rule=False):
        pass

    def interrupt(self):
        return ""


class _FakeTracker:
    def start(self, *a, **k):
        pass

    def finish(self, *a, **k):
        pass

    def denied(self, *a, **k):
        pass

    def stop(self):
        pass


def _tool_chunk(name, args_json, call_id):
    delta = SimpleNamespace(content=None,
                            tool_calls=[SimpleNamespace(
                                index=0, id=call_id,
                                function=SimpleNamespace(name=name, arguments=args_json))])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="tool_calls")],
                           usage=None, model="deepseek-test-v1")


def _usage_chunk(prompt=10, completion=5):
    return SimpleNamespace(choices=[], usage={"prompt_tokens": prompt,
                                              "completion_tokens": completion,
                                              "total_tokens": prompt + completion},
                           model="deepseek-test-v1")


def _unanswered(msgs):
    """列出所有没配到结果的 tool_call id。空列表 = 这份对话对 API 合法。"""
    bad = []
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            answered = {x.get("tool_call_id") for x in msgs[i + 1:] if x.get("role") == "tool"}
            bad += [tc["id"] for tc in m["tool_calls"] if tc["id"] not in answered]
    return bad


def _tool_scripts(n):
    """n 个「调 read_file」的响应 —— 循环会一直想干活，撞到上限为止。"""
    return [[_tool_chunk("read_file", '{"path": "a.py"}', f"c{i}"), _usage_chunk()]
            for i in range(1, n + 1)]


def _text_script(text):
    delta = SimpleNamespace(content=text, tool_calls=None)
    return [SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
                            usage=None, model="deepseek-test-v1"),
            _usage_chunk()]


def _run_loop(scripts, max_steps):
    """真跑一遍 _agent_loop_inner（预算压到 max_steps）。

    返回 (假客户端, 跑完的 messages, 本轮终态)。
    """
    fake = _FakeClient(scripts)
    saved = (agent.client, agent.get_system_prompt, agent.StreamView,
             agent.info, agent.warn, agent.tracker, agent.MAX_STEPS)
    agent.client = fake
    agent.get_system_prompt = lambda _ctx: "SYS"
    agent.StreamView = _FakeView
    agent.info = agent.warn = lambda *a, **k: None
    agent.tracker = _FakeTracker()
    agent.MAX_STEPS = max_steps              # 别真等 20 步
    try:
        messages = [{"role": "system", "content": ""},
                    {"role": "user", "content": "审 a.py"}]
        status = agent._agent_loop_inner(messages, {})
    finally:
        (agent.client, agent.get_system_prompt, agent.StreamView,
         agent.info, agent.warn, agent.tracker, agent.MAX_STEPS) = saved
    return fake, messages, status


def test_loop_sends_the_notice_and_keeps_it_out_of_history():
    """真跑一遍循环（预算压到 3 步），看它每一步到底发了什么。"""
    _bootstrap.make_file("a.py", "x = 1\n")
    salvaged = "## 审查结论\n- [严重] a.py:1 — 只看完了这一处"
    fake, messages, status = _run_loop(_tool_scripts(3) + [_text_script(salvaged)], 3)

    assert len(fake.seen) == 4                    # 3 步 + 1 次抢救
    for k, request in enumerate(fake.seen[:3]):
        sent = request["messages"]
        assert sent[-1]["role"] == "user"
        assert "[步数预算]" in sent[-1]["content"]
        assert f"{k + 1}/3" in sent[-1]["content"]
        assert _unanswered(sent) == []            # 尾部加消息没破坏配对

    # 最后一步必须是最硬的那一档：它是唯一能救回产出的机会
    assert "停止一切工具调用" in fake.seen[2]["messages"][-1]["content"]

    # 第 4 次调用是抢救：不许带工具（带了它可能又去调，留下配不到结果的 tool_call）
    salvage = fake.seen[3]
    assert "tools" not in salvage
    assert salvage["messages"][-1]["content"] == agent.BUDGET_EXHAUSTED_PROMPT
    assert salvage["stream_options"] == {"include_usage": True}

    # 工作副本里不许留下预算提示 —— 它是会存盘的原文
    assert all("[步数预算]" not in str(m.get("content", "")) for m in messages)
    # 报告在前、[Error] 在后：这个顺序是 bench 认出「有产出但仍是失败」的依据
    assert messages[-2]["content"] == salvaged
    assert messages[-1]["content"].startswith("[Error] max steps (3)")
    assert status == agent.FAILED             # 撞上限是失败 —— main.py 靠它跳过记忆提炼


def test_salvaged_report_is_output_but_still_a_failure():
    """抢救出的报告要能被 bench 数成产出，同时**不能**把失败洗成成功。

    run_task 和 replay 都按「从尾部往前找第一条非 [Error] 的 assistant 文本」取报告，
    再按 [Error] 定 end_reason。这条顺序一旦被调换，撞上限的运行就会混进成功率里 ——
    那正是这个项目最忌讳的「口径被偷换」。
    """
    import sys
    sys.path.insert(0, str(_bootstrap.AGENT_DIR / "bench"))
    import replay                                            # noqa: E402
    import run_task                                          # noqa: E402

    salvaged = "## 审查结论\n- [严重] a.py:1 — 只来得及看这些"
    msgs = [
        {"role": "user", "content": "审一下"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x = 1"},
        {"role": "assistant", "content": salvaged},
        {"role": "assistant", "content": "[Error] max steps (20) exceeded, stop to avoid infinite loop"},
    ]

    rec = run_task.summarize("2026-09-27_00-00-00", msgs)
    assert rec["report_chars"] == len(salvaged)                       # 产出被记下来
    assert rec["end_reason"].startswith("[Error] max steps")         # 但仍然算撞上限
    assert replay.final_report(msgs) == salvaged


def test_salvage_failure_falls_back_to_plain_fatal():
    """抢救那一次调用失败时，不能把整轮也带走：照旧落 [Error]，只是没有报告。"""
    _bootstrap.make_file("a.py", "x = 1\n")
    fake, messages, status = _run_loop(_tool_scripts(2), 2)   # 脚本只有 2 条，抢救那次没响应可用

    assert len(fake.seen) == 3                        # 第 3 次是发出去的抢救请求，它失败了
    assert messages[-2]["role"] == "tool"             # 没有多出报告
    assert messages[-1]["content"].startswith("[Error] max steps (2)")
    assert status == agent.FAILED


def test_salvage_can_be_switched_off():
    """关掉开关就回到老行为（撞墙 = 0 字），做对照实验时两种口径不能混着比。"""
    _bootstrap.make_file("a.py", "x = 1\n")
    saved = agent.SALVAGE_ON_MAX_STEPS
    agent.SALVAGE_ON_MAX_STEPS = False
    try:
        fake, messages, status = _run_loop(_tool_scripts(2), 2)
    finally:
        agent.SALVAGE_ON_MAX_STEPS = saved

    assert len(fake.seen) == 2                        # 没有多花那一次调用
    assert messages[-1]["content"].startswith("[Error] max steps (2)")
    assert status == agent.FAILED


def test_a_normal_finish_reports_ok():
    """跑完的一轮返回 OK —— 它是唯一允许提炼记忆的终态。"""
    _bootstrap.make_file("a.py", "x = 1\n")
    fake, messages, status = _run_loop([_text_script("## 审查结论\n没问题")], 3)

    assert len(fake.seen) == 1                # 一句就说完了，没有多余的调用
    assert status is agent.OK
    assert messages[-1]["content"].startswith("## 审查结论")


# ── 交白卷不算跑完 ─────────────────────────────────────────────────────

def _empty_script():
    """一条「既没内容、也没工具调用」的响应。"""
    return _text_script("")


def test_empty_final_answer_is_asked_again_once():
    """白卷不是「跑完了」，是「什么都没交」—— 先重问一次。"""
    _bootstrap.make_file("a.py", "x = 1\n")
    report = "## 审查结论\n补上了"
    fake, messages, status = _run_loop([_empty_script(), _text_script(report)], 5)

    assert len(fake.seen) == 2
    resent = fake.seen[1]["messages"]
    assert resent[-2]["content"] == agent.EMPTY_ANSWER_PROMPT   # 重问那句
    assert "[步数预算]" in resent[-1]["content"]                 # 后面照旧跟预算提示
    assert status is agent.OK                       # 重问之后有内容了，才算跑完
    assert messages[-1]["content"] == report
    # 白卷那条 assistant 消息不许进对话：有些服务商不接受 content 为空的 assistant 消息
    assert all(str(m.get("content") or "").strip()
               for m in messages if m["role"] == "assistant")


def test_empty_final_answer_twice_is_a_failure():
    """重问一次还是白卷 —— 按失败处理，不装作跑完了。"""
    _bootstrap.make_file("a.py", "x = 1\n")
    fake, messages, status = _run_loop([_empty_script(), _empty_script()], 5)

    assert len(fake.seen) == 2                      # 只重问一次，不无限追问
    assert status == agent.FAILED
    assert messages[-1]["content"].startswith("[Error] model returned an empty final answer")


def test_empty_answer_at_the_last_step_falls_into_the_salvage():
    """步数刚好用完时交白卷：重问的 continue 会踩进 MAX_STEPS 的抢救分支。"""
    _bootstrap.make_file("a.py", "x = 1\n")
    fake, messages, status = _run_loop([_empty_script(), _text_script("## 抢救出来的报告")], 1)

    assert len(fake.seen) == 2
    assert "tools" not in fake.seen[1]              # 第二次是抢救：不许调工具
    assert messages[-2]["content"] == "## 抢救出来的报告"
    assert status == agent.FAILED                   # 抢救出报告也仍然算撞上限
