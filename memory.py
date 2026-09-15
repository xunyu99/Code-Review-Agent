"""
记忆管理模块
"""
import json,re
from config import MEMORY_DIR, API_KEY, BASE_URL, MODEL
from openai import OpenAI
from ui import info

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

def list_memory_files():
    """列出所有记忆文件的元信息（name, description, filename）。"""
    entries = []
    if not MEMORY_DIR.exists():
        return entries
    for path in sorted(MEMORY_DIR.glob("*.md")):
        if path.name == "MEMORY.md": #索引文件
            continue
        raw = path.read_text(encoding="utf-8")
        meta, _ = _parse_frontmatter(raw)   # 复用 s07 的解析函数
        entries.append({
            "name": meta.get("name", path.stem),
            "description": meta.get("description", ""),
            "filename": path.name,
        })
    return entries

def _rebuild_index():
    """扫描 .memory/，重写 MEMORY.md 索引（name 与 filename 显式区分）。"""
    lines = [
        f"- **{m['name']}** (file: {m['filename']}): {m['description']}"
        for m in list_memory_files()
    ]
    (MEMORY_DIR / "MEMORY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_memory_file(name, mem_type, description, body):
    """把一个记忆写成 .md 文件，并重建索引。"""
    slug = name.lower().replace(" ", "-")   # "User Prefers Tabs" -> "user-prefers-tabs"
    filepath = MEMORY_DIR / f"{slug}.md"
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    _rebuild_index()
    return filepath


def extract_memories(messages):
    """对话结束后，从最近 10 条消息里提取新记忆。"""
    dialogue = json.dumps([m for m in messages[-10:] if m.get("role") != "system"], ensure_ascii=False)[:4000]
    existing = "\n".join(f"- {m['name']}: {m['description']}" for m in list_memory_files())
    prompt = (
        "You are a memory extractor for a code-review agent.\n"
        "From the dialogue, extract ONLY knowledge that helps FUTURE review tasks:\n"
        "1. user_preference — stable user preferences (review depth, report style, tool choices)\n"
        "2. code_style — user's coding conventions\n"
        "3. project_fact — durable facts about a project FAMILY (tech stack, conventions), not one-off paths\n"
        "4. review_finding — a generalizable review conclusion, e.g. 'this codebase never handles API timeouts'\n"
        "Return JSON array: [{name, type, description, body}]. Return [] if nothing durable.\n\n"
        "DO NOT record:\n"
        "- transient tool failures or errors (usually environment/config issues, not facts)\n"
        "- one-off file paths or locations of the current review target\n"
        "- workaround commands used to bypass a tool\n"
        "- anything already covered by existing memories\n\n"
        f"Existing memories:\n{existing}\n\nDialogue:\n{dialogue}"
    )

    try:
        response = client.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": prompt}],
            max_tokens=800, stream=False,
            extra_body={"thinking": {"type": "disabled"}},
        )
        text = response.choices[0].message.content or "[]"
        match = re.search(r"\[.*?\]", text, re.S)
        items = json.loads(match.group()) if match else []
        for item in items:
            if item.get("name") and item.get("description"):
                write_memory_file(item["name"], item.get("type", "user"),
                                  item["description"], item.get("body", item["description"]))
        info(f"Memory: extracted {len(items)} new memories")
    except Exception as e:
        info(f"Memory: extract failed: {e}")

# 合并阈值
CONSOLIDATE_THRESHOLD = 10

def consolidate_memories():
    """记忆文件超阈值时，让 LLM 合并去重，重写记忆库。"""
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD:
        return  # 太少，不值得整理
    contents = "\n\n".join(
        (MEMORY_DIR / f["filename"]).read_text(encoding="utf-8") for f in files
    )
    # "去重、合并矛盾信息，并删除过时条目。\n"
    # "返回记忆信息的JSON数组：[{名称, 类型, 描述, 主体内容}]。\n\n"
    # f"当前记忆：\n{contents}"
    prompt = (
        "Deduplicate, merge contradictions, and drop outdated entries.\n"
        "Return JSON array of memories: [{name, type, description, body}].\n\n"
        f"Current memories:\n{contents}"
    )
    try:
        response = client.chat.completions.create(
            model=MODEL, messages=[{"role": "user", "content": prompt}],
            max_tokens=3000, stream=False,
            extra_body={"thinking": {"type": "disabled"}},
        )
        match = re.search(r"\[.*?\]", response.choices[0].message.content or "", re.S)
        items = json.loads(match.group()) if match else []
        for f in files:
            # 只能删文件，不能删目录
            (MEMORY_DIR / f["filename"]).unlink()
        for item in items:
            if item.get("name"):
                write_memory_file(item["name"], item.get("type", "user"),
                                  item.get("description", ""), item.get("body", ""))
        info(f"Memory: consolidated to {len(items)} memories")
    except Exception as e:
        info(f"Memory: consolidate failed: {e}")

def _parse_frontmatter(raw: str):
    """解析 SKILL.md 开头的 YAML frontmatter，返回 (元信息字典, 正文)。"""
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":   # 第一行不是 ---，说明没有 frontmatter
        return {}, raw                            # 整篇当正文返回
    meta = {}
    i = 1
    # 从第 2 行开始读，一直读到第二个 "---" 为止
    while i < len(lines) and lines[i].strip() != "---":
        if ":" in lines[i]:
            key, value = lines[i].split(":", 1)   # 只在第一个冒号处切一刀
            meta[key.strip()] = value.strip()
        i += 1
    body = "\n".join(lines[i + 1:])   # 跳过第二个 ---，剩下的都是正文
    return meta, body