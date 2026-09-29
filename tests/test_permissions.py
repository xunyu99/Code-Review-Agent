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
    assert verdict(r"D:\elsewhere\notes.md") == "deny"


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


# ── 密钥文件：读、写、以及 bash 的各种写法，一律 deny ──

def test_secret_files_are_denied_in_the_workdir():
    """工作目录里的密钥文件也是 deny。

    真事（2026-09-27，CLI 模式审一个真实项目）：CLI 不拷副本，WORKDIR 就是真目录，
    而权限表放行「读工作目录内的文件」—— 那个项目 .env 里的 4 个 API key 当时是可读的。
    读进去会进 archives，并随下一次请求发给模型服务商。
    """
    for name in (".env", ".env.local", ".env.production", "prod.env",
                 "id_rsa", "id_ed25519", "server.pem", "client.key", "secrets.json"):
        assert verdict(name) == "deny", name
        assert verdict(name, "write") == "deny", name
    assert verdict("config/app.py") == "allow"                    # 正常文件不受影响


def test_example_env_files_stay_readable():
    """示例文件要放行 —— 审查本来就要看键名，把 `.env.example` 也拒了等于挡住正常工作。"""
    for name in (".env.example", ".env.sample", ".env.template"):
        assert verdict(name) == "allow", name


def test_bash_cannot_read_secrets():
    """bash 侧的漏点：`.env` 既没有路径分隔符也没有扩展名，`_bash_paths` 的启发式抓不到它。

    实测过：补这一条之前，`type .env` 和 `findstr /s needle .env` 都是 allow ——
    而这正是模型读密钥最常用的两种写法。
    """
    for cmd in ("type .env", "type *.env", "more id_rsa",
                "findstr /s needle .env", "Get-Content .env", "copy .env backup.txt"):
        assert bash_verdict(cmd) == "deny", cmd
    assert bash_verdict("type app\\main.py") == "allow"           # 正常读不受影响
    assert bash_verdict("type .env.example") == "allow"


def test_one_definition_of_secret_for_both_paths():
    """评测路径（拷贝仓库时过滤）和权限层用的是**同一份判定**，不许各写一份。"""
    for name in (".env", ".env.example", "id_rsa", "server.pem", "app.py"):
        assert (permissions.is_secret_filename(name)
                == (verdict(name) == "deny")), name


def test_model_is_told_about_secret_files():
    """提示词里必须提到这条，否则模型会当成普通越界、换个写法继续试，白烧步数。"""
    text = permissions.scope_lines()
    assert "Secret files" in text and ".env" in text


def test_bash_writes_inside_repo_ask():
    # 和 write_file 同判：同一个动作换个工具不能得出两个结果
    assert bash_verdict("echo hi > notes.txt") == "ask"
    assert bash_verdict("Set-Content notes.txt hi") == "ask"
    assert bash_verdict("Remove-Item code.py -Force") == "ask"
    assert bash_verdict("git diff > out.txt") == "ask"


def test_bash_writes_outside_repo_denied():
    assert bash_verdict("Set-Content " + str(permissions.SKILLS_DIR / "x.md") + " hi") == "deny"
    assert bash_verdict(r"echo x > D:\elsewhere\notes.md") == "deny"
    assert bash_verdict(r"Set-Content $env:TEMP\x.txt hi") == "deny"


def test_bash_tmp_is_free():
    assert bash_verdict("Remove-Item " + TMP + r"\probe.py -Force") == "allow"
    assert bash_verdict("Set-Content " + TMP + r"\a.py hi") == "allow"
    assert bash_verdict("python -m pytest tests/ > " + TMP + r"\log.txt") == "allow"


def test_bash_outside_reads():
    assert bash_verdict(r"type %TEMP%\secret.txt") == "ask"     # 解析不出的位置：问
    assert bash_verdict(r"type D:\elsewhere\notes.md") == "deny"
    assert bash_verdict(r"type ..\..\..\Windows\win.ini") == "deny"


def test_bash_denylist_wins():
    assert bash_verdict("sudo rm -rf /") == "deny"
    assert bash_verdict("shutdown /s") == "deny"


def test_findstr_pattern_is_not_treated_as_a_path():
    """真跑栽在这一条上：findstr 的正则被当成路径 → 整条勘察命令被误拒。

    `\.git\\` 以 `\` 开头，在 Windows 上是「根相对」，resolve 之后变成 `<当前盘>:\.git`，
    落到白名单之外。实测过：修之前 verdict=deny、理由里写着 'C:\\.git'；修之后 allow。
    """
    cmd = f'cd /d "{permissions.WORKDIR}" && dir /s /b | findstr /v "\\.git\\\\" | findstr /v "node_modules"'
    verdict, _title, detail = permissions.decide("bash", {"command": cmd})
    assert verdict == "allow", detail
    assert ".git" not in detail              # 正则不该出现在「被拒路径」里


def test_pattern_skip_does_not_open_a_hole():
    """修上面那条不能顺带把真正的根相对路径放进来。"""
    assert bash_verdict(r"type \Windows\win.ini") == "deny"
    assert bash_verdict(r"findstr /s needle ..\..\secret.txt") == "deny"   # 真实路径参数照样管


def test_scope_lines_matches_policy():
    text = permissions.scope_lines()
    assert "your temp dir" in text
    assert "skills" in text and "tool results" in text and "memory" in text
    assert "sessions" in text and "transcripts" in text
    assert "Anything else: refused" in text


def test_code_dir_is_denied_before_workdir():
    """代码目录（源码 / .env / 权限表本身）必须在 POLICY 里整棵禁掉，且排在 WORKDIR 之前。

    AGENT_HOME 把家搬走之后，代码目录就落到 (WORKDIR, allow, ask) 那条规则的范围里了 ——
    少了这条，workdir 指到代码目录的父目录时，`.env` 和权限表本身都是可读的。
    """
    code = [i for i, (root, r, w, _n) in enumerate(permissions.POLICY)
            if root == permissions.CODE_DIR and r == "deny" and w == "deny"]
    work = [i for i, (root, *_rest) in enumerate(permissions.POLICY)
            if root == permissions.WORKDIR]
    assert code, "POLICY 里缺代码目录的 deny 条目"
    assert work, "POLICY 里缺 WORKDIR 条目"
    assert code[0] < work[0], "代码目录的 deny 必须排在 WORKDIR 之前"


def test_code_dir_deny_beats_the_workdir_rule():
    """把 WORKDIR 换成代码目录的父目录：`.env` 仍必须是 deny。

    这条直接复现 AGENT_HOME 引入的风险场景，防止以后有人「顺手」把那条规则删掉。
    """
    saved = (permissions.POLICY, permissions.WORKDIR)
    try:
        parent = permissions.CODE_DIR.parent
        permissions.POLICY = tuple(
            (parent if root == permissions.WORKDIR else root, r, w, n)
            for root, r, w, n in permissions.POLICY)
        permissions.WORKDIR = parent
        env = str(permissions.CODE_DIR / ".env")
        assert permissions.classify_path(env, "read")[0] == "deny"
        assert permissions.classify_path(env, "write")[0] == "deny"
    finally:
        permissions.POLICY, permissions.WORKDIR = saved
