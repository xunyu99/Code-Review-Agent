"""main.py — 启动入口：会话级共享 messages + rich UI + resume。"""
import os, sys, json, time
from pathlib import Path

_PROJECT = Path(__file__).parent.resolve()


def _is_agent_home(p) -> bool:
    """agent 的家（或它的子目录）不能当审查目标 —— 那是私产，不是仓库。"""
    try:
        rp = Path(p).resolve()
    except Exception:
        return False
    return rp == _PROJECT or rp.is_relative_to(_PROJECT)


# ⚠ WORKDIR 必须在 import config 之前设定（config import 时冻结，且会做家的边界检查）
# ⚠ 审查目录不能是 agent 的家（含它的子目录）—— 家的私产不是审查对象
# 命令：
#   python main.py E:\xxx\repo            → 新会话
#   python main.py resume <session_id>    → 恢复旧会话（workdir 从文件读）
_args = sys.argv[1:]
_resume_id = None
if _args and _args[0] == "resume":
    _resume_id = _args[1] if len(_args) > 1 else None
    if not _resume_id:
        print("用法: python main.py resume <session_id>")
        sys.exit(1)
    # 此时还不能 import config（会冻结 WORKDIR），先裸读会话文件拿 workdir
    y, m, d = _resume_id[:4], _resume_id[5:7], _resume_id[8:10]
    _sf = _PROJECT / "sessions" / y / m / d / f"{_resume_id}.json"
    if not _sf.exists():
        print(f"找不到会话 {_resume_id}")
        sys.exit(1)
    repo = json.loads(_sf.read_text(encoding="utf-8"))["workdir"]
else:
    repo = _args[0] if _args else None
    if not repo:
        # 默认值用「当前目录」而不是 agent 的家；从家里启动时干脆不给默认值，逼你给一个真仓库
        _default = None if _is_agent_home(Path.cwd()) else Path.cwd()
        while True:
            hint = f"[{_default}]" if _default else "（必填：要审查的仓库路径）"
            try:
                repo = (input(f"审查目录 {hint}: ").strip()
                        or (str(_default) if _default else ""))
            except (KeyboardInterrupt, EOFError):
                print("\n已取消")      # 此时还没 import ui，只能用裸 print
                sys.exit(0)
            if not repo:
                print("✗ 必须给一个审查目录")
                continue
            if _is_agent_home(repo):
                print(f"✗ {repo} 是 agent 的家（或它的子目录），不是审查对象；请换一个目录")
                continue
            break
os.environ["WORKDIR"] = repo

# ── 统一 import（WORKDIR 冻结之后）──
from config import WORKDIR, MAX_INPUT_CHARS
from agent import agent_loop, repair_dangling_tool_calls
from system_prompt import update_context
from session import new_session, save_session, load_session
from memory import extract_memories, consolidate_memories, CONSOLIDATE_THRESHOLD, list_memory_files
from ui import console, welcome, ask, show_answer, show_user, warn, info

REPLAY_MESSAGES = 6      # resume 时最多回放最近几条消息，别把屏幕刷满
DOUBLE_CTRL_C = 1.5      # 秒：Ctrl+C 连按两次才退出，按一次只是中断本轮


def safe_save(session):
    """存盘兜底：失败只警告，不抛 traceback 打断交互。"""
    try:
        repair_dangling_tool_calls(session["messages"])   # 中断留下的半截工具调用先补个占位结果
        save_session(session)
    except Exception as e:
        warn(f"会话存盘失败: {type(e).__name__}: {e}")


def goodbye(session, messages):
    """退出统一出口：有内容就存盘并给出续聊命令。"""
    if any(m["role"] == "user" for m in messages):
        safe_save(session)
        console.print()
        console.print("[ui.dim]To continue this session, run:[/]")
        console.print(f"  [ui.structure]python main.py resume {session['id']}[/]")
    else:
        console.print("[ui.dim]bye[/]")


def main():
    welcome()

    if _resume_id:
        session = load_session(_resume_id)
        info(f"恢复会话 {session['id']}：{session['title']}\n")
        # 只回放最近几轮：长会话全量重播会把终端刷到没法看
        history = [m for m in session["messages"]
                   if m.get("role") in ("user", "assistant") and m.get("content")]
        shown = history[-REPLAY_MESSAGES:]
        if len(history) > len(shown):
            info(f"（更早的 {len(history) - len(shown)} 条消息已省略）")
        for m in shown:
            if m["role"] == "user":
                show_user(m["content"])
            else:
                show_answer(m["content"])
    else:
        session = new_session(WORKDIR)

    messages = session["messages"]                          # ← 会话级共享这一份
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": ""})   # 占位，agent_loop 每轮重建

    interrupted_at = 0.0             # 上一次 Ctrl+C 的时间；连按两次才退出
    while True:
        try:
            query = ask()
            interrupted_at = 0.0     # 正常拿到输入 → 双击计时清零
            if query.lower() in ("exit", "quit", "q"):
                goodbye(session, messages)
                break
            if not query:
                continue    # 空行不退出，继续等输入
            if len(query) > MAX_INPUT_CHARS:
                warn(f"输入 {len(query)} 字符，超过 {MAX_INPUT_CHARS}，已截断")
                query = query[:MAX_INPUT_CHARS]
            if session["title"] == "(未命名)":
                session["title"] = query[:50]

            context = update_context()
            messages.append({"role": "user", "content": query})
            show_user(query)
            agent_loop(messages, context)   # 里面的 Ctrl+C 由 agent_loop 自己消化，本轮作废
            safe_save(session)                               # 每轮结束存盘
            extract_memories(messages)
            if len(list_memory_files()) >= CONSOLIDATE_THRESHOLD:
                consolidate_memories()
            # 回答已经由 ui.StreamView 边流边打完了，这里不再补打一遍（以前会重复整段）
        except EOFError:
            # Ctrl+D / Ctrl+Z：输入流结束，按约定退出
            console.print()
            goodbye(session, messages)
            break
        except KeyboardInterrupt:
            # 停在输入行上按 Ctrl+C（本轮生成中的 Ctrl+C 走不到这里）
            console.print()
            now = time.time()
            if now - interrupted_at < DOUBLE_CTRL_C:
                goodbye(session, messages)
                break
            interrupted_at = now
            if any(m["role"] == "user" for m in messages):
                safe_save(session)      # 中断点也存一次，别丢已经聊过的内容
            info("^C 已中断 · 再按一次 Ctrl+C 退出（或输入 exit）")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        # 启动阶段（welcome / 回放历史）按 Ctrl+C：干净退出，不打 traceback
        console.print()
        console.print("[ui.dim]bye[/]")
