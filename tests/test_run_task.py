"""跑一次的入口测试：用假客户端和假 worker 把整条链路走通。不联网、不花钱。

最要紧的一条是「token 到底有没有被记下来」——usage 以前被 `continue` 跳过了，
埋点要是没接上，真跑一次就是白花钱。另外几条验证隔离承诺：改动落在副本上、
原件不动、每次运行有个全新的家。
"""
import json
import io
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

import _bootstrap                                            # noqa: F401  先设临时仓库
sys.path.insert(0, str(_bootstrap.AGENT_DIR / "bench"))

import agent                                                 # noqa: E402
import run_task                                              # noqa: E402
import session                                               # noqa: E402
import ui                                                    # noqa: E402


# ── 假客户端：形状照着 OpenAI 流式接口做 ──────────────────────────────────

def _obj(**kw):
    return SimpleNamespace(**kw)


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __enter__(self):
        return iter(self._chunks)

    def __exit__(self, *exc):
        return False


class _Completions:
    def __init__(self, scripts, seen):
        self._scripts, self._seen = scripts, seen

    def create(self, **kwargs):
        self._seen.append(kwargs)
        if not self._scripts:
            raise AssertionError("假客户端：响应序列用完了，说明循环没在该停的地方停")
        return _Stream(self._scripts.pop(0))


class FakeClient:
    def __init__(self, scripts):
        self.seen = []
        self.chat = _obj(completions=_Completions(list(scripts), self.seen))


def _text_chunk(text, finish="stop", model="deepseek-test-v1"):
    delta = _obj(content=text, tool_calls=None)
    return _obj(choices=[_obj(delta=delta, finish_reason=finish)], usage=None, model=model)


def _tool_chunk(name, args_json, call_id="call_1", model="deepseek-test-v1"):
    tc = _obj(index=0, id=call_id, function=_obj(name=name, arguments=args_json))
    delta = _obj(content=None, tool_calls=[tc])
    return _obj(choices=[_obj(delta=delta, finish_reason="tool_calls")], usage=None, model=model)


def _usage_chunk(prompt, completion, model="deepseek-test-v1"):
    """收尾分片：**没有 choices，只有 usage** —— 埋点必须在 continue 之前读它。"""
    return _obj(choices=[], usage={"prompt_tokens": prompt,
                                   "completion_tokens": completion,
                                   "total_tokens": prompt + completion}, model=model)


def _final(text, prompt=10, completion=5):
    return [_text_chunk(text), _usage_chunk(prompt, completion)]


def _ask_then_final(name, args_json, text, p1=20, c1=7, p2=10, c2=5):
    """**两个响应**：先要工具，看到结果之后再给最终回答。

    注意层级：`_final()` 返回一个响应（若干分片），本函数返回一个响应列表。
    所以传给 FakeClient 时不要再套一层方括号。
    """
    return [[_tool_chunk(name, args_json), _usage_chunk(p1, c1)],
            _final(text, p2, c2)]


# ── 测试用的临时目录 ──────────────────────────────────────────────────────

_TMP = []


def _redirect_state():
    """把会话 / 存档指到临时目录 —— 测试绝不能往真项目里写。"""
    for attr, prefix in (("SESSIONS_DIR", "cr-run-task-sessions-"),
                         ("ARCHIVE_DIR", "cr-run-task-archives-")):
        d = Path(tempfile.mkdtemp(prefix=prefix))
        setattr(session, attr, d)
        _TMP.append(d)


def _mktemp(prefix="cr-run-task-"):
    d = Path(tempfile.mkdtemp(prefix=prefix))
    _TMP.append(d)
    return d


def _cleanup():
    for d in _TMP:
        shutil.rmtree(d, ignore_errors=True)
    _TMP.clear()


# ── token / 模型版本必须被记下来 ──────────────────────────────────────────

def test_usage_and_reported_model_are_recorded():
    _redirect_state()
    try:
        client = FakeClient([_final("报告正文 a.py:1")])
        res = run_task.run_session("审一下", client=client)
        assert res["usage"]["calls"] == 1
        assert res["usage"]["prompt_tokens"] == 10
        assert res["usage"]["completion_tokens"] == 5
        assert res["usage"]["total_tokens"] == 15
        assert res["model_reported"] == "deepseek-test-v1"     # 不是配置里的别名
        assert client.seen[0]["stream_options"] == {"include_usage": True}   # 不开就收不到 token
        records = [json.loads(l) for l in
                   session.log_path(res["session_id"]).read_text(encoding="utf-8").splitlines()
                   if l.strip()]
        usage = [r for r in records if r.get("type") == "usage"]
        assert len(usage) == 1 and usage[0]["usage"]["total_tokens"] == 15
        assert res["report"].strip() == "报告正文 a.py:1"
        assert res["end_reason"] == "ok"
    finally:
        _cleanup()


def test_tool_round_trip_is_counted():
    _redirect_state()
    _bootstrap.make_file("a.py", "x = 1\n")
    try:
        client = FakeClient(_ask_then_final("read_file", '{"path": "a.py"}', "看完了"))
        res = run_task.run_session("审 a.py", client=client)
        assert client.seen[0]["stream_options"] == {"include_usage": True}
        assert res["steps"] == 1
        assert res["tool_calls"] == 1
        assert res["tools"] == {"read_file": 1}
        assert res["usage"]["calls"] == 2                     # 两次调用都要计上
        assert res["usage"]["total_tokens"] == 27 + 15
    finally:
        _cleanup()


def test_two_usage_chunks_do_not_leak_into_each_other():
    """第二次调用的 token 不许把第一次的覆盖掉。"""
    _redirect_state()
    _bootstrap.make_file("b.py", "y = 1\n")
    try:
        client = FakeClient(_ask_then_final("read_file", '{"path": "b.py"}', "好",
                                            p1=100, c1=1, p2=2, c2=3))
        res = run_task.run_session("审 b.py", client=client)
        assert res["usage"]["prompt_tokens"] == 102
        assert res["usage"]["completion_tokens"] == 4
    finally:
        _cleanup()


def test_fatal_error_is_not_counted_as_a_report():
    """整轮作废时的 [Error] 不是审查结论，不能进 report / report_chars。"""
    msgs = [
        {"role": "user", "content": "审一下"},
        {"role": "assistant", "content": "[Error] APIConnectionError: Connection error."},
    ]
    res = run_task.summarize("2026-09-26_00-00-00", msgs)
    assert res["report"] == ""
    assert res["report_chars"] == 0
    assert res["end_reason"].startswith("[Error]")
    assert res["usage"]["total_tokens"] == 0


def test_denials_are_split_by_kind():
    """"越界尝试" 和 "该问人但没人可问" 不是一回事，要分开数。"""
    def call(cid, name):
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}]}

    msgs = [
        call("c1", "bash"),
        {"role": "tool", "tool_call_id": "c1", "content": "Permission denied by user"},
        call("c2", "read_file"),
        {"role": "tool", "tool_call_id": "c2",
         "content": "Permission denied: 'C:\\x' is outside allowed directories"},
        call("c3", "bash"),
        {"role": "tool", "tool_call_id": "c3", "content": "Blocked: 命令含高危操作 'sudo'"},
    ]
    res = run_task.summarize("2026-09-26_00-00-00", msgs)
    assert res["denied_no_human"] == 1        # 该问人，但没人可问
    assert res["denied_by_policy"] == 2       # 越界 + 高危命令
    assert res["permission_denials"] == 3


def test_permission_prompt_without_console_is_deny():
    """没有可用控制台时不能崩：按拒绝处理。

    真跑第一次就是死在这里 —— prompt_toolkit 在无控制台时抛
    NoConsoleScreenBufferError，它不属于 KeyboardInterrupt / EOFError，
    异常冒到顶层，整轮任务连同已经跑出来的中间结果全没了。
    """
    saved_prompt, saved_console = ui.pt_prompt, ui.console

    def boom(*_a, **_k):
        raise RuntimeError("No Windows console found. Are you running cmd.exe?")

    try:
        ui.pt_prompt = boom
        ui.console = Console(file=io.StringIO(), theme=ui.THEME, width=100)
        assert ui.permission_prompt("Write outside workdir", "C:\\some\\path") is False
    finally:
        ui.pt_prompt, ui.console = saved_prompt, saved_console


def test_ask_denied_without_console_does_not_kill_the_run():
    """无人值守时「要问的事」= 拒绝，而且这一轮必须活着跑完。"""
    _redirect_state()
    saved_prompt = ui.pt_prompt
    try:
        def boom(*_a, **_k):
            raise RuntimeError("no console")
        ui.pt_prompt = boom
        args = json.dumps({"path": "new_file.py", "content": "x = 1\n"})
        client = FakeClient([[_tool_chunk("write_file", args), _usage_chunk(5, 1)],
                             _final("写不进去，我改成只读审查")])
        res = run_task.run_session("审一下", client=client)
        assert res["end_reason"] == "ok"                 # 没崩
        assert res["steps"] == 1
        assert res["permission_denials"] == 1            # 记下了这次「需要人点头」
        assert not (_bootstrap.REPO / "new_file.py").exists()   # 也没真的写进去
    finally:
        ui.pt_prompt = saved_prompt
        _cleanup()


def test_temperature_only_sent_when_configured():
    _redirect_state()
    saved = agent.TEMPERATURE
    try:
        client = FakeClient([_final("ok")])
        run_task.run_session("x", client=client)
        assert "temperature" not in client.seen[0]            # 不设就不发（默认行为不变）

        agent.TEMPERATURE = 0
        client2 = FakeClient([_final("ok")])
        run_task.run_session("x", client=client2)
        assert client2.seen[0]["temperature"] == 0
    finally:
        agent.TEMPERATURE = saved
        _cleanup()


# ── 隔离：指纹、拷贝、副本 ────────────────────────────────────────────────

def test_tree_digest_and_diff():
    root = _mktemp()
    try:
        (root / "a.py").write_text("1", encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "b.py").write_text("2", encoding="utf-8")
        before = run_task.tree_digest(root)
        assert set(before) == {"a.py", "sub/b.py"}

        (root / "a.py").write_text("changed", encoding="utf-8")
        (root / "c.py").write_text("new", encoding="utf-8")
        after = run_task.tree_digest(root)
        assert run_task.diff_digests(before, after) == ["a.py", "c.py"]
        assert run_task.diff_digests(after, after) == []
    finally:
        _cleanup()


def test_copy_repo_skips_junk_but_keeps_code():
    src = _mktemp("cr-src-")
    dst = _mktemp("cr-dst-") / "repo"
    try:
        (src / ".git").mkdir()
        (src / ".git" / "config").write_text("secret", encoding="utf-8")
        (src / "__pycache__").mkdir()
        (src / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        (src / "a.py").write_text("1", encoding="utf-8")
        # 密钥文件绝不进副本：读工作目录是放行的，拷进去就等于让它读、还会发给模型服务商
        (src / ".env").write_text("API_KEY=sk-real-secret", encoding="utf-8")
        (src / ".env.local").write_text("API_KEY=sk-real-secret", encoding="utf-8")
        (src / ".env.example").write_text("API_KEY=sk-your-key", encoding="utf-8")
        run_task.copy_repo(src, dst)
        assert (dst / "a.py").exists()
        assert (dst / ".env.example").exists()                # 示例文件留着（审查要看它）
        assert not (dst / ".env").exists()                    # 真密钥不进副本
        assert not (dst / ".env.local").exists()
        assert not (dst / ".git").exists()                    # 版本库不进副本
        assert not (dst / "__pycache__").exists()
    finally:
        _cleanup()


# ── 驱动：起子进程、收尾记账、原件不动 ────────────────────────────────────

_STUB = '''
import json, sys, os
from pathlib import Path
argv = sys.argv[1:]
result_file = argv[argv.index("--result-file") + 1]
repo = Path(os.environ["WORKDIR"])
home = Path(os.environ["AGENT_HOME"])
(home / "archives").mkdir(parents=True, exist_ok=True)
archive = home / "archives" / "stub.jsonl"
archive.write_text('{"type":"msg"}\\n', encoding="utf-8")
(repo / "written_by_model.py").write_text("x = 1\\n", encoding="utf-8")
assert float(os.environ["TEMPERATURE"]) == 0.0
Path(result_file).write_text(json.dumps({
    "session_id": "stub", "steps": 1, "tool_calls": 0, "tools": {},
    "usage": {"calls": 1, "prompt_tokens": 3, "completion_tokens": 0, "total_tokens": 3},
    "model_reported": "stub-model", "report_chars": 2, "report": "hi",
    "end_reason": "ok", "archive": str(archive)}, ensure_ascii=False), encoding="utf-8")
'''


def test_execute_run_isolates_and_records():
    src = _mktemp("cr-review-src-")
    out = _mktemp("cr-runs-")
    stub = _mktemp("cr-stub-") / "stub_worker.py"
    try:
        (src / "target.py").write_text("print(1)\n", encoding="utf-8")
        stub.write_text(_STUB, encoding="utf-8")
        before = run_task.tree_digest(src)

        rec = run_task.execute_run(index=1, label="demo", repo_src=src, task="审一下",
                                   out_root=out, temperature=0.0, timeout=120,
                                   verbose=False,
                                   worker_cmd=[sys.executable, str(stub)])

        # 记录齐了
        assert rec["temperature"] == 0.0
        assert rec["usage"]["total_tokens"] == 3
        assert rec["model_reported"] == "stub-model"
        assert rec["returncode"] == 0 and rec["end_reason"] == "ok"

        # 模型动了被审代码 → 必须被发现并列出文件名
        assert rec["repo_modified"] is True
        assert rec["repo_changed_files"] == ["written_by_model.py"]

        # 原件一个字节没动
        assert not (src / "written_by_model.py").exists()
        assert run_task.tree_digest(src) == before

        # 记录、报告、存档都落了盘，且 archive 路径已经改到新位置
        run_dir = out / "demo" / "run-1"
        assert (run_dir / "record.json").exists()
        assert (run_dir / "report.md").read_text(encoding="utf-8") == "hi"
        assert (run_dir / "home" / "archives" / "stub.jsonl").exists()
        assert rec["archive"] == str(run_dir / "home" / "archives" / "stub.jsonl")
        # 默认把被审副本留着：报告里的 文件:行号 得拿它核对
        assert (run_dir / "repo" / "written_by_model.py").exists()
    finally:
        _cleanup()


def test_repo_copy_can_be_discarded():
    """--no-keep-repo 时才扔掉副本。"""
    src = _mktemp("cr-review-src-")
    out = _mktemp("cr-runs-")
    stub = _mktemp("cr-stub-") / "stub_worker.py"
    try:
        (src / "target.py").write_text("print(1)\n", encoding="utf-8")
        stub.write_text(_STUB, encoding="utf-8")
        run_task.execute_run(index=1, label="gone", repo_src=src, task="审一下",
                             out_root=out, temperature=None, timeout=120, verbose=False,
                             keep_repo=False, worker_cmd=[sys.executable, str(stub)])
        assert not (out / "gone" / "run-1" / "repo").exists()
    finally:
        _cleanup()


def test_execute_run_survives_worker_crash():
    """worker 崩了也要留下一条记录 —— 不然这次花的钱连个痕迹都没有。"""
    src = _mktemp("cr-review-src-")
    out = _mktemp("cr-runs-")
    try:
        (src / "target.py").write_text("print(1)\n", encoding="utf-8")
        rec = run_task.execute_run(
            index=1, label="crash", repo_src=src, task="审一下", out_root=out,
            temperature=None, timeout=120, verbose=False,
            worker_cmd=[sys.executable, "-c", "raise SystemExit(3)"])
        assert rec["returncode"] == 3
        assert rec["usage"]["total_tokens"] == 0
        assert rec["repo_modified"] is False
        run_dir = out / "crash" / "run-1"
        assert (run_dir / "record.json").exists()
        assert (run_dir / "worker.log").exists()
    finally:
        _cleanup()


def test_rerun_does_not_clobber_the_previous_run():
    """同一个 label 再跑一次必须落到新目录 —— 覆盖等于把上一次的钱和数据一起扔掉。"""
    src = _mktemp("cr-review-src-")
    out = _mktemp("cr-runs-")
    stub = _mktemp("cr-stub-") / "stub_worker.py"
    try:
        (src / "target.py").write_text("print(1)\n", encoding="utf-8")
        stub.write_text(_STUB, encoding="utf-8")
        base = dict(label="twice", repo_src=src, task="审一下", out_root=out,
                    temperature=0.0, timeout=120, verbose=False,
                    worker_cmd=[sys.executable, str(stub)])

        assert run_task.next_run_index(out / "twice") == 1
        run_task.execute_run(index=1, **base)
        assert run_task.next_run_index(out / "twice") == 2     # 自动接着往后排
        run_task.execute_run(index=2, **base)

        assert (out / "twice" / "run-1" / "record.json").exists()
        assert (out / "twice" / "run-2" / "record.json").exists()
        assert run_task.next_run_index(out / "twice") == 3
    finally:
        _cleanup()


def test_execute_run_refuses_to_clobber():
    """就算号给重了也必须报错，不许静默覆盖。"""
    src = _mktemp("cr-review-src-")
    out = _mktemp("cr-runs-")
    try:
        (src / "target.py").write_text("print(1)\n", encoding="utf-8")
        (out / "dup" / "run-1").mkdir(parents=True)
        (out / "dup" / "run-1" / "record.json").write_text("{}", encoding="utf-8")
        _bootstrap.assert_raises(
            FileExistsError, run_task.execute_run, index=1, label="dup", repo_src=src,
            task="审一下", out_root=out, temperature=0.0, timeout=120, verbose=False)
    finally:
        _cleanup()


# 这个 stub 只回报「新家里有没有记忆」
_STUB_REPORT_MEMORY = '''
import json, os, sys
from pathlib import Path
argv = sys.argv[1:]
result_file = argv[argv.index("--result-file") + 1]
mem = Path(os.environ["AGENT_HOME"]) / ".memory"
files = sorted(p.name for p in mem.glob("*.md")) if mem.is_dir() else []
Path(result_file).write_text(json.dumps({"memory_files": files, "end_reason": "ok"}),
                             encoding="utf-8")
'''


def test_memory_is_seeded_into_the_fresh_home():
    """隔离不能把 agent 的记忆一起剥掉 —— 那等于换了个更弱的 agent 在测。

    真事：家的位置一换，MEMORY_DIR 落到空目录，update_context() 就只剩 skills。
    而 .memory 里恰好存着"这类仓库常见哪几种缺陷"的结论，剥掉之后模型得从零重新发现，
    于是就一直查证、查不完。CLI 那次跑得通，正是因为带着这 11 条。
    """
    src = _mktemp("cr-review-src-")
    out = _mktemp("cr-runs-")
    profile = _mktemp("cr-profile-")
    stub = profile / "stub_mem.py"
    try:
        (src / "t.py").write_text("x = 1\n", encoding="utf-8")
        mem = profile / "memories"
        mem.mkdir()
        (mem / "MEMORY.md").write_text("- x", encoding="utf-8")
        (mem / "lesson.md").write_text("y", encoding="utf-8")
        stub.write_text(_STUB_REPORT_MEMORY, encoding="utf-8")
        base = dict(repo_src=src, task="审一下", out_root=out, temperature=None,
                    timeout=60, verbose=False, worker_cmd=[sys.executable, str(stub)])

        seeded = run_task.execute_run(index=1, label="with", memory_src=mem, **base)
        assert seeded["memory_files"] == ["MEMORY.md", "lesson.md"]
        assert seeded["memory_seeded"] == str(mem)

        stripped = run_task.execute_run(index=1, label="without", memory_src=None, **base)
        assert stripped["memory_files"] == []
        assert stripped["memory_seeded"] is None
    finally:
        _cleanup()


def test_failed_run_is_not_distilled_into_memory():
    """和 main.after_turn 同一条规矩：没跑完的一轮不许提炼记忆。

    提炼本身要花一次 API 调用，所以这里把 agent_loop 和 memory.extract_memories
    都换掉 —— 测的是「闸门开不开」，不是提炼质量。
    """
    _redirect_state()
    import memory
    called = []
    saved_loop, saved_extract = agent.agent_loop, memory.extract_memories
    memory.extract_memories = lambda _msgs: called.append(1)
    try:
        agent.agent_loop = lambda _m, _c: agent.FAILED       # 报错 / 撞上限的轮次
        run_task.run_session("审一下", client=FakeClient([]), with_memory=True)
        assert called == []

        agent.agent_loop = lambda _m, _c: agent.OK           # 跑完了的轮次
        run_task.run_session("审一下", client=FakeClient([]), with_memory=True)
        assert called == [1]
    finally:
        agent.agent_loop, memory.extract_memories = saved_loop, saved_extract
        _cleanup()


# worker 崩了、但 archives 已经写下去了：数据必须能从存档里捞回来
_STUB_CRASH_AFTER_ARCHIVING = '''
import json, os, sys
from pathlib import Path
repo = Path(os.environ["WORKDIR"])
home = Path(os.environ["AGENT_HOME"])
d = home / "archives" / "2026" / "09" / "26"
d.mkdir(parents=True, exist_ok=True)
records = [
    {"type": "meta", "id": "2026-09-26_00-00-00", "workdir": str(repo)},
    {"type": "msg", "message": {"role": "user", "content": "audit"}},
    {"type": "usage", "model": "stub-model",
     "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}},
    {"type": "msg", "message": {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function",
         "function": {"name": "write_file", "arguments": "{}"}}]}},
    {"type": "msg", "message": {"role": "tool", "tool_call_id": "c1",
                                "content": "Permission denied by user"}},
    {"type": "msg", "message": {"role": "assistant", "content": "report: code.py:1"}},
]
(d / "2026-09-26_00-00-00.jsonl").write_text(
    "\\n".join(json.dumps(r, ensure_ascii=False) for r in records), encoding="utf-8")
sys.exit(1)
'''


def test_crashed_worker_data_is_recovered_from_archive():
    """worker 崩了没写 result.json，但 archives 里有 —— 这次花的钱不能白花。"""
    src = _mktemp("cr-review-src-")
    out = _mktemp("cr-runs-")
    stub = _mktemp("cr-stub-") / "stub_crash.py"
    try:
        (src / "t.py").write_text("x = 1\n", encoding="utf-8")
        stub.write_text(_STUB_CRASH_AFTER_ARCHIVING, encoding="utf-8")
        rec = run_task.execute_run(index=1, label="crashed", repo_src=src, task="审一下",
                                   out_root=out, temperature=0.0, timeout=120,
                                   verbose=False,
                                   worker_cmd=[sys.executable, str(stub)])
        assert rec["returncode"] == 1
        assert rec["rebuilt_from_archive"] is True
        assert rec["session_id"] == "2026-09-26_00-00-00"
        assert rec["steps"] == 1 and rec["tool_calls"] == 1
        assert rec["usage"]["total_tokens"] == 9
        assert rec["model_reported"] == "stub-model"
        assert rec["permission_denials"] == 1
        assert rec["report_chars"] > 0
        assert (out / "crashed" / "run-1" / "report.md").read_text(encoding="utf-8") != ""
    finally:
        _cleanup()
