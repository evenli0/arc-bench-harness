#!/usr/bin/env python3
"""本地跑一道题。

云端这些准备是平台做的：设 ARCBENCH_TASK_DIR / ARCBENCH_WEB_PORT，
把公开测试挂到 /workspace/tests，注入密钥。这里手工把同一套环境摆好，
好让交付前能在本地看见真实结果 —— 而不是上传之后才知道。

    python run-local.py                 # 跑 ./task，公开测试取 ./tests
    python run-local.py --check         # 只验配置和密钥，不花 token
    python run-local.py --task <目录> --tests <目录> --out <目录>

密钥从同目录 .env 读，绝不打印。**打包时 .env 不能进 zip。**
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import uuid
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARC = ROOT / "arc"

# Windows 控制台默认 GBK，一行 ✓ 就能把整个脚本崩在最后一步。
# 强制 UTF-8 并允许替换，输出的可读性不该取决于终端用的是什么代码页。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # 老 Python / 被重定向过的流
        pass


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def masked(value: str) -> str:
    return f"{value[:6]}…{value[-4:]}" if len(value) > 12 else "(未设置)"


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def probe_endpoint(base: str, key: str) -> str:
    """GET /models：不计费，只证明端点活着。"""
    req = urllib.request.Request(base.rstrip("/") + "/models",
                                 headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return f"HTTP {resp.status}（端点在线）"
    except urllib.error.HTTPError as exc:
        if exc.code < 500:
            return f"HTTP {exc.code}（端点在线）"
        return f"HTTP {exc.code}（端点报错）"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description="本地跑一道 ARC 题")
    parser.add_argument("--task", default=str(ROOT / "task"), help="需求目录（含 requirements.yaml）")
    parser.add_argument("--tests", default=str(ROOT / "tests"), help="公开验收测试目录（含 *.spec.ts）")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    parser.add_argument("--out", default=None, help="交付目录，默认 arc-output/<任务名>-<短hash>")
    parser.add_argument("--port", type=int, default=43100, help="判分端口（应用必须站在这里）")
    parser.add_argument("--smoke-port", type=int, default=None, help="自测端口，默认 port+1")
    parser.add_argument("--template", default=None, help="已生成的 app，复制进来做 evolution")
    parser.add_argument("--time-budget", type=int, default=None, help="整轮墙钟秒数")
    parser.add_argument("--node-timeout", type=int, default=None, help="单节点秒数")
    parser.add_argument("--check", action="store_true", help="只验配置，不跑模型")
    args = parser.parse_args()

    env_file = load_env(Path(args.env))
    key = env_file.get("OPENAI_API_KEY") or ""
    base = env_file.get("OPENAI_BASE_URL") or ""
    model = env_file.get("MODEL") or "deepseek-v4-flash"

    task = Path(args.task).resolve()
    tests = Path(args.tests).resolve()

    print(f"需求：{task / 'requirements.yaml'}")
    print(f"测试：{tests}")
    print(f"模型：{model}")
    print(f"密钥：{masked(key)}")
    print(f"端点：{base}")

    problems: list[str] = []
    if not key:
        problems.append(f"没有密钥：{args.env} 里缺 OPENAI_API_KEY")
    if not base:
        problems.append(f"没有端点：{args.env} 里缺 OPENAI_BASE_URL")
    if not (task / "requirements.yaml").is_file():
        problems.append(f"缺需求文件：{task / 'requirements.yaml'}")
    if not (ARC / "main.py").is_file():
        problems.append(f"缺 arc/main.py：{ARC / 'main.py'}")
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        return 2

    # 测试目录是**可选**的，而且是刻意的：正赛根本没有它。
    #
    # 原先这里是一条硬失败 —— 「测试目录里没有 *.spec.ts」直接退出。于是本地
    # 永远在「自带公开卷子」的条件下跑，而正赛的卷子是隐藏的，两者的行为差别
    # 正好落在验收机制上（见 arc/main.py 的 author_acceptance_suite）。一条只在
    # 本地成立的前置条件，会把最需要演练的那条路径挡在门外。
    #
    # 没有卷子时不设 ARCBENCH_TESTS_DIR：`locate_acceptance_tests()` 找不到，
    # `tests_dir` 是 None，harness 自己按需求树出一份 —— 和判分机上一模一样。
    has_tests = any(tests.rglob("*.spec.ts"))
    if has_tests:
        print(f"公开测试：{len(list(tests.rglob('*.spec.ts')))} 份（{tests}）")
    else:
        print(f"公开测试：无（{tests}）—— 按正赛赛制跑：harness 自己出卷")

    if key and base:
        print(f"端点探测：{probe_endpoint(base, key)}")

    smoke = args.smoke_port or args.port + 1
    for p in (args.port, smoke):
        if not port_free(p):
            print(f"  ✗ 端口 {p} 被占用，先腾出来")
            return 2

    if args.check:
        print("\n✓ 配置齐全（--check，没跑模型）")
        return 0

    out = Path(args.out).resolve() if args.out else \
        ARC / "arc-output" / f"{task.name}-{uuid.uuid4().hex[:8]}"
    if args.template:
        template = Path(args.template).resolve()
        if not (template / "frontend").is_dir() or not (template / "backend").is_dir():
            print(f"  ✗ 模板缺 frontend/ 或 backend/：{template}")
            return 2
        if out.exists():
            print(f"  ✗ 交付目录已存在，不覆盖：{out}")
            return 2
        shutil.copytree(template, out, symlinks=True,
                        ignore=shutil.ignore_patterns("node_modules", "dist", "requirements",
                                                      "skill-output", ".octos", "*.jsonl", "design"))
        print(f"模板：{template}（已复制，进入 evolution 模式）")

    child = os.environ.copy()
    for name in ("OCTOS_HOME", "OCTOS_CONFIG_DIR", "ARCBENCH_TEMPLATE_DIR",
                 "OCTOS_INSTANCE_DATA_DIR", "OCTOS_DANGER_FULL_ACCESS"):
        child.pop(name, None)
    child.update(
        OPENAI_API_KEY=key,
        OPENAI_BASE_URL=base,
        MODEL=model,
        OCTOS_MODEL=model,
        OCTOS_PROVIDER="custom",
        OCTOS_MAX_ITERATIONS=os.environ.get("OCTOS_MAX_ITERATIONS", "60"),
        OCTOS_NODE_TIMEOUT=str(args.node_timeout or os.environ.get("OCTOS_NODE_TIMEOUT", "1800")),
        OCTOS_TIME_BUDGET=str(args.time_budget or os.environ.get("OCTOS_TIME_BUDGET", "5400")),
        OCTOS_SMOKE_PORT=str(smoke),
    )
    if has_tests:
        child["ARCBENCH_TESTS_DIR"] = str(tests)
    # 平台就是拿 PORT=3000 起后端的；本地用判分端口，避免和别的东西撞。
    child.pop("PORT", None)
    # 子进程同样按 UTF-8 输出：验收里那些符号在 GBK 控制台上会直接抛
    # UnicodeEncodeError，把一轮真实的判分变成一次假崩溃。
    child["PYTHONIOENCODING"] = "utf-8"

    print(f"\n交付目录：{out}")
    print(f"判分端口：{args.port}    自测端口：{smoke}")
    print("=" * 72, flush=True)

    return subprocess.run(
        [sys.executable, "main.py", str(task), "--output-dir", str(out),
         "--type", "web", "--web-port", str(args.port)],
        cwd=str(ARC), env=child).returncode


if __name__ == "__main__":
    sys.exit(main())
