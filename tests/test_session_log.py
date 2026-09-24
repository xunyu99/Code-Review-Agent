"""会话存档测试：原文日志（只追加）和工作副本（可压缩）必须分开。

这是本项目最容易搞混的一处边界 —— 压缩可以改写发给模型的上下文，
但绝对不能动磁盘上的原文。下面几条把这个边界钉死。
"""
import atexit
import json
import shutil
import tempfile
from pathlib import Path

import _bootstrap  # noqa: F401

import agent
import session

_TMP_DIRS: list[Path] = []


@atexit.register
def _cleanup_tmp_dirs():
    """临时目录别留给下一次运行 —— 跑十遍就攒十个。"""
    for d in _TMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)


def _new_session():
    """把 session 的存档根目录指到临时目录，返回一个新会话。"""
    session.SESSIONS_DIR = Path(tempfile.mkdtemp(prefix="cr-agent-sessions-"))
    _TMP_DIRS.append(session.SESSIONS_DIR)
    return session.new_session("E:\\fake\\repo")


def _records(sid):
    """把日志逐行 parse 出来。"""
    text = session.log_path(sid).read_text(encoding="utf-8")
    return [json.loads(l) for l in text.splitlines() if l.strip()]


def _fill(msgs, n):
    for i in range(n):
        msgs.append({"role": "user", "content": f"问题{i}"})
        msgs.append({"role": "assistant", "content": f"回答{i}"})


# ── 基本读写 ──

def test_state_roundtrip_and_log_only_gets_real_messages():
    s = _new_session()
    msgs, log = session.open_session(s)
    assert s["messages"] is msgs                    # 必须挂回去，否则 state 会存旧的 list

    msgs.insert(0, {"role": "system", "content": ""})   # 占位，不该进日志
    msgs.append({"role": "user", "content": "你好"})
    msgs.append({"role": "assistant", "content": "在"})
    session.save_session(s)

    assert [r["type"] for r in _records(s["id"])] == ["meta", "msg", "msg"]
    assert [m["content"] for m in session.load_raw_messages(s["id"])] == ["你好", "在"]

    back = session.load_session(s["id"])
    assert [m["content"] for m in back["messages"]] == ["", "你好", "在"]


def test_paths_split_into_two_files():
    s = _new_session()
    assert session.state_path(s["id"]).name == f"{s['id']}.state.json"
    assert session.log_path(s["id"]).name == f"{s['id']}.jsonl"
    assert session.state_path(s["id"]).parent == session.log_path(s["id"]).parent


# ── 核心：压缩不许碰存档 ──

def test_compaction_never_touches_the_log():
    """模拟 agent.py 的 messages[:] = ... 压缩，日志必须一个字节都不变。"""
    s = _new_session()
    msgs, log = session.open_session(s)
    _fill(msgs, 5)
    session.save_session(s)
    before = session.log_path(s["id"]).read_text(encoding="utf-8")

    msgs[:] = [{"role": "user", "content": "[Compacted] 摘要"}]   # ← 压缩
    session.save_session(s)

    assert session.log_path(s["id"]).read_text(encoding="utf-8") == before
    raw = session.load_raw_messages(s["id"])
    assert len(raw) == 10 and raw[0]["content"] == "问题0" and raw[-1]["content"] == "回答4"
    assert len(session.load_session(s["id"])["messages"]) == 1     # 工作副本确实被压了


def test_micro_compact_in_place_still_leaves_original():
    """L2 是就地改 msg["content"]，日志里那条 tool 结果必须还是原样。"""
    s = _new_session()
    msgs, log = session.open_session(s)
    msgs.append({"role": "tool", "tool_call_id": "t1", "content": "原始的工具结果" * 50})
    session.save_session(s)

    msgs[0]["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    session.save_session(s)

    assert session.load_raw_messages(s["id"])[0]["content"] == "原始的工具结果" * 50
    assert "compacted" in session.load_session(s["id"])["messages"][0]["content"]


def test_real_compactors_from_agent_leave_the_log_intact():
    """拿 agent.py 真正的三层压缩跑一遍（顺序和 agent_loop 里一致），
    日志必须一个字节都不变，且原文一条不少。
    """
    agent.OUTPUT_DIR = Path(tempfile.mkdtemp(prefix="cr-agent-outputs-"))
    _TMP_DIRS.append(agent.OUTPUT_DIR)
    s = _new_session()
    msgs, _ = session.open_session(s)
    msgs.append({"role": "user", "content": "总目标"})
    for i in range(18):                                  # 够多才压得动（snip 要 >50 条）
        msgs.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "read_file", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": f"结果{i}" * 3000})
        msgs.append({"role": "assistant", "content": f"第{i}轮结论"})
    session.save_session(s)
    before = session.log_path(s["id"]).read_text(encoding="utf-8")
    original_count = len(msgs)

    # ← agent.py 里压缩触发的原样调用
    msgs[:] = agent.tool_result_budget(msgs)   # L3 大输出落盘
    msgs[:] = agent.snip_compact(msgs)         # L1 裁中间
    msgs[:] = agent.micro_compact(msgs)        # L2 旧工具结果占位
    session.save_session(s)

    assert session.log_path(s["id"]).read_text(encoding="utf-8") == before
    assert len(msgs) < original_count                                   # 上下文确实变小了
    raw = session.load_raw_messages(s["id"])
    assert len(raw) == original_count                                   # 原文一条不少
    assert raw[2]["content"] == "结果0" * 3000                          # 且是落盘前/占位前的原文


def test_repair_dangling_tool_calls_is_logged():
    """中断补的占位结果也是真进了对话，得记进日志。"""
    s = _new_session()
    msgs, _ = session.open_session(s)
    msgs.append({"role": "assistant", "content": "",
                 "tool_calls": [{"id": "c9", "type": "function",
                                 "function": {"name": "read_file", "arguments": "{}"}}]})
    agent.repair_dangling_tool_calls(msgs)

    raw = session.load_raw_messages(s["id"])
    assert [m["role"] for m in raw] == ["assistant", "tool"]
    assert raw[1]["tool_call_id"] == "c9"


# ── 跨轮 / 恢复 ──

def test_resume_does_not_reseed_and_keeps_appending():
    s = _new_session()
    msgs, _ = session.open_session(s)
    msgs.append({"role": "user", "content": "第一轮"})
    session.save_session(s)

    s2 = session.load_session(s["id"])              # 模拟 resume
    msgs2, _ = session.open_session(s2)
    msgs2.append({"role": "user", "content": "第二轮"})
    session.save_session(s2)

    assert [r["type"] for r in _records(s["id"])] == ["meta", "msg", "msg"]   # 没有重复 meta/seed
    assert [m["content"] for m in session.load_raw_messages(s["id"])] == ["第一轮", "第二轮"]


def test_legacy_json_still_loads_and_seeds_the_log():
    s = _new_session()
    p = session.legacy_path(s["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    s["messages"] = [{"role": "user", "content": "老会话"}]
    p.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")

    assert not session.log_path(s["id"]).exists()    # 还没打开就没有日志
    back = session.load_session(s["id"])
    assert back["messages"][0]["content"] == "老会话"

    session.open_session(back)
    recs = _records(s["id"])
    assert [r["type"] for r in recs] == ["meta", "seed", "msg"]
    assert recs[1]["source"] == "state"              # 如实标注：这是工作副本，不是原文
    assert session.load_raw_messages(s["id"])[0]["content"] == "老会话"


# ── 鲁棒性 ──

def test_broken_line_does_not_break_rebuild():
    s = _new_session()
    msgs, _ = session.open_session(s)
    msgs.append({"role": "user", "content": "好的"})
    with session.log_path(s["id"]).open("a", encoding="utf-8") as f:
        f.write('{"type":"msg","message":{"role":"user","cont')   # 断电留下的半截行
    msgs.append({"role": "user", "content": "坏的后面"})

    assert [m["content"] for m in session.load_raw_messages(s["id"])] == ["好的", "坏的后面"]


def test_missing_session_returns_none():
    _new_session()
    assert session.load_session("2099-01-01_00-00-00") is None
    assert session.load_raw_messages("2099-01-01_00-00-00") is None
