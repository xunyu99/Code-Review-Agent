"""工具层 I/O 测试：编码回退、换行符、分页、截断、glob 越界。

这一层是「模型的手」——编码猜错会让它读不到文件、换行翻译会改坏文件、
glob 的 `..` 会真的去扫整块盘，所以都值得钉住。
"""
import _bootstrap  # noqa: F401
from _bootstrap import make_bytes, make_file

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
    assert out.startswith("line1") and "line4" not in out and "还有" in out
    assert H["read_file"]("page.txt", offset=99) == "(no more lines)"


def test_edit_only_first_occurrence_and_missing_text():
    make_file("e.txt", "ab ab ab\n")
    msg = H["edit_file"]("e.txt", "ab", "XY")
    assert "successfully" in msg and "出现 3 次" in msg
    assert H["read_file"]("e.txt").strip() == "XY ab ab"
    assert "未找到" in H["edit_file"]("e.txt", "不存在的内容", "x")


def test_write_creates_parent_dirs():
    H["write_file"]("deep/dir/new.txt", "hi")
    assert H["read_file"]("deep/dir/new.txt") == "hi"


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


def test_bash_empty_output_gets_sentinel_and_caps_size():
    assert H["bash"]("ver > nul") == "(no output)"
    big = H["bash"]('python -c "print(\'x\'*80000)"')
    assert len(big) < len("x") * 40000 and "截断" in big      # 30_000 上限 + 提示


def test_bash_denylist_blocks_execution():
    assert H["bash"]("sudo whoami").startswith("Blocked")


def test_bash_timeout_is_bounded():
    assert config.BASH_TIMEOUT > 0
