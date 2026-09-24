"""tests/run_tests.py — 零依赖跑法（pytest 没装时用这个）。

    python tests/run_tests.py
    python tests/run_tests.py session_log      # 只跑名字里带这个的模块

收集 tests/test_*.py 里所有 test_* 函数逐个跑。_bootstrap 会把 agent 目录和
tests 目录放进 sys.path，并把 WORKDIR 指向一个临时假仓库、API key 换成占位符，
所以全程不联网、不碰真项目。
"""
import importlib
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def main(argv: list[str]) -> int:
    keyword = argv[0] if argv else ""
    passed = failed = 0
    for path in sorted(HERE.glob("test_*.py")):
        if keyword and keyword not in path.stem:
            continue
        module = importlib.import_module(path.stem)
        names = sorted(n for n in dir(module)
                       if n.startswith("test_") and callable(getattr(module, n)))
        print(f"\n[{path.stem}]")
        for name in names:
            try:
                getattr(module, name)()
                passed += 1
                print(f"  ok   {name}")
            except Exception:
                failed += 1
                print(f"  FAIL {name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
