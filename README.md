# Code Review Agent

终端里的代码审查 agent：给它一个仓库路径，它自己勘察项目结构、逐文件审查，最后输出带严重度分级的报告。

模型走 OpenAI 兼容接口（默认指向 DeepSeek）。工具调用循环、权限表、四层上下文压缩、记忆、技能加载全部手写，依赖只有 `openai` / `rich` / `prompt_toolkit` —— 目的是把 agent 的骨架拆开看清楚，而不是包一层框架。

## 快速开始

```bash
pip install -r requirements.txt
copy .env.example .env        # 然后把 .env 里的 key 换成你自己的
```

运行（`WORKDIR` 是「被审查的仓库」，不是本项目的目录）：

```bash
python main.py E:\some\repo                  # 新会话
python main.py                               # 不带参数就交互式问你要审查目录
python main.py resume 2026-09-15_00-20-02    # 恢复某个会话
```

交互约定：`Enter` 提交 · `Alt+Enter` 换行 · `↑↓` 翻历史 · `Ctrl+C` 中断本轮（连按两次退出）· `exit` 退出。

## 它有哪些能力

- **工具调用循环**：`bash` / `read_file` / `write_file` / `edit_file` / `glob` / `load_skill` / `load_memory`，流式输出、串行执行、结果回灌。
- **权限层**：路径级白名单，三态裁决（放行 / 问一句 / 直接拒），单点收敛在 `permissions.py`。
- **上下文压缩**：L1 裁中间 → L2 旧结果换占位符 → L3 大输出落盘 → L4 LLM 摘要，外加超限时的 reactive 兜底。
- **错误恢复**：429/529 指数退避重试、输出被 `max_tokens` 截断后的续写、上下文超限后的强制压缩。
- **技能**：`skills/*/SKILL.md` 启动时扫成注册表，模型按需 `load_skill` 拉正文（两级加载，正文不常驻上下文）。
- **记忆**：每轮结束让模型从对话里提炼可复用结论，落成 `.memory/*.md`，超过阈值自动合并去重。
- **会话**：一个会话两份文件——`<id>.jsonl` 是只追加的**原文日志**（压缩碰不到），
  `<id>.state.json` 是每轮覆盖的**工作副本**（发给模型的那份，会被压缩改写）。随时 `resume` 接着聊。

## 目录结构

```
code-review-agent/
├── main.py            入口：解析审查目录、恢复会话、交互循环
├── agent.py           主循环 + 钩子系统 + 四层压缩 + 错误恢复
├── permissions.py     权限裁决唯一出处：POLICY 表、路径解析、bash 判据
├── tools.py           工具定义与实现（只用 guard() 兜底，不写策略）
├── system_prompt.py   system prompt 组装 + 技能注册表
├── memory.py          记忆提炼 / 索引 / 合并
├── session.py         会话读写：原文日志（只追加）+ 工作副本（可压缩）
├── ui.py              终端展示层（rich + prompt_toolkit），全项目唯一直接碰终端的地方
├── config.py          所有常量的唯一出处（含「家不能当审查目标」的守卫）
├── skills/            SKILL.md 技能库
├── sessions/          会话存档（不进仓库）
├── .memory/           记忆库（不进仓库）
└── .tmp/              agent 自己的临时工作区（读写免问，不进仓库）
```

## 权限模型

一句话：**免问区只有临时区；被审查的仓库读自由、写要问；技能 / 落盘 / 记忆只读；会话、对话存档、agent 自己的家全禁；名单外一律直接拒绝。**

唯一出处是 `permissions.py` 的 `POLICY` 表，按「从上到下第一条命中」生效：

| 目录 | 读 | 写 | 说明 |
|---|---|---|---|
| `WORKDIR`（被审查的仓库） | allow | ask | 审查是读的活，改仓库先问用户 |
| `.tmp` | allow | allow | agent 自己的临时区，唯一免问的地方 |
| `skills` / `.task_outputs` / `.memory` | allow | deny | 代码写、模型读，模型不该自己写 |
| `sessions` / `.transcripts` | deny | deny | agent 自己管，模型碰不得 |
| `PROJECT_DIR`（本项目的家） | deny | deny | 源码 / `.env` / key 都在里面，整棵树禁掉 |
| 其它任何路径 | deny | deny | 名单外直接拒，连问都不问 |

两条设计上的取舍值得知道：

- **bash 的判据是「翻译」，不是「另立一套」**：先把命令翻译成「要动哪些路径、是读还是写」，再查同一张表。所以 `echo hi > notes.txt` 和 `write_file notes.txt` 得到同一个裁决（问），`type skills/.../SKILL.md` 和 `read_file` 也是（放行）。
- **bash 只是启发式网，不是沙箱**：它挡的是模型不小心越界，挡不住蓄意绕过（`python -c "open(...)"`、`powershell -EncodedCommand` 这类内联执行看不见）。真要隔离得上 Windows ACL 或受限令牌。

## 配置项

全部在 `config.py`（带 * 的可写进 `.env`）：

| 常量 | 默认 | 作用 |
|---|---|---|
| `MODEL` * | `deepseek-v4-flash` | 模型名，按账号可用值填 |
| `DEEPSEEK_API_KEY` * | — | API key，只存在 `.env` 里 |
| `MAX_TOKENS` * | 8192 | 单次回复最大 token |
| `THRESHOLD` | 200000 | 上下文压缩阈值（单位是**字符**，不是 token） |
| `MAX_STEPS` | 20 | 单轮任务最多循环几步 |
| `BASH_TIMEOUT` | 60 | bash 超时秒数 |
| `MAX_INPUT_CHARS` | 2000 | 单轮用户输入上限 |
| `MAX_RECOVERY_RETRIES` | 3 | 输出被截断后的续写次数 |
| `REPEAT_HINT_AFTER` | 5 | 同一轮里同一调用重复几次开始提醒 |

## 测试与量化

测试方案、用例清单、量化口径都写在 [TESTING.md](TESTING.md)：模型不确定的部分不进测试，边界内是纯函数（表驱动覆盖），边界外用假流模拟，需要真模型的只做手工验收。

```bash
python tests/run_tests.py     # 零依赖，没装 pytest 也能跑
pytest -q tests/              # 装了 pytest 就用这个
python bench/measure.py       # 打印量化表（离线，不调 API）
```

## 已知边界

- **文件没有回退**：`write_file` / `edit_file` 直接落盘，写前不备份；`Ctrl+C` 只保住半截回答，不撤销已写入的文件。要回退请用 git。
- **权限是路径级的，不是内容级的**：它只判断「这个路径让不让碰」，不会拦「读工作目录里的 `.env` / 密钥文件」这种行为。
- **压缩会丢信息**：L1 / L2 不可逆（旧工具结果被换成占位符），需要时模型得重新读。
- **重复调用只提醒不拦截**：到 `REPEAT_HINT_AFTER` 次开始附一句「换个做法」，防的是原地打转。
- **模型返回坏 JSON / 参数不对不崩**：转成一条 `Error: tool call failed: ...` 的工具结果回给模型自纠。
- **中文 Windows 的编码**：读文件会依次尝试 UTF-8 / GBK，编辑时按原编码写回；bash 输出按 UTF-8 → 系统默认编码解码。

## 什么不该进仓库

`.gitignore` 已经挡掉 `.env`、`__pycache__/`、`sessions/`、`.memory/`、`.tmp/`、`.task_outputs/`、`.transcripts/`。

新克隆的人只需要 `copy .env.example .env` 再填 key —— **`.env` 永远只提交副本，不提交真件**。

## git 提交仓库

Set-Alias git "E:\工作\git-local\Git\cmd\git.exe"
git --version

cd E:\learn_claude_code\code-review-agent
git init
git config user.name  "你的名字"      # 以前没配过才需要
git config user.email "你的邮箱"

git status --short                   # ← 关键一步，见下面的检查标准
git add .
git status --short
git commit -m "init: code review agent"
git branch -M main


建远程并推送
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git push -u origin main

以后日常 + 回退
git status                          # 看改了什么
git add -A && git commit -m "说明"  # 存一个点
git push

git log --oneline --graph -10        # 看历史
git checkout -- path\to\file.py      # 丢掉某个文件还没提交的改动
git restore path\to\file.py          # 同上（新写法）
git reset --hard <commit-id>         # 回到某次提交（未提交的改动会没）
