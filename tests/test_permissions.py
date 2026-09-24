"""权限层测试：裁决矩阵、bash 翻译、未知工具兜底。

这一层最值得测 —— 它是「模型能碰到什么」的唯一出口，而且全是纯函数、
不用联网、表驱动就能覆盖。
"""
import _bootstrap  # noqa: F401  （必须先 import：定好 WORKDIR / key）
import permissions

from _bootstrap import AGENT_DIR, REPO, assert_raises

TMP = str(permissions.TMP_DIR)


def verdict(path, mode="read"):
    return permissions.classify_path(path, mode)[0]


# ── 路径裁决矩阵 ──

def test_repo_is_read_free_write_ask():
    assert verdict("main.py") == "allow"
    assert verdict("sub/dir/file.py") == "allow"
    assert verdict("main.py", "write") == "ask"


def test_tmp_is_the_only_free_write():
    assert verdict(TMP + r"\scratch.py", "write") == "allow"
    assert verdict(TMP + r"\scratch.py") == "allow"


def test_readonly_dirs_readable_but_not_writable():
    for root in (permissions.SKILLS_DIR, permissions.OUTPUT_DIR, permissions.MEMORY_DIR):
        assert verdict(str(root / "x.md")) == "allow"
        assert verdict(str(root / "x.md"), "write") == "deny"


def test_sessions_and_transcripts_are_denied_both_ways():
    for root in (permissions.SESSIONS_DIR, permissions.TRANSCRIPTS_DIR):
        assert verdict(str(root / "x.json")) == "deny"
        assert verdict(str(root / "x.json"), "write") == "deny"


def test_agent_home_is_denied_even_though_it_may_sit_inside_workdir():
    # 家的子目录各自有规则，剩下的（源码 / .env）必须整树禁掉
    assert verdict(str(AGENT_DIR / "agent.py")) == "deny"
    assert verdict(str(AGENT_DIR / ".env")) == "deny"
    assert verdict(str(AGENT_DIR / "agent.py"), "write") == "deny"


def test_outside_the_whitelist_is_denied():
    assert verdict(r"C:\Windows\win.ini") == "deny"
    assert verdict(r"..\..\..\Windows\win.ini") == "deny"
    assert verdict(r"\\server\share\file.txt") == "deny"
    assert verdict(r"E:\learn_claude_code\v4_plan.md") == "deny"


def test_policy_order_beats_nesting():
    # .tmp 在家的里面，但排在前面 → 必须先命中 allow（顺序错了这条会红）
    assert permissions.TMP_DIR.is_relative_to(permissions.PROJECT_DIR)
    assert verdict(TMP + r"\a.py") == "allow"


def test_guard_raises_outside_and_allows_inside():
    assert permissions.guard("main.py") == REPO / "main.py"
    assert_raises(PermissionError, permissions.guard, str(permissions.SESSIONS_DIR / "x.json"))
    assert_raises(PermissionError, permissions.guard, r"C:\Windows\win.ini")


# ── 工具级裁决 ──

def test_file_tools_verdicts():
    def v(tool, **args):
        return permissions.decide(tool, args)[0]

    assert v("read_file", path="main.py") == "allow"
    assert v("write_file", path="main.py", content="x") == "ask"
    assert v("edit_file", path="main.py", old_text="a", new_text="b") == "ask"
    assert v("read_file", path=str(permissions.SKILLS_DIR / "s.md")) == "allow"
    assert v("write_file", path=str(permissions.SKILLS_DIR / "s.md"), content="x") == "deny"
    assert v("read_file", path=str(permissions.SESSIONS_DIR / "s.json")) == "deny"
    assert v("read_file", path=str(permissions.MEMORY_DIR / "MEMORY.md")) == "allow"


def test_unknown_tool_is_denied():
    # 忘了登记的新工具是「用不了」，不是「免检」
    for name in ("delete_file", "http_fetch", "run_python", ""):
        assert permissions.decide(name, {"path": "x"})[0] == "deny"


def test_pathless_tools_are_allowed():
    for name in ("glob", "load_skill", "load_memory"):
        assert permissions.decide(name, {"pattern": "*", "name": "n"})[0] == "allow"


# ── bash 命令翻译 ──

def bash_verdict(cmd):
    return permissions.decide("bash", {"command": cmd})[0]


def test_bash_reads():
    assert bash_verdict("dir") == "allow"
    assert bash_verdict("git diff") == "allow"
    assert bash_verdict("python -m pytest tests/") == "allow"
    assert bash_verdict("cmd /c dir /s /b") == "allow"          # /c 不是路径
    assert bash_verdict("type " + str(permissions.SKILLS_DIR / "s.md")) == "allow"
    assert bash_verdict("Get-Content " + str(permissions.MEMORY_DIR / "MEMORY.md")) == "allow"


def test_bash_writes_inside_repo_ask():
    # 和 write_file 同判：同一个动作换个工具不能得出两个结果
    assert bash_verdict("echo hi > notes.txt") == "ask"
    assert bash_verdict("Set-Content notes.txt hi") == "ask"
    assert bash_verdict("Remove-Item code.py -Force") == "ask"
    assert bash_verdict("git diff > out.txt") == "ask"


def test_bash_writes_outside_repo_denied():
    assert bash_verdict("Set-Content " + str(permissions.SKILLS_DIR / "x.md") + " hi") == "deny"
    assert bash_verdict(r"echo x > E:\learn_claude_code\v4_plan.md") == "deny"
    assert bash_verdict(r"Set-Content $env:TEMP\x.txt hi") == "deny"


def test_bash_tmp_is_free():
    assert bash_verdict("Remove-Item " + TMP + r"\probe.py -Force") == "allow"
    assert bash_verdict("Set-Content " + TMP + r"\a.py hi") == "allow"
    assert bash_verdict("python -m pytest tests/ > " + TMP + r"\log.txt") == "allow"


def test_bash_outside_reads():
    assert bash_verdict(r"type %TEMP%\secret.txt") == "ask"     # 解析不出的位置：问
    assert bash_verdict(r"type E:\learn_claude_code\v4_plan.md") == "deny"
    assert bash_verdict(r"type ..\..\..\Windows\win.ini") == "deny"


def test_bash_denylist_wins():
    assert bash_verdict("sudo rm -rf /") == "deny"
    assert bash_verdict("shutdown /s") == "deny"


def test_scope_lines_matches_policy():
    text = permissions.scope_lines()
    assert "your temp dir" in text
    assert "skills" in text and "tool results" in text and "memory" in text
    assert "sessions" in text and "transcripts" in text
    assert "Anything else: refused" in text
