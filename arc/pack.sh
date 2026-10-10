#!/bin/sh
# 把 arc/ 打成 ARC 平台要的提交包（main.py 必须在 zip 根目录）
set -e
# Optional first argument is deployment policy, not generated app content.
ROUTES=""
if [ "$#" -gt 0 ]; then
    ROUTES="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1")"
fi
cd "$(dirname "$0")"
if [ -n "$ROUTES" ]; then
    python3 -c 'import sys; from pathlib import Path; from llm_proxy import model_routes; model_routes(Path(sys.argv[1]).read_text())' "$ROUTES"
fi
rm -f ../octos-arc-bundle.zip
# public-tests/ is deliberately NOT shipped. The runner mounts the public specs at
# /workspace/tests, which locate_acceptance_tests() prefers anyway: across 13 completed
# cloud runs (smoke, smoke-evolution, ticket-booking, arc-bench-web, 2026-09-17) every
# single one logged "[tests] N spec files at /workspace/tests" and none ever reached the
# bundled copy. Shipping it would only put a per-task, task-title-keyed set of spec files
# in the submission -- dead weight in the cloud, and indistinguishable from pre-loading
# task data for anyone auditing the bundle. Local runs are unaffected: the local runner
# reads the specs straight from the repo (/workspace/tests is the cloud-only path).
# local_driver.py 是必带的：main.py 顶层就 import 它，漏掉的话云机上第一行
# 就 ImportError，整轮 0 分 —— 而且失败发生在建目录之前，日志里什么都看不到。
# tool_driver.py 同样是必带的：main.py 顶层 import 它，漏掉的话云机上第一行就
# ImportError，整轮 0 分，而且失败发生在建立目录之前，日志里什么都看不到。
#
# layers.py / contract.py / orchestrator.py / text_repair.py 完全是同一个道理：
# 前三个是分层编排本身（main.py 顶层 import layers + orchestrator，orchestrator
# 顶层 import contract + layers），text_repair.py 是 tool_driver.py 顶层 import 的。
# **少任何一个，云机第一行 ImportError，整轮 0 分，日志里什么都看不到。**
# codegen.py 已删除（旧的 DFS/codegen 架构不再存在），不要把它加回来。
#
# rust_engine.py / arc-policy.toml / prompts/ 也已删除：那是**旧 Rust octos 内核**
# 那条路（`OCTOS_ARC_ENGINE=rust` → `octos arc run --policy arc-policy.toml`），
# 全仓库没有任何地方设那个开关，平台也不设。`prompts/*.md` 只被那个内核读，
# Python 这条路一个字节都不读。三条一起删，别单独加回来。
# ⚠️ 注意 `octos-arc/arc/prompts/` 是**另一个目录**，Rust 那边靠
#    `include_str!("../../../arc/prompts/…")` 编译进二进制，删这里的副本不影响它。
#
# ccb_driver.py（v4 的默认驱动，main.py 在驱动选择那一支惰性 import）和
# vendor/（内核产物 serve.mjs + ripgrep）同理必带 —— 只是它们的失败更晚：
# ccb_driver.py 漏了 → 落到 KernelNotFound 或 ImportError，发生在建完目录之后，
# 至少日志里看得见；vendor/ 漏了 → 同样是 KernelNotFound，报错文本里会点名
# pack.sh。**两个都不进包的话，默认驱动起不来、整轮 0 分。**
#
# vendor/ 里有什么、多大：
#   vendor/kernel-dist/serve.mjs              单文件内核（约 25 MB，构建产物）
#   vendor/ripgrep/<arch>-<platform>/rg[.exe]  Glob/Grep 的外部依赖
#     三份都放：x64-win32（约 5 MB，本机）+ x64-linux / arm64-linux
#     （约 5 MB / 4 MB，判分机）。**不是按构建机的平台放的** ——
#     `ripgrep.ts:70-73` 是按**运行机**的 `process.arch`/`platform` 拼目录名，
#     在 Windows 上打出来的包如果只带 x64-win32，判分机上 Glob/Grep 两个工具
#     就整个废掉（它们 100% 依赖 rg，`GrepTool.ts:18` / `glob.ts:119`）。
#     Linux 那两份是 **musl 静态**（不依赖 glibc 版本），从
#     github.com/BurntSushi/ripgrep/releases 的 15.2.0 取，sha256 已对过官方
#     .sha256 文件。
#     ⚠️ zip 不保证保留可执行位 ⇒ Linux 上靠 `ccb_driver.prepare_ripgrep()`
#        运行期 chmod 755，不赌解压工具的行为。
zip -qr ../octos-arc-bundle.zip main.py local_driver.py tool_driver.py ccb_driver.py octos_stdio.py requirement_order.py acceptance.py verify_app.py action_errors.cjs page_errors.ts guard.py llm_proxy.py layers.py contract.py orchestrator.py text_repair.py hooks requirements.txt arcbench_agent_runtime vendor -x '*/__pycache__/*' '*.pyc'
if [ -n "$ROUTES" ]; then
    python3 -c 'import sys; from zipfile import ZipFile; z=ZipFile("../octos-arc-bundle.zip", "a"); z.write(sys.argv[1], "model-routes.json"); z.close()' "$ROUTES"
fi
echo "打包完成：$(cd .. && pwd)/octos-arc-bundle.zip"
shasum -a 256 ../octos-arc-bundle.zip
