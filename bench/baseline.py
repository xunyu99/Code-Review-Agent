"""bench/baseline.py — 跑基线对照：换 worker，其余记账一字不改。

它就是 `bench/run_task.py` 的一层薄壳，替你补两个默认值：

    --worker-script bench/bare_worker.py     换成裸循环（没有技能/记忆/压缩/预算提示/兜底）
    --out           bench/runs-baseline      写到**另一棵树**，不碰 bench/runs/ 一个字节

label 保持同名（caption-v1 还叫 caption-v1），所以两棵树能直接并排看：

    python bench/baseline.py --label my-task --repo D:\\path\\to\\repo --runs 3 \
        --temperature none \
        --task "审查这个仓库，找出代码里的缺陷，按严重程度分级，每条给出 文件:行号。"

    python bench/score.py --root bench/runs-baseline caption-v1   # 看基线
    python bench/score.py caption-v1                              # 看主 agent

跑之前先读 `bench/bare_worker.py` 开头那张"剥掉了什么"的表 —— 那张表就是这次对照的
口径，换了它，数字就不可比了。
"""
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

import run_task                                               # noqa: E402


def main(argv: list[str]) -> int:
    args = list(argv)
    if "--worker-script" not in args:
        args += ["--worker-script", str(BENCH / "bare_worker.py")]
    if "--out" not in args:
        args += ["--out", str(BENCH / "runs-baseline")]
    return run_task.main(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
