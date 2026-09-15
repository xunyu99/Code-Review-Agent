"""session.py — 会话持久化：每个会话一个 JSON，按 年/月/日 分层。"""
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


def session_path(session: dict) -> Path:
    """id 自带年月日时分秒 → 自动算出 年/月/日 目录。"""
    sid = session["id"]                     # "2026-09-13_20-15-00"
    y, m, d = sid[:4], sid[5:7], sid[8:10]
    return SESSIONS_DIR / y / m / d / f"{sid}.json"


def save_session(session: dict) -> None:
    """每次对话后存盘：messages 全量写回。"""
    p = session_path(session)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


def load_session(sid: str) -> dict | None:
    """按 id 恢复会话（第三步做列表时用）。"""
    y, m, d = sid[:4], sid[5:7], sid[8:10]
    p = SESSIONS_DIR / y / m / d / f"{sid}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))
