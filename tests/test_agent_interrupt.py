"""中断收尾测试：Ctrl+C 之后留下的对话，必须「合法」且「带标记」。

合法 = 每个 assistant.tool_calls 都配得到一条 tool 结果，否则下次发 API 直接 400。
带标记 = 模型得知道自己那句是被砍断的，不然它会想着补完，而不是重新计划 ——
那正好和用户按 Ctrl+C 的意图相反。
"""
import _bootstrap  # noqa: F401

import agent


class _FakeView:
    """顶替 ui.StreamView：只需要 interrupt() 这一个动作。"""

    def __init__(self, text):
        self._text = text

    def interrupt(self):
        return self._text


def _after_tool_round():
    """一条完整历史：用户提问 → 模型调了工具 → 工具结果已回来。"""
    return [
        {"role": "user", "content": "审查一下 a.py"},
        {"role": "assistant", "content": "我先读 a.py",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "a.py 的内容"},
    ]


def _dangling():
    """一条悬空历史：模型发了两个 tool_call，只有第一个有结果。"""
    return [
        {"role": "user", "content": "审查 a.py 和 b.py"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}},
                        {"id": "c2", "type": "function",
                         "function": {"name": "read_file", "arguments": '{"path":"b.py"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "a.py 的内容"},
    ]


def _unanswered(msgs):
    """列出所有没配到结果的 tool_call id。空列表 = 这份对话是合法的。"""
    bad = []
    for i, m in enumerate(msgs):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            answered = {x.get("tool_call_id") for x in msgs[i + 1:] if x.get("role") == "tool"}
            bad += [tc["id"] for tc in m["tool_calls"] if tc["id"] not in answered]
    return bad


def _abort(msgs, partial):
    """跑一次 _abort_turn，顺手把 warn 的噪音和 _ACTIVE 全局改回去。"""
    old_warn, old_view = agent.warn, agent._ACTIVE["view"]
    agent.warn = lambda *a, **k: None
    agent._ACTIVE["view"] = _FakeView(partial)
    try:
        agent._abort_turn(msgs)
    finally:
        agent.warn, agent._ACTIVE["view"] = old_warn, old_view


# ── Ctrl+C 落在模型吐字中途 ──

def test_partial_text_is_kept_and_marked():
    msgs = _after_tool_round()
    _abort(msgs, "我看到 a.py 里有个空指针，接下来我看看调用方")

    last = msgs[-1]
    assert last["role"] == "assistant"
    assert last["content"].startswith("我看到 a.py 里有个空指针")   # 半截内容一个字没丢
    assert "user interrupted mid-response" in last["content"]      # 但带上了中断标记
    assert _unanswered(msgs) == []


def test_owner_of_partial_mark_is_the_model_not_the_screen():
    """标记只进 messages —— 用户知道自己按了 Ctrl+C，屏幕上不用再提醒一遍。"""
    assert agent.PARTIAL_MARK.strip().startswith("[")
    assert "interrupted" in agent.PARTIAL_MARK


# ── Ctrl+C 落在工具执行中／权限弹窗上 ──

def test_dangling_tool_call_gets_sealed():
    msgs = _dangling()
    n = len(msgs)
    _abort(msgs, "")            # commit() 已清空 buffer，所以 partial 是空串

    assert _unanswered(msgs) == []                      # 悬空的 c2 被补上了
    assert msgs[-1]["role"] == "tool"
    assert "interrupted by user" in msgs[-1]["content"]
    assert len(msgs) == n + 1                           # 只多了一条占位
    assert all(m["role"] != "assistant" for m in msgs[n:])   # 不该凭空多出 assistant 消息


def test_seal_happens_before_partial_append():
    """两件事假设同时发生时，悬空的调用也必须被封住。

    实际跑起来它们互斥（commit() 会清空 buffer）。但如果先把半截 assistant 塞进去，
    repair 会把它当成对话结尾然后 break，悬空的 tool_call 就漏封了 ——
    那份 messages 发出去就是 400。顺序反过来，这里就永远是合法的。
    """
    msgs = _dangling()
    _abort(msgs, "半截文本")

    assert _unanswered(msgs) == []


def test_abort_twice_does_not_double_seal():
    """再按一次 Ctrl+C、或退出时 safe_save 又补一遍，都不该重复补占位。"""
    msgs = _dangling()
    _abort(msgs, "")
    once = list(msgs)
    _abort(msgs, "")
    assert msgs == once


# ── 给调用方的信号 ──

def test_agent_loop_signals_interruption():
    """"被打断"和"正常跑完"必须是两种返回值，main.py 靠它决定跳不跳记忆提炼。"""
    original, old_warn = agent._agent_loop_inner, agent.warn
    agent.warn = lambda *a, **k: None
    try:
        agent._agent_loop_inner = lambda m, c: (_ for _ in ()).throw(KeyboardInterrupt())
        assert agent.agent_loop([], None) == "interrupted"

        agent._agent_loop_inner = lambda m, c: None
        assert agent.agent_loop([], None) is None          # 正常路径还是 None
    finally:
        agent._agent_loop_inner, agent.warn = original, old_warn
