"""记忆提炼的门槛：只有正常跑完的一轮才配提炼。

改之前 fatal() 返回 None，和「跑完了」是同一个值，于是报错、撞上限的轮次照样进
记忆提炼 —— 把没有结论的东西沉淀成长期记忆，跟着进以后每一次会话。现在失败是一个
独立的终态（agent.FAILED），由 agent.turn_completed 一句话判定。

撞上限那轮即使被抢救出一份报告，也**不算跑完**：那份报告自己就标着「未能验证」。
"""
import _bootstrap  # noqa: F401

import shutil

import agent


# ── 终态本身 ───────────────────────────────────────────────────────────

def test_only_a_completed_turn_counts_as_done():
    assert agent.turn_completed(agent.OK) is True
    assert agent.turn_completed(agent.FAILED) is False
    assert agent.turn_completed(agent.INTERRUPTED) is False


def test_ok_is_still_none():
    """成功判据保持 None：bench/run_task 忽略返回值，老调用方也不用改。"""
    assert agent.OK is None


def test_fatal_reports_failed_and_still_writes_the_error_into_the_turn():
    msgs = [{"role": "user", "content": "审一下"}]
    saved, agent.warn = agent.warn, lambda *a, **k: None
    try:
        status = agent.fatal(msgs, "boom")
    finally:
        agent.warn = saved

    assert status == agent.FAILED
    assert msgs[-1] == {"role": "assistant", "content": "[Error] boom"}


def test_agent_loop_passes_the_failed_status_through():
    saved = agent._agent_loop_inner
    try:
        agent._agent_loop_inner = lambda m, c: agent.FAILED
        assert agent.agent_loop([], None) == agent.FAILED
    finally:
        agent._agent_loop_inner = saved


# ── 真正的闸门在 main.after_turn 里 ────────────────────────────────────

def _import_main():
    """把 main 导进来。

    不能直接 import：main.py 在 **import 期**就解析 sys.argv，没给审查目录时还会
    input() 等你打字 —— 测试里那会直接挂住（或 sys.exit）。所以先把 argv 换成
    「一个合法的临时审查目录」，导完立刻还原环境变量。
    """
    import importlib
    import os
    import sys
    import tempfile

    probe = tempfile.mkdtemp(prefix="cr-main-import-")
    saved_argv, saved_workdir = sys.argv, os.environ.get("WORKDIR")
    sys.argv = ["main.py", probe]
    try:
        return importlib.import_module("main"), probe
    finally:
        sys.argv = saved_argv
        if saved_workdir is None:
            os.environ.pop("WORKDIR", None)
        else:
            os.environ["WORKDIR"] = saved_workdir


def test_main_only_distills_a_completed_turn():
    main, probe = _import_main()
    calls: list[str] = []
    saved = (main.extract_memories, main.consolidate_memories,
             main.list_memory_files, main.info)
    main.extract_memories = lambda _msgs: calls.append("extract")
    main.consolidate_memories = lambda: calls.append("consolidate")
    main.info = lambda *a, **k: None
    msgs = [{"role": "user", "content": "审一下"}]
    try:
        for status in (agent.FAILED, agent.INTERRUPTED):
            calls.clear()
            main.after_turn(msgs, status)
            assert calls == [], f"{status} 不该提炼记忆，实际做了 {calls}"

        # 跑完了才提炼；记忆条数没到阈值就不做合并
        main.list_memory_files = lambda: []
        calls.clear()
        main.after_turn(msgs, agent.OK)
        assert calls == ["extract"]

        # 到阈值就顺手合并
        main.list_memory_files = lambda: [{}] * main.CONSOLIDATE_THRESHOLD
        calls.clear()
        main.after_turn(msgs, agent.OK)
        assert calls == ["extract", "consolidate"]
    finally:
        (main.extract_memories, main.consolidate_memories,
         main.list_memory_files, main.info) = saved
        shutil.rmtree(probe, ignore_errors=True)
