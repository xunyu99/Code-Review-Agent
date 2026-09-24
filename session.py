"""session.py — 会话持久化：一个会话两份文件，按 年/月/日 分层。

两份文件的分工（这是本项目最容易搞混的一处，别混）：

  sessions/年/月/日/<id>.jsonl        原文日志 —— 只追加，永不改写
      每条消息产生时就落一行。压缩对它零影响。
      用途：存档、审计、兜底重建。

  sessions/年/月/日/<id>.state.json   工作副本 —— 每轮整体覆盖
      就是发给模型的那份 messages，可能已经被 L1–L4 压过。
      以前它和存档是同一份文件，所以压缩一动手，原文就没了。

一句话：**上下文会被压缩，存档不会。**

旧版本只有 <id>.json（当时它既是存档又是上下文）。load_session 仍然能读它，
读到后当工作副本用，并在第一次打开时把现有消息灌进日志（带 seed 标记，
如实说明这份是当时的工作副本、可能已经压过）。
"""
import json
from datetime import datetime
from pathlib import Path
from config import SESSIONS_DIR


def new_session(workdir: str) -> dict:
    """新建会话：id=时间戳，title=待定（第一条 query 来时再填）。"""
    ts = datetime.now()
    return {
        "id": ts.strftime("%Y-%m-%d_%H-%M-%S"),
        "title": "(未命名)",
        "created": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "workdir": str(workdir),
        "messages": [],
    }


def _dir(sid: str) -> Path:
    """id 自带年月日时分秒 → 自动算出 年/月/日 目录。"""
    y, m, d = sid[:4], sid[5:7], sid[8:10]      # "2026-09-13_20-15-00"
    return SESSIONS_DIR / y / m / d


def state_path(sid: str) -> Path:
    """工作副本（发给模型的那份上下文）。"""
    return _dir(sid) / f"{sid}.state.json"


def log_path(sid: str) -> Path:
    """原文日志（只追加的存档）。"""
    return _dir(sid) / f"{sid}.jsonl"


def legacy_path(sid: str) -> Path:
    """旧版单文件格式，只读兼容。"""
    return _dir(sid) / f"{sid}.json"


def save_session(session: dict) -> None:
    """写工作副本。整体覆盖 —— 它就该反映「模型当前看到的上下文」。"""
    p = state_path(session["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(sid: str) -> dict | None:
    """按 id 恢复会话；没有新格式就回退到旧版 <id>.json。"""
    for p in (state_path(sid), legacy_path(sid)):
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _warn(msg: str) -> None:
    """写日志失败时提醒一次。ui 延迟 import，避免存储层和 UI 层互相牵住。"""
    try:
        from ui import warn
        warn(msg)
    except Exception:
        print(msg)


class SessionLog:
    """原文日志：每条消息产生时追加一行，永不改写。

    记录类型只有四种，都很轻：
      meta   会话身份（id / title / workdir / created），开日志时写一次
      seed   老会话补灌历史时的说明（说明这份来自 state，可能已压过）
      msg    原始消息，一行一条 —— 这就是「原文」
    """

    def __init__(self, session: dict):
        self.path = log_path(session["id"])
        self._session = session
        self._started = self._exists()      # 内存标志：判断首次靠它，别靠 exists()
        self._warned = False

    def _exists(self) -> bool:
        try:
            return self.path.exists() and self.path.stat().st_size > 0
        except OSError:
            return False

    def exists(self) -> bool:
        return self._exists()

    def _write(self, record: dict) -> None:
        """单条追加。写失败只警告一次 —— 存档重要，但不该把交互打断。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            prefix = ""
            if self.path.exists() and self.path.stat().st_size > 0:
                with self.path.open("rb") as f:    # 只看最后一个字节
                    f.seek(-1, 2)
                    if f.read(1) != b"\n":
                        # 上次写到一半就断了。先给那截封口，否则这次会接在它后面，
                        # 把两条记录连成一条坏 JSON —— 坏一行和坏两行的区别。
                        prefix = "\n"
            with self.path.open("a", encoding="utf-8") as f:
                f.write(prefix + json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as e:
            if not self._warned:
                self._warned = True
                _warn(f"原文日志写入失败（本会话不再重复提醒）: {type(e).__name__}: {e}")

    def ensure_started(self, messages) -> int:
        """首次打开时建日志头；老会话把现有工作副本灌进去，返回灌入条数。

        注意用 _started 判断「是否首次」而不是 exists()：write_meta 之后文件
        就已经存在了，用 exists() 判会把 seed 整段误跳过去。
        """
        if self._started:
            return 0
        s = self._session
        self._write({"type": "meta", "ts": _now(), "id": s.get("id"),
                     "title": s.get("title"), "created": s.get("created"),
                     "workdir": s.get("workdir")})
        n = 0
        if messages:
            self._write({"type": "seed", "ts": _now(), "source": "state",
                         "note": "该会话早于日志功能，此处灌入的是当时的工作副本，可能已压缩过"})
            for m in messages:
                if m.get("role") == "system":
                    continue
                self.append_message(m)
                n += 1
        self._started = True
        return n

    def append_message(self, message: dict) -> None:
        """记一条原始消息。system 不记 —— 它每步都被 agent_loop 重建，不是对话内容。"""
        if message.get("role") == "system":
            return
        self._write({"type": "msg", "ts": _now(), "message": message})


class LoggedList(list):
    """工作副本：append 时顺手把原文写进日志，整体替换则不动日志。

    关键在「只认 append」这件事：
      · 新消息 = messages.append(...)     → 这是原文，记进日志
      · 压缩   = messages[:] = ...        → 这是改上下文，不记进日志
    slice 赋值走的是 list.__setitem__，这里不拦，所以压缩对日志天然无感。
    （agent.py 里七处 append 和四处 messages[:] = 正好落在这条界线的两边，
      因此 agent.py 一行都不用改。）
    """

    def __init__(self, items=(), log: "SessionLog | None" = None):
        super().__init__(items)
        self._log = log

    def append(self, message) -> None:
        super().append(message)
        if self._log is not None:
            self._log.append_message(message)


def open_session(session: dict) -> tuple[LoggedList, SessionLog]:
    """打开会话：返回 (工作副本 messages, 日志)。

    返回的 messages 会**原地挂回 session["messages"]** —— 必须挂回去，
    否则 save_session 存的还是旧的那个 list，两边就分叉了。
    """
    log = SessionLog(session)
    msgs = session.get("messages") or []
    log.ensure_started(msgs)
    logged = LoggedList(msgs, log)
    session["messages"] = logged
    return logged, log


def load_raw_messages(sid: str) -> list | None:
    """从日志重建原文（审计 / 兜底用）。没有日志返回 None。

    半截行（比如写到一半断电）直接跳过，不让一行坏数据毁掉整次重建。
    """
    p = log_path(sid)
    if not p.exists():
        return None
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") == "msg" and isinstance(rec.get("message"), dict):
            out.append(rec["message"])
    return out
