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
python main.py D:\my-project                                        # 新会话（启动CLI终端界面）
python main.py                               # 不带参数就交互式问你要审查目录
python main.py resume 2026-09-15_00-20-02    # 恢复某个会话（启动CLI终端界面）
```

交互约定：`Enter` 提交 · `Alt+Enter` 换行 · `↑↓` 翻历史 · `Ctrl+C` 中断本轮（连按两次退出）· `exit` 退出。

## 它有哪些能力

- **工具调用循环**：`bash` / `read_file` / `write_file` / `edit_file` / `glob` / `load_skill` / `load_memory`，流式输出、串行执行、结果回灌。
- **权限层**：路径级白名单，三态裁决（放行 / 问一句 / 直接拒），单点收敛在 `permissions.py`。
- **上下文压缩**：L1 裁中间 → L2 旧结果换占位符 → L3 大输出落盘 → L4 LLM 摘要，外加超限时的 reactive 兜底。
- **错误恢复**：429/529 指数退避重试、输出被 `max_tokens` 截断后的续写、上下文超限后的强制压缩。
- **技能**：`skills/*/SKILL.md` 启动时扫成注册表，模型按需 `load_skill` 拉正文（两级加载，正文不常驻上下文）。
- **记忆**：每轮结束让模型从对话里提炼可复用结论，落成 `.memory/*.md`，超过阈值自动合并去重。**只有正常跑完的一轮才提炼**——被中断 / 报错 / 撞上 `MAX_STEPS` 的轮次没有结论（撞上限那轮即使被抢救出一份报告也不算），不往长期记忆里灌。
- **会话**：一个会话拆成两棵树——`sessions/` 放每轮覆盖的**工作副本**（发给模型的那份，会被压缩改写），
  `archives/` 放只追加的**原文日志**（压缩碰不到）。一句话：**上下文会被压缩，存档不会。** 随时 `resume` 接着聊。

## 目录结构

```
code-review-agent/
├── main.py            入口：解析审查目录、恢复会话、交互循环
├── agent.py           主循环 + 钩子系统 + 四层压缩 + 错误恢复
├── permissions.py     权限裁决唯一出处：POLICY 表、路径解析、bash 判据
├── tools.py           工具定义与实现（只用 guard() 兜底，不写策略）
├── system_prompt.py   system prompt 组装 + 技能注册表
├── memory.py          记忆提炼 / 索引 / 合并
├── session.py         会话读写：archives/ 原文日志（只追加）+ sessions/ 工作副本（可压缩）
├── ui.py              终端展示层（rich + prompt_toolkit），全项目唯一直接碰终端的地方
├── config.py          所有常量的唯一出处（含「家不能当审查目标」的守卫）
├── skills/            SKILL.md 技能库
├── sessions/          会话的当前上下文（不进仓库）
├── archives/          会话的原文存档（不进仓库）
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
| `sessions` / `archives` / `.transcripts` | deny | deny | agent 自己管，模型碰不得 |
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
| `STEP_WARN_REMAINING` | 5 | 剩余几步时，提示从「该收尾了」升级成「停止调工具、开始写报告」 |
| `STEP_BUDGET_NOTICE` | on | 关掉（`off`）就完全不追加步数预算提示 —— 消融实验用 |
| `SALVAGE_ON_MAX_STEPS` | True | 撞上 `MAX_STEPS` 后再花一次调用，逼它用已有信息抢救一份报告 |
| `REPORT_CHARS_LIMIT` | 3000 | 报告总长上限（写进提示词，靠模型自觉；**不是**靠 `MAX_TOKENS` 截断） |
| `BASH_TIMEOUT` | 60 | bash 超时秒数 |
| `MAX_INPUT_CHARS` | 2000 | 单轮用户输入上限 |
| `MAX_RECOVERY_RETRIES` | 3 | 输出被截断后的续写次数 |
| `REPEAT_HINT_AFTER` | 5 | 同一轮里同一调用重复几次开始提醒 |

`STEP_WARN_REMAINING` 和 `SALVAGE_ON_MAX_STEPS` 是一对：平时的预算提示是**劝**（每步在发给 API 的消息末尾追加一条剩余步数，见 `agent.step_budget_notice`），撞上限后的抢救是**兜底**（`agent.salvage_report`）。抢救出的报告排在 `[Error] max steps` 之前，所以 `bench` 会同时记下「报告字数 > 0」和「结束原因是撞上限」——有产出，但不算成功。

`REPORT_CHARS_LIMIT` 单独说一句：**别用 `MAX_TOKENS` 去管报告长度**。它管的是「单次回复生成到哪停下」，报到上限会被 `finish_reason="length"` 接住、走续写分支，把整段对话重发一遍换后半截 —— 报告没变短，输入 token 反而翻倍。实测那两份 6,292 / 8,310 字的报告整轮输出才 4,579 token，离 8192 差得远：它写这么长是没人告诉它别写。

这三项（`STEP_BUDGET_NOTICE` / `REPORT_CHARS_LIMIT` / `STEP_WARN_REMAINING`）都支持**环境变量覆盖**，所以消融实验不用改代码：

```powershell
$env:STEP_BUDGET_NOTICE = "off"     # 摘掉步数预算提示
$env:REPORT_CHARS_LIMIT = "0"       # 摘掉报告长度约束（0 = 完全不提）
python bench\run_task.py --label caption-v1-ablation ...
```

这条约束的**措辞和数字都被真跑改过**：1500 那版写的是「最多 5 条，宁可丢掉弱的」，复测发现字数砍到 1/3、召回也从 6.5/8 掉到 3.5/8 —— 那句话等于授权模型少报。现在改成「压每一条的写法，不许砍掉有证据的发现」，条数放宽到 8，数字放到 3000。

## 测试与量化

原则是**模型不确定的部分不进单测**：边界内全是纯函数（表驱动覆盖），边界外用假响应模拟，需要真模型的只做手工验收。测试全程用占位 key、不联网、也绝不读 `.env` 里的真 key（见 `tests/_bootstrap.py`）。

```bash
python tests/run_tests.py     # 零依赖，没装 pytest 也能跑
pytest -q tests/              # 装了 pytest 就用这个
```

带真模型跑的评测放在 `bench/`（要 API key 和网络，跑之前先想清楚花销）。`--repo` 指到哪个仓库就审哪个：

```bash
python bench/run_task.py --label my-task --repo D:\path\to\repo   # 跑主 agent
python bench/baseline.py --label my-task --repo D:\path\to\repo   # 跑基线：同样记账，换成 60 行裸循环
python bench/score.py my-task                                     # 看主 agent 这轮的结果
python bench/score.py --root bench/runs-baseline my-task          # 看基线那轮
```

**评测任务夹具不在仓库里**（`bench/tasks/` 已被 `.gitignore` 挡掉）——自己指一个仓库去跑就行，结论对任何仓库都成立。

`bench/baseline.py` 是**对照跑道**：同一批任务、同一套记账、同一个 `MAX_STEPS`，只把 agent 实现换成裸循环（没有技能 / 记忆 / 压缩 / 预算提示 / 撞上限兜底）。它落在 `bench/runs-baseline/`，不碰 `bench/runs/`。剥掉了什么、保留了什么，`bench/bare_worker.py` 开头有张表——那张表就是口径。

## 已知边界

- **文件没有回退**：`write_file` / `edit_file` 直接落盘，写前不备份；`Ctrl+C` 只保住半截回答，不撤销已写入的文件。要回退请用 git。
- **权限是路径级的，不是内容级的**：它只判断「这个路径让不让碰」，不会扫文件内容。但有**一条内容无关的例外**：`.env` / `*.env` / `*.pem` / `*.key` / `id_rsa` 这类**按文件名就能认出的密钥文件，任何目录下都不给读也不给写**（`.env.example` 这类示例文件放行）。拦不住的是蓄意绕过 —— `python -c "open('.env').read()"` 这种内联执行看不见。
- **压缩会丢信息**：L1 / L2 不可逆（旧工具结果被换成占位符），需要时模型得重新读。
- **重复调用只提醒不拦截**：到 `REPEAT_HINT_AFTER` 次开始附一句「换个做法」，防的是原地打转。
- **模型返回坏 JSON / 参数不对不崩**：转成一条 `Error: tool call failed: ...` 的工具结果回给模型自纠。
- **中文 Windows 的编码**：读文件会依次尝试 UTF-8 / GBK，编辑时按原编码写回；bash 输出按 UTF-8 → 系统默认编码解码。

## 什么不该进仓库

`.gitignore` 已经挡掉 `.env`、`__pycache__/`、`sessions/`、`archives/`、`.memory/`、`.tmp/`、`.task_outputs/`、`.transcripts/`，以及评测的**运行记录和任务夹具**（`bench/runs/`、`bench/runs-baseline/`、`bench/tasks/`）——那些是本地数据，体积大、还带本机绝对路径，不该进仓库。

新克隆的人只需要 `copy .env.example .env` 再填 key —— **`.env` 永远只提交副本，不提交真件**。
