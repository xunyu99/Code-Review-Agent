"""
tools.py — 工具注册表 + 实现。对齐 s13 原版（guard 围栏 + workdir 相对路径）。
V0 只留 5 个基础工具；todo/task/compact/load_skill 等 V1 回归。
改：run_read 增加 offset（原版只有 limit，读不到文件中间段）；BASH_TIMEOUT 进 config。
改：权限判定全部搬去 permissions.py（唯一出处）。这里不写任何策略，只用 guard() 兜底。
"""
import codecs
import locale
import subprocess
from config import WORKDIR, BASH_TIMEOUT
from permissions import DENY_LIST, guard
from system_prompt import SKILL_REGISTRY
from memory import list_memory_files, MEMORY_DIR
from pathlib import Path

# ── 工具定义（给模型看；路径一律相对 workdir）──
TOOLS = [
    {"type": "function", "function": {
        "name": "bash",
        "description": "Run a shell command and return its stdout/stderr.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "The shell command to run."}
        }, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read the contents of a file. Returns up to `limit` lines starting at `offset` if specified. "
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


def run_bash(command: str) -> str:
    """真正执行 shell 命令，把输出返回给模型。"""
    MAX_OUTPUT = 30000   # ≈7500 token  源头截断：防止几十 MB 输出撑爆上下文
    lower = command.lower()
    for bad in DENY_LIST:
        if bad in lower:
            return f"Blocked: 命令含高危操作 '{bad}'，已拦截不执行。"
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True,
            timeout=BASH_TIMEOUT, cwd=WORKDIR,   # cwd 固定 workdir，换目录启动也安全
        )
        output = _decode_output(result.stdout + result.stderr).strip()
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + f"\n...(截断：共 {len(output)} 字符，需要完整内容时用 read_file 读)"
        return output if output else "(no output)"
    except Exception as e:
        return f"Error: {e}"


def run_read(path, offset=1, limit=1000):
    """分页读：默认从第 1 行读 1000 行；offset 翻页。"""
    try:
        text, _enc = _read_text(guard(path))
        lines = text.splitlines()
        start = max(1, offset)
        chunk = lines[start - 1: start - 1 + limit]
        if not chunk:
            return "(no more lines)"
        out = "\n".join(chunk)
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


def run_edit(path, old_text, new_text):
    try:
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



def run_glob(pattern):
    """查找匹配文件（相对 WORKDIR；软链穿透到外部的一律不列出）。

    权限上它不需要裁决 —— 结果永远被 resolve() 过滤在工作目录内。但 pattern 里的
    ".." 会真的让 pathlib 越界遍历（实测 `..\\..\\**\\*.py` 能把整个盘扫一遍，
    结果虽然被过滤掉，遍历本身照样发生），所以这里直接拒：
    结构上就不给它出去的机会，比事后过滤便宜。
    """
    if ".." in Path(pattern).parts:
        return "Error: pattern must stay inside the workdir ('..' is not allowed)"
    try:
        matches = []
        for p in WORKDIR.glob(pattern):
            rp = p.resolve()
            if rp.is_relative_to(WORKDIR) and rp.exists():
                matches.append(str(p.relative_to(WORKDIR)))
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
