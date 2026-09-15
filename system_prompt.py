"""
system_prompt.py — 技能两级加载 + system prompt 组装 + 缓存。
从 s13 原版搬运（_parse_frontmatter / _scan_skills / list_skills / get_system_prompt 缓存 / update_context）。
改：identity 删掉 todo_write/load_skill 指令（V0 没有这两个工具，留着模型会幻觉调用不存在的工具）；
    update_context 只刷技能（记忆 V1 回归）。
"""
import json
from config import WORKDIR, TMP_DIR, SKILLS_DIR, MEMORY_DIR
from permissions import scope_lines
from ui import console

# ── 技能注册表（两级加载的第一级：只存 name + description，正文不常驻上下文）──
SKILL_REGISTRY: dict[str, dict] = {}

def _parse_frontmatter(raw: str):
    # raw是skill.md文件的所有内容
    """解析 SKILL.md 开头的 YAML frontmatter，返回 (元信息字典, 正文)。"""
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":   # 第一行不是 ---，说明没有 frontmatter
        return {}, raw
    meta = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        if ":" in lines[i]:
            key, value = lines[i].split(":", 1)
            meta[key.strip()] = value.strip()
        i += 1
    body = "\n".join(lines[i + 1:])
    return meta, body


# _scan_skills() 先扫描 skills/ 目录，
# 把每个技能的目录信息（_parse_frontmatter解析出的元信息和正文）装进注册表（SKILL_REGISTRY）。

def _scan_skills():
    """启动时扫描 skills/ 目录，把每个技能的目录信息装进注册表。"""
    if not SKILLS_DIR.exists():
        return
    # iterdir() 拿出该文件夹下所有直接子项的 Path 对象（文件、文件夹、软链接全都包含）
    for d in sorted(SKILLS_DIR.iterdir()):
      # 判断是不是文件夹
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if manifest.exists():
            raw = manifest.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(raw)
            name = meta.get("name", d.name)
            desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
            SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": body}

_scan_skills()  # 启动时只跑一次

def list_skills() -> str:
    """把注册表整理成目录字符串（只含名字+描述），注入 SYSTEM prompt。"""
    return "\n".join(
        f"- **{s['name']}**: {s['description']}"
        for s in SKILL_REGISTRY.values()
    )

PROMPT_SECTIONS = {
    "identity": (
        "You are a code-review agent running on Windows. "
        "The working directory is the repository under review: it is the user's project, not your home, "
        "and not your own source code. "
        "Permissions: you read freely in the working directory, but writing there needs the user's approval "
        "— a review means reading, and edits happen only when the user asked for them. "
        "Your own skills / tool-results / memory directories are read-only: your code writes them, you do not. "
        "Your temp dir is the only unrestricted place: read and write there, no approval needed. "
        "Everything else — including your own home directory, sessions and transcripts — is refused outright. "
        "If a path is refused, do not reach it another way — no shell workaround, no relative-path trick, "
        "no re-encoding: report the refusal, or ask the user for a different route. "
        "Use load_skill / load_memory to read your own skills and memories; they need no approval. "
        "Use the most specific tool for the job. "
        "Read files in chunks (read_file with offset/limit) instead of loading whole files at once. "
        "Prefer small, verifiable steps over one giant action."
    ),
    # 路径说明直接从 permissions.POLICY 生成，提示词和裁决表同源，改一处就够了
    "workspace": f"Working directory: {WORKDIR}\nPath rules:\n{scope_lines()}",
    "output_style": (
        "Response rules:\n"
        "- Answer in Simplified Chinese. Keep code, commands, paths and identifiers verbatim.\n"
        "- While calling tools, do not narrate what you are about to do; just call the tool.\n"
        "- When all tools are done, output exactly one final answer: no recap of tool calls, no pleasantries.\n"
        "- Exactly one blank line between paragraphs; never two or more.\n"
        "- A code review report uses this structure, with these exact headings, in this order: "
        "总体结论 → 问题清单 (sorted 严重 / 重要 / 次要) → 修改建议.\n"
        "- Each issue is one Markdown list item containing: 位置 / 问题 / 依据 / 建议.\n"
        "- Mark code, commands and file names as inline code. Severity labels must be exactly "
        "严重 / 重要 / 次要; do not add emoji."
    ),
        "temp_files": (
        "Temporary files:\n"
        f"- If you need a scratch script, intermediate output, or a temp file, write it under {TMP_DIR} "
        "using its ABSOLUTE path. That dir is the only place you may write without asking; "
        "do not leave scratch files behind, and do not edit the repository unless the user asked.\n"
        "- That directory is your own workspace: delete your own files when you are done, "
        "no permission needed. Only delete files you created yourself.\n"
        "- Do not run recursive deletes on directories; remove only the exact files you made.\n"
        "- Prefer running the project's code with PYTHONDONTWRITEBYTECODE=1 so you do not litter its __pycache__."
    ),
}

def assemble_system_prompt(context: dict) -> str:
    """按需拼接：始终加载的 + 根据真实状态加载的。"""
    sections = [
        PROMPT_SECTIONS["identity"],
        PROMPT_SECTIONS["workspace"],
        PROMPT_SECTIONS["temp_files"],
        PROMPT_SECTIONS["output_style"],
    ]
    if context.get("skills"):
        sections.append(f"Skills available:\n{context['skills']}")
    if context.get("memories"):          # V1 记忆回归后自动生效
        sections.append(f"Relevant memories:\n{context['memories']}")
    return "\n\n".join(sections)

_last_context_key = None
_last_prompt = None

def get_system_prompt(context: dict) -> str:
    """context 没变就返回缓存，变了才重新拼接。"""
    global _last_context_key, _last_prompt
    # 将上下文字典序列化，固定字典顺序、保留中文、Path / 日期这类对象自动转字符串，
    # 生成稳定字符串，一般拿来做缓存的 key。
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        console.print("[dim][cache hit][/]")
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)
    console.print(f"[dim]assembled: {', '.join(context.keys())}[/]")
    return _last_prompt

def update_context() -> dict:
    """根据真实运行状态刷新 context（V0 只有技能；记忆 V1 回归）。"""
    result = {}
    if list_skills():
        result["skills"] = list_skills()
    # {"skills":"所有skill的name和description"}
    index_file = MEMORY_DIR / "MEMORY.md"
    if index_file.exists():
        memories = index_file.read_text(encoding="utf-8").strip()
        result["memories"] = memories
    # {"memories":"所有记忆的name和description和filename"}
    return result
