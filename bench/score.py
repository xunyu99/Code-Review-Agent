"""bench/score.py — 把一个任务的所有跑次摆在一起，对着标准答案看。

    python bench/score.py                 # 全部任务
    python bench/score.py caption-v1      # 只看一个
    python bench/score.py --root bench/runs-baseline caption-v1   # 看基线那一棵树

**它不自动判命中**。关键词匹配在这种报告上不可靠（同一个词在不同句子里意思完全不同），
所以这里只做三件事：把标准答案列出来、把每次跑的机械指标算出来、把报告的条目原样摆出来。
命中与否由人看着判——判完的数字才敢往简历上写。

机械部分（步数/token/引用）来自存档，自动；召回部分来自人，所以要留痕。
"""
import json
import io
import re
import sys
from contextlib import redirect_stdout
from pathlib import Path

BENCH = Path(__file__).resolve().parent
RUNS = BENCH / "runs"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):
    pass

sys.path.insert(0, str(BENCH))
import replay                                                # noqa: E402
import run_task                                              # noqa: E402

# 每个任务的标准答案条目（抄自各自的 .expected.md，只留标题；细节去那儿看）
KEYS = {
    "caption-v1": [
        ":19 硬编码 Linux 绝对路径",
        ":199 cfg['au_level'][au] 无保护",
        ":215 cfg['b3_p95_per_expression'][expression] 无保护",
        ":194-196 AU9 的第三种 tier 'mention'",
        ":202-225 angry+AU26 跳过 b1/b2 与 p95",
        ":321 neutral 只做精确匹配",
        ":212+:315 null 中断 + 非原子写（半截文件）",
        ":270 rng.sample 依赖池顺序",
    ],
    "caption-v2": [
        ":20 硬编码 Linux 绝对路径",
        ":261 cfg['au_level'][au] 无保护",
        ":265 cfg['au26_angry_override'] 无保护",
        ":326 抽样种子写死 20260907，--seed 无效",
        ":341-343 neutral 契约顺序倒置（先跑 mention_rows）",
        ":270+:338 null 中断 + 非原子写",
        ":256-258 AU9 的第三种 tier 'mention'",
        ":110 模板写死 'A {adj}'，绕过 article()",
    ],
    "agent-loop-s01": [
        ":77-78 丢掉 returncode",
        ":49-66 关键词黑名单可绕过",
        ":135-136/:70-76 解析与执行无保护（坏 JSON / 缺 command / shell 元字符）",
        ":83-84 while True 没有轮次上限",
        ":97 model_dump(exclude_none=True) 丢掉 content 键",
        ":34 工具描述没说跑的是 cmd.exe",
    ],
    "permission-s03": [
        ":247 safe_path 用 startswith 比前缀，同前缀兄弟目录可逃逸",
        ":150-181 两道闸门都是子串匹配，可同时绕过",
        ":239-240 丢掉 returncode",
        ":282-289 run_glob 的 '..' 列出工作目录外的文件",
        ":195 裸 input()，非交互环境 EOFError 崩",
        ":301-302 while True 没有轮次上限",
        ":350-356 硬拒绝与用户拒绝回给模型的是同一句话",
    ],
    "background-s13": [
        ":847 run_write 返回 'Wrote3chars' 少空格",
        ":827 safe_path 用 startswith 比前缀",
        ":867 run_glob 列出工作目录外的文件",
        ":819-820 丢掉 returncode",
        ":739-754+:805-807 DENY_LIST 子串匹配且重复实现",
        ":1067-1069 is_slow_operation 子串误判",
        ":1326-1328 预处理器每轮无条件跑，就地改工具结果",
        ":760 裸 input()，非交互环境崩",
        ":748 json.loads 无保护",
        ":1322 while True 没有轮次上限",
    ],
}
BULLET = re.compile(r"^[-*]\s+\S")


def run_dirs(label, root=RUNS):
    d = root / label
    if not d.exists():
        return []
    return sorted((p for p in d.iterdir() if p.is_dir() and p.name.startswith("run-")),
                  key=lambda p: int(p.name.split("-")[1]))


def summarize(run_dir):
    rec_path = run_dir / "record.json"
    rec = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.exists() else {}
    arch = run_task.newest_archive(run_dir / "home")
    mech = replay.analyze_run(arch) if arch else {}
    try:
        with redirect_stdout(io.StringIO()):     # check_session 会自己打印，这里只要它的返回值
            cit = replay.check_session(arch, False, None) if arch else {}
    except Exception:                                        # noqa: BLE001
        cit = {}
    report_path = run_dir / "report.md"
    report = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    return rec, mech, cit, report


def main(argv):
    # --root 换一棵树（基线在 bench/runs-baseline/，结构一样、label 一样）
    root, labels, rest = RUNS, [], list(argv)
    while rest:
        a = rest.pop(0)
        if a == "--root" and rest:
            root = Path(rest.pop(0)).resolve()
        else:
            labels.append(a)
    if not labels:
        labels = list(KEYS)
    for label in labels:
        if label not in KEYS:
            print(f"不认识的任务：{label}（可选：{', '.join(KEYS)}）")
            return 1
        dirs = run_dirs(label, root)
        print("=" * 78)
        print(f"{label} —— 标准答案 {len(KEYS[label])} 条，已跑 {len(dirs)} 次"
              f"{'' if root == RUNS else f'（root={root}）'}")
        print("=" * 78)
        for i, name in enumerate(KEYS[label], 1):
            print(f"   {i:>2}. {name}")
        print()
        for d in dirs:
            rec, mech, cit, report = summarize(d)
            u = rec.get("usage") or {}
            print(f"--- {d.name} ---")
            print(f"    步 {mech.get('steps', 0)} · 调用 {mech.get('calls', 0)} · "
                  f"输入 {u.get('prompt_tokens', 0)} tok · "
                  f"报告 {len(report)} 字符 · 结束 "
                  f"{'撞上限' if mech.get('steps_hit_cap') else mech.get('end_reason', '?')}")
            if cit:
                print(f"    引用 {cit.get('total', 0)} 条 · 幻觉 {cit.get('hallucination', 0)} · "
                      f"无法判定 {cit.get('unknown', 0)} · 行号存疑 {cit.get('drifted', 0)}")
            bullets = [l.strip() for l in report.splitlines() if BULLET.match(l)]
            if not bullets:
                print("    （没有报告 → 召回按 0 算）")
            else:
                print(f"    报告条目 {len(bullets)} 条（逐条对着上面的答案判命中）：")
                for b in bullets[:24]:
                    print(f"      · {b[:120]}")
                if len(bullets) > 24:
                    print(f"      ...（还有 {len(bullets) - 24} 条）")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
