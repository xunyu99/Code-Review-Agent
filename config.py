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
# 采样温度：不设就不发这个参数（行为和以前完全一样）。评测时设 TEMPERATURE=0，
# 免得步数方差里混着采样噪声。
TEMPERATURE = float(os.getenv("TEMPERATURE")) if os.getenv("TEMPERATURE") else None

# ── 路径（全部从项目根派生，换目录就能用）──
# 代码所在目录（= config.py 所在目录）：源码、.env、skills 都在这里，永远不搬。
CODE_DIR = Path(__file__).parent.resolve()

# Agent 的「家」：会话 / 存档 / 记忆 / 临时区 / 落盘的工具结果都写在它下面。
# 默认就是代码目录（和以前完全一样）。评测时用 AGENT_HOME 指到一个一次性的空目录，
# 上一次运行留下的记忆和会话就带不进下一次 —— 隔离靠换家，不靠事后删文件。
PROJECT_DIR = Path(os.getenv("AGENT_HOME") or CODE_DIR).resolve()

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
#   家 在 workdir 里面（例如把整个代码目录当审查对象）→ 放行：permissions.POLICY 里
#   有一条 (PROJECT_DIR, deny, deny)，家的整棵树（源码 / .env / 会话）都不可读写，
#   它的子目录（skills / .memory / .tmp / tool results）各自另有规则、不受影响
if WORKDIR == PROJECT_DIR or WORKDIR.is_relative_to(PROJECT_DIR):
    raise SystemExit(
        f"[config] WORKDIR 不能是 agent 的家或其子目录：{WORKDIR}\n"
        f"         请传真正要审查的仓库路径，例如：python main.py E:\\some\\repo"
    )
if PROJECT_DIR.is_relative_to(WORKDIR) or CODE_DIR.is_relative_to(WORKDIR):
    print(f"[config] 注意：agent 的家（{PROJECT_DIR}）或代码目录（{CODE_DIR}）在审查目录之内；"
          f"按权限表，它们都不可读写，不会被当成审查对象。")

# 技能跟着**代码**走，不跟着家走：它是随代码发布的只读资产，不是运行时产生的状态。
SKILLS_DIR = CODE_DIR / "skills"
OUTPUT_DIR = PROJECT_DIR / ".task_outputs" / "tool-results"  # L3 落盘
# 会话拆成两棵树，各放各的 —— 同目录里放两份长得很像的文件，最容易被看混：
#   sessions/  上下文：发给模型的 messages，每轮覆盖写，会被压缩改写
#   archives/  存档　：原文流水账，只追加，压缩碰不到
SESSIONS_DIR = PROJECT_DIR / "sessions"
ARCHIVE_DIR = PROJECT_DIR / "archives"
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

# ── 步数预算提醒 ──
# 剩余步数降到这个值，提示就从「该收尾了」升级成「立刻写报告」（见 agent.step_budget_notice）。
# 20 步的任务下这是第 15 步。想让模型更早收手就往大调（比如 8）。
#
# 下面三个旋钮都支持**环境变量覆盖** —— 消融实验要靠它，不然每次都得改代码：
#   STEP_BUDGET_NOTICE=off      完全不追加步数预算提示（看"劝它收手"到底压了多少召回）
#   REPORT_CHARS_LIMIT=0        完全不提报告长度（同上，测字数那条）
#   STEP_WARN_REMAINING=2       只改"什么时候开始催"
STEP_WARN_REMAINING = int(os.getenv("STEP_WARN_REMAINING", "5"))
STEP_BUDGET_NOTICE = os.getenv("STEP_BUDGET_NOTICE", "on").strip().lower() not in (
    "off", "0", "no", "none", "")

# 撞上 MAX_STEPS 之后，要不要再花一次调用、逼它用已有信息抢救一份报告。
# 开着（默认）：失败不再等于 0 产出，代价是多一次调用。
# 关掉：回到「撞墙就是 0 字」，做对照实验时用（两种口径的报告不能混着比）。
SALVAGE_ON_MAX_STEPS = True

# ── 输出长度 ──
# 报告总长上限（单位：字符）。这是**提示词级别的约束**，靠模型自觉遵守，不是硬截断。
#
# 为什么不用 MAX_TOKENS 管这件事：它管的是「单次回复生成到哪停下」。报到上限会被
# finish_reason="length" 接住、走续写分支，把整段对话再发一遍去换后半截 —— 报告没
# 变短，输入 token 反而翻倍。实测那两份 6,292 / 8,310 字的报告整轮输出才 4,579 token，
# 离 8192 差得远，**上限一次都没碰到**：它写这么长是没人告诉它别写。
#
# 数值怎么定的：1500 那次复测（run-7 / run-8）字数砍到 1/3，召回也从 6.5/8 掉到 3.5/8
# —— 因为当时的措辞是「最多 5 条，宁可丢掉弱的」，等于授权模型少报。现在改成「压每条、
# 别砍条数」，条数放宽到 8，数字也放到 3000（6~8 条 × 每条 4 行短句刚好装得下）。
# 设成 0（或负数）= 提示词里完全不提长度，用于消融对照。
REPORT_CHARS_LIMIT = int(os.getenv("REPORT_CHARS_LIMIT", "3000"))

MAX_INPUT_CHARS = 2000 # 单轮用户输入上限（防误操作/恶意消耗 token）



