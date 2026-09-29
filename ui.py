"""ui.py — 终端展示层（rich + prompt_toolkit）。全项目唯一直接碰终端的模块。"""
import re
import sys
import time
from rich.cells import cell_len
from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.spinner import Spinner
from rich.style import Style
from rich.syntax import SyntaxTheme
from rich.table import Table
from rich.text import Text
from rich.theme import Theme
from pygments.token import (Comment, Error, Generic, Keyword, Name, Number,
                            Operator, Punctuation, String, Token)
from prompt_toolkit import PromptSession
from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style as PromptStyle
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from config import MODEL, WORKDIR

# ---------- 配色：只用 白 / 蓝 / 金 ----------
# 蓝 = 结构与技术实体（框线、标签、列表符号、链接、行内代码）——「界面在说话」的部分
# 金 = 需要你注意的（标题、警告、失败、[PERMISSION]）——点睛用，不铺开
# 白 = 内容本身。正文用终端默认色而不是写死 white，浅色终端才不会瞎
# 目的：把 rich 默认的洋红标题、黑底青字行内代码、洋红引用块全部换掉
THEME = Theme({
    "ui.structure": "bright_blue",
    "ui.accent": "gold1",
    "ui.dim": "dim",
    "ui.dim.structure": "dim blue",
    "ui.fail": "gold1",
    "ui.fail.dim": "dim gold1",
    "ui.label": "bright_blue on #303030",     # 「用户>」标签：蓝字 + 保留的灰底
    "ui.title": "bold gold1",
    # —— Markdown ——
    "markdown.h1": "bold gold1",
    "markdown.h2": "bold gold1",
    "markdown.h3": "bold bright_white",
    "markdown.h4": "bold bright_white",
    "markdown.h5": "dim",
    "markdown.h6": "dim",
    "markdown.code": "bright_blue",           # 行内代码：蓝，去掉黑底
    "markdown.code_block": "none",
    "markdown.link": "underline bright_blue",
    "markdown.link_url": "dim bright_blue",
    "markdown.block_quote": "dim",
    "markdown.item.bullet": "bright_blue",
    "markdown.item.number": "bright_blue",
    "markdown.list": "none",
    "markdown.hr": "dim",
    "markdown.table.border": "bright_blue",
    "markdown.table.header": "bold bright_white",
})

console = Console(theme=THEME)


class PaletteSyntax(SyntaxTheme):
    """代码块高亮也只用白/蓝/金：关键字与操作符=金，字符串与内建名=蓝，注释=dim，其余默认白。
    rich 自带的主题（monokai / ansi_dark / friendly / bw）没有一个是这个色域的。"""

    _MAP = {
        Comment: Style(dim=True),
        Comment.Preproc: Style(color="bright_blue"),
        Keyword: Style(color="gold1"),
        Keyword.Type: Style(color="bright_blue"),
        Operator: Style(color="gold1"),
        Punctuation: Style(color="bright_black"),
        Name.Builtin: Style(color="bright_blue"),
        Name.Function: Style(color="bright_white", bold=True),
        Name.Class: Style(color="gold1"),
        Name.Decorator: Style(color="gold1"),
        Name.Constant: Style(color="bright_blue"),
        Name.Attribute: Style(color="bright_blue"),
        Name.Tag: Style(color="gold1"),
        String: Style(color="bright_blue"),
        Number: Style(color="bright_blue"),
        Generic.Deleted: Style(color="gold1"),
        Generic.Inserted: Style(color="bright_blue"),
        Generic.Heading: Style(color="bright_white", bold=True),
        Error: Style(color="gold1"),
    }

    def __init__(self):
        self._cache = {}

    def get_style_for_token(self, token_type):
        """按 pygments 的 token 层级往上找最近的规则。"""
        hit = self._cache.get(token_type)
        if hit is not None:
            return hit
        token = token_type
        while token is not None:
            if token in self._MAP:
                self._cache[token_type] = self._MAP[token]
                return self._MAP[token]
            token = token.parent
        return Style.null()

    def get_background_style(self):
        return Style.null()      # 不铺底色，代码块和终端背景融为一体


CODE_THEME = PaletteSyntax()     # 无状态，全局共用一个

# 输出被重定向（管道 / 文件）时 Windows 按 GBK 编码，✓ ✗ ─ 这类字符会直接抛 UnicodeEncodeError
if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

PAD_X = 2                    # 正文区左右边距（列），不让文字紧贴屏幕边缘
LABEL_BG = "bg:#303030"      # 输入行 / 回显行标签用的同一块灰底
STREAM_FPS = 12              # 流式预览的刷新上限（帧/秒），太密会卡
STREAM_MD_LIMIT = 24_000     # 预览缓冲超过它就退化成纯文本，免得每帧重排 Markdown


def welcome() -> None:
    """启动欢迎面板——标题在框内，宽度按显示宽度（中文占 2 列）自适应，且不超过终端。"""
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style="dim")
    t.add_column()
    t.add_row("author:", Text("pre", style="ui.dim"))
    t.add_row("model:", Text(str(MODEL), style="bold bright_white"))
    t.add_row("workdir:", Text(str(WORKDIR), style="ui.structure"))
    longest = max(cell_len(s) for s in ("author: pre", f"model: {MODEL}", f"workdir: {WORKDIR}"))
    w = min(longest + 8, max(24, console.width - 2))   # ← 深路径不再撑破终端
    content = Group(
        Text(">_ Code Review Agent", style="ui.title"),
        Text(""),
        t,
    )
    console.print(Panel(content, border_style="ui.structure", width=w))
    console.print()
    console.print("[ui.dim]输入问题 · Enter 提交 · Alt+Enter 换行 · ↑↓ 移动/翻历史 · "
                  "Ctrl+C 中断本轮（连按两次退出） · exit 退出[/]")


# ---------- 用户输入：prompt_toolkit 接管 ----------
_input_history = InMemoryHistory()
_input_style = PromptStyle.from_dict({
    # 「用户>」标签：蓝字 + 灰底（保留）；正文不铺底，回显才和输入时长得一样
    "prompt": f"fg:ansibrightblue {LABEL_BG}",
})

# multiline 模式：Enter 提交；↑↓ 在输入内容内移动（含软换行），到首/尾行再按才翻历史
_kb = KeyBindings()

@_kb.add("enter")
def _submit(event):
    event.current_buffer.validate_and_handle()

@_kb.add("up")
def _up(event):
    event.current_buffer.auto_up(count=event.arg)

@_kb.add("down")
def _down(event):
    event.current_buffer.auto_down(count=event.arg)

@_kb.add("escape", "enter")   # Alt+Enter（终端发的就是 ESC + CR）
def _newline(event):
    event.current_buffer.insert_text("\n")


# erase_when_done 是 PromptSession 的构造参数，顶层 prompt() 不收（3.0.x 起都不收），
# 所以这里改成建一个常驻 session。懒建是故意的：构造时要碰终端，别在 import 阶段就碰。
_session = None


def _get_session() -> PromptSession:
    global _session
    if _session is None:
        _session = PromptSession(
            style=_input_style,
            history=_input_history,
            multiline=True,
            key_bindings=_kb,
            erase_when_done=True,   # 提交后擦净输入区，再由 show_user 重绘
        )
    return _session


def ask() -> str:
    """prompt_toolkit 输入：Enter 提交、Alt+Enter 换行、长文本软换行内移动、粘贴/历史。"""
    console.print("")
    try:
        query = _get_session().prompt(
            HTML("<prompt> 用户> </prompt>"),
        )
    except (KeyboardInterrupt, EOFError):
        print()
        raise
    console.print("")
    return query.rstrip()      # 只去尾部空白，保留粘贴代码的行首缩进


def show_user(text: str) -> None:
    """渲染一条用户消息（历史回放/回显）。用 Text 拼接，text 不当 markup 解析；
    标签用和输入行相同的灰底、正文不加样式——输入时与回显后长得一样，才不像被重画了两遍。"""
    console.print(Text(" 用户> ", style="ui.label") + Text(" " + str(text)))


def show_answer(text: str, rule: bool = True) -> None:
    """最终回答：分隔线 + 左右留边距的 Markdown，段落间距归一化。
    rule=False 用于「中途解说」和「续写片段」——它们和上一段同属一段回答，不重复画线。"""
    text = re.sub(r"\n{3,}", "\n\n", str(text))
    console.print("")
    if rule:
        rule_w = max(10, console.width - PAD_X * 2)
        console.print(Padding(Text("─" * rule_w, style="ui.dim.structure"), (0, PAD_X)))
    console.print(Padding(Markdown(text, code_theme=CODE_THEME), (0, PAD_X)))
    console.print("")


# ---------- 工具生命周期：spinner 转动 → 冻结成 ✓/✗ ----------
# 注意：仅支持串行工具调用（本项目工具一个个执行）；并发需按 tool_call_id 分别建状态。
_FAIL_MARKERS = ("Error", "Blocked", "Permission denied", "Unknown tool", "Invalid")
# 两种形状都算：`(exit code 7)` 和 `(no output, exit code 7)`
_NONZERO_EXIT = re.compile(r"\bexit code [1-9]\d*\)\s*$", re.MULTILINE)


def _looks_failed(text: str) -> bool:
    """拿不准成败时按文本判断（调用方给不出 failed 才会走到这里）。

    非零退出码也算失败：`run_bash` 会把 `(exit code N)` 附在输出末尾，
    不认它的话，一条失败的命令会在屏幕上被打成 ✓ —— 那就是界面在撒谎。
    """
    return text.startswith(_FAIL_MARKERS) or bool(_NONZERO_EXIT.search(text))


class ToolTracker:
    def __init__(self):
        self._live = None
        self._spinner = None
        self.name = ""
        self._ts = 0.0

    def start(self, name: str, args_summary: str = ""):
        if self._live is not None:   # 上一个没收尾（异常路径）先收掉：rich 同时只认一个 Live
            self.finish("", failed=True, note="no result returned")
        self._spinner = Spinner("dots", style="dim")
        self._live = Live(self._spinner, console=console,
                          refresh_per_second=10, transient=True)
        self._live.start()
        self.name = name
        self._ts = time.time()
        label = f"{name}({args_summary})" if args_summary else name
        # 传 Text 而不是 str：Spinner 对 str 会走 Text.from_markup，参数里的 [a-z] 会被当标签解析
        self._spinner.update(text=Text(label, style="dim"))

    def stop(self):
        """停掉转动中的 spinner，不打印任何东西。"""
        if self._live is not None:
            try:
                self._live.stop()
            finally:
                self._live = None

    def finish(self, output: str, failed: bool | None = None, note: str = ""):
        """收尾并冻结成一行。failed 由调用方给出；拿不准时才退回前缀启发式。"""
        elapsed = (time.time() - self._ts) * 1000
        self.stop()
        s = str(output)
        if failed is None:
            failed = _looks_failed(s)
        mark, color = ("✗", "ui.fail") if failed else ("✓", "ui.structure")
        size = f"{len(s)} chars" if s else "no output"
        line = Text(f"{mark} ", style=color)
        line.append(f"{self.name} · {size} · {elapsed:.0f}ms", style="dim")
        console.print(Padding(line, (0, PAD_X)), highlight=False)
        detail = " ".join(str(note or (s if failed else "")).split())   # 多行原因压成一行
        if detail:
            console.print(Padding(Text(f"  ↳ {detail[:120]}", style="ui.fail.dim"), (0, PAD_X)),
                          highlight=False)

    def denied(self, name: str, summary: str, reason: str):
        """权限被拒。以前这条路径什么都不显示，用户只看到界面像卡住了。"""
        self.start(name, summary)
        self.finish(reason, failed=True, note=reason)


tracker = ToolTracker()


# ---------- 流式预览：delta 进 → 屏幕上滚 → 结束冻结成正式回答 ----------
class StreamView:
    """模型输出的流式视图：agent 只管把 delta 丢进来，渲染与收尾都留在这里。"""

    def __init__(self):
        self._buf = []
        self._live = None
        self._ts = 0.0

    # ── 生命周期 ──
    def start(self):
        """开始一段流。重试时会再调一次，所以先把上一段的残余清干净。"""
        self.stop()
        tracker.stop()          # rich 同时只认一个 Live，把屏让给流式预览
        self._buf = []
        self._ts = 0.0
        self._live = Live(console=console, refresh_per_second=STREAM_FPS,
                          transient=True, vertical_overflow="crop")
        self._live.start()

    def on_text(self, chunk: str):
        """收到一段 delta。按 STREAM_FPS 节流刷新，否则每个 token 都要重排一遍 Markdown。"""
        if not chunk:
            return
        self._buf.append(chunk)
        now = time.time()
        if self._live is not None and now - self._ts >= 1 / STREAM_FPS:
            self._ts = now
            self._live.update(self._renderable())

    @property
    def text(self) -> str:
        return "".join(self._buf)

    def stop(self):
        if self._live is not None:
            try:
                self._live.stop()      # transient：把预览从屏幕上抹掉
            finally:
                self._live = None

    def abort(self):
        """异常/重试：擦掉半截预览，否则重试后同一段会显示两遍。"""
        self.stop()
        self._buf = []

    def commit(self, rule: bool = True) -> str:
        """正常结束：冻结成正式回答（完整 Markdown）并返回全文。"""
        text = self.text
        self.abort()
        if text.strip():
            show_answer(text, rule=rule)
        return text

    def interrupt(self) -> str:
        """Ctrl+C：已经生成的内容照常留下，不能凭空消失。"""
        text = self.text
        self.abort()
        if text.strip():
            show_answer(text)
        return text

    # ── 内部 ──
    def _renderable(self):
        """只渲染缓冲区末尾放得下的一段：Live 溢出时保留的是顶部，它自己不会跟尾部。"""
        text = self.text
        height = max(3, (console.height or 25) - 3)
        lines = text.splitlines()
        if len(lines) > height:
            text = "\n".join(lines[-height:])
            body = (Markdown(text, code_theme=CODE_THEME) if len(text) <= STREAM_MD_LIMIT
                    else Text(text))
            return Group(Text(f"⋯ 前文已滚动（共 {len(lines)} 行）", style="ui.dim"),
                         Padding(body, (0, PAD_X)))
        body = Markdown(text, code_theme=CODE_THEME) if len(text) <= STREAM_MD_LIMIT else Text(text)
        return Padding(body, (0, PAD_X))


def warn(msg: str) -> None:
    console.print(Padding(Text(f"⚠ {msg}", style="ui.accent"), (0, PAD_X)))


def info(msg: str) -> None:
    console.print(Padding(Text(str(msg), style="ui.dim"), (0, PAD_X)))


def permission_prompt(title: str, detail: str) -> bool:
    """权限确认——也走 prompt_toolkit，统一输入通道。Ctrl+C / Ctrl+D 一律算拒绝。

    没有可用控制台时（重定向、后台、评测子进程）prompt_toolkit 会直接抛
    NoConsoleScreenBufferError，它不属于 KeyboardInterrupt / EOFError。
    这种环境"问不了人"，一律按拒绝处理；绝不让异常冒到顶层——它一冒出去，
    整轮任务连同已经跑出来的中间结果全没了（真跑第一次就是这么整轮丢掉的）。
    """
    console.print(Padding(Text("[PERMISSION] ", style="bold ui.accent") + Text(str(title)), (0, PAD_X)))
    console.print(Padding(Text(str(detail), style="ui.dim"), (0, PAD_X)))
    try:
        choice = pt_prompt("  Allow? [y/N] ", style=_input_style).strip().lower()
    except (KeyboardInterrupt, EOFError):
        print()
        return False
    except Exception as e:
        info(f"没有可用的交互控制台（{type(e).__name__}），这次操作按拒绝处理")
        return False
    return choice in ("y", "yes")
