"""基线（裸循环）自己的测试。

基线是**对照组的定义**，它要是悄悄带上了框架的功能，整张对照表就废了 —— 所以这里
钉住的不是"它能跑通"，而是"它**没有**主 agent 那些东西"：没有步数预算提示、没有撞上限
抢救、没有额外的重问。
"""
import _bootstrap  # noqa: F401

import sys
from types import SimpleNamespace

sys.path.insert(0, str(_bootstrap.AGENT_DIR / "bench"))

import agent                                                  # noqa: E402
import bare_worker                                            # noqa: E402
import run_task                                               # noqa: E402


# ── 假客户端：形状和 test_step_budget.py 里那套一致 ──────────────────────

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
            raise AssertionError("假客户端：响应序列用完了（循环该停没停）")
        return _Stream(self._scripts.pop(0))


class _FakeClient:
    def __init__(self, scripts):
        self.completions = _Completions(list(scripts))
        self.chat = SimpleNamespace(completions=self.completions)

    @property
    def seen(self):
        return self.completions.seen


def _tool_chunk(name, args_json, call_id="call_1"):
    delta = SimpleNamespace(content=None,
                            tool_calls=[SimpleNamespace(
                                index=0, id=call_id,
                                function=SimpleNamespace(name=name, arguments=args_json))])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="tool_calls")],
                           usage=None, model="deepseek-test-v1")


def _text_chunk(text):
    delta = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")],
                           usage=None, model="deepseek-test-v1")


def _usage_chunk(prompt=10, completion=5):
    return SimpleNamespace(choices=[],
                           usage={"prompt_tokens": prompt, "completion_tokens": completion,
                                  "total_tokens": prompt + completion},
                           model="deepseek-test-v1")


def _tool_call_then_final(name, args_json, text):
    return [[_tool_chunk(name, args_json), _usage_chunk()],
            [_text_chunk(text), _usage_chunk()]]


def _run_bare(scripts, fake_repo_file=("a.py", "x = 1\n")):
    if fake_repo_file:
        _bootstrap.make_file(*fake_repo_file)
    fake = _FakeClient(scripts)
    saved_client = agent.client
    agent.client = fake
    try:
        messages = [{"role": "system", "content": ""},
                    {"role": "user", "content": "审 a.py"}]
        status = bare_worker.bare_loop(messages, {})
    finally:
        agent.client = saved_client
    return fake, messages, status


# ── 它该做的事 ─────────────────────────────────────────────────────────

def test_bare_loop_runs_a_tool_then_stops():
    fake, messages, status = _run_bare(_tool_call_then_final(
        "read_file", '{"path": "a.py"}', "## 结论\n没问题"))

    assert status is None
    assert len(fake.seen) == 2                       # 一次调工具、一次收尾
    assert messages[0]["content"] == bare_worker.BARE_PROMPT
    assert messages[-1]["content"].startswith("## 结论")
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "assistant"]
    assert messages[3]["content"].strip().endswith("x = 1")     # 真的执行了工具


# ── 它**不该**带的东西（这才是重点）────────────────────────────────────

def test_bare_loop_has_no_step_budget_notice():
    """主 agent 每步都在消息末尾追加预算提示；基线不许有。"""
    fake, _messages, _status = _run_bare(_tool_call_then_final(
        "read_file", '{"path": "a.py"}', "## 结论"))

    for request in fake.seen:
        assert not any("[步数预算]" in str(m.get("content", "")) for m in request["messages"])


def test_bare_loop_hits_the_cap_without_salvaging():
    """撞上限就是 0 产出 —— 基线没有抢救兜底，这正是要对照出来的差异。"""
    saved = bare_worker.MAX_STEPS
    bare_worker.MAX_STEPS = 2
    try:
        fake, messages, status = _run_bare(
            [[_tool_chunk("read_file", '{"path": "a.py"}', "c1"), _usage_chunk()],
             [_tool_chunk("read_file", '{"path": "a.py"}', "c2"), _usage_chunk()],
             # 第三个响应故意备着：真出现说明它偷偷多打了一次（抢救）
             [_text_chunk("抢救报告"), _usage_chunk()]])
    finally:
        bare_worker.MAX_STEPS = saved

    assert len(fake.seen) == 2                       # 只跑了 2 步，没有第三次调用
    assert status == "failed"
    assert messages[-1]["content"].startswith("[Error] max steps (2)")
    assert "抢救报告" not in str(messages)            # 也没留下任何抢救产物


def test_bare_loop_always_sends_the_tools():
    """基线全程带着工具跑；主 agent 的抢救那一次是**不传 tools** 的，别混。"""
    fake, _messages, _status = _run_bare(_tool_call_then_final(
        "read_file", '{"path": "a.py"}', "## 结论"))
    assert all("tools" in r for r in fake.seen)


# ── 猴子补丁不许泄漏 ────────────────────────────────────────────────────

def test_run_bare_session_restores_the_real_loop():
    seen = {}

    def stub(task, verbose=False, with_memory=False):
        seen["during"] = agent.agent_loop          # 进入 run_session 的那一刻是什么
        return {"stub": True}

    saved_run_session = run_task.run_session
    run_task.run_session = stub
    try:
        assert bare_worker.run_bare_session("审一下") == {"stub": True}
    finally:
        run_task.run_session = saved_run_session
    assert seen["during"] is bare_worker.bare_loop                 # 跑的时候确实换掉了
    assert agent.agent_loop is not bare_worker.bare_loop           # 跑完必须还回去


def test_baseline_writes_to_its_own_tree():
    """基线必须落在另一棵树、换另一个 worker —— 免得污染主 agent 的 runs/。"""
    import baseline

    seen = {}
    saved = baseline.run_task.main
    baseline.run_task.main = lambda argv: seen.setdefault("argv", list(argv)) or 0
    try:
        baseline.main(["--label", "caption-v1"])
    finally:
        baseline.run_task.main = saved

    argv = " ".join(seen["argv"])
    assert "--worker-script" in seen["argv"] and "bare_worker.py" in argv
    assert "--out" in seen["argv"] and "runs-baseline" in argv
