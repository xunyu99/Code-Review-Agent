"""
permissions.py — 全项目唯一的权限裁决处：谁能读、谁能写、要不要问。

一句话模型（对人和对模型都是这一句）：
    免问区只有一个 —— agent 自己的临时区（TMP_DIR），读写都免问；
    被审查的仓库读自由、写要问；skills / tool results / memory 只读；
    sessions / transcripts / agent 自己的家 全禁；名单外一律直接拒绝。

为什么单独一个模块：以前权限散在三处 —— tools.py 的 root 列表、agent.py 的
permission_hook、system_prompt.py 手抄的路径清单，三份说法迟早对不上（事实
上已经对不上：白名单里有 sessions、没有 .memory，读白名单其实等于免问）。
现在裁决表、bash 启发式、给模型看的路径说明全由这里生成，别的模块只调用、不复制。

这张表管的是「模型通过工具能碰什么」。agent 自己的内部存取（存会话、写记忆、
L3 落盘、写 transcript）走内部代码，不经过这里，也不该经过。
"""
import re
from pathlib import Path
from config import (PROJECT_DIR, CODE_DIR, WORKDIR, SKILLS_DIR, SESSIONS_DIR, OUTPUT_DIR,
                     TMP_DIR, TRANSCRIPTS_DIR, MEMORY_DIR, ARCHIVE_DIR)

# 家的整棵树 + 代码目录整棵树都要禁掉（源码 / .env / keys）。
# 两者默认是同一个目录。评测时用 AGENT_HOME 把家搬到别处，代码目录就落到下面
# (WORKDIR, allow, ask) 那条规则上去了 —— 那时它变成「可读」，`.env` 里的 key
# 和权限表本身就读得到。所以必须补这一条，且位置要在私有目录之后、WORKDIR 之前。
_PRIVATE_DIRS = [(PROJECT_DIR, "deny", "deny", "the agent's own home (source, .env, keys)")]
if CODE_DIR != PROJECT_DIR:
    _PRIVATE_DIRS.append((CODE_DIR, "deny", "deny", "the agent's code dir (source, .env, keys)"))

# ── 裁决表：唯一出处。改权限只动这里 ──
# 规则按「从上到下第一条命中」生效，所以具体目录必须排在 WORKDIR 之前：
# WORKDIR 有可能是 agent 家的父目录，靠顺序先把私有目录摘出去。
#   读/写 ∈ allow（放行）| ask（弹窗问用户）| deny（直接拒绝，连问都不问）
# 第 4 列是给人看的名字，也直接进 system prompt —— 提示词不可能再和裁决表漂移。
POLICY = (
    # 根目录              读       写       名字（进 system prompt）
    (SESSIONS_DIR,    "deny",  "deny",  "sessions"),
    (ARCHIVE_DIR,     "deny",  "deny",  "archives"),
    (TRANSCRIPTS_DIR, "deny",  "deny",  "transcripts"),
    (TMP_DIR,         "allow", "allow", "your temp dir"),
    (SKILLS_DIR,      "allow", "deny",  "skills"),
    (OUTPUT_DIR,      "allow", "deny",  "tool results"),
    (MEMORY_DIR,      "allow", "deny",  "memory"),
    # 家整棵树禁掉（源码 / .env / 一切私产）。必须在上面几条之后、WORKDIR 之前：
    # 家的子目录已经各自有规则，剩下的部分（agent.py、permissions.py、.env…）
    # 才是这条要挡的 —— 少这条，workdir 指向家的父目录时 .env 和源码就是可读可写的，
    # 模型能改自己的权限表，围栏直接失效。
    *_PRIVATE_DIRS,
    (WORKDIR,         "allow", "ask",   "the working directory (the repo under review)"),
)

# 给模型看的说明：规则 → 人话，以及先展示哪一组（宽松的在前）
_RULE_TEXT = {
    ("allow", "allow"): ("Read & write, no approval needed", 0),
    ("allow", "ask"):   ("Read freely, writing needs your approval", 1),
    ("allow", "deny"):  ("Read only, no approval needed", 2),
    ("deny", "deny"):   ("Off-limits, never", 3),
}

# ── 高危命令：硬拒（run_bash 也查一次，双保险）──
DENY_LIST = [
    "rm -rf /", "sudo", "shutdown", "reboot",
    "mkfs", "dd if=", "> /dev/sda",
    "format", "rmdir /s", "del /f /s",
]


# ══════════════════════ 路径裁决 ══════════════════════

def resolve_path(path) -> Path:
    """把工具给的路径解析成绝对路径：相对路径一律基于 WORKDIR，~ 展开，软链解析。"""
    p = Path(str(path)).expanduser()
    if not p.is_absolute():
        p = WORKDIR / p
    return p.resolve()


# ── 密钥文件：一律不碰（读和写都拒）──
# 为什么单独一条：POLICY 是按**路径前缀**匹配的，而「密钥文件」是按**文件名**匹配的
# （任何目录下的 .env 都算），所以它写不成 POLICY 里的一行，而是 classify_path 里的
# 第一道闸。好处是 read_file / write_file / edit_file / bash 全部自动同判：
#     read_file .env  ==  type .env  ==  findstr … .env
#
# 真事（CLI 模式下审一个真实项目时发现的）：CLI 不拷副本，WORKDIR 就是
# 真目录，而权限表放行「读工作目录内的文件」—— 于是那个项目 .env 里的 4 个 API key
# 是可读的。读进去就会进 archives，并随下一次请求发给模型服务商。评测路径（run_task）
# 靠"不把 .env 拷进副本"挡住了，CLI 路径没有，这条闸两个路径都覆盖。
SECRET_FILENAME_EXACT = {
    ".netrc", ".htpasswd", ".npmrc", ".pypirc", ".git-credentials", ".authinfo.gpg",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "secrets.json",
}
# `.env` 也在后缀里：`prod.env` / `config.env` / `secrets.env` 这类同样装密钥，
# 只按「以 .env 开头」判会漏掉它们。代价是偶尔误伤一个非密钥的 `sample.env`——
# 这个方向上的不对称是故意的：多拒一个文件只是让模型换条路，漏读一次密钥就是
# 它随下一次请求发给模型服务商。
SECRET_FILENAME_SUFFIXES = (".env", ".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")
SECRET_DIR_MARKERS = (".ssh", ".aws", ".gnupg")


def _is_secret_path(p: Path) -> bool:
    """密钥文件判定。

    `.env.example` / `.sample` / `.template` **不算** —— 审查本来就要看键名，
    把示例文件也拒了等于把正常工作挡在外面。
    """
    # lstrip("*")：bash 里 `type *.env`、`more *.pem` 这种通配写法也要认出来
    name = p.name.lower().lstrip("*")
    if name.startswith(".env"):
        return not name.endswith((".example", ".sample", ".template"))
    if name in SECRET_FILENAME_EXACT or name.endswith(SECRET_FILENAME_SUFFIXES):
        return True
    return any(part.lower() in SECRET_DIR_MARKERS for part in p.parts[:-1])


def is_secret_filename(name: str) -> bool:
    """按**文件名**判密钥，给"过滤文件列表"的调用方用（比如 run_task 拷仓库时）。

    和 `_is_secret_path` 同一份定义 —— 两处各写一份，早晚会漂（这个项目已经吃过一次）。
    """
    return _is_secret_path(Path(name))


def deny_reason(path, resolved: Path) -> str:
    """deny 时回给模型的理由。

    密钥文件必须说清楚「该看哪个文件」，否则它会当成普通越界、换个写法继续试
    （白白烧步数）。这也和"拒绝理由要能指导下一步"那条一致。
    """
    if _is_secret_path(resolved):
        return (f"Permission denied: '{path}' is a secret file (API keys / private keys) "
                f"— it is never read or modified. Read the example file instead "
                f"(e.g. .env.example), or ask the user.")
    return f"Permission denied: '{path}' is outside allowed directories"


def classify_path(path, mode: str = "read") -> tuple[str, Path]:
    """返回 (裁决, 绝对路径)。裁决 ∈ {"allow", "ask", "deny"}；mode ∈ {"read", "write"}。

    名单外（没有任何一条 POLICY 命中）→ deny。写模式的 ask 只出现在 WORKDIR：
    对着自己的临时区写免问，对着被审查的仓库写要问一句，对着别处写直接拒。
    """
    resolved = resolve_path(path)
    if _is_secret_path(resolved):        # 密钥文件先判：不管在哪个目录、读还是写
        return "deny", resolved
    for root, read_rule, write_rule, _name in POLICY:
        if resolved.is_relative_to(root):
            return (read_rule if mode == "read" else write_rule), resolved
    return "deny", resolved


def guard(path, mode: str = "read") -> Path:
    """工具实现的兜底门闩：命中 allow / ask 才放行，名单外抛 PermissionError。

    ask 放行是故意的 —— 「要不要问用户」只有 agent.permission_hook 做得了
    （它才有弹窗），这里只保证名单外的路径一定进不来。
    """
    verdict, resolved = classify_path(path, mode)
    if verdict == "deny":
        raise PermissionError(deny_reason(path, resolved))
    return resolved


# ══════════════════════ bash 启发式 ══════════════════════
# bash 是个「什么都能干」的逃逸口，只能靠启发式网兜。它挡的是「模型不小心越界」，
# 挡不住处心积虑的绕过；真沙箱得上 ACL 或受限令牌。
# 办法是「翻译」而不是「另立一套判据」：先把命令翻成 (路径, 读还是写)，再去查 POLICY，
# 所以同一个动作换个工具不会得出两个结果。翻不出来、或者命令里有解释器内联执行时，
# 翻译能力就到头了（这是已知上限，不是漏洞兜底）。
# 判定一律走 token 不走子串 —— "model" 里有 "del "、"forward" 里有 "rd "、
# "cmd /c" 会被 "md " 命中，子串匹配既漏又误伤。
_WRITE_VERBS = {
    "rm", "rmdir", "del", "erase", "rd", "md", "mkdir", "mv", "move", "cp", "copy",
    "xcopy", "robocopy", "tee", "touch", "truncate", "chmod", "chown", "attrib",
    "icacls", "cacls", "takeown", "reg", "schtasks", "install",
    "out-file", "set-content", "add-content", "clear-content", "new-item",
    "copy-item", "move-item", "remove-item", "rename-item",
}
_WRITE_PHRASES = ("git apply", "git checkout", "git reset", "git clean",
                  "pip install", "npm install", "sed -i")
_DESTRUCTIVE_VERBS = {"rm", "rmdir", "del", "erase", "rd", "remove-item",
                      "clear-content", "format", "takeown", "icacls", "cacls"}
_ENV_REF = re.compile(r"(%[^%\s]+%|\$env:|\$[A-Za-z_])", re.I)


def bash_tokens(cmd: str) -> list[str]:
    """切 token：先去引号（免得带空格的路径被劈成两半），再按空白和 shell 分隔符切。"""
    return [t for t in re.split(r"[\s;,&|=()]+", re.sub(r"[\"']", " ", cmd)) if t]


def _verb(token: str) -> str:
    """取 token 的命令名：去路径前缀、去 .exe、转小写（看它是不是写/删类命令）。"""
    base = token.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def _has_write_verb(cmd: str, tokens: list[str]) -> bool:
    """命令本身是不是写类命令（Set-Content / Remove-Item / copy / git apply…）。

    是写类命令 → 它带的那些路径参数就是写目标（`Remove-Item x` 里的 x）。
    """
    if any(v in _WRITE_VERBS for v in map(_verb, tokens)):
        return True
    lower = cmd.lower()
    return any(p in lower for p in _WRITE_PHRASES)


def _redirect_targets(cmd: str) -> list[str]:
    """重定向的写目标：`>` `>>` 后面紧跟的那个 token。

    `2>&1` 不算（`&` 不在目标字符集里，匹配不上就是没目标）。
    单独写这条是为了分清「命令在写哪儿」和「命令在读哪儿」：
    `pytest tests/ > log.txt` 里 tests/ 是读、log.txt 是写，别把前者也当成写。
    """
    targets = []
    for m in re.finditer(r">>?\s*(\"[^\"]*\"|'[^']*'|[^\s;,&|]+)", cmd):
        token = m.group(1).strip("\"'")
        if token:
            targets.append(token)
    return targets


def _verb_write_targets(tokens: list[str]) -> list[str]:
    """写类命令的目标参数：紧跟写类命令的第一个不是开关的参数。

    `Set-Content notes.txt hi` 里的 notes.txt 不带分隔符，光靠「长得像路径」挑不出来，
    必须按位置取。只取第一个就够 —— 不带分隔符的名字一定落在 workdir 里（相对路径按
    workdir 解析），多取几个改变不了裁决，只会让弹窗里的路径列表变长。
    """
    for i, token in enumerate(tokens):
        if _verb(token) not in _WRITE_VERBS:
            continue
        after_flag = False
        for nxt in tokens[i + 1:]:
            if nxt.startswith("-") or re.fullmatch(r"/[A-Za-z]{1,2}", nxt):
                after_flag = True                 # -Force / /s / -ItemType 这类开关
                continue
            if after_flag:                        # 开关的取值（-ItemType File 的 File），不是目标
                after_flag = False
                continue
            return [nxt]
    return []


def _looks_destructive(tokens: list[str]) -> bool:
    """删除 / 改权限类命令：不碰自己临时区时问一句。"""
    return any(v in _DESTRUCTIVE_VERBS for v in map(_verb, tokens))


# 这些命令的第一个非开关参数是**搜索串/正则**，不是路径。
# 不排掉它的话，`findstr /v "\.git\\"` 里的 `\.git\\` 会被当成路径 —— 而它以 `\` 开头，
# 在 Windows 上是「根相对」，resolve 之后变成 `<当前盘>:\.git`，落到白名单之外，
# **整条命令被误拒**。真跑就栽在这：模型想排除 .git，结果勘察命令被拒，
# 拿不到文件行数，只能默认读 1000 行，后面全乱。
_PATTERN_COMMANDS = {"findstr", "find", "grep", "rg", "egrep", "fgrep", "select-string"}


def _pattern_operands(tokens: list[str]) -> set[int]:
    """标出「搜索串」所在的下标（每条模式匹配命令的第一个非开关参数）。"""
    idx: set[int] = set()
    for i, t in enumerate(tokens):
        if _verb(t) not in _PATTERN_COMMANDS:
            continue
        pos = i + 1
        while pos < len(tokens) and (tokens[pos].startswith("-")
                                     or re.fullmatch(r"/[A-Za-z]{1,2}", tokens[pos])):
            pos += 1
        if pos < len(tokens):
            idx.add(pos)
    return idx


def _bash_paths(tokens: list[str], skip: set[int] | None = None) -> list[str]:
    """从 token 里挑出路径：绝对路径 / UNC / ~ 开头 / 含分隔符 / 含 ..。

    故意宽松 —— 宁可多认出几个路径去问用户，也不要漏掉一个越界写。
    skip 里是「搜索串」的下标，它们长得像路径但不是路径。
    """
    skip = skip or set()
    paths = []
    for i, t in enumerate(tokens):
        if i in skip:
            continue
        # 空 token、-rf 这种开关、cmd 的 /c /s /q 这种斜杠开关：都不是路径
        if not t or t.startswith("-") or re.fullmatch(r"/[A-Za-z]{1,2}", t):
            continue
        if (re.match(r"^[A-Za-z]:[\\/]", t)       # C:\  C:/
                or t.startswith("\\\\")            # \\server\share
                or t.startswith("~")
                or "\\" in t or "/" in t
                or ".." in t):
            paths.append(t)
            continue
        # 没有分隔符的**裸文件名**也可能是密钥：`type .env`、`more id_rsa`、`type *.pem`。
        # 这一条是补漏：真事（2026-09-27）证明 `type .env` 会被判成 allow ——
        # 因为 `.env` 既没有分隔符也没有扩展名，上面那串启发式一个都不命中。
        if _is_secret_path(Path(t)):
            paths.append(t)
    return paths


def opaque_paths(tokens: list[str]) -> list[str]:
    """带环境变量引用、解析不出真实位置的路径（%TEMP%\\x、$env:TEMP\\x）。

    解析不出来就不能算「在 workdir 里面」—— 读要问、写要拒，
    而不是当成「在 workdir 里」悄悄放过。
    """
    return [t for t in _bash_paths(tokens) if _ENV_REF.search(t)]


def _decide_bash(cmd: str) -> tuple[str, str, str]:
    """bash 的裁决：先把命令翻译成「要动哪些路径、是读还是写」，再逐条查同一张 POLICY 表。

    翻译规则（宁严不宽）：
      - 写类命令（Set-Content / Remove-Item / copy / git apply…）→ 它带的路径全算写
      - 重定向 > >> 后面那个 token → 算写
      - 其余路径 → 算读
    这样同一个动作换个工具不会得出两个结果：
      `echo hi > notes.txt` 与 write_file notes.txt 同判（仓库内写 → 问），
      `type <skills>\\SKILL.md` 与 read_file 同判（白名单读 → 放行）。
    多条命中时取最严的一条作为命令的最终裁决。
    """
    lower = cmd.lower()
    for pattern in DENY_LIST:
        if pattern in lower:
            return "deny", "", f"Permission denied: '{pattern}' is forbidden"

    tokens = bash_tokens(cmd)
    write_cmd = _has_write_verb(cmd, tokens)
    targets = _redirect_targets(cmd) + _verb_write_targets(tokens)
    opaque = opaque_paths(tokens)

    # 候选路径 = 普通 token + 重定向目标（后者能正确处理带引号的写法）
    candidates = _bash_paths(tokens, _pattern_operands(tokens))
    candidates += [t for t in targets if t not in candidates]

    hits: list[tuple[str, Path]] = []             # [(裁决, 绝对路径)]
    for raw in candidates:
        try:
            resolved = resolve_path(raw)
        except Exception:
            continue                              # 解析不了就不是路径，交给 DENY_LIST 兜
        if any(resolved == seen for _v, seen in hits):
            continue
        mode = "write" if (write_cmd or raw in targets) else "read"
        hits.append((classify_path(resolved, mode)[0], resolved))

    # 名单外的路径：连问都不问
    for verdict, resolved in hits:
        if verdict == "deny":
            return "deny", "", deny_reason(resolved, resolved)
    # 要写、但写向一个解析不出来的位置（环境变量拼接）→ 直接拒
    if (write_cmd or targets) and opaque:
        return "deny", "", f"Permission denied: cannot verify write target: {', '.join(opaque)}"

    destructive = str(TMP_DIR).lower() not in lower and _looks_destructive(tokens)
    ask_paths = [str(resolved) for verdict, resolved in hits if verdict == "ask"]
    if ask_paths or opaque or destructive:
        detail_lines = ask_paths + [f"{t}  (unresolved path)" for t in opaque]
        if destructive:
            detail_lines.append("(destructive command)")
        if write_cmd or targets:
            title = "Write inside the reviewed repo"
        elif destructive:
            title = "Destructive command"
        else:
            title = "Command touches an unresolved path"
        return "ask", title, "\n".join(detail_lines) or cmd

    return "allow", "", ""


# ══════════════════════ 总裁决 ══════════════════════

# 不接受路径/命令的工具：它们出不去是靠实现约束，不是靠这里的裁决
#   glob         → WORKDIR.glob() + 结果 resolve() 过滤（并且拒 ".." pattern）
#   load_skill   → 只查 SKILL_REGISTRY 注册表，不拼模型给的路径
#   load_memory  → 只查 list_memory_files() 里的名字，不拼路径
_NO_PATH_TOOLS = ("glob", "load_skill", "load_memory")


def decide(tool_name: str, args: dict) -> tuple[str, str, str]:
    """工具调用的总裁决：返回 (verdict, 弹窗标题, 说明)。

    verdict ∈ {"allow", "ask", "deny"}。调用方（agent.permission_hook）只做三件事：
    allow 放行 / ask 弹窗（拒绝就回 "Permission denied by user"）/ deny 把说明当理由回给模型。

    未知工具名一律 deny（fail-closed）：将来加工具忘了登记，是「用不了」而不是「免检」。
    """
    if tool_name == "bash":
        return _decide_bash(str(args.get("command") or ""))
    if tool_name in ("read_file", "write_file", "edit_file"):
        path = str(args.get("path") or "")
        mode = "read" if tool_name == "read_file" else "write"
        try:
            verdict, resolved = classify_path(path, mode)
        except Exception as e:
            return "deny", "", f"Invalid path: {path} ({type(e).__name__})"
        if verdict == "deny":
            return "deny", "", deny_reason(path, resolved)
        if verdict == "ask":
            title = "Read outside workdir" if mode == "read" else "Write outside workdir"
            return "ask", title, str(resolved)
        return "allow", "", ""
    if tool_name in _NO_PATH_TOOLS:
        return "allow", "", ""
    return "deny", "", f"Permission denied: tool '{tool_name}' is not registered in the permission table"


# ══════════════════════ 给 prompt 用 ══════════════════════

def scope_lines() -> str:
    """把 POLICY 渲染成给模型看的路径说明。提示词和裁决表同源，不可能对不上。"""
    groups: dict[tuple[str, str], list[str]] = {}
    for root, read_rule, write_rule, name in POLICY:
        groups.setdefault((read_rule, write_rule), []).append(name)
    ordered = sorted(groups, key=lambda rule: _RULE_TEXT.get(rule, ("", 9))[1])
    lines = [
        f"{_RULE_TEXT.get(rule, (f'read={rule[0]}, write={rule[1]}', 9))[0]}: "
        + " · ".join(groups[rule])
        for rule in ordered
    ]
    lines.append("Anything else: refused, no prompt.")
    lines.append("Secret files are never read or modified, anywhere: .env / *.env / *.pem / "
                 "*.key / id_rsa … (the .example / .sample / .template variants are fine).")
    return "\n".join(lines)
