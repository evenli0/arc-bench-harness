"""并排看两次跑：节点预算、超时、工具档耗时。

修问题一/二之前那次是 `tb-v3-tool-smoke`，之后是 `tb-v3-tool-b`。
用法：python arc/compare_runs.py <旧目录> <新目录>
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FLOW = re.compile(r"\[flow\] (.*)")
BUDGET = re.compile(r"node (\d+)/(\d+) (\S+) starting \(budget (\d+)s")
# label 里带空格（`REQ-3.2 repair 1/3`），所以是非贪婪的 .+? 而不是 \S+
TURN = re.compile(r"\[flow\] (.+?) (ok|FAILED) in (\d+)s \(tools=(\d+) wrote=(\w+) verified=(\w+)\)")
DIE = re.compile(r"\[flow\] (\S+): (-?\d+)s left, below the (\d+)s a repair needs")


def turns(log: Path) -> dict:
    """一眼看去重：main.py 的 log() 同时写 stdout 和 stderr，所以每行出现两次。"""
    seen: set[str] = set()
    out: dict = {"lines": [], "budgets": {}, "turns": [], "died": [], "neg": 0}
    for raw in log.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        m = BUDGET.search(line)
        if m:
            out["budgets"][m.group(3)] = int(m.group(4))
        m = TURN.search(line)
        if m:
            out["turns"].append({"label": m.group(1), "ok": m.group(2) == "ok",
                                 "secs": int(m.group(3)), "tools": int(m.group(4)),
                                 "wrote": m.group(5) == "True"})
        m = DIE.search(line)
        if m:
            out["died"].append((m.group(1), int(m.group(2))))
            if int(m.group(2)) < 0:
                out["neg"] += 1
        if "[tool] turn overran" in line or "aborting the request" in line:
            out["lines"].append(line)
    return out


def usage(app: Path) -> tuple[int, float, float]:
    p = app / ".arc" / "llm-usage.jsonl"
    if not p.is_file():
        return 0, 0.0, 0.0
    reqs = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                reqs.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    secs = [r.get("elapsed_ms", 0) / 1000.0 for r in reqs]
    return len(reqs), sum(secs), max(secs) if secs else 0.0


def side(label: str, app: Path) -> dict:
    log = app.with_suffix(".log")
    if not log.is_file():
        log = Path(str(app) + ".log")
    info = turns(log) if log.is_file() else {"turns": [], "budgets": {}, "died": [], "neg": 0, "lines": []}
    n, total, worst = usage(app)
    print(f"── {label}  ({app.name})")
    print(f"   请求 {n} 个，模型合计 {total:.0f}s，最慢单次 {worst:.1f}s")
    print(f"   节点预算: " + ", ".join(f"{k}={v}s" for k, v in info["budgets"].items()))
    print(f"   预算被打成负数: {info['neg']} 次")
    for lbl, left in info["died"]:
        print(f"     {lbl}: {left}s 剩")
    print("   回合:")
    for t in info["turns"]:
        print(f"     {'ok ' if t['ok'] else 'FAIL'} {t['label']:34} {t['secs']:>4}s "
              f"tools={t['tools']:<3} wrote={t['wrote']}")
    if info["lines"]:
        print("   超时相关:")
        for line in info["lines"]:
            print(f"     {line}")
    return {"reqs": n, "total": total, "worst": worst, "neg": info["neg"],
            "turns": len(info["turns"]),
            "ok": sum(1 for t in info["turns"] if t["ok"]),
            "max_turn": max((t["secs"] for t in info["turns"]), default=0)}


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    old = side("修复前", ROOT / sys.argv[1])
    print()
    new = side("修复后", ROOT / sys.argv[2])
    print()
    print("── 差别")
    print(f"   最慢单次请求  {old['worst']:.1f}s -> {new['worst']:.1f}s")
    print(f"   预算负数次数  {old['neg']} -> {new['neg']}")
    print(f"   单回合最长    {old['max_turn']}s -> {new['max_turn']}s")
    print(f"   成功回合      {old['ok']}/{old['turns']} -> {new['ok']}/{new['turns']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
