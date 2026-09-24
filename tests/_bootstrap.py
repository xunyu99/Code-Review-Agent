"""测试公共引导 —— 所有测试文件第一行都 import 它。

两件事必须在 import config 之前做完（config 在 import 时冻结这些值）：
  1. WORKDIR 指向一个临时“假仓库”：config 会拒绝「agent 自己的家」当审查目录，
     而测试又需要一条合法的相对路径基准。
  2. API key 用占位符：测试全程不联网、也绝不会读到 .env 里的真 key。

依赖只有标准库，所以 pytest 没装也能跑（见 tests/run_tests.py）。
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AGENT_DIR))

# 假仓库：测试里所有「工作目录内」的文件都写在这儿，退出时删掉
REPO = Path(tempfile.mkdtemp(prefix="cr-agent-fake-repo-"))
atexit.register(shutil.rmtree, REPO, True)

os.environ["WORKDIR"] = str(REPO)
os.environ["DEEPSEEK_API_KEY"] = "sk-test-placeholder"   # override：不用真 key

import agent          # noqa: E402
import config         # noqa: E402
import permissions    # noqa: E402
import tools          # noqa: E402


def make_file(rel_path: str, text: str, encoding: str = "utf-8") -> Path:
    """在假仓库里造一个文件（自动建父目录），返回绝对路径。"""
    p = REPO / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding=encoding, newline="")
    return p


def make_bytes(rel_path: str, raw: bytes) -> Path:
    """在假仓库里造一个二进制/指定编码的文件。"""
    p = REPO / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(raw)
    return p


def assert_raises(exc, fn, *args, **kwargs):
    """零依赖的 pytest.raises：没装 pytest 也能用。"""
    try:
        fn(*args, **kwargs)
    except exc:
        return None
    except Exception as e:                     # 抛了别的异常也算失败，但要说清楚
        raise AssertionError(f"期望 {exc.__name__}，实际抛了 {type(e).__name__}: {e}")
    raise AssertionError(f"期望 {exc.__name__}，但没有抛异常")
