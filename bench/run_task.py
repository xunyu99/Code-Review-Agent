"""bench/run_task.py — 跑一次审查任务，并把这次运行的一切记下来。

为什么要单独一个入口：`main.py` 是聊天界面（prompt_toolkit 要真控制台），脚本喂不进去。

为什么必须起子进程：`config` 在 import 那一刻就把 WORKDIR 和「家」的位置冻住了，
一个进程里改不掉。所以「一次运行 = 一个子进程」是硬约束，不是设计偏好。

每次运行做五件事：
  1. 把被审目录拷成一份临时副本（原件不碰）
  2. 建一个全新的空「家」（AGENT_HOME）—— 记忆 / 会话 / 存档都不会串到下一次
  3. 子进程里跑 agent_loop，对话落进那个家的 archives/
  4. 跑完比对副本指纹，报告模型有没有动被审代码
  5. 把这些写成 record.json，连同 archives 一起留在 bench/runs/<label>/run-<n>/

用法：
    python bench/run_task.py --label my-task --repo D:\\path\\to\\repo
    python bench/run_task.py --label my-task --repo ... --runs 3 --temperature 0
    python bench/run_task.py --label my-task --repo ... --verbose   # 把 agent 的实时输出也留下

真跑要 API key（放在代码目录的 .env 里）和网络。不吃 key 的自测在 tests/test_run_task.py。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
AGENT_DIR = BENCH_DIR.parent
RUNS_DIR = BENCH_DIR / "runs"

# 记录必须有固定形状：worker 崩了、没产出结果时，这几项也要在，
# 不然「这次花了钱但什么都没记下」正好发生在最需要看数据的时候。
RESULT_DEFAULTS = {
    "session_id": None, "steps": 0, "tool_calls": 0, "tools": {},
    "usage": {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    "model_reported": None, "report_chars": 0, "end_reason": "unknown", "archive": None,
}

# 拷贝和指纹都要跳过的东西。**这份清单是拿真实项目量出来的**，不是为了好看：
# 用户给的四个仓库合计 12 GB，其中 11.9 GB 是模型权重 / 虚拟环境 / 缓存 / 向量库，
# 真正的源码不到 4 MB。不跳的话，一次运行要拷几个 GB 再逐个算哈希 —— 直接跑不动。
IGNORE_DIRS = {
    # 版本库与缓存
    ".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".pytest_tmp",
    ".cache", ".eval_out", ".tmp", ".task_outputs", ".next", ".tox",
    # 依赖
    ".venv", "venv", "rag_venv", "node_modules", "site-packages", "dist", "build", "target",
    # 数据与模型（真实项目里最占地方的就是这些）
    "models", "storage", "volumes", "fastembed_cache", "checkpoints", "data", "datasets",
}
# 后缀清单：明显不是"被审代码"的二进制与日志。跳掉它们，汇报里会写清跳了多少。
IGNORE_SUFFIXES = (
    ".lock", ".log", ".sqlite3", ".db", ".bin", ".onnx", ".safetensors", ".pth", ".pt",
    ".h5", ".ckpt", ".zip", ".tar", ".gz", ".7z", ".pdf", ".mp4", ".mov", ".pyc",
    ".so", ".dll", ".pyd", ".exe", ".woff", ".woff2", ".ttf",
)
IGNORE_NAMES = {"package-lock.json", "poetry.lock", "uv.lock", "yarn.lock", "pnpm-lock.yaml"}

# 拷之前先量。超过就拒跑 —— 与其拷 6.9 GB 再让你后悔，不如现在说清楚。
MAX_COPY_BYTES = 50 * 1024 * 1024
MAX_COPY_FILES = 3000


def _ignored_dir(name: str) -> bool:
    return name in IGNORE_DIRS or name.startswith("uploads")


def _ignored_file(name: str) -> bool:
    return (name in IGNORE_NAMES or name.endswith(IGNORE_SUFFIXES)
            or _is_secret_file(name))


def _is_secret_file(name: str) -> bool:
    """密钥文件永不进被审副本。

    为什么这条单独写：权限表允许**读工作目录内的文件**（它只管路径，不管内容），
    所以 `.env` 一旦被拷进副本，agent 读它 → 密钥进 `archives/*.jsonl` →
    还会随请求发给模型服务商。示例文件（`.env.example` 等）不涉密，留着——
    审查本来就要看它。

    **判定复用 permissions 那份**（延迟导入：run_task 顶层不能碰 config，
    否则从项目根跑 `bench/score.py` 会因为 WORKDIR 没设而直接退出）。
    """
    from permissions import is_secret_filename
    return is_secret_filename(name)


def _copytree_ignore(dirpath, names):
    return {n for n in names if _ignored_dir(n) or _ignored_file(n)}

# 任务要**有界**。早先那版写的是"找出真实存在的缺陷"——没有收尾信号，而模型没有
# 步数预算的概念，"找出真实存在的"这种措辞只会让它一直查证下去（三次跑全是这么死的）。
# 对照：9-24 唯一一次跑通的用户指令是「审查一下这个项目，输出在100字内，不用太详细」，
# 明确告诉了它什么时候停、要多少。所以这里也把篇幅和范围写死。
DEFAULT_TASK = (
    "审查这个仓库，找出最重要的几个问题。按严重程度分级（严重 / 重要 / 次要），"
    "每条给出 文件:行号，结论控制在 500 字以内。"
)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass


# ══════════════════════ 被审代码的隔离 ══════════════════════

def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _ignored_dir(d)]
        for name in filenames:
            if _ignored_file(name):
                continue
            p = Path(dirpath) / name
            yield p, p.relative_to(root)


def survey_repo(root: Path) -> dict:
    """量一下"筛完之后要拷多少"。也顺手记下跳掉了多少，免得筛过头没人发现。"""
    kept = kept_bytes = skipped = skipped_bytes = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for d in list(dirnames):
            if _ignored_dir(d):
                dirnames.remove(d)
                # 注意：这里不能用 _iter_files —— 它会把"该忽略的文件"先滤掉，
                # 而我们要数的正是那些。用它算跳过量会少算一个数量级。
                n, size = _tree_size(Path(dirpath) / d)
                skipped += n
                skipped_bytes += size
        for name in filenames:
            p = Path(dirpath) / name
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if _ignored_file(name):
                skipped += 1
                skipped_bytes += size
            else:
                kept += 1
                kept_bytes += size
    return {"files": kept, "bytes": kept_bytes,
            "skipped_files": skipped, "skipped_bytes": skipped_bytes}


def _tree_size(root: Path) -> tuple[int, int]:
    """整棵树有多少个文件、多少字节 —— 不做任何过滤。"""
    n = size = 0
    for dirpath, _dirs, filenames in os.walk(root):
        for name in filenames:
            try:
                size += (Path(dirpath) / name).stat().st_size
                n += 1
            except OSError:
                pass
    return n, size


def tree_digest(root: Path) -> dict[str, str]:
    """{相对路径: 内容哈希}。用它判断模型有没有动被审代码 —— 不依赖 git。"""
    dig: dict[str, str] = {}
    for p, rel in _iter_files(root):
        try:
            dig[rel.as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
        except OSError:
            dig[rel.as_posix()] = "unreadable"
    return dig


def diff_digests(before: dict, after: dict) -> list[str]:
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def copy_repo(src: Path, dst: Path) -> None:
    """拷一份被审代码。用拷贝而不是 git worktree：不依赖 git，且能对任何目录用。"""
    shutil.copytree(src, dst, ignore=_copytree_ignore)


def git_rev(path: Path) -> str | None:
    """短 commit（工作区脏就带 +dirty）。git 不在或不是仓库就返回 None。"""
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=15)
        rev = (r.stdout or "").strip()
        if r.returncode != 0 or not rev:
            return None
        s = subprocess.run(["git", "-C", str(path), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=15)
        return rev + ("+dirty" if (s.stdout or "").strip() else "")
    except (OSError, subprocess.SubprocessError):
        return None


# ══════════════════════ 一次会话（子进程里跑的就是这个）══════════════════════

# 权限拒绝在对话里的落点：agent.py 把 permission_hook 的返回值当工具结果塞回去，
# 所以它长成一条以这些词开头的 tool 消息。两种拒绝要分开数，它们说的不是一回事：
#   by user   = 这一步该问人，但没人可问（无人值守）→ HITL 摩擦
#   by policy = 策略直接拒（越界路径 / 高危命令）  → 越界尝试
DENY_BY_USER = "Permission denied by user"
DENY_BY_POLICY = ("Permission denied", "Blocked", "Invalid path")


def _core_summary(session_id: str, messages: list, archive: Path | None) -> dict:
    steps, calls = 0, []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            steps += 1
            for tc in m["tool_calls"]:
                calls.append(((tc.get("function") or {}).get("name")) or "?")

    denied_no_human = denied_by_policy = 0
    for m in messages:
        if m.get("role") != "tool":
            continue
        text = (m.get("content") or "").lstrip()
        if text.startswith(DENY_BY_USER):        # 先判它：它同时以 "Permission denied" 开头
            denied_no_human += 1
        elif text.startswith(DENY_BY_POLICY):
            denied_by_policy += 1

    report, end_reason = "", "ok"
    for m in reversed(messages):
        text = m.get("content") or ""
        if m.get("role") != "assistant":
            continue
        if text.startswith("[Error]") and end_reason == "ok":
            end_reason = text[:200]
        # 收尾的 [Error] 是「这轮作废」，不是报告正文 —— 别把它当成审查结论记下来
        if (not report and not m.get("tool_calls")
                and text.strip() and not text.startswith("[Error]")):
            report = text

    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    models: list[str] = []
    if archive is not None and archive.exists():
        for line in archive.read_text(encoding="utf-8", errors="replace").splitlines():
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
        "session_id": session_id,
        "steps": steps,
        "tool_calls": len(calls),
        "tools": {n: calls.count(n) for n in sorted(set(calls))},
        "permission_denials": denied_no_human + denied_by_policy,
        "denied_no_human": denied_no_human,
        "denied_by_policy": denied_by_policy,
        "usage": usage,
        "model_reported": models[-1] if models else None,
        "report_chars": len(report),
        "report": report,
        "end_reason": end_reason,
        "archive": str(archive) if archive else None,
    }


def summarize(session_id: str, messages: list) -> dict:
    """从这次对话里数出步数、工具分布、token 用量、报告正文。"""
    import session as session_mod

    return _core_summary(session_id, messages, session_mod.log_path(session_id))


def summarize_archive(path: Path) -> dict:
    """只凭一个 archives 文件重建摘要 —— worker 崩了、没来得及写 result.json 时兜底。

    压缩碰不到存档，所以这里读到的就是原文，跟活着的时候看到的是同一份。
    """
    messages = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") == "msg" and isinstance(rec.get("message"), dict):
            messages.append(rec["message"])
    out = _core_summary(path.stem, messages, path)
    out["rebuilt_from_archive"] = True
    return out


def newest_archive(home: Path) -> Path | None:
    try:
        files = sorted((home / "archives").rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return files[-1] if files else None


def run_session(task: str, *, client=None, verbose: bool = False,
                with_memory: bool = False) -> dict:
    """在当前进程、当前环境（WORKDIR / AGENT_HOME 已由调用方设好）里跑一次。

    client 只给测试用：塞一个假客户端进来，整条链路就能在不联网、不花钱的情况下走通。
    with_memory 默认关：记忆提炼要多花一次 API 调用，而隔离之后它对本轮没有任何影响。
    关掉这件事会被记进 record.json —— 不然成本对比就成了两码事。
    """
    import agent
    import config
    import session as session_mod
    import system_prompt
    import ui
    from rich.console import Console
    from system_prompt import update_context

    if not verbose:
        # 界面写进内存：不刷屏，也不让 Live / spinner 干扰管道
        quiet = Console(file=io.StringIO(), theme=ui.THEME, width=100)
        ui.console = quiet
        # system_prompt 是 `from ui import console` 直接抓走的对象，得单独换一次，
        # 否则它那句 "assembled: ..." 还是会打到真实控制台上。
        system_prompt.console = quiet
    if client is not None:
        agent.client = client

    sess = session_mod.new_session(str(config.WORKDIR))
    sess["title"] = task[:50]
    messages, _log = session_mod.open_session(sess)
    messages.insert(0, {"role": "system", "content": ""})   # agent_loop 每轮重建它
    messages.append({"role": "user", "content": task})
    status = agent.agent_loop(messages, update_context())
    session_mod.save_session(sess)

    # 和 main.after_turn 同一条规矩：只有跑完的一轮才提炼。中断 / 报错 / 撞上限
    # 都没有结论，拿它们去提炼等于往 .memory/ 里灌没验证过的东西。
    if with_memory and agent.turn_completed(status):
        from memory import extract_memories
        extract_memories(messages)

    return summarize(sess["id"], messages)


# ══════════════════════ 驱动：准备环境 → 起子进程 → 收尾记账 ══════════════════════

def execute_run(*, index: int, label: str, repo_src: Path, task: str, out_root: Path,
                temperature: float | None, timeout: int, verbose: bool,
                keep_repo: bool = True, worker_cmd: list[str] | None = None,
                with_memory: bool = False,
                memory_src: Path | None = None) -> dict:
    run_dir = out_root / label / f"run-{index}"
    # 一次运行就是一笔钱。覆盖已有记录 = 把钱和它产生的数据一起扔掉，宁可报错也别静默覆盖。
    if (run_dir / "record.json").exists():
        raise FileExistsError(
            f"{run_dir} 已经有记录了。换个 run 号（driver 会自动找下一个空位）。")
    run_dir.mkdir(parents=True, exist_ok=True)

    # 先量再拷：真实项目动辄几个 GB（模型权重 / 虚拟环境 / 向量库），
    # 不拦的话这一步就把磁盘和时间吃光了。
    survey = survey_repo(repo_src)
    if survey["bytes"] > MAX_COPY_BYTES or survey["files"] > MAX_COPY_FILES:
        raise SystemExit(
            f"被审目录筛完之后还是太大，不跑：{survey['files']} 个文件 / "
            f"{survey['bytes'] / 1024 / 1024:.1f} MB（上限 {MAX_COPY_FILES} 个 / "
            f"{MAX_COPY_BYTES // 1024 // 1024} MB）。\n"
            f"  要么把 --repo 指到真正的代码子目录（例如 .../app），"
            f"要么把该忽略的目录名加进 bench/run_task.py 的 IGNORE_DIRS。")
    if survey["skipped_files"]:
        print(f"  已跳过 {survey['skipped_files']} 个非源码文件"
              f"（{survey['skipped_bytes'] / 1024 / 1024:.1f} MB）：模型权重 / 环境 / 缓存 / 日志")

    work = Path(tempfile.mkdtemp(prefix=f"cr-eval-{label}-{index}-"))
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)

    # 隔离 != 把 agent 的记忆也扒掉。家的位置一换，MEMORY_DIR 就落到空目录里，
    # update_context() 于是什么都不带 —— 那等于换了个（更弱的）agent 在测。
    # 正确做法：从一个**冻结的快照**播种，这样每次运行的起点一致，又不会互相污染。
    memory_files: list[str] = []
    if memory_src is not None and memory_src.is_dir():
        shutil.copytree(memory_src, home / ".memory", dirs_exist_ok=True)
        memory_files = sorted(p.name for p in (home / ".memory").glob("*.md"))

    repo_copy = work / "repo"
    copy_repo(repo_src, repo_copy)
    before = tree_digest(repo_copy)

    task_file = work / "task.txt"
    task_file.write_text(task, encoding="utf-8")
    result_file = work / "result.json"

    env = os.environ.copy()
    env.update({
        "WORKDIR": str(repo_copy),          # 被审的是副本，不是原件
        "AGENT_HOME": str(home),            # 全新的家：记忆 / 会话 / 存档都隔离在这里
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(AGENT_DIR),
    })
    if temperature is not None:
        env["TEMPERATURE"] = str(temperature)

    cmd = list(worker_cmd or [sys.executable, str(Path(__file__).resolve()), "--worker"])
    cmd += ["--task-file", str(task_file), "--result-file", str(result_file)]
    if verbose:
        cmd.append("--verbose")
    if with_memory:
        cmd.append("--with-memory")

    started = datetime.now()
    try:
        proc = subprocess.run(cmd, env=env, cwd=str(AGENT_DIR), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
        returncode, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        returncode, out, err = -9, "", f"timeout after {timeout}s"
    ended = datetime.now()

    (run_dir / "worker.log").write_text(out + err, encoding="utf-8")

    result: dict = {}
    if result_file.exists():
        try:
            result = json.loads(result_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            result = {}
    if not result:
        result = {"end_reason": f"worker exited with code {returncode}"}
    result = {**RESULT_DEFAULTS, **result}

    changed = diff_digests(before, tree_digest(repo_copy))

    # 家搬过来留下（archives 就是数据）；被审副本是扔货，除非 --keep-repo
    dest_home = run_dir / "home"
    if dest_home.exists():
        shutil.rmtree(dest_home, ignore_errors=True)
    shutil.move(str(home), str(dest_home))

    # worker 没写出结果（崩了 / 被超时杀掉）时，手上只剩一句 "exited with code 1"。
    # 但 archives 里每一次调用的记录都在 —— 从那里把数据捞回来，这次钱才算没白花。
    if not result.get("session_id"):
        archive = newest_archive(dest_home)
        if archive is not None:
            rebuilt = summarize_archive(archive)
            rebuilt["end_reason"] = result.get("end_reason") or rebuilt["end_reason"]
            result = rebuilt
    if result.get("archive"):
        result["archive"] = str(result["archive"]).replace(str(home), str(dest_home), 1)

    report = result.pop("report", "") or ""
    if report:
        (run_dir / "report.md").write_text(report, encoding="utf-8")
    if keep_repo:
        dest_repo = run_dir / "repo"
        if dest_repo.exists():
            shutil.rmtree(dest_repo, ignore_errors=True)
        shutil.move(str(repo_copy), str(dest_repo))
    shutil.rmtree(work, ignore_errors=True)

    record = {
        "label": label,
        "run": index,
        "started": started.strftime("%Y-%m-%d %H:%M:%S"),
        "ended": ended.strftime("%Y-%m-%d %H:%M:%S"),
        "seconds": round((ended - started).total_seconds(), 1),
        "task": task,
        "repo_src": str(repo_src),
        "repo_rev": git_rev(repo_src),
        "repo_survey": survey,
        "agent_commit": git_rev(AGENT_DIR),
        # 这次用的是哪个 worker 实现。基线对照跑的是 bare_worker.py，必须在记录里
        # 一眼看出来 —— 否则两棵树的 record.json 长得一模一样，几个月后没人分得清。
        "worker": worker_name(worker_cmd),
        "model_alias": os.getenv("MODEL") or "deepseek-v4-flash",
        "temperature": temperature,
        "with_memory": with_memory,
        "memory_seeded": str(memory_src) if memory_src else None,
        "memory_files": memory_files,
        "returncode": returncode,
        "repo_modified": bool(changed),
        "repo_changed_files": changed[:50],
        **result,
    }
    (run_dir / "record.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def next_run_index(label_dir: Path) -> int:
    """下一个空位的编号。同一个 label 再跑，接着往后排，不去覆盖上一次。"""
    n = 1
    while (label_dir / f"run-{n}").exists():
        n += 1
    return n


def worker_name(worker_cmd: list[str] | None) -> str:
    """这次跑用的 worker 实现名（默认就是本文件）。"""
    if worker_cmd and len(worker_cmd) > 1:
        return Path(worker_cmd[1]).name
    return Path(__file__).name


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="跑一次审查任务并把这次运行记下来")
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--task-file", help=argparse.SUPPRESS)
    ap.add_argument("--result-file", help=argparse.SUPPRESS)
    ap.add_argument("--verbose", action="store_true",
                    help="让 agent 的实时输出也留下来（默认写进内存，只留 archives）")
    ap.add_argument("--with-memory", action="store_true",
                    help="跑完做记忆提炼（默认关：多花一次 API 调用）")
    ap.add_argument("--seed-memory", default=str(AGENT_DIR / ".memory"),
                    help="把哪份记忆播种进这次运行的家（默认=代码目录的 .memory，"
                         "也就是你平时跑 CLI 用的那份）。填 none 表示不喂记忆，用于对照")
    ap.add_argument("--label", help="任务名，也是 bench/runs/ 下的目录名")
    ap.add_argument("--repo", help="被审目录")
    ap.add_argument("--task", default=DEFAULT_TASK)
    ap.add_argument("--runs", type=int, default=1, help="同一个任务跑几次")
    ap.add_argument("--temperature", default="none",
                    help="采样温度。默认 none = 不发送这个参数（和 CLI 的行为一致）；"
                         "要锁就显式写 --temperature 0")
    ap.add_argument("--timeout", type=int, default=1200, help="单次运行超时秒数")
    ap.add_argument("--out", default=str(RUNS_DIR), help="运行记录写到哪")
    ap.add_argument("--keep-repo", dest="keep_repo", action="store_true",
                    help=argparse.SUPPRESS)
    ap.add_argument("--no-keep-repo", dest="keep_repo", action="store_false",
                    help="跑完把被审副本删掉。默认**保留**：报告里的 文件:行号 要拿它核对，"
                         "删了那份报告就永远验不了")
    ap.add_argument("--worker-script",
                    help="换一个 worker 实现（基线对照用，见 bench/baseline.py）。"
                         "默认就是本文件；换掉之后 record.json 里的 worker 字段会跟着变")
    ap.set_defaults(keep_repo=True)
    args = ap.parse_args(argv)

    if args.worker:
        task = Path(args.task_file).read_text(encoding="utf-8")
        result = run_session(task, verbose=args.verbose, with_memory=args.with_memory)
        Path(args.result_file).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    if not args.label or not args.repo:
        ap.error("--label 和 --repo 是必填的")
    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"被审目录不存在：{repo}")
        return 2
    raw = str(args.temperature).strip().lower()
    temperature = None if raw in ("none", "null", "") else float(raw)
    seed = str(args.seed_memory).strip().lower()
    memory_src = None if seed in ("none", "null", "") else Path(args.seed_memory).resolve()
    out_root = Path(args.out).resolve()

    print(f"任务 {args.label} · 被审 {repo} · 跑 {args.runs} 次 · "
          f"temperature={'不发送' if temperature is None else temperature} · "
          f"记忆播种={memory_src or '不喂'} · 跑完提炼={'开' if args.with_memory else '关'} · "
          f"worker={args.worker_script or 'run_task.py（完整 agent）'}")
    records = []
    start = next_run_index(out_root / args.label)
    print(f"记录写到 {out_root / args.label} / run-{start}…run-{start + args.runs - 1}")
    worker_cmd = [sys.executable, args.worker_script, "--worker"] if args.worker_script else None
    for k in range(args.runs):
        i = start + k
        rec = execute_run(index=i, label=args.label, repo_src=repo, task=args.task,
                          out_root=out_root, temperature=temperature,
                          timeout=args.timeout, verbose=args.verbose,
                          keep_repo=args.keep_repo, with_memory=args.with_memory,
                          memory_src=memory_src, worker_cmd=worker_cmd)
        records.append(rec)
        u = rec.get("usage") or {}
        print(f"  run-{i}: {rec['seconds']}s · {rec.get('steps', 0)} 步 · "
              f"{rec.get('tool_calls', 0)} 次工具 · "
              f"权限拒绝 {rec.get('permission_denials', 0)}"
              f"（越界 {rec.get('denied_by_policy', 0)} / 无人可问 {rec.get('denied_no_human', 0)}） · "
              f"in/out {u.get('prompt_tokens', 0)}/{u.get('completion_tokens', 0)} tok · "
              f"模型 {rec.get('model_reported') or '未记录'} · "
              f"改了被审代码: {'是 ' + str(rec['repo_changed_files'][:3]) if rec['repo_modified'] else '否'} · "
              f"{'失败 ' + rec.get('end_reason', '')[:60] if rec.get('end_reason') != 'ok' else 'ok'}")

    tot_in = sum((r.get("usage") or {}).get("prompt_tokens", 0) for r in records)
    tot_out = sum((r.get("usage") or {}).get("completion_tokens", 0) for r in records)
    print(f"合计 {len(records)} 次：in {tot_in} tok · out {tot_out} tok · "
          f"记录在 {out_root / args.label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
