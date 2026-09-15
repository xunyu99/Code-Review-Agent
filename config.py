"""config.py — 所有常量的唯一出处，改配置只动这一个文件。"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # 读 .env

# ── 模型 ──
MODEL = os.getenv("MODEL", "deepseek-v4-flash")
API_KEY = os.getenv("DEEPSEEK_API_KEY")
BASE_URL = "https://api.deepseek.com"
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "8192"))   

# ── 路径（全部从项目根派生，换目录就能用）──
# config.py 所在的那个目录（= 项目根目录 E:\learn_claude_code\code-review-agent）。
PROJECT_DIR = Path(__file__).parent.resolve()  # Agent 的家：skills / .transcripts / .task_outputs

# - 如果操作系统环境变量 WORKDIR 被设置了，就读取这个环境变量的字符串作为路径；
# - 如果没有设置 WORKDIR 环境变量，就使用默认值 PROJECT_DIR。
# Path()
#   把拿到的路径字符串包装成 pathlib 的 Path 对象
# .resolve()
#   - 转为绝对真实路径
#   - 自动解析软链接、消除 . / ..
#   . 单点：当前目录  ./main.py 等价 main.py ; src/./test.py 等价 src/test.py
#   .. 双点：往上跳一层，到父文件夹 假设当前目录 /a/b  ../file.txt → /a/file.txt ; - `../../log.txt` → 连续向上跳两层 → `/log.txt`
#   - 默认 strict=True：目录必须真实存在，否则直接抛异常

# 干活的地方：被审查的仓库。默认取「当前目录」—— 绝不默认成 agent 自己的家，
# 否则「工作目录 = 随便读写」这条豁免会把 skills / sessions / .memory 一起放行。
WORKDIR = Path(os.getenv("WORKDIR") or Path.cwd()).resolve()

# ── 家的边界守卫 ──
#   workdir == 家，或 workdir 在家里面  → 拒绝启动（家是 agent 的私产，不是审查对象）
#   家 在 workdir 里面（例如审查 E:\learn_claude_code）→ 放行：permissions.POLICY 里
#   有一条 (PROJECT_DIR, deny, deny)，家的整棵树（源码 / .env / 会话）都不可读写，
#   它的子目录（skills / .memory / .tmp / tool results）各自另有规则、不受影响
if WORKDIR == PROJECT_DIR or WORKDIR.is_relative_to(PROJECT_DIR):
    raise SystemExit(
        f"[config] WORKDIR 不能是 agent 的家或其子目录：{WORKDIR}\n"
        f"         请传真正要审查的仓库路径，例如：python main.py E:\\some\\repo"
    )
if PROJECT_DIR.is_relative_to(WORKDIR):
    print(f"[config] 注意：agent 的家（{PROJECT_DIR}）在审查目录之内；"
          f"按权限表，家的源码 / .env / 会话都不可读写，不会被当成审查对象。")

SKILLS_DIR = PROJECT_DIR / "skills"                          # 下一步技能要用
OUTPUT_DIR = PROJECT_DIR / ".task_outputs" / "tool-results"  # L3 落盘
SESSIONS_DIR = PROJECT_DIR / "sessions"               # 会话目录
TRANSCRIPTS_DIR = PROJECT_DIR / ".transcripts"         # L4 全量对话存档（原来漏了，写 transcript 会 NameError）
MEMORY_DIR = PROJECT_DIR / ".memory"                          # L5 记忆落盘
TMP_DIR = PROJECT_DIR / ".tmp"                                # agent 自己的临时工作区
# ── 上下文管理 ──

# THRESHOLD 的单位是「字符」，不是 token —— estimate_token_count() 就是
# 把所有 message 的 len(content) 加起来，别把这两个单位搞混。
# 代码/中英混排大约 2~2.5 字符 = 1 token。本项目模型 deepseek-flash 窗口 1M token，
# 但按窗口 60~70% 折算太激进，这里取 200K 字符（≈ 80K token，约占窗口 8%）：
# 够连续读完几十个文件，又不至于每一轮请求都背上几十万 token 的账单。
# 想省钱就往小调（比如 120_000），想更少触发就往大调 —— 只有一个旋钮，改这里就行。
THRESHOLD = 200_000           # 消息总量超它 → 才动手压缩（单位：字符）

# ── 错误恢复 ──
BASE_DELAY_MS = 500
MAX_RECOVERY_RETRIES = 3
MAX_529_RETRIES = 3

# ── 工具 ──
BASH_TIMEOUT = 60             # bash 超时秒数

FALLBACK_MODEL = None

MAX_STEPS = 20   # 单次任务最大循环轮数，防模型死循环空转

MAX_INPUT_CHARS = 2000 # 单轮用户输入上限（防误操作/恶意消耗 token）



