"""核查器自己的测试：三档判定必须真的分得开。

`bench/replay.py` 最危险的失败模式是"永远输出真引用"——报告看着一切正常，
其实一个字都没查。所以每一档都配一个**必须被判错**的反例。

用最小的合成会话，不依赖 archives 里那份真实轨迹（它可能被清理），
这样在任何机器和 CI 上都能跑。
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import _bootstrap                                            # noqa: F401  先设临时仓库
sys.path.insert(0, str(_bootstrap.AGENT_DIR / "bench"))

import replay as rp                                          # noqa: E402

REPO = _bootstrap.REPO


def _cite(file, line, end=None):
    return {"file": file, "line": line, "end": end}


def _read_call(rel, start, limit, body, cid="t1"):
    """造一对「assistant 请求 read_file」+「tool 返回内容」。"""
    args = json.dumps({"path": rel, "offset": start, "limit": limit})
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": "read_file", "arguments": args}}]},
        {"role": "tool", "tool_call_id": cid, "content": body},
    ]


def _page(rel, start, limit, total, cid="t1"):
    """一页 read_file：读满 limit 行就带续读提示，读到文件尾就不带。"""
    end = min(start + limit - 1, total)
    body = "\n".join(f"L{i}" for i in range(start, end + 1))
    if end < total:
        body += f"\n... (还有 {total - end} 行未读，用 offset={end + 1} 继续)"
    return _read_call(rel, start, limit, body, cid)


def _convo(rel, total, start=1, limit=10):
    _bootstrap.make_file(rel, "\n".join(f"L{i}" for i in range(1, total + 1)) + "\n")
    return str(REPO), _page(rel, start, limit, total)


# ── 三档各自都有反例 ──────────────────────────────────────────────────────

def test_covered_line_is_real():
    wd, msgs = _convo("rev/a.py", 50)
    cov = rp.read_coverage(msgs, wd)
    assert rp.judge(_cite("rev/a.py", 7), cov, wd)[0] == "real"


def test_out_of_range_is_hallucination():
    wd, msgs = _convo("rev/b.py", 50)
    cov = rp.read_coverage(msgs, wd)
    verdict, why = rp.judge(_cite("rev/b.py", 999), cov, wd)
    assert verdict == "hallucination"
    assert "越界" in why


def test_missing_file_is_hallucination():
    wd, msgs = _convo("rev/c.py", 50)
    cov = rp.read_coverage(msgs, wd)
    assert rp.judge(_cite("rev/nope.py", 1), cov, wd)[0] == "hallucination"


def test_existing_but_unread_is_unknown():
    """文件与行号都对，但这个会话没读过它——不许并进「真」，也不许算「幻觉」。"""
    wd, msgs = _convo("rev/d.py", 50)
    _bootstrap.make_file("rev/other.py", "x\ny\n")
    cov = rp.read_coverage(msgs, wd)
    verdict, why = rp.judge(_cite("rev/other.py", 2), cov, wd)
    assert verdict == "unknown"
    assert "没有读到" in why


def test_only_the_read_range_is_covered():
    """没读到的行落在「无法判定」，不许被「这个文件存在」蒙混过去。"""
    wd, msgs = _convo("rev/e.py", 100)
    cov = rp.read_coverage(msgs, wd)
    assert rp.judge(_cite("rev/e.py", 5), cov, wd)[0] == "real"
    assert rp.judge(_cite("rev/e.py", 80), cov, wd)[0] == "unknown"


# ── 覆盖范围是「并集」，且只能由 read_file 提供 ────────────────────────────

def test_pages_are_merged_into_a_union():
    """两页拼起来之后，交界处必须是「真」——证明覆盖是并集，不是最后一页。"""
    rel, total = "rev/f.py", 24
    _bootstrap.make_file(rel, "\n".join(f"L{i}" for i in range(1, total + 1)) + "\n")
    msgs = _page(rel, 1, 10, total, "t1") + _page(rel, 11, 14, total, "t2")
    cov = rp.read_coverage(msgs, str(REPO))
    assert list(cov.values()) == [[(1, 10), (11, 24)]]
    assert rp._merge(list(cov.values())[0]) == [(1, 24)]
    assert rp.judge(_cite(rel, 1), cov, str(REPO))[0] == "real"
    assert rp.judge(_cite(rel, 24), cov, str(REPO))[0] == "real"


def test_bash_result_does_not_fake_coverage():
    """bash 拿到的内容没有行号结构，不能算读过——那条该进「无法判定」。"""
    _bootstrap.make_file("rev/g.py", "L1\nL2\n")
    msgs = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "bash", "arguments": json.dumps({"command": "type rev\\g.py"})}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "L1\nL2"},
    ]
    cov = rp.read_coverage(msgs, str(REPO))
    assert rp.judge(_cite("rev/g.py", 2), cov, str(REPO))[0] == "unknown"


def test_failed_read_does_not_count_as_coverage():
    """报错的 read_file 什么都没读到，不能算覆盖。"""
    msgs = _read_call("rev/h.py", 1, 10, "Error: path outside the workdir")
    _bootstrap.make_file("rev/h.py", "L1\nL2\n")
    cov = rp.read_coverage(msgs, str(REPO))
    assert cov == {}


# ── 区间引用与各种写法 ────────────────────────────────────────────────────

def test_range_citation_checks_both_ends():
    wd, msgs = _convo("rev/i.py", 50, limit=20)
    cov = rp.read_coverage(msgs, wd)
    assert rp.judge(_cite("rev/i.py", 3, 9), cov, wd)[0] == "real"
    assert rp.judge(_cite("rev/i.py", 9, 999), cov, wd)[0] == "hallucination"


def test_citation_forms():
    text = ("`a.py:12` 与 b/c.py:34-40 还有 d.py 第 56 行、e.py:第78行；"
            "外带 E:\\x\\y\\f.py:90 和文档 g.md:1")
    got = {(c["file"], c["line"], c["end"]) for c in rp.extract_citations(text)}
    assert ("a.py", 12, None) in got
    assert ("b/c.py", 34, 40) in got
    assert ("d.py", 56, None) in got
    assert ("e.py", 78, None) in got
    assert ("E:\\x\\y\\f.py", 90, None) in got
    assert ("g.md", 1, None) in got


def test_same_citation_counted_once():
    cits = rp.extract_citations("a.py:3 又见 a.py:3")
    assert len(cits) == 1 and cits[0]["hits"] == 2


def test_case_and_separator_normalized_on_windows():
    if os.name != "nt":
        return                              # 大小写敏感的文件系统上这条不成立
    wd, msgs = _convo("rev/j.py", 10, limit=10)
    cov = rp.read_coverage(msgs, wd)
    assert rp.judge(_cite("REV\\J.PY", 3), cov, wd)[0] == "real"


# ── 第三～五项的口径（轨迹 / 成本）────────────────────────────────────────

def test_unused_limit_is_not_counted_as_rereading():
    """文件只有 10 行却请求 1000 行：那 990 行是「本来就没有」，不是「重复读」。

    按请求里的 limit 算，会把一次干净的通读记成 99% 的重复率。
    """
    wd, msgs = _convo("rev/k.py", 10, limit=1000)
    st = rp.read_stats(msgs, wd)
    assert st["read_calls"] == 1
    assert st["lines_read"] == 10
    assert st["lines_covered"] == 10
    assert st["reread_rate"] == 0.0


def test_fragment_hunting_shows_up_as_rereading():
    """小窗口来回翻同一个文件 → 重读率上去。这是「逐层深挖」型打转的口径。"""
    rel, total = "rev/l.py", 200
    _bootstrap.make_file(rel, "\n".join(f"L{i}" for i in range(1, total + 1)) + "\n")
    # 先完整读一遍，再把前 60 行拆成 3 个 20 行的小窗口重读 —— 参数各不相同，
    # 所以「完全重复」是 0，但覆盖没有增加
    msgs = _page(rel, 1, 200, total, "t0")
    for i, start in enumerate((1, 21, 41), 1):
        msgs += _page(rel, start, 20, total, f"t{i}")
    st = rp.read_stats(msgs, str(REPO))
    assert st["lines_read"] == 260          # 200 + 60
    assert st["lines_covered"] == 200       # 覆盖没涨
    assert st["reread_rate"] > 0.2


def test_error_message_is_not_a_report():
    """撞上限时最后那条 [Error] 不是报告 —— 否则「0 产出」会被记成「有 60 字产出」。"""
    msgs = [
        {"role": "user", "content": "审一下"},
        {"role": "assistant", "content": "[Error] max steps (20) exceeded, stop to avoid infinite loop"},
    ]
    assert rp.final_report(msgs) == ""


def test_finding_counter_ignores_nested_bullets():
    """顶格才算一条问题；每条下面的「依据：」「建议：」是子项，不是新问题。

    真数据：s01 那次报告 11 条问题，因为带子项被数成了 41 条。
    """
    report = (
        "- 严重 / `code.py:18` — 模型名不存在。\n"
        "  - 依据：官方没有这个 ID。\n"
        "  - 建议：改用 deepseek-chat。\n"
        "\n"
        "- 次要 / `code.py:84` — 没有轮次上限。\n"
        "  - 建议：加 MAX_STEPS。\n"
    )
    bullets = [ln for ln in report.splitlines() if rp._FINDING.match(ln)]
    assert len(bullets) == 2
    assert sum(1 for ln in bullets if rp.CITE_RE.search(ln)) == 2


def test_quote_check_catches_line_drift():
    """报告抄了代码却把行号写偏 —— 机械三档查不出来，这条能。

    真事：caption 那份报告 12 条引用全过机械层，手工一抽四条全对不上（偏 2~20 行）。
    根因是 read_file 不返回行号，模型只能自己数。
    """
    _bootstrap.make_file("rev/m.py",
                         'import os\nDATASET_ROOT = "/data"\nprint(DATASET_ROOT)\n')
    wd = str(REPO)
    report = ('- `rev/m.py:1` — `DATASET_ROOT = "/data"` 是硬编码路径。\n'   # 其实在第 2 行
              "- `rev/m.py:3` — `print(DATASET_ROOT)` 是残留调试输出。\n")   # 第 3 行，准的
    cits = rp.extract_citations(report)
    drift = rp.quote_mismatches(report, cits, wd)
    assert len(drift) == 1
    assert drift[0]["raw"] == "rev/m.py:1"
    assert "第 2 行" in drift[0]["why"] and "+1" in drift[0]["why"]


def test_workdir_falls_back_to_record_repo_src():
    """评测跑的副本删了之后，退回 record.json 里的被审原件。"""
    root = Path(tempfile.mkdtemp(prefix="cr-runlayout-"))
    try:
        src = root / "original_repo"
        src.mkdir()
        (src / "code.py").write_text("x = 1\n", encoding="utf-8")
        archives = root / "run-1" / "home" / "archives" / "2026" / "09" / "26"
        archives.mkdir(parents=True)
        log = archives / "2026-09-26_00-00-00.jsonl"
        log.write_text('{"type":"meta","id":"s"}\n', encoding="utf-8")
        (root / "run-1" / "record.json").write_text(
            json.dumps({"repo_src": str(src), "repo_modified": False}), encoding="utf-8")

        meta = {"workdir": str(root / "gone-copy")}          # 副本已经不存在
        assert rp.resolve_workdir(log, meta, None) == str(src)
        assert rp.resolve_workdir(log, meta, "E:\\override") == "E:\\override"
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 第 2 项：工具调用质量 ────────────────────────────────────────────────

def _archive(root: Path, messages: list) -> Path:
    """把一串消息写成最小存档，喂给 analyze_run / tool_stats。"""
    d = root / "archives" / "2026" / "09" / "26"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "2026-09-26_00-00-00.jsonl"
    lines = [json.dumps({"type": "meta", "id": "t", "workdir": str(REPO)}, ensure_ascii=False)]
    lines += [json.dumps({"type": "msg", "message": m}, ensure_ascii=False) for m in messages]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _pair(cid, name, args, result):
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": cid, "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": cid, "content": result},
    ]


def test_tool_stats_counts_small_pages_and_failures():
    root = Path(tempfile.mkdtemp(prefix="cr-toolstats-"))
    try:
        msgs = (
            _pair("c1", "read_file", {"path": "a.py", "limit": 30}, "L1\nL2")   # 小页
            + _pair("c2", "read_file", {"path": "a.py"}, "L1\nL2")              # 默认整页
            + _pair("c3", "bash", {"command": "findstr /n x a.py"}, "1:x")      # bash 读文件
            + _pair("c4", "bash", {"command": "exit 7"}, "(no output, exit code 7)")  # 工具失败
            + _pair("c5", "write_file", {"path": "b.py"}, "Permission denied by user")  # 不算失败
        )
        st = rp.tool_stats(_archive(root, msgs))
        assert st["calls"] == 5
        assert st["small_pages"] == 1          # 只有 limit=30 那次
        assert st["read_via_bash"] == 1
        assert st["failures"] == 1             # 只有 exit code 7；权限拒绝不算
        assert st["failure_rate"] == 0.2
        assert st["tools"] == {"read_file": 2, "bash": 2, "write_file": 1}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_tool_stats_ignores_permission_denials():
    """权限拒绝是权限层的决定，不是工具失败 —— 混进去会把工具质量算低。"""
    root = Path(tempfile.mkdtemp(prefix="cr-toolstats-"))
    try:
        msgs = (_pair("c1", "read_file", {"path": "x"}, "Permission denied: 'C:\\x' is outside")
                + _pair("c2", "bash", {"command": "dir"}, "Blocked: 命令含高危操作 'sudo'"))
        st = rp.tool_stats(_archive(root, msgs))
        assert st["failures"] == 0 and st["failure_rate"] == 0.0
    finally:
        shutil.rmtree(root, ignore_errors=True)
