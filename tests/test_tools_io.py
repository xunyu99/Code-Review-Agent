"""工具层 I/O 测试：编码回退、换行符、分页、截断、glob 越界。

这一层是「模型的手」——编码猜错会让它读不到文件、换行翻译会改坏文件、
glob 的 `..` 会真的去扫整块盘，所以都值得钉住。
"""
import _bootstrap  # noqa: F401
from _bootstrap import make_bytes, make_file

import time

import config
import permissions
import tools

H = tools.TOOL_HANDLERS


# ── 编码 ──

def test_read_utf8_and_bom_and_gbk():
    make_file("a_utf8.txt", "中文 UTF-8\n第二行\n")
    make_bytes("a_bom.txt", b"\xef\xbb\xbf" + "带 BOM\n".encode("utf-8"))
    make_bytes("a_gbk.txt", "中文 GBK\n第二行\n".encode("gbk"))

    assert "中文 UTF-8" in H["read_file"]("a_utf8.txt")
    assert tools._read_text(permissions.resolve_path("a_utf8.txt"))[1] == "utf-8"
    assert tools._read_text(permissions.resolve_path("a_bom.txt"))[1] == "utf-8-sig"
    text, enc = tools._read_text(permissions.resolve_path("a_gbk.txt"))
    assert enc == "gbk" and "中文 GBK" in text


def test_read_binary_does_not_crash():
    make_bytes("blob.bin", bytes(range(256)) * 4)
    out = H["read_file"]("blob.bin")
    assert not out.startswith("Error")           # 读不了也要给内容，别只给一句 Error


def test_edit_keeps_original_encoding_and_newlines():
    p = make_bytes("gbk_crlf.txt", "第一行\r\n第二行\r\n".encode("gbk"))
    assert "Edited" in H["edit_file"]("gbk_crlf.txt", "第一行", "第一行改")
    raw = p.read_bytes()
    assert raw.decode("gbk") == "第一行改\r\n第二行\r\n"   # 原编码 + 原换行都没被动
    assert b"\r\r\n" not in raw                          # 没有多余 CR


def test_edit_refuses_binary():
    make_bytes("mixed.bin", b"\x80\xff" * 100)   # UTF-8 和 GBK 都解不出来的字节
    assert "拒绝编辑" in H["edit_file"]("mixed.bin", "a", "b")


def test_write_does_not_translate_newlines():
    p = make_file("w.txt", "")
    H["write_file"]("w.txt", "a\nb\n")
    assert p.read_bytes() == b"a\nb\n"


def test_decode_output_handles_gbk_and_utf8():
    assert tools._decode_output("中文\n".encode("utf-8")) == "中文\n"
    assert tools._decode_output("中文\n".encode("gbk")) == "中文\n"
    assert tools._decode_output(b"\xff\xfe\x00bad")          # 不抛异常就行


# ── 读/写/编辑 ──

def test_read_paging_and_no_more_lines():
    make_file("page.txt", "\n".join(f"line{i}" for i in range(1, 11)))
    out = H["read_file"]("page.txt", offset=1, limit=3)
    assert out.startswith("     1\tline1") and "line4" not in out and "还有" in out
    assert H["read_file"]("page.txt", offset=99) == "(no more lines)"


def test_read_file_numbers_lines_with_file_line_numbers():
    """行号必须是**文件里的真实行号**，不是页内序号 —— 翻页时最容易搞错。"""
    make_file("numbered.txt", "\n".join(f"L{i}" for i in range(1, 21)))
    out = H["read_file"]("numbered.txt", offset=11, limit=3)
    lines = out.splitlines()
    assert lines[0] == "    11\tL11"
    assert lines[2].endswith("\tL13")


def test_edit_strips_line_number_prefix():
    """模型把 read_file 的行号前缀一起抄进来时：既要照样改得动，也不能把行号写进文件。"""
    make_file("n2.py", "alpha = 1\nbeta = 2\n")
    assert "     2\tbeta = 2" in H["read_file"]("n2.py")
    msg = H["edit_file"]("n2.py", "     2\tbeta = 2", "     2\tbeta = 99")
    assert "successfully" in msg
    assert tools._strip_line_numbers(H["read_file"]("n2.py")) == "alpha = 1\nbeta = 99"


def test_edit_only_first_occurrence_and_missing_text():
    make_file("e.txt", "ab ab ab\n")
    msg = H["edit_file"]("e.txt", "ab", "XY")
    assert "successfully" in msg and "出现 3 次" in msg
    assert tools._strip_line_numbers(H["read_file"]("e.txt")).strip() == "XY ab ab"
    assert "未找到" in H["edit_file"]("e.txt", "不存在的内容", "x")


def test_write_creates_parent_dirs():
    H["write_file"]("deep/dir/new.txt", "hi")
    assert tools._strip_line_numbers(H["read_file"]("deep/dir/new.txt")) == "hi"


# ── 围栏（工具层兜底，不经过权限钩子也要拦）──

def test_tool_layer_fence_blocks_escape():
    assert H["read_file"](r"..\..\Windows\win.ini").startswith("Error")
    assert H["write_file"](str(permissions.SKILLS_DIR / "x.md"), "x").startswith("Error")
    assert H["read_file"](str(permissions.SESSIONS_DIR / "x.json")).startswith("Error")


# ── glob / bash ──

def test_glob_stays_inside_and_rejects_dotdot():
    make_file("g1.py", "")
    make_file("sub/g2.py", "")
    assert "g1.py" in H["glob"]("*.py")
    assert "sub/g2.py" in H["glob"]("**/*.py")
    assert "not allowed" in H["glob"](r"..\*.py")
    assert H["glob"](r"E:\**\*.py").startswith("Error")      # 绝对 pattern 直接被 pathlib 拒


def test_glob_does_not_flood_from_venv():
    """依赖目录不进结果。

    真事（2026-09-27，被审项目带 .venv）：`glob("**/*.py")` 返回 17,952 行 / 1.2 MB，
    99.5% 是 site-packages 里的路径 —— 一条调用就把上下文撑到需要压缩。
    """
    make_file("app/a.py", "")
    make_file(".venv/Lib/site-packages/pkg/__init__.py", "")
    make_file("node_modules/dep/index.js", "")
    make_file("storage/blob.jsonl", "")

    out = H["glob"]("**/*.py")
    assert "app/a.py" in out
    assert ".venv" not in out and "site-packages" not in out
    assert "node_modules" not in H["glob"]("**/*.js")
    assert "storage" not in out


def test_glob_caps_the_number_of_results():
    """任何 pattern 都不许一次性吐回几十万字符。"""
    saved = tools.GLOB_MAX_RESULTS
    tools.GLOB_MAX_RESULTS = 3
    try:
        for i in range(8):
            make_file(f"cap/f{i}.py", "")
        out = H["glob"]("cap/*.py")
    finally:
        tools.GLOB_MAX_RESULTS = saved

    lines = out.splitlines()
    assert len(lines) == 4                       # 3 条结果 + 1 行截断提示
    assert "已截断" in lines[-1]


def test_bash_empty_output_gets_sentinel_and_caps_size():
    # 空输出也必须带退出码：不带的话，「跑成功了没打印」和「跑失败了」分不出来
    assert H["bash"]("ver > nul") == "(no output, exit code 0)"
    big = H["bash"]('python -c "print(\'x\'*80000)"')
    assert len(big) < len("x") * 40000 and "截断" in big      # 30_000 上限 + 提示


def test_bash_reports_nonzero_exit_code():
    """命令失败必须自己说出来 —— 否则模型以为跑成功了，会一直换写法重试。"""
    # 失败了但有输出
    out = H["bash"]("dir /nosuchswitch")
    assert "exit code 1" in out
    # 失败了而且没有输出：这条最要命，旧实现只回一句 "(no output)"
    quiet = H["bash"]("exit 7")
    assert quiet == "(no output, exit code 7)"


def test_bash_success_does_not_add_noise():
    """成功（退出码 0）且有输出时，不多嘴加一行 —— 常见路径保持干净。"""
    out = H["bash"]("echo hello")
    assert "hello" in out and "exit code" not in out


def test_screen_mark_agrees_with_bash_result():
    """屏幕上那个 ✓/✗ 必须和命令真实成败一致 —— 界面不能撒谎。"""
    import ui
    assert ui._looks_failed(H["bash"]("exit 7"))              # 失败且无输出
    assert ui._looks_failed(H["bash"]("dir /nosuchswitch"))   # 失败但有输出
    assert not ui._looks_failed(H["bash"]("echo hello"))      # 成功
    assert not ui._looks_failed(H["bash"]("ver > nul"))       # 成功且无输出


def test_bash_denylist_blocks_execution():
    assert H["bash"]("sudo whoami").startswith("Blocked")


def test_bash_timeout_is_bounded():
    assert config.BASH_TIMEOUT > 0


def test_bash_timeout_says_so():
    """超时要明说是超时，不能退化成一句模糊的话 —— 模型才知道换更快的做法。"""
    saved = tools.BASH_TIMEOUT
    try:
        tools.BASH_TIMEOUT = 1
        started = time.time()
        out = tools.run_bash('python -c "import time; time.sleep(10)"')
        elapsed = time.time() - started
        assert "超过 1s" in out
        # 「说了超时」不等于「真的停下来了」。以前只杀 cmd.exe，孙进程还攥着管道，
        # 这条命令实际要等满 10 秒（实测 10.0s）—— 遇到永不退出的孙进程就是永久卡死。
        assert elapsed < 5, f"超时没真的生效：等了 {elapsed:.1f}s"
    finally:
        tools.BASH_TIMEOUT = saved


def test_bash_does_not_wait_for_stdin():
    """读 stdin 的命令必须立刻拿到 EOF，不能挂在那儿等人敲键盘。

    真事（2026-09-27 caption-v1 run-6）：模型跑 `find /c /n "build_au_caption_v4.py"` ——
    find 没给文件参数时会去读 **stdin**，而 bench worker 的 stdin 继承的是用户的终端，
    于是整轮任务静默卡死 5 分 40 秒，直到用户按 Ctrl+C。两个条件缺一不可：stdin 没关 +
    超时只杀 cmd.exe（孙进程攥着管道，communicate() 永远等下去）。

    这条用例在**交互式终端**里才有完全的鉴别力：CI 里 stdin 本来就不是终端，关不关都会
    立刻拿到 EOF。所以断言写成「有没有卡住」而不是「有没有传 DEVNULL」。
    """
    saved = tools.BASH_TIMEOUT
    tools.BASH_TIMEOUT = 5
    try:
        started = time.time()
        out = tools.run_bash('find /c /n "no_such_file_anywhere"')
        elapsed = time.time() - started
    finally:
        tools.BASH_TIMEOUT = saved

    assert elapsed < 5, f"命令在等 stdin：{elapsed:.1f}s 没返回"
    assert "超过" not in out            # 也不该退化成超时分支
