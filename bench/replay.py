"""bench/replay.py — 离线重放 archives/*.jsonl。不调 API、不读真 key、不改任何东西。

这一层回答"报告是不是真的"，不回答"报告好不好"——后者要 LLM-judge 和人工打标。

    python bench/replay.py citations                  # 全部会话
    python bench/replay.py citations --detail         # 逐条列出每条引用
    python bench/replay.py citations --session <id>   # 只看一个会话

第 1 项 · 报告可验证性（事实层，三档）：
    真引用     文件存在 ∧ 行号在范围内 ∧ archives 能证明读过那一行
    幻觉引用   文件不存在，或行号越界          ← 硬判据，没有假阳性
    无法判定   文件与行号都对，但读过的证据不足  ← 单独计数，不许并进前两档

覆盖范围由 read_file 的**返回内容**推，不用请求里的 offset/limit：
  有续读提示（"还有 N 行未读"）→ 读满了 limit 行 → [start, start+limit-1]
  没有续读提示                 → 读到文件尾   → [start, 当前文件行数]
（用"请求区间"会高估覆盖：那是请求，不是结果。）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = AGENT_DIR / "archives"

# 输出固定成 UTF-8：控制台默认走 cp936，重定向到文件 / CI 日志里就会变成乱码，
# 而这张表的用途之一就是贴进 README。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

# ── 引用识别 ────────────────────────────────────────────────────────────────
# 支持的写法：`code.py:1355` / code.py:1355-1370 / E:\a\b\code.py:12 / code.py 第 12 行
_EXT = (r"py|pyi|js|jsx|ts|tsx|java|go|rs|c|h|cpp|hpp|cs|rb|php|sh|ps1|bat"
        r"|sql|md|json|ya?ml|toml|ini|cfg|txt|env")
_FILE = rf"(?:[A-Za-z]:)?[^\s`\"'*|()\[\]{{}}<>,，。；、]*?\.(?:{_EXT})"
CITE_RE = re.compile(
    rf"(?P<file>{_FILE})\s*(?:"
    rf"[:：]\s*(?P<l1>\d+)\s*(?:[-–~]\s*(?P<l2>\d+))?"
    rf"|[:：]?\s*第\s*(?P<l3>\d+)\s*行"
    rf")",
    re.IGNORECASE,
)

_READ_TRAILER = re.compile(r"还有\s*(\d+)\s*行未读")
_NO_MORE = "(no more lines)"
# 问题条目 = **顶格**的列表项。两个坑都踩过了：
#   ① 只认 `- **[严重]**` 一种写法 → 换个格式就数出 0 条，看着像"没发现问题"；
#   ② 连缩进的子项也数 → 每条问题下面的「依据：」「建议：」都被算成独立问题，
#      11 条真问题会被数成 41 条。
# 所以：不限定严重度的写法，但必须顶格。
_FINDING = re.compile(r"^[-*]\s+\S", re.MULTILINE)

# 报告在给出行号时，常顺手抄一段代码（`` `BASE = "..."` ``）。抄来的这段可以拿来
# **回查那一行**——对不上就说明行号漂了。这是机械层能做的最便宜的语义检查：
# 光验「行存在 + 读过」挡不住行号偏移（真事：caption 那份报告 12/12 全绿，
# 手工一抽四条全对不上，偏移 2~20 行）。
_QUOTED = re.compile(r"`([^`\n]{6,90})`")


def _looks_like_code(s: str) -> bool:
    """挑出像代码的引文，排除掉纯中文解释和文件路径。"""
    if re.search(r"[\u4e00-\u9fff]", s):          # 有中文 → 是解释不是代码
        return False
    if re.fullmatch(r"[\w./\\-]+\.\w{1,5}(:\d+(-\d+)?)?", s):   # 纯路径/文件名
        return False
    return bool(re.search(r"[=(){}\[\]\"']|->|::|\s", s))


# 从引文里抽出"有辨识度的标识符"用来定位。按标识符比而不是按整串比，
# 是因为报告抄的代码经常跨行或被压缩过（`subprocess.run(command, shell=True)`
# 在源码里其实分在 811/812 两行）——按整串比会误报。
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{5,}")


def quote_mismatches(report: str, cits: list[dict], workdir: str) -> list[dict]:
    """逐条引用回查：报告抄的那段代码，到底在不在于它指的那一行。

    对不上就去找它在第几行，直接报偏移量——比只说"有问题"有用得多。
    """
    out = []
    for line in report.splitlines():
        found = [c for c in cits if c["raw"] in line]
        # 只查「一条 bullet 只引一处」的：一条里引了好几处时，没法知道哪段引文
        # 配哪个行号，硬配就会误报（真踩过：`code.py:22` 那条同时引了 :22 和 :1180，
        # 结果拿后半句的引文去对 :22，报出假的偏移）。宁可少查，不许说错。
        if len(found) != 1:
            continue
        quotes = [q for q in _QUOTED.findall(line) if _looks_like_code(q)]
        quotes = [q.strip("`").strip() for q in quotes]
        quotes = [q for q in quotes if _abs(q, workdir) not in line]   # 别拿引用本身当引文
        if not quotes:
            continue                                   # 没抄代码，没法回查
        cit = found[0]
        path = Path(_abs(cit["file"], workdir))
        text = _read_text(path) if path.exists() else None
        if text is None:
            continue
        src = text.splitlines()
        toks = sorted({t for q in quotes for t in _IDENT.findall(q)}, key=len, reverse=True)
        if not toks:
            continue                                   # 引文里没有能定位的标识符
        cited = cit["line"]
        if cited <= len(src) and any(t in src[cited - 1] for t in toks):
            continue                                   # 指的那一行里就有这些标识符 → 没问题
        near = [i for i, ln in enumerate(src, 1)
                if abs(i - cited) <= 80 and any(t in ln for t in toks)]
        if near:
            best = min(near, key=lambda i: abs(i - cited))
            out.append({**cit, "why": f"引文其实在第 {best} 行（偏 {best - cited:+d}）"})
        else:
            out.append({**cit, "why": "引文在这个文件里找不到"})
    return out


# ── 读存档 ────────────────────────────────────────────────────────────────

def iter_sessions(session: str | None, root: Path | None = None) -> list[Path]:
    base = root or ARCHIVE_DIR
    if not base.exists():
        return []
    paths = sorted(base.rglob("*.jsonl"))
    if session:
        paths = [p for p in paths if p.stem == session]
    return paths


def load_records(path: Path) -> tuple[dict, list[dict]]:
    """返回 (meta, messages)。半截 / 坏行直接跳过，别让一行坏数据毁掉整次重放。"""
    meta, messages = {}, []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") == "meta":
            meta = rec
        elif rec.get("type") == "msg" and isinstance(rec.get("message"), dict):
            messages.append(rec["message"])
    return meta, messages


# ── 文件与覆盖 ────────────────────────────────────────────────────────────

def _read_text(p: Path) -> str | None:
    for enc in ("utf-8-sig", "gbk"):
        try:
            return p.read_text(encoding=enc)
        except (UnicodeDecodeError, LookupError, OSError):
            continue
    return None


def _abs(path: str, workdir: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = Path(workdir) / p
    try:
        p = p.resolve()
    except OSError:
        pass
    s = str(p)
    # Windows 上大小写不敏感：CODE.PY / code.py / ./code.py 必须是同一个键
    return s.lower().replace("/", "\\") if os.name == "nt" else s


def read_coverage(messages: list[dict], workdir: str) -> dict[str, list[tuple[int, int]]]:
    """从 read_file 的调用 + 它返回的内容，推出每个文件被读过的行区间（并集）。"""
    calls: dict[str, tuple[str, str]] = {}          # tool_call_id -> (name, raw_args)
    cov: dict[str, list[tuple[int, int]]] = {}
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                calls[tc.get("id") or ""] = (fn.get("name") or "", fn.get("arguments") or "{}")
            continue
        if role != "tool":
            continue
        name, raw = calls.get(m.get("tool_call_id") or "", ("", "{}"))
        if name != "read_file":
            continue
        try:
            args = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(args, dict) or not args.get("path"):
            continue
        out = m.get("content") or ""
        if out.startswith("Error") or out.strip() == _NO_MORE:
            continue                                # 没读到内容 = 没有覆盖
        start = max(1, int(args.get("offset", 1) or 1))
        limit = int(args.get("limit", 1000) or 1000)
        if _READ_TRAILER.search(out):
            end = start + limit - 1                 # 读满了一页，精确，且不依赖当前文件
        else:
            text = _read_text(Path(_abs(args["path"], workdir)))
            end = len(text.splitlines()) if text else start
        cov.setdefault(_abs(args["path"], workdir), []).append((start, max(start, end)))
    return cov


def read_stats(messages: list[dict], workdir: str) -> dict:
    """读文件这件事的效率：读了多少行、其中多少行是**新的**。

    这是第 3 项里抓「逐层深挖型打转」的口径。只看"完全相同的调用重复几次"会漏掉它：
    有一轮 23 次调用**一次都没重复**，却把同一个文件用 20~60 行的小窗口翻了十几遍——
    参数各不相同，所以重复检测是 0%，但它跟原地打转是一回事。
    换成"重复读取率"就抓得住：累计读的行数远远多于真正覆盖到的行数。
    """
    calls: dict[str, tuple[str, str]] = {}
    read_calls, lines_read = 0, 0
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                calls[tc.get("id") or ""] = (fn.get("name") or "", fn.get("arguments") or "{}")
            continue
        if m.get("role") != "tool":
            continue
        name, raw = calls.get(m.get("tool_call_id") or "", ("", "{}"))
        if name != "read_file":
            continue
        try:
            args = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(args, dict):
            continue
        out = m.get("content") or ""
        if out.startswith("Error") or out.strip() == _NO_MORE:
            continue
        # 按**实际返回的行数**算，不按请求里的 limit：文件只有 166 行却请求 1000 行时，
        # 那 834 行是"本来就没有"，不是"重复读"。按 limit 算会把正常的一次读全文
        # 记成 83% 的重复率。
        n = len(out.splitlines())
        if _READ_TRAILER.search(out):
            n -= 1                      # 末尾那行是续读提示，不算内容
        read_calls += 1
        lines_read += max(0, n)

    cov = read_coverage(messages, workdir)
    covered = sum(b - a + 1 for intervals in cov.values() for a, b in _merge(intervals))
    rate = (1 - covered / lines_read) if lines_read else 0.0
    return {"read_calls": read_calls, "lines_read": lines_read,
            "lines_covered": covered, "reread_rate": round(max(0.0, rate), 3)}


_REPEAT_HINT = "[重复调用]"

# 第 2 项：工具选得对不对。这几条是能机械判的"错位"信号。
# `type file` / `findstr /n … file` 是拿 bash 干 read_file 的活；
# 权限层本来就要求"同一个动作换个工具得出同一裁决"，所以这是它自己立的标准。
_READ_VIA_BASH = re.compile(r"(^|[\\/&|]\s*)(type|findstr|more|get-content|gc)\b",
                            re.IGNORECASE)
_NONZERO_EXIT = re.compile(r"\bexit code [1-9]\d*\)")
_SMALL_PAGE = 100          # read_file 默认整页 1000 行；远小于它 = 手搓小页


def tool_stats(path: Path, workdir_override: str | None = None) -> dict:
    """第 2 项的机械口径：合法性（有没有报错）、选择偏差（用错工具）、参数质量（小页读）。"""
    meta, messages = load_records(path)
    calls: dict[str, tuple[str, str]] = {}
    hist: dict[str, int] = {}
    read_via_bash = small_pages = 0
    results = failures = 0

    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                name = fn.get("name") or "?"
                raw = fn.get("arguments") or "{}"
                calls[tc.get("id") or ""] = (name, raw)
                hist[name] = hist.get(name, 0) + 1
                try:
                    args = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(args, dict):
                    continue
                if name == "bash" and _READ_VIA_BASH.search(str(args.get("command") or "")):
                    read_via_bash += 1
                elif name == "read_file":
                    if int(args.get("limit", 1000) or 1000) < _SMALL_PAGE:
                        small_pages += 1
            continue
        if m.get("role") != "tool":
            continue
        results += 1
        text = (m.get("content") or "").lstrip()
        # 口径：只有"工具自己失败"才算。权限拒绝不算——那是权限层的决定，另有一列。
        if (text.startswith(("Error", "Unknown tool", "Invalid"))
                or bool(_NONZERO_EXIT.search(text[:400]))):
            failures += 1

    total = sum(hist.values())
    return {
        "session": meta.get("id") or path.stem,
        "calls": total,
        "tools": hist,
        "failures": failures,
        "failure_rate": round(failures / results, 3) if results else 0.0,
        "read_via_bash": read_via_bash,
        "small_pages": small_pages,
        "top_tool": max(hist.items(), key=lambda kv: kv[1])[0] if hist else "-",
    }


def analyze_run(path: Path, workdir_override: str | None = None) -> dict:
    """第 3、4 项的离线口径 + 成本，全部只看存档。"""
    meta, messages = load_records(path)
    workdir = resolve_workdir(path, meta, workdir_override)

    steps, calls = 0, []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            steps += 1
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                calls.append((fn.get("name") or "?", fn.get("arguments") or ""))
    distinct = len(set(calls))

    hints = sum(1 for m in messages
                if m.get("role") == "tool" and _REPEAT_HINT in (m.get("content") or ""))

    denied_no_human = denied_by_policy = 0
    end_reason = "ok"
    for m in messages:
        if m.get("role") != "tool":
            continue
        text = (m.get("content") or "").lstrip()
        if text.startswith("Permission denied by user"):
            denied_no_human += 1
        elif text.startswith(("Permission denied", "Blocked", "Invalid path")):
            denied_by_policy += 1

    report = final_report(messages)
    for m in reversed(messages):
        if m.get("role") == "assistant" and (m.get("content") or "").startswith("[Error]"):
            end_reason = (m["content"] or "")[:120]
            break

    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    models: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "usage":
            continue
        usage["calls"] += 1
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            usage[k] += int((rec.get("usage") or {}).get(k) or 0)
        if rec.get("model"):
            models.append(str(rec["model"]))

    return {
        "session": meta.get("id") or path.stem,
        "steps": steps,
        "calls": len(calls),
        "distinct_calls": distinct,
        "duplicate_rate": round(1 - distinct / len(calls), 3) if calls else 0.0,
        "repeat_hints": hints,
        "steps_hit_cap": end_reason.startswith("[Error] max steps"),
        "end_reason": end_reason,
        "report_chars": len(report),
        "denied_no_human": denied_no_human,
        "denied_by_policy": denied_by_policy,
        "usage": usage,
        "model_reported": models[-1] if models else None,
        "archive": str(path),
        **read_stats(messages, workdir),
    }


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


# ── 抽引用 + 判档 ─────────────────────────────────────────────────────────

def final_report(messages: list[dict]) -> str:
    """最终报告 = 最后一条不带 tool_calls 的 assistant 消息。"""
    for m in reversed(messages):
        if m.get("role") != "assistant" or m.get("tool_calls"):
            continue
        text = m.get("content") or ""
        # 收尾的 [Error]（撞上限 / 崩了）是「这轮作废」，不是报告正文
        if text.strip() and not text.startswith("[Error]"):
            return text
    return ""


def sibling_record(path: Path) -> dict | None:
    """在存档的上层找这次运行的 record.json（评测跑的布局：run-N/home/archives/…）。"""
    for up in path.parents[:7]:
        p = up / "record.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
    return None


def resolve_workdir(path: Path, meta: dict, override: str | None) -> str:
    """定这次核查拿哪份代码当基准。

    评测跑记的是**临时副本**的路径，跑完可能已经删了。副本不在时退回到 record.json 里的
    `repo_src`（被审原件）——模型没改过副本的话，两边是同一份字节，行号对得上。
    """
    if override:
        return override
    wd = meta.get("workdir") or ""
    if wd and Path(wd).exists():
        return wd
    src = (sibling_record(path) or {}).get("repo_src")
    if src and Path(src).exists():
        return src
    return wd


def extract_citations(text: str) -> list[dict]:
    seen: dict[tuple[str, int], dict] = {}
    for mo in CITE_RE.finditer(text):
        line = int(mo.group("l1") or mo.group("l3"))
        end = int(mo.group("l2")) if mo.group("l2") else None
        key = (mo.group("file"), line)
        if key in seen:
            seen[key]["hits"] += 1
            continue
        seen[key] = {"file": mo.group("file"), "line": line, "end": end,
                     "raw": mo.group(0).strip(), "hits": 1}
    return list(seen.values())


def judge(cit: dict, cov: dict[str, list[tuple[int, int]]], workdir: str) -> tuple[str, str]:
    key = _abs(cit["file"], workdir)
    p = Path(key)
    if not p.exists():
        return "hallucination", "文件不存在"
    if not p.is_file():
        return "hallucination", "不是文件"
    text = _read_text(p)
    if text is None:
        return "unknown", "文件读不出来（编码 / 权限）"
    total = len(text.splitlines())
    lines = [n for n in (cit["line"], cit["end"]) if n is not None]
    for n in lines:
        if n < 1 or n > total:
            return "hallucination", f"行号 {n} 越界（文件共 {total} 行）"
    intervals = _merge(cov.get(key, []))
    for n in lines:
        if not any(a <= n <= b for a, b in intervals):
            return "unknown", "行号在范围内，但 archives 里没有读到该行的 read_file 记录"
    return "real", ""


def check_session(path: Path, detail: bool, workdir_override: str | None = None) -> dict:
    meta, messages = load_records(path)
    sid = meta.get("id") or path.stem
    workdir = resolve_workdir(path, meta, workdir_override)
    report = final_report(messages)
    cits = extract_citations(report)
    cov = read_coverage(messages, workdir)
    buckets = {"real": [], "hallucination": [], "unknown": []}
    for c in cits:
        verdict, why = judge(c, cov, workdir)
        buckets[verdict].append({**c, "why": why})
    # 单独一档：行号落在范围内、也读过，但报告自己抄的那段代码对不上那一行
    drifted = quote_mismatches(report, cits, workdir)

    bullets = [ln for ln in report.splitlines() if _FINDING.match(ln)]
    cited = sum(1 for ln in bullets if CITE_RE.search(ln))
    print(f"会话 {sid} · 被审目录 {workdir}")
    print(f"  报告 {len(report)} 字符 · 顶格列表项 {len(bullets)} 条（其中带行号 {cited} 条）"
          f" · 抽出引用 {len(cits)} 条")
    for name, label in (("real", "真引用"), ("hallucination", "幻觉引用"), ("unknown", "无法判定")):
        n = len(buckets[name])
        pct = (n / len(cits) * 100) if cits else 0.0
        mark = "  " if name == "real" else "!!"
        print(f"{mark}{label:<6} {n:>3}   {pct:>5.1f}%")
    for name in ("hallucination", "unknown"):
        for c in buckets[name]:
            print(f"     · {c['raw']}  → {c['why']}")
    if drifted:
        print(f"!!行号存疑 {len(drifted)}   （引文对不上它指的那一行；机械层抓不到、"
              f"但语义层一定抓得到）")
        for c in drifted:
            print(f"     · {c['raw']}  → {c['why']}")
    if detail:
        print("  逐条：")
        for name in ("real", "hallucination", "unknown"):
            for c in buckets[name]:
                print(f"     [{name:<13}] {c['raw']}")
    return {"session": sid, "total": len(cits), "findings": len(bullets), "cited_findings": cited,
            "drifted": len(drifted),
            **{k: len(v) for k, v in buckets.items()}}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="离线重放 archives/*.jsonl")
    ap.add_argument("item", choices=["citations", "steps", "tools"],
                    help="第 1 项 citations（引用核查）／第 2 项 tools（工具质量）"
                         "／第 3~5 项 steps（轨迹与成本）")
    ap.add_argument("--session", help="只看一个会话 id")
    ap.add_argument("--detail", action="store_true", help="逐条列出每条引用")
    ap.add_argument("--from", dest="archive_root",
                    help="存档根目录（默认项目自己的 archives/）。"
                         "给 bench/runs/<label>/run-N/home/archives 就能核查那次评测跑的产物")
    ap.add_argument("--workdir", help="覆盖存档里记的被审目录（评测跑的副本删了之后，指回原件）")
    args = ap.parse_args(argv)

    paths = iter_sessions(args.session,
                          Path(args.archive_root) if args.archive_root else None)
    if not paths:
        print(f"没有找到存档：{args.archive_root or ARCHIVE_DIR}"
              + (f"（session={args.session}）" if args.session else ""))
        return 1

    if args.item == "tools":
        header = (f"{'会话':<22}{'调用':>6}{'最多用的':>12}{'失败':>6}{'失败率':>8}"
                  f"{'bash读文件':>11}{'小页读':>8}")
        print(header)
        print("-" * len(header))
        for p in paths:
            r = tool_stats(p, args.workdir)
            print(f"{r['session']:<22}{r['calls']:>6}{r['top_tool']:>12}"
                  f"{r['failures']:>6}{r['failure_rate']:>8.2f}"
                  f"{r['read_via_bash']:>11}{r['small_pages']:>8}")
        print("\n口径：失败 = 工具自己报错（权限拒绝不算，那是权限层的决定）。")
        print("bash读文件 = 用 type/findstr 干 read_file 的活。**这是信号不是判错**：")
        print("            `findstr /n` 会带行号，而 read_file 不带 —— 要引行号时它反而更合适。")
        print(f"小页读 = read_file 的 limit 小于 {_SMALL_PAGE} 行（默认整页是 1000）。")
        return 0

    if args.item == "steps":
        header = (f"{'会话':<22}{'步':>4}{'调用':>6}{'不同':>6}{'重复率':>8}"
                  f"{'重读率':>8}{'提醒':>6}{'越界':>6}{'无人':>6}"
                  f"{'报告':>7}{'输入tok':>10}  结束")
        print(header)
        print("-" * len(header))
        for p in paths:
            r = analyze_run(p, args.workdir)
            print(f"{r['session']:<22}{r['steps']:>4}{r['calls']:>6}{r['distinct_calls']:>6}"
                  f"{r['duplicate_rate']:>8.2f}{r['reread_rate']:>8.2f}{r['repeat_hints']:>6}"
                  f"{r['denied_by_policy']:>6}{r['denied_no_human']:>6}"
                  f"{r['report_chars']:>7}{r['usage']['prompt_tokens']:>10}  "
                  f"{'撞上限' if r['steps_hit_cap'] else ('ok' if r['end_reason'] == 'ok' else '异常')}")
        print("\n口径：重复率 = 1 − 不同调用/总调用（抓原地重复）；"
              "重读率 = 1 − 覆盖行数/累计读行数（抓逐层深挖）。")
        print("报告 0 字符 = 那一轮没交出任何东西。")
        return 0

    totals = {"total": 0, "real": 0, "hallucination": 0, "unknown": 0}
    per = []
    for i, p in enumerate(paths):
        if i:
            print()
        per.append(check_session(p, args.detail, args.workdir))
        for k in totals:
            totals[k] += per[-1][k]

    if len(paths) > 1:
        print(f"\n合计 {len(paths)} 个会话：引用 {totals['total']} 条 · "
              f"真 {totals['real']} · 幻觉 {totals['hallucination']} · 无法判定 {totals['unknown']}")
    print("\n口径：三档 = 真引用 / 幻觉 / 无法判定。")
    print("顶格列表项 = 报告里顶格的列表项，问题 / 建议 / 亮点三节都算在内，不等于问题条数。")
    print("基准是**当前磁盘上的文件**——被审仓库在运行之后改过的话，行号可能对不上。")
    print("第 2 层（语义支撑，抽样上 judge）与第 3 层（judge-人工一致率）要人工参与，尚未实现。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
