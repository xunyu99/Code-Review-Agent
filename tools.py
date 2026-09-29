"""
tools.py — 工具注册表 + 实现。对齐 s13 原版（guard 围栏 + workdir 相对路径）。
V0 只留 5 个基础工具；todo/task/compact/load_skill 等 V1 回归。
改：run_read 增加 offset（原版只有 limit，读不到文件中间段）；BASH_TIMEOUT 进 config。
改：权限判定全部搬去 permissions.py（唯一出处）。这里不写任何策略，只用 guard() 兜底。
"""
import codecs
import locale
import re
import subprocess
import tempfile
from config import WORKDIR, BASH_TIMEOUT
from permissions import DENY_LIST, guard
from system_prompt import SKILL_REGISTRY
from memory import list_memory_files, MEMORY_DIR
from pathlib import Path

# ── 工具定义（给模型看；路径一律相对 workdir）──
TOOLS = [
    {"type": "function", "function": {
        "name": "bash",
        "description": "Run a shell command and return its output. On this Windows box it runs cmd.exe, "
                       "NOT bash — use dir / findstr / type, not ls / find / grep / cat. "
                       "Don't pipe to `more`: its output is lost when not attached to a console. "
                       "Standard input is closed, so anything that reads it (find / sort / more with no "
                       "file argument) gets EOF instead of waiting for you — `find /c /n \"str\"` counts "
                       "lines from that empty input and silently returns 0. To count the lines of a file "
                       "use `find /c /v \"\" <file>`. "
                       "Empty output comes back as `(no output, exit code N)`, and a non-zero exit code "
                       "is appended to whatever the command printed — so a failure never looks like a success.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The shell command to run."}
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read the contents of a file. Returns up to `limit` lines starting at `offset` if specified. "
                       "Every line is prefixed with its line number (`    12\\t…`) — quote those numbers when "
                       "citing code, and do NOT paste the prefix into edit_file's old_text/new_text "
                       "(it is stripped anyway, but cleaner without). "
                       "Paths outside the working directory are read-only and need user approval; "
                       "paths outside the allowed directories are refused.",
        "parameters": {"type": "object", "properties": {
            "path":   {"type": "string", "description": "Path to the file, relative to workdir."},
            "offset": {"type": "integer", "description": "Start line (1-based)."},
            "limit":  {"type": "integer", "description": "Maximum number of lines to read."}
        }, "required": ["path"]},
    }},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create a file or overwrite an existing file with content. "
                       "You may write inside the working directory or your temp dir; "
                       "writing anywhere else is refused.",
        "parameters": {"type": "object", "properties": {
            "path":    {"type": "string", "description": "Path to the file, relative to workdir."},
            "content": {"type": "string", "description": "The full content to write."}
        }, "required": ["path", "content"]},
    }},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace old_text with new_text in a file (only the first occurrence). "
                       "You may edit files inside the working directory or your temp dir; "
                       "editing anywhere else is refused.",
        "parameters": {"type": "object", "properties": {
            "path":     {"type": "string", "description": "Path to the file, relative to workdir."},
            # 要查找的准确文本。
            "old_text": {"type": "string", "description": "The exact text to find."},
            "new_text": {"type": "string", "description": "The text to replace it with."}
        }, "required": ["path", "old_text", "new_text"]},
    }},
    {"type": "function", "function": {
        "name": "glob",
        # *.py 仅当前目录下，后缀为.py的文件，不进入子文件夹
        # **/*.py **：递归通配符，代表任意层级子目录，当前目录 + 所有递归子目录里全部 .py文件
        # 查找匹配指定模式的文件（例如 '*.py'、'**/*.py'）。
        "description": "Find files matching a pattern (e.g. '*.py', '**/*.py').",
        "parameters": {"type": "object", "properties": {
            "pattern": {"type": "string", "description": "Glob pattern to match."}
        }, "required": ["pattern"]},
    }},
    {"type": "function", "function": {
        "name": "load_skill",
        "description": "Load the full content of a skill by name. Use when you need the detailed instructions behind a skill listed in the system prompt.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill name, e.g. 'code-review'."}
        }, "required": ["name"]},
    }},
        {"type": "function", "function": {
        "name": "load_memory",
        "description": "Load the full content of a memory by name. Use when you need the details behind a memory listed in the system prompt.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Memory name, e.g. 'user-prefers-tabs'."}
        }, "required": ["name"]},
    }},
    
]

# ── 编码止损：Windows 中文环境默认不是 UTF-8 ──
def _decode_output(raw: bytes) -> str:
    """子进程输出解码：UTF-8 → 系统默认编码（中文 Windows 是 GBK/cp936）→ 兜底替换。

    只写 errors="replace" 不够：GBK 的中文按 UTF-8 硬解会全变成 �，
    命令明明跑成功，模型却只能读到一堆问号，等于瞎。
    """
    for enc in ("utf-8", locale.getpreferredencoding(False), "gbk"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def _read_text(p) -> tuple[str, str | None]:
    """读文本文件，返回 (内容, 检测到的编码)。

    被审查的仓库里 GBK 文件很常见：写死 utf-8 会直接 UnicodeDecodeError，
    模型只拿到一句 Error —— 那个文件它永远读不到。宁可给它一份能读的内容。
    返回编码是给 edit_file 用的：改完按原编码写回，别把 GBK 文件悄悄变成 UTF-8。
    编码是 None 表示"两种都不是/有替换字符"，这时写回去会损坏原文件。
    """
    raw = p.read_bytes()
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("gbk"), "gbk"
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), None


def _kill_tree(proc) -> None:
    """超时清理：连子孙一起杀，别留下孤儿进程。

    `proc.kill()` 只杀 cmd.exe，孙进程（find.exe / python 之类）会活下来继续跑。Windows 上
    用 taskkill /T 杀整棵树；其它平台退回 kill()。
    """
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, stdin=subprocess.DEVNULL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_bash(command: str) -> str:
    """真正执行 shell 命令，把输出返回给模型。

    返回里**一定带退出码**，这是有意的：只回一句 "(no output)" 的话，
    「跑成功了但没打印」和「命令失败了、什么都没输出」长得一模一样。
    模型分不清，就只能一遍遍换写法重试 —— 第一次真跑就是这么烧掉 12 万 token 的
    （22 次调用里 18 次是 bash，其中一大半是在试探环境）。

    Windows 上 shell=True 走的是 cmd.exe：工具名叫 bash，但 ls / find / grep 都不成立，
    `| more` 还会把输出吞掉（脱离了控制台）。这条同时写进了工具描述，让模型一开始就知道。

    两个坑都在这里堵死（2026-09-27 run-6 真事：整轮静默卡死 5 分 40 秒，用户只能 Ctrl+C）：
      1. stdin 必须是 DEVNULL。worker 的 stdin 继承的是用户的终端，而 `find /c /n "x"`
         这种没给文件参数的命令会去读 stdin —— 于是它等着人敲键盘，永远不返回。
      2. 超时要把**整棵进程树**杀掉。subprocess.run 超时只杀 cmd.exe，孙进程（find.exe）
         还攥着 stdout 管道，它内部的 communicate() 就会一直等下去 —— 超时形同虚设
         （实测：BASH_TIMEOUT=1 的命令真等了 10 秒；find 那种情况就是永远）。
    """
    MAX_OUTPUT = 30000   # ≈7500 token  源头截断：防止几十 MB 输出撑爆上下文
    lower = command.lower()
    for bad in DENY_LIST:
        if bad in lower:
            return f"Blocked: 命令含高危操作 '{bad}'，已拦截不执行。"
    try:
        # 输出走临时文件，不走管道。管道有个致命处：超时杀了 cmd.exe 之后，孙进程还攥着
        # 写端，我们这边读管道就会一直等它 —— 而它可能永远不结束（find 等 stdin 就是这样）。
        # 文件句柄握在我们自己手里，进程死没死都不影响读取。
        with tempfile.TemporaryFile() as sink:
            proc = subprocess.Popen(
                command, shell=True, cwd=WORKDIR,   # cwd 固定 workdir，换目录启动也安全
                stdin=subprocess.DEVNULL,           # 见上面第 1 条：别让它等人敲键盘
                stdout=sink, stderr=subprocess.STDOUT,
            )
            try:
                proc.wait(timeout=BASH_TIMEOUT)     # 等的是进程本身，与管道无关
                code = proc.returncode
            except subprocess.TimeoutExpired:
                _kill_tree(proc)                    # 杀不掉也别恋战：句柄在我们手里，不会再卡
                return (f"Error: 命令超过 {BASH_TIMEOUT}s 没返回，已终止。"
                        f"换一个更快的做法，或者分几步来。")
            sink.seek(0)
            raw = sink.read(MAX_OUTPUT * 2 + 8)     # 多留几个字节，别把多字节字符劈成两半
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"

    output = _decode_output(raw).strip()
    if len(output) > MAX_OUTPUT:
        output = output[:MAX_OUTPUT] + f"\n...(截断：只保留前 {MAX_OUTPUT} 字符，需要完整内容时用 read_file 读)"
    if not output:
        return f"(no output, exit code {code})"
    if code != 0:
        return f"{output}\n(exit code {code})"
    return output


def run_read(path, offset=1, limit=1000):
    """分页读：默认从第 1 行读 1000 行；offset 翻页。每行前面带 `行号<TAB>`。

    **为什么要带行号**：报告要求给 `文件:行号`，而以前返回的内容没有行号，模型只能
    自己数 —— 实测数出来的行号能偏 2~60 行（同一份报告里前后还不一致，因为它有时
    绕道用 `findstr /n`）。带上之后它能直接抄，不用猜。

    行号前缀只是**参照**，不是文件内容。edit_file 会把误抄进去的前缀自动剥掉。
    """
    try:
        text, _enc = _read_text(guard(path))
        lines = text.splitlines()
        start = max(1, offset)
        chunk = lines[start - 1: start - 1 + limit]
        if not chunk:
            return "(no more lines)"
        out = "\n".join(f"{start + i:>6}\t{line}" for i, line in enumerate(chunk))
        if start + limit - 1 < len(lines):
            out += f"\n... (还有 {len(lines) - (start + limit - 1)} 行未读，用 offset={start + limit} 继续)"
        return out
    except Exception as e:
        return f"Error: {e}"



def run_write(path, content):
    """写入文件内容。"""
    try:
        p = guard(path, "write")
        p.parent.mkdir(parents=True, exist_ok=True)
        # newline=""：原样落盘，别把模型给的 \n 翻译成 \r\n（Windows 默认会翻）
        p.write_text(content, encoding="utf-8", newline="")
        return f"Wrote {len(content)} chars to {path}"
    except Exception as e:
        return f"Error: {e}"


# read_file 的每行形如 `    12\t内容`。模型有时候会把行号前缀一起抄进 old_text /
# new_text：抄进 old_text 就永远匹配不上（报"未找到"，白跑一轮），抄进 new_text
# 就会把行号**写进文件**（静默改坏）。所以这里统一剥掉。
_LINE_NO_PREFIX = re.compile(r"^\s*\d+\t", re.MULTILINE)


def _strip_line_numbers(text):
    return _LINE_NO_PREFIX.sub("", text) if isinstance(text, str) else text


def run_edit(path, old_text, new_text):
    try:
        old_text = _strip_line_numbers(old_text)
        new_text = _strip_line_numbers(new_text)
        p = guard(path, "write")
        text, enc = _read_text(p)
        if enc is None:      # 不是 UTF-8/GBK 文本（可能混了二进制）→ 写回去会损坏它
            return f"Error: {path} 不是 UTF-8/GBK 文本，拒绝编辑以免写坏它。"
        # 看old_text出现了几次
        count = text.count(old_text)
        if count == 0:
            return f"Error: '{old_text}' 未找到。old_text 必须与文件内容完全一致（含缩进空格）。"
        # 按原编码写回：GBK 文件改完还是 GBK，别顺手把它变成 UTF-8；
        # newline="" 保住原来的换行符，别把 CRLF 变成 CRCRLF
        p.write_text(text.replace(old_text, new_text, 1), encoding=enc, newline="")
        msg = f"Edited {path} successfully"
        if count > 1:
            msg += f" (note: '{old_text}' 出现 {count} 次，只替换了第一次)"
        return msg
    except Exception as e:
        return f"Error: {e}"



# glob 的两道闸：忽略名单 + 条数上限。
# 真事（被审项目有 96 个 py 文件）：`glob("**/*.py")`
# 返回了 **17,952 行 / 1,185,797 字符，其中 99.5% 是 `.venv\Lib\site-packages` 里的**。
# 这一条调用把上下文从 69,534 token 直接推到需要压缩（L3 落盘 1.2 MB、上下文只留 2000 字符预览）。
# 工具层里只有它没有上限：run_bash 截 30k 字符、run_read 有 limit —— glob 什么都漏。
GLOB_IGNORE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".mypy_cache", ".pytest_cache", ".pytest_tmp",
    ".cache", ".tox", ".next", ".venv", "venv", "rag_venv", "env", "node_modules",
    "site-packages", "dist-packages", "dist", "build", "target",
    "storage", "models", "checkpoints", "data", "datasets", "volumes",
}
GLOB_MAX_RESULTS = 200


def run_glob(pattern):
    """查找匹配文件（相对 WORKDIR；软链穿透到外部的一律不列出）。

    权限上它不需要裁决 —— 结果永远被 resolve() 过滤在工作目录内。但 pattern 里的
    ".." 会真的让 pathlib 越界遍历（实测 `..\\..\\**\\*.py` 能把整个盘扫一遍，
    结果虽然被过滤掉，遍历本身照样发生），所以这里直接拒：
    结构上就不给它出去的机会，比事后过滤便宜。

    另外两道闸见 GLOB_IGNORE_DIRS / GLOB_MAX_RESULTS —— 它们防的不是越界，是**噪音**：
    一个带 .venv 的项目，`**/*.py` 能吐回 1.2 MB 的 site-packages 路径。
    """
    if ".." in Path(pattern).parts:
        return "Error: pattern must stay inside the workdir ('..' is not allowed)"
    try:
        matches = []
        for p in WORKDIR.glob(pattern):
            rp = p.resolve()
            if not rp.is_relative_to(WORKDIR) or not rp.exists():
                continue
            if GLOB_IGNORE_DIRS.intersection(p.parts):      # 依赖 / 数据目录不进结果
                continue
            if len(matches) >= GLOB_MAX_RESULTS:            # 到上限就停，别把上下文撑爆
                matches.append(f"...(已截断：只列前 {GLOB_MAX_RESULTS} 条，"
                               f"换个更具体的 pattern 再查)")
                break
            # 统一用 POSIX 分隔符输出：模型拿到的路径要么直接回传（read_file 两种都认），
            # 要么抄进报告。Windows 的 `app\a.py` 混进 Markdown 报告里既难读、又难比对。
            matches.append(p.relative_to(WORKDIR).as_posix())
        return "\n".join(matches) if matches else "(no matches)"
    except Exception as e:
        return f"Error: {e}"

def run_load_skill(name: str) -> str:
    """按名字加载技能的完整内容。只查注册表，不走文件路径（防路径遍历）。"""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        return f"Skill not found: {name}"
    return skill["content"]

def run_load_memory(name: str) -> str:
    """按名字加载记忆的完整内容。只查 .memory 目录元信息，不走路径拼接（防路径遍历）。"""
    for f in list_memory_files():
        if f["name"] == name:
            return (MEMORY_DIR / f["filename"]).read_text(encoding="utf-8")
    return f"Memory not found: {name}"


TOOL_HANDLERS = {
    "bash":      run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob":      run_glob,
    "load_skill": run_load_skill,
    "load_memory": run_load_memory,
}
