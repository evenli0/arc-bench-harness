#!/usr/bin/env python3
"""ARC-Bench custom agent bundle: Octos as the coding agent.

The ARC-Bench platform invokes this as:

    python main.py <requirement_path> [--output-dir DIR] [--web-port N]

Flow (layered — see `orchestrator.py` and `layers.py` for the whole argument):

    L0 blueprint   -> the architecture contract (the only cross-node coupling)
    L1 skeleton    -> the frame every later node lands in; done when
                      `npm run build` passes and every contract route answers
    expand         -> one contract expansion turn per depth (3-4 turns total)
    leaves         -> implement each leaf; a node is committed only once the
                      baseline is still green (invariant 3), then verification
                      walks up its ancestors (invariant 2)
    finalize       -> the aggregation points that were reached, plus one last
                      full-suite baseline

Three invariants, and they are the entire orchestration:

    1. implementation goes large -> small: a node starts only with every
       ancestor's contract already written
    2. acceptance goes small -> large: a node passes only when its whole
       subtree passes; parents are aggregated after their children
    3. monotonicity: what passed can only grow. A commit that reddens the
       baseline is rolled back and the turn is told exactly whom it broke

There is no codegen mode and no separate skeleton/design turn: every turn
works through tools, on the real app. Harness does two things only -- hand the
model enough information, and verify globally.

Evolution mode (ARCBENCH_TEMPLATE_DIR already holds frontend/ + backend/):
nodes already passing are seeded into the verdict before the leaves run, so
they stay passing without paying for a turn.

Environment (all optional):
    OPENAI_API_KEY / OPENAI_BASE_URL / MODEL   OpenAI-compatible endpoint
    OCTOS_NODE_TIMEOUT        seconds per model turn (default 1200)
    OCTOS_TIME_BUDGET         seconds for the whole generation (default max(3600, 1500 x nodes),
                              capped at 6h for <=40-node trees / 10h for bigger ones)
    OCTOS_SECONDS_PER_NODE    per-node allowance used for that default (1500)
    OCTOS_MIN_REPAIR_SECONDS  do not start a repair turn with less than this left (300)
    OCTOS_NODE_TIME_BUDGET    cap per node incl. repairs (default 1500)
    OCTOS_REPAIR_ROUNDS       K, acceptance repair rounds per node (default 5)
    OCTOS_SMALL_TASK_NODES    trees up to this size get the minimal self-verification text (2)
    OCTOS_VERIFY_MODE         auto (default) | minimal | full
    OCTOS_ARC_REASONING       auto (default: none for <=1 node to implement, else low) | low | medium | high | none | passthrough
    OCTOS_ARC_IMPLEMENT_REASONING  optional override for first implement turns of small tasks (default: base mode)
    OCTOS_ARC_INLINE_SPECS    "0" stops quoting the node's spec files into the prompt (default: quote up to 24k chars)
    OCTOS_ARC_DESTREAM        "0" lets streaming requests reach the platform as SSE (default: one JSON response upstream)
    OCTOS_ARC_TRIM_PROMPT     "0" keeps the kernel system prompt and all tool schemas (default: drop ARC-irrelevant sections/tools)
    OCTOS_ARC_DROP_SHELL      "0" leaves bash/shell available in minimal-verification turns (default: removed)
    OCTOS_ARC_IMPLEMENT_REQUESTS / OCTOS_ARC_REPAIR_REQUESTS  hard per-turn request caps enforced at the proxy (20 for small tasks / 10; 0 = off)
    OCTOS_ARC_INLINE_SOURCE_CHARS  budget for quoting the app's sources into repair prompts (90000; 0 = off)
    OCTOS_ARC_MAX_TOKENS      minimum max_tokens the proxy enforces on chat requests (32768; kernel arc.11 sends 4096)
    OCTOS_SESSION_SCOPE       turn (default) | node | run — when a fresh octos session starts
    OCTOS_ARC_INSTALL_PLAYWRIGHT  "0" never installs Playwright on the fly
    OCTOS_ARC_ALIAS_SPEC_IDS  "0" stops mirroring node states onto spec ids
    OCTOS_PERF_CONTRACT       "0" drops the performance rules from prompts
    OCTOS_GUARD               "0" logs guard findings without injecting them
"""

from __future__ import annotations

import argparse
import atexit
from contextlib import contextmanager
import json
import os
import re
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from arcbench_agent_runtime import AgentRuntime  # noqa: E402
from acceptance import (  # noqa: E402
    workers_for_memory, process_cwd, workspace_contains, free_owned_ports,
    kill_pid_tree, listening_pids, owned_by_workspace, win_kill_tree, win_process_table,
    AcceptanceRunner, AppServer, RunSummary, acceptance_work_dir, clip_ends, container_memory_limit, ensure_playwright,
    failure_signature, failure_summaries, failure_source_context, find_playwright_by_search, find_playwright_root, map_specs_to_nodes,
    nodes_for_failures, playwright_candidates, playwright_version_hint, restore_tree,
    mutated_by_tests, restore_worktree, snapshot_worktree, startup_error_digest, tree_digest,
    final_workers, reap_workspace_processes)
from guard import TurnMonitor  # noqa: E402
from layers import layerize  # noqa: E402
from llm_proxy import LlmProxy, configured_model_routes  # noqa: E402
from local_driver import LocalDriver  # noqa: E402
from orchestrator import Orchestrator, _atomic_final  # noqa: E402
from requirement_order import node_fingerprint  # noqa: E402
from tool_driver import ToolDriver  # noqa: E402

BUNDLE_DIR = Path(__file__).resolve().parent


def log(msg: str) -> None:
    """Progress lines go to BOTH stdout and stderr (the platform truncates
    stdout on long runs but keeps stderr as a separate field)."""
    print(msg, flush=True)
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 临时目录
#
# 这一轮建过的每个临时目录都登记下来，退出时删掉。**私有 Playwright 一份 767 MB**
# —— 每跑一次留一份，几轮就把磁盘吃满（2026-09-29 实测攒到 5 份、3.0 GB）。
#
# 只靠 `finally: postflight()` 不够：那个只覆盖「进程还活着走到退出」。关掉终端
# 窗口、被硬杀、或者在**装 Playwright 的那一分钟里**被打断（那一步在 run() 的 try
# 之前），finally 根本没机会跑。所以分两层：
#
#   _temp_dir()   建的时候登记 —— 只要进程还能走完退出流程就一定删掉
#   _sweep_temp() 下次启动扫上一轮的残骸 —— 专治「进程没走到退出」
#
# 目录名里带**属主 pid**：扫的时候要能分清「上一轮的残骸」和「另一个正在跑的
# 任务」（本地用 --port 43300 并行跑两题是常规操作），只删属主进程已经不在的。

_TEMP_DIRS: list[Path] = []


def _temp_dir(prefix: str) -> Path:
    """mkdtemp + 登记。名字里带自己的 pid，见 _sweep_temp()。"""
    path = Path(tempfile.mkdtemp(prefix=f"{prefix}{os.getpid()}-"))
    _TEMP_DIRS.append(path)
    return path


def _drop_temp_dirs() -> None:
    """atexit：正常退出、抛异常、Ctrl-C 都会走到这里。"""
    while _TEMP_DIRS:
        shutil.rmtree(_TEMP_DIRS.pop(), ignore_errors=True)


atexit.register(_drop_temp_dirs)


def _pid_alive(pid: int) -> bool:
    """这个 pid 还在不在。**不要用 os.kill(pid, 0) 在 Windows 上探活** ——
    那在 POSIX 上是发信号 0，在 Windows 上却是 TerminateProcess，会把人家杀掉。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sweep_temp(prefixes: tuple[str, ...] = ("octos-arc-playwright-", "octos-data-",
                                             "octos-config-", "octos-protected-")) -> None:
    """删掉上一次没走到退出留下的临时目录。

    只认自己建的这四种前缀，而且只认名字里带 pid、pid 已经不在的那些 —— 没有
    pid 的旧格式目录和正在跑的另一个任务都不动。
    """
    freed = 0
    for entry in Path(tempfile.gettempdir()).glob("octos-*"):
        if not entry.is_dir():
            continue
        prefix = next((p for p in prefixes if entry.name.startswith(p)), None)
        if prefix is None:
            continue
        owner = entry.name[len(prefix):].split("-", 1)[0]
        if not owner.isdigit() or _pid_alive(int(owner)):
            continue
        try:
            size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
        except OSError:
            size = 0
        shutil.rmtree(entry, ignore_errors=True)
        if entry.exists():
            continue
        freed += size
        log(f"[temp] removed {entry.name} ({size // (1024 * 1024)} MiB) — left by a run that never exited")
    if freed:
        log(f"[temp] reclaimed {freed // (1024 * 1024)} MiB")


# ---------------------------------------------------------------- postflight

def _postflight_structure_check(output_dir: Path) -> None:
    """Log the deliverable tree; lift a one-level-nested app into place."""
    tree_lines = []
    for root, dirs, files in os.walk(output_dir):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "dist", "__pycache__")]
        depth = Path(root).relative_to(output_dir).parts
        if len(depth) > 2:
            dirs[:] = []
            continue
        indent = "  " * len(depth)
        tree_lines.append(f"{indent}{Path(root).name}/")
        for f in sorted(files)[:8]:
            tree_lines.append(f"{indent}  {f}")
        if len(tree_lines) > 60:
            tree_lines.append("... (truncated)")
            break
    log("[postflight] workspace tree:\n" + "\n".join(tree_lines))
    if (output_dir / "frontend").is_dir() and (output_dir / "backend").is_dir():
        log("[postflight] frontend/ and backend/ present at workspace root")
        return
    for child in [p for p in output_dir.iterdir() if p.is_dir() and p.name not in (".git", ".arc", "requirements")]:
        if (child / "frontend").is_dir() and (child / "backend").is_dir():
            log(f"[postflight] app found nested at {child.name}/; lifting to root")
            for item in child.iterdir():
                dest = output_dir / item.name
                if not dest.exists():
                    shutil.move(str(item), str(dest))
            return
    log("[postflight] WARNING: no frontend/+backend/ found anywhere; runner will reject the template")


_WIN_STRAY_NAMES = ("chrome", "chromium", "headless_shell", "playwright", "node", "npm", "npx", "sh", "bash")


def _reap_stray_processes_windows(tag: str, output_dir: Path, me: int) -> None:
    """Windows 版的同一件事：杀掉本进程拉起来的残留（含 Chromium/Playwright）。

    本地没有 `free`、没有 cgroup 文件、也没有 `ps -eo`，所以这一段只做「认人 + 杀」；
    内存那几行诊断照旧只有 Linux 有 —— 它们是判分机排障用的，本地复现不出来。
    认人判据和 Linux 那条的 `pid in descendants` 逐字同义：只认自己的子孙。
    """
    table = win_process_table()
    if not table:
        log(f"[reap:{tag}] Windows process table unavailable; nothing to kill")
        return
    children: dict[int, list[int]] = {}
    for pid, ppid, _, _, _ in table:
        children.setdefault(ppid, []).append(pid)
    descendants: set[int] = set()
    frontier = [me]
    while frontier:
        for child in children.get(frontier.pop(), []):
            if child not in descendants:
                descendants.add(child)
                frontier.append(child)
    victims = [pid for pid, _, name, _, _ in table
               if pid not in (me, os.getppid())
               and Path(name).stem.lower() in _WIN_STRAY_NAMES
               and pid in descendants]
    for pid in victims:
        win_kill_tree(pid)
    if victims:
        log(f"[reap:{tag}] killed {len(victims)} stray process(es): {victims}")
    else:
        log(f"[reap:{tag}] nothing to kill")


def _reap_stray_processes(tag: str, output_dir: Path) -> None:
    """Report resource use and stop matching descendants or workspace processes.

    A killed grader does not by itself establish memory exhaustion. Never
    attribute another run's processes merely from a browser or Node name.
    """
    me = os.getpid()
    if sys.platform == "win32":
        _reap_stray_processes_windows(tag, output_dir, me)
        return
    def _run(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"<{cmd[0]} unavailable: {exc}>"
    log(f"[reap:{tag}] memory:\n" + _run(["free", "-m"]).rstrip())
    # `free` shows the host; cgroup counters help distinguish container
    # memory pressure from other causes of a killed grading process.
    cg = []
    for f in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current",
              "/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory.events",
              "/sys/fs/cgroup/memory/memory.limit_in_bytes",
              "/sys/fs/cgroup/memory/memory.max_usage_in_bytes",
              "/sys/fs/cgroup/memory/memory.failcnt", "/sys/fs/cgroup/pids.max"):
        try:
            cg.append(f"{f}={Path(f).read_text(encoding='utf-8', errors='replace').strip().replace(chr(10), ' ')}")
        except OSError:
            pass
    log(f"[reap:{tag}] cgroup: " + ("; ".join(cg) or "<no cgroup files>"))
    ps = _run(["ps", "-eo", "pid,ppid,rss,etime,args", "--sort=-rss"])
    log(f"[reap:{tag}] top processes by RSS:\n"
        + "\n".join(ps.splitlines()[:20]))
    rows = []
    for line in ps.splitlines()[1:]:
        parts = line.split(None, 4)
        if len(parts) != 5 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append((int(parts[0]), int(parts[1]), parts[4]))
    descendants = {me}
    while True:
        expanded = descendants | {pid for pid, parent, _ in rows if parent in descendants}
        if expanded == descendants:
            break
        descendants = expanded
    victims = []
    for pid, _, args in rows:
        if pid in (me, os.getppid()):
            continue
        if not any(marker in args.lower() for marker in
                   ("chrom", "headless_shell", "playwright", "octos serve", "node ", "npm ", "/node")):
            continue
        if pid in descendants or workspace_contains(process_cwd(pid), output_dir):
            victims.append(pid)
    # Windows 没有 SIGKILL（getattr 兜到 SIGTERM）：这个函数跑在收尾路径上，
    # 本地一次 AttributeError 就会把整轮结果连报告一起带走 —— 而它要做的
    # 只是「尽量清掉残留进程」，清不干净不该让整轮失败。Linux 上取值不变。
    _sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    for sig in (signal.SIGTERM, _sigkill):
        for pid in victims:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        time.sleep(2 if sig == signal.SIGTERM else 0)
    if victims:
        log(f"[reap:{tag}] killed {len(victims)} stray process(es): {victims}")
        log(f"[reap:{tag}] memory after:\n" + _run(["free", "-m"]).rstrip())
    else:
        log(f"[reap:{tag}] nothing to kill")


def _free_web_port(web_port: int, output_dir: Path) -> None:
    """Release only listeners attributable to this workspace."""
    free_owned_ports([web_port], output_dir)


def _port_watchdog(web_port: int, output_dir: Path, stop: threading.Event) -> None:
    """Kill OUR processes that bind the grading port during generation (the
    runner terminates a run that serves the grading port early). Foreign
    listeners are left alone: the runner host is shared."""
    while not stop.is_set():
        for pid in listening_pids(web_port):
            cwd = process_cwd(pid) or ""
            if owned_by_workspace(pid, output_dir):
                log(f"[watchdog] port {web_port} bound by our process {pid} (cwd={cwd}); killing")
                kill_pid_tree(pid)
            else:
                log(f"[watchdog] port {web_port} held by foreign process {pid} (cwd={cwd or '?'}); leaving it")
        stop.wait(5)


# ---------------------------------------------------------------- requirements

def load_requirement_tree(req_dir: Path) -> dict:
    req_file = req_dir / "requirements.yaml"
    if not req_file.exists():
        req_file = req_dir / "requirements.yml"
    data = yaml.safe_load(req_file.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "id" not in data:
        for wrapper in ("root", "requirement"):
            if isinstance(data.get(wrapper), dict):
                data = data[wrapper]
                break
    if not isinstance(data, dict) or "id" not in data:
        raise ValueError(f"invalid requirements.yaml in {req_dir}")
    return data


def describe_node(node: dict) -> str:
    lines = [f"ID: {node.get('id')}", f"Name: {node.get('name', '')}"]
    if node.get("description"):
        lines.append(f"Description: {node['description']}")
    scenarios = node.get("scenarios") or []
    if scenarios:
        lines.append("Scenarios:")
        for sc in scenarios:
            lines.append(f"  - {sc.get('name', 'scenario')}")
            for step in sc.get("steps") or []:
                if isinstance(step, dict):
                    lines.append(f"      {step.get('keyword', '')} {str(step.get('content', '')).strip()}")
    deps = node.get("dependencies") or []
    if deps:
        lines.append(f"Depends on: {', '.join(map(str, deps))}")
    return "\n".join(lines)


def folder_descendants(tree: dict) -> dict[str, list[str]]:
    """Non-atomic node id -> ids of its ATOMIC descendants (document order)."""
    out: dict[str, list[str]] = {}

    def walk(node: dict) -> list[str]:
        children = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        node_type = str(node.get("type") or "").upper()
        node_id = str(node.get("id") or "")
        if node_type == "ATOMIC" or (not children and node_type != "FOLDER"):
            return [node_id]
        ids: list[str] = []
        for child in children:
            ids.extend(walk(child))
        if node_id:
            out[node_id] = ids
        return ids

    walk(tree)
    return out


def previous_requirement_records(output_dir: Path) -> dict[str, dict]:
    """The previous run's requirement table (committed with the template)."""
    path = output_dir / ".arc" / "traceability" / "requirements.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def unchanged_node_ids(nodes: list[dict], previous: dict[str, dict]) -> set[str]:
    out = set()
    for node in nodes:
        prev = previous.get(str(node.get("id")))
        if prev and node_fingerprint(prev) == node_fingerprint(node):
            out.add(str(node.get("id")))
    return out


def inline_sources(output_dir: Path, max_chars: int = 90000, exts: tuple = (".js", ".mjs", ".cjs", ".html", ".css", ".json")) -> str:
    """Quote the app's source files (frontend sources, backend JS) so a repair
    turn edits immediately instead of spending its request budget on reads.

    Bounded, largest first: whatever has to be dropped should be the file least
    likely to need editing, and quoting smallest first made that the largest
    one. In cloud e767e871a6c6 the seed data, the lockfiles and the secondary
    pages were quoted and `frontend/src/index.html` — the whole UI, and where
    nearly every failure lives — was the one file omitted.
    """
    files: list[Path] = []
    for part in ("frontend", "backend"):
        base = output_dir / part
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                rel = path.relative_to(output_dir)
                if any(seg in ("node_modules", "dist", ".git") for seg in rel.parts):
                    continue
                if path.is_file() and path.suffix in exts:
                    files.append(path)
    parts, total = [], 0
    for path in sorted(files, key=lambda p: (-p.stat().st_size, str(p))):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if total + len(text) > max_chars:
            # A task with a hundred nodes grows one dominant UI file past the
            # whole budget on its own. Dropping it left the prompt quoting the
            # stylesheet and the seed data and not the file every repair edits,
            # which is the failure the largest-first order was meant to prevent.
            # Give any file too big to quote whole a share of the budget instead
            # of nothing, capped at half so one file cannot crowd out the rest;
            # the elision marker keeps it from reading as the complete file.
            room = min(max_chars // 2, max_chars - total)
            if room >= 2000:
                total += room
                parts.append(f"--- {path.relative_to(output_dir)} --- (too large to quote whole, "
                             f"{len(text)} chars; the part shown is clipped, read it for the rest)\n"
                             f"{clip_ends(text, room)}\n")
                continue
            parts.append(f"--- {path.relative_to(output_dir)} --- (omitted, {len(text)} chars; read it if you must change it)\n")
            continue
        total += len(text)
        parts.append(f"--- {path.relative_to(output_dir)} ---\n{text.rstrip()}\n")
    return ("Current source files (quoted; edit them directly, no need to read):\n" + "".join(parts)) if parts else ""


# ---------------------------------------------------------------- octos driver

def protected_hooks(protected_dirs: list[Path] | None) -> list[dict]:
    """before_tool_call hook denying file writes into the official tests /
    requirements directories (exit 1 = deny). Shell commands are redacted by
    the kernel and cannot be checked here; the harness restores the trees
    after every turn as the second layer."""
    hook_script = BUNDLE_DIR / "hooks" / "deny_protected.py"
    if not protected_dirs or not hook_script.is_file():
        return []
    return [{
        "event": "before_tool_call",
        "command": [sys.executable, str(hook_script), *[str(p) for p in protected_dirs]],
        "timeout_ms": 4000,
        "tool_filter": ["write_file", "edit_file", "diff_edit", "apply_patch", "create_file", "append_file"],
    }]


def write_profile_defaults(data_dir: Path, config_dir: Path, hooks: list[dict]) -> None:
    """Belt and braces: the solo ProfileRuntime builds its HookExecutor from
    the profile's own config (the stdio driver patches `hooks` into the
    profile registry file — the mechanism verified to deny with a real turn);
    a `profile-defaults.json` covers code paths that merge store defaults."""
    if not hooks:
        return
    for root in (data_dir, config_dir):
        try:
            root.mkdir(parents=True, exist_ok=True)
            (root / "profile-defaults.json").write_text(json.dumps({"hooks": hooks}, indent=2), encoding="utf-8")
        except OSError:
            pass


def build_octos_env(config_dir: Path, protected_dirs: list[Path] | None = None) -> dict:
    """Prepare env + minimal config.json for non-interactive octos.

    `protected_dirs` (official tests, requirements) get a before_tool_call
    hook that denies write_file/edit_file into them (exit 1 = deny)."""
    env = os.environ.copy()
    api_key = env.get("OPENAI_API_KEY", "")
    base_url = env.get("OPENAI_BASE_URL", "")
    model = os.environ.get("OCTOS_MODEL") or env.get("MODEL", "")
    provider = os.environ.get("OCTOS_PROVIDER")
    if not provider:
        provider = "deepseek" if "deepseek" in base_url else "anthropic" if "anthropic" in base_url else "openai"
    key_env = "OPENAI_API_KEY"
    if provider == "deepseek" and api_key:
        env.setdefault("DEEPSEEK_API_KEY", api_key)
        key_env = "DEEPSEEK_API_KEY"
    elif provider == "anthropic" and api_key:
        env.setdefault("ANTHROPIC_API_KEY", api_key)
        key_env = "ANTHROPIC_API_KEY"
    elif provider not in ("openai", "deepseek", "anthropic") and api_key:
        env.setdefault(f"{provider.upper()}_API_KEY", api_key)
        key_env = f"{provider.upper()}_API_KEY"
    config = {
        "provider": provider,
        "model": model,
        "sandbox": {"allow_network": True},
        "memory": {"refresh": {"enabled": False}},
        # deepseek-v4 spends its default 4096 output budget on reasoning and
        # returns empty content; give it real headroom.
        "gateway": {"max_output_tokens": 65536},
    }
    if provider not in ("openai", "deepseek", "anthropic") and base_url:
        config["base_url"] = base_url
    if provider == "deepseek":
        config["gateway"]["reasoning_effort"] = "low"
    hooks = protected_hooks(protected_dirs)
    if hooks:
        config["hooks"] = hooks
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    env["OCTOS_CONFIG_DIR"] = str(config_dir)
    env.setdefault("OCTOS_DISABLE_STREAMING", "1")   # platform proxies reject SSE
    env.setdefault("OCTOS_DANGER_FULL_ACCESS", "1")  # the container is the sandbox
    env.setdefault("npm_config_registry", "https://registry.npmmirror.com")
    env.setdefault("NPM_CONFIG_REGISTRY", "https://registry.npmmirror.com")
    rules_file = BUNDLE_DIR / "EXTRA_RULES.md"
    if rules_file.is_file() and "OCTOS_ARC_EXTRA_RULES" not in env:
        env["OCTOS_ARC_EXTRA_RULES"] = rules_file.read_text(encoding="utf-8")[:8000]
    env["_ARC_PROVIDER"] = provider
    env["_ARC_MODEL"] = model
    env["_ARC_BASE_URL"] = base_url
    env["_ARC_KEY_ENV"] = key_env
    return env


_CHAT_FLAGS_CACHE: dict[str, set[str]] = {}


def _chat_supported_flags(octos_bin: str) -> set[str]:
    if octos_bin not in _CHAT_FLAGS_CACHE:
        try:
            proc = subprocess.run([octos_bin, "chat", "--help"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
            help_text = (proc.stdout or "") + (proc.stderr or "")
        except Exception:  # noqa: BLE001
            help_text = ""
        _CHAT_FLAGS_CACHE[octos_bin] = {
            flag for flag in ("--json", "--cwd", "--data-dir", "--sandbox", "--profile",
                              "--max-iterations", "--no-session-persistence") if flag in help_text}
    return _CHAT_FLAGS_CACHE[octos_bin]


def run_octos(octos_bin: str, cwd: Path, prompt: str, env: dict, data_dir: Path,
              timeout: int, max_iterations: int) -> tuple[bool, str]:
    """One non-interactive `octos chat` turn (fallback driver)."""
    flags = _chat_supported_flags(octos_bin)
    cmd = [octos_bin, "chat", "-m", prompt]
    if "--json" in flags:
        cmd.append("--json")
    if "--cwd" in flags:
        cmd += ["--cwd", str(cwd)]
    if "--data-dir" in flags:
        cmd += ["--data-dir", str(data_dir)]
    if "--max-iterations" in flags:
        cmd += ["--max-iterations", str(max_iterations)]
    if "--no-session-persistence" in flags:
        cmd.append("--no-session-persistence")
    if "--sandbox" in flags and env.get("OCTOS_DANGER_FULL_ACCESS") == "1":
        cmd += ["--sandbox", "danger-full-access"]
    if "--profile" in flags:
        cmd += ["--profile", os.environ.get("OCTOS_CHAT_PROFILE", "coding")]
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"octos timed out after {timeout}s"
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, f"octos exited {proc.returncode}: {out or (proc.stderr or '').strip()[-2000:]}"
    try:
        payload = json.loads(out)
        if isinstance(payload, dict) and payload.get("error"):
            return False, str(payload["error"])
        return True, str(payload.get("text", "")) if isinstance(payload, dict) else out
    except json.JSONDecodeError:
        return True, out[-4000:]


class DryRunDriver:
    """OCTOS_ARC_DRYRUN=1: no kernel, no model. Every turn returns a fixed reply
    so the whole flow — tree order, probes, acceptance, repair/budget logic,
    events — runs end to end for structural parity checks. Real-path behaviour is
    untouched."""

    def __init__(self) -> None:
        self.hooks: list = []
        self.turns = 0

    def run(self, prompt: str, timeout: int, monitor: TurnMonitor | None = None) -> tuple[bool, str]:
        self.turns += 1
        time.sleep(0.05)
        return True, "dry run: no model call; nothing written."

    @contextmanager
    def without_tools(self):
        """No-op: a dry run has no kernel and therefore no tools to take away.

        Kept because the real drivers have it: #140 gave `OctosDriver` a
        `without_tools()` and called it, but not `DryRunDriver`, so
        OCTOS_ARC_DRYRUN=1 aborted with AttributeError('DryRunDriver' object has
        no attribute 'without_tools') — the free structural-parity path was broken
        by an interface the drivers did not share. The protocol is the point, not
        the current call count.
        """
        yield

    def end_scope(self, *args, **kwargs) -> None:
        pass

    def close(self, *args, **kwargs) -> None:
        pass


class PermanentProviderError(RuntimeError):
    """Account failures require external action, not another generation attempt."""


def permanent_provider_error(text: str) -> bool:
    lowered = text.lower()
    codes = re.findall(r"\bhttp(?:/\d(?:\.\d)?)?\s+(\d{3})\b", lowered)
    return any(code in {"401", "402", "403"} for code in codes) or any(term in lowered for term in (
        "insufficient_balance", "quota exhausted", "balance is exhausted", "invalid_api_key",
        "authentication failed", "unauthorized"))


def quota_exhausted(text: str) -> bool:
    """平台配额耗尽 —— **整段接口不可用**，和普通限流分开对待。

    2026-10-01 正赛实测文本是 `bridge_error: API Error: 429 Free allocated quota
    exceeded.`：它当时的归类是「谁都不认」——不进 permanent（词表里写的是
    "quota exhausted"，一字之差），也不进 transient（那条正则要 `http 429`
    的形态）——于是不重试、3 秒速死，一次平台停摆照常记成一次失败，白扣修复
    轮次（那一小时烧掉 github 28 个 / sheet 24 个回合，REQ-1 整支就是这么残的）。
    """
    lowered = text.lower()
    if any(marker in lowered for marker in ("quota exceeded", "allocated quota", "quota will reset")):
        return True
    return bool(re.search(r"\b429\b", lowered)
                and any(marker in lowered for marker in ("quota", "rate limit", "too many requests")))


def sleep_logging(seconds: float, chunk: float = 30.0) -> None:
    """带着心跳的等待 —— 平台会杀静默进程（回合里的 30s keepalive 同理）。"""
    end = time.monotonic() + max(0.0, seconds)
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(chunk, left))
        left = end - time.monotonic()
        if left > 0:
            log(f"[quota] still holding ({left:.0f}s left in this nap)")


class OctosDriver:
    """stdio UI-protocol session (default) or one-shot chat turns.

    By default every turn gets a fresh session: the per-turn prompt already
    carries all the state the model needs, and a short, byte-stable prefix
    (system prompt + tool schemas) is what the provider's prefix cache keys on.
    """

    def __init__(self, octos_bin: str, cwd: Path, env: dict, data_dir: Path,
                 max_iterations: int, events_log: Path) -> None:
        self.mode = os.environ.get("OCTOS_DRIVER", "stdio")
        # "turn": new session every turn; "node": one session per requirement
        # node (design -> implement -> repairs share context); "run": one session.
        # Default "turn" since specs are quoted into every prompt: a repair turn
        # is self-contained, while a shared node session made each repair
        # request carry the whole implement history (v8-tb: 49 requests, 1.1M
        # prompt tokens for 7 repairs).
        self.session_scope = os.environ.get("OCTOS_SESSION_SCOPE", "turn")
        if os.environ.get("OCTOS_SESSION_PER_TURN") == "0" and "OCTOS_SESSION_SCOPE" not in os.environ:
            self.session_scope = "run"
        self.octos_bin = octos_bin
        self.cwd = cwd
        self.env = env
        self.data_dir = data_dir
        self.max_iterations = max_iterations
        self.events_log = events_log
        self._session = None
        self.monitor: TurnMonitor | None = None
        self.hooks: list = []  # profile hooks (protected-directory deny), set by the flow
        self.tools_disabled = False

    @contextmanager
    def without_tools(self):
        """Tool policy belongs to the kernel profile, so never reuse a profile across modes."""
        previous = self.tools_disabled
        if not previous:
            self.close()
        self.tools_disabled = True
        try:
            yield
        finally:
            if not previous:
                self.close()
            self.tools_disabled = previous

    def _log_event(self, method: str, params: dict) -> None:
        if method == "core/marker":
            log(f"[core-mod] {params.get('line', '')}")
        if self.monitor is not None and method in ("tool/started", "tool/completed"):
            try:
                self.monitor.observe(method, params)
            except Exception:  # noqa: BLE001 - guard must never break a turn
                pass
        try:
            with self.events_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"method": method, "params": params}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _get_session(self):
        if self._session is None:
            from octos_stdio import OctosStdioSession
            self._session = OctosStdioSession(self.octos_bin, self.cwd, self.env, self.data_dir,
                                              on_event=self._log_event)
            self._session.bootstrap_profile(
                provider=self.env.get("_ARC_PROVIDER", "openai"),
                model=self.env.get("_ARC_MODEL", ""),
                base_url=self.env.get("_ARC_BASE_URL") or None,
                api_key_env=self.env.get("_ARC_KEY_ENV") or None,
                hooks=self.hooks,
                tools_disabled=self.tools_disabled,
            )
            self._session.open()
        return self._session

    def run(self, prompt: str, timeout: int, monitor: TurnMonitor | None = None) -> tuple[bool, str]:
        self.monitor = monitor
        if self.mode == "chat" and not self.tools_disabled:
            fn = lambda remaining: run_octos(self.octos_bin, self.cwd, prompt, self.env, self.data_dir,  # noqa: E731
                                   remaining, self.max_iterations)
        else:
            fn = lambda remaining: self._run_stdio(prompt, remaining)  # noqa: E731
        try:
            ok, text = self._run_with_heartbeat(lambda: self._run_with_retries(fn, timeout))
        finally:
            self.monitor = None
            if self.session_scope == "turn":
                self.close()
        if monitor is not None:
            monitor.finish(text)
        return ok, text

    @staticmethod
    def _run_with_heartbeat(fn) -> tuple[bool, str]:
        """Run a turn in a thread, logging a keepalive every 30s (the runner
        kills silent processes)."""
        box: dict = {}

        def target() -> None:
            try:
                box["r"] = fn()
            except Exception as exc:  # pragma: no cover - defensive
                box["r"] = (False, f"turn raised: {exc}"[:500])

        th = threading.Thread(target=target, daemon=True)
        th.start()
        t0 = time.time()
        while True:
            th.join(30)
            if not th.is_alive():
                break
            log(f"[flow] turn still running ({int(time.time() - t0)}s elapsed)")
        return box.get("r", (False, "turn thread ended without result"))

    @staticmethod
    def _transient(text: str) -> bool:
        lowered = text.lower()
        if "octos turn timed out" in lowered or "octos timed out after" in lowered:
            return False  # our own wall-clock cap, not a provider hiccup: never replay the turn
        if permanent_provider_error(text):
            return False
        codes = re.findall(r"\bhttp(?:/\d(?:\.\d)?)?\s+(\d{3})\b", lowered)
        if codes:
            return any(code in {"408", "425", "429", "500", "502", "503", "504"} for code in codes)
        return any(k in lowered for k in (
            "temporarily unavailable", "rate limit", "timeout", "timed out",
            "connection reset", "overloaded", "failed to send", "streaming request"))

    def _run_with_retries(self, fn, timeout: float, attempts: int = 3) -> tuple[bool, str]:
        deadline = time.monotonic() + max(0, timeout)
        ok, text = False, "octos turn timed out"
        for attempt in range(1, attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ok, text = fn(remaining)
            if ok or not self._transient(text) or attempt == attempts:
                break
            wait = 30 * attempt
            if wait >= deadline - time.monotonic():
                log("[driver] remaining turn budget cannot accommodate retry backoff")
                break
            log(f"[driver] transient error, retry {attempt + 1}/{attempts} after {wait}s: {text[:200]}")
            time.sleep(wait)
            self.close()
        return ok, text

    def _run_stdio(self, prompt: str, timeout: float) -> tuple[bool, str]:
        deadline = time.monotonic() + timeout
        try:
            session = self._get_session()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "octos turn timed out"
            return session.run_turn(prompt, timeout=remaining)
        except Exception as exc:  # noqa: BLE001
            self.close()
            if self.tools_disabled:
                return False, f"tool-free stdio driver error: {exc}"[:1000]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "octos turn timed out"
            chat_ok, chat_text = run_octos(self.octos_bin, self.cwd, prompt, self.env, self.data_dir,
                                           remaining, self.max_iterations)
            if chat_ok:
                return True, chat_text
            return False, f"stdio driver error: {exc}; chat fallback: {chat_text}"[:1000]

    def end_scope(self, scope: str) -> None:
        """Called by the flow at node boundaries; closes the session when the
        configured scope ends."""
        if scope == self.session_scope or self.session_scope == "turn":
            self.close()

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None


# ---------------------------------------------------------------- prompts
#
# Prompt text is deliberately static (no timestamps, fixed section order) so
# that identical turns share the provider's prefix cache.

# Measured on cloud 6e82a7ff571c's own app (32 official specs, 3 runs each, a
# pristine store per run). Its note actions were `visibility: hidden; opacity: 0`
# revealed on hover, and every one of the twelve graded failures was a note-card
# action, so a clause telling the model not to hide them looked obvious. It is
# wrong. Baseline 16/16/16; fading opacity alone 15/14/15; leaving the controls
# always visible 15/15/15. Both alternatives fixed REQ-2.5.4 and REQ-2.7.4 and
# broke REQ-2.6.1, REQ-2.7.2 and REQ-2.8.3. Hiding an item's controls until the
# pointer arrives is what keeps a name-based lookup landing on the hovered card,
# which is what the per-item bullet below already says. Do not add a clause
# against hover-hiding without measuring it on a real app first.
UI_CONTRACT_CORE = """\
UI behavior follows the requirement and the current application:
- Use semantic controls, accessible names and labels appropriate to each action. Preserve required routes, text, visibility, enabled states and interactions. Choose input types and validation behavior from the requirements; hidden views, dialogs and dynamic rendering are allowed when needed.
- An explicit `role` replaces an element's native role instead of adding to it: a real <button>, <a>, <input>, <select>, <li> or heading that carries a role like `menuitem` or `option` stops being a button, a link, a field or a list item to any role lookup, and a lookup by the native role then finds nothing. Where a control must also present a role its tag does not have, keep both: put the ARIA role on a wrapper around the real control (or a child inside it) rather than on the control itself.
- Decorative marks (▸ ▾ ▲ ▼ ✓ ✕ and their kind) are not text: rendered as a text node inside a labelled element they join that element's text content, and `aria-hidden` or `display:none` does not take them out of `textContent` — an exact-text assertion then fails while the feature works. Draw them with a CSS pseudo-element or an SVG icon instead.
- Where the requirements describe an order or a lifecycle — 'saves it, then reopens', 'closes and shows…', 'returns to the list' — the order and the ending state are part of the requirement: perform the steps in that order and leave the interface in the state the text ends in. A success path the text says closes, dismisses or returns must actually end in that state, not merely finish its work with the previous view still open.
- Keep IDs unique and label associations correct. Repeated text and links can be valid. If an actual locator is ambiguous, inspect its scope and the intended interaction instead of deleting unrelated content.
- A control repeated once per item needs an accessible name that says which item it acts on. Identical names across items leave a name-based lookup resolving to an arbitrary one, and a control that stays exposed after the pointer leaves its item makes that worse.
- Keep simultaneously available controls independently operable by pointer and keyboard. When adding controls, update their shared layout so their hit areas do not overlap and intercept each other's input.
- Derive state ownership and persistence from requirements: distinguish per-view, per-session and shared data. Do not reset persisted user data on startup. For persistent data, initialize required records only for a new store or an explicit migration. Later startups must preserve user edits, deletions and archive state; a missing record does not mean the store is new. Reset data only when the requirements explicitly demand it. Provide a loading state when initialization is asynchronous.
- Use local assets where practical. Add styling, animation, asynchronous updates or external services when required; keep interactions responsive and report failures clearly.
- Acceptance specs are examples of required behavior, not permission to hardcode test outcomes or omit untested requirements.
"""


UI_CONTRACT_DATA = """\
- Treat examples as examples unless the requirement explicitly identifies initial records or enumerated values. Implement general handling for other valid inputs. Preserve required initial data without overwriting existing user data; do not invent broad lists or fixed sample accounts.
"""

UI_CONTRACT_SESSION = """\
- Derive authentication routes, redirects, labels and session lifetime from the requirements and existing app. Keep authentication state isolated between users; preserve sessions only as required. Failed authentication must not create a session or mutate protected data. Choose error disclosure appropriate to the security requirements.
"""


PERFORMANCE_CONTRACT = """\
Performance and robustness:
- Measure slow operations using observed timings and the configured runtime budgets; do not assume a fixed CPU slowdown or browser count. A timeout can reflect a missing element, incorrect state or an unresolved request; inspect the actual failure before changing performance settings.
- Keep request handlers responsive and avoid unnecessary work. Do not introduce password-hashing implementations or reduce cryptographic strength to improve timings. Follow the required authentication behavior, using an existing authentication service when available; do not replace authentication with plaintext password storage or bypass credential checks.
- Set cookie flags, scope and lifetime from the deployment protocol and session requirements. Use HttpOnly for session cookies and Secure on HTTPS; do not hardcode a host or lifetime. Preserve required client-side interactions and persistent storage semantics.
"""

ARCHITECTURE_CONTRACT = """\
Runtime integration:
- Preserve the platform contract: frontend/ has npm run build producing frontend/dist/; backend/ has npm start and reads PORT (default {port}). Within that contract, preserve the existing application architecture and choose libraries or storage appropriate to the requirements and available environment.
- Prefer existing dependencies and avoid unnecessary installation. Do not prohibit frameworks, native modules or durable storage when the task requires them. Use the configured package registry.
- Handle expected request errors with appropriate responses, including 404 for missing resources. Log unexpected failures; do not suppress uncaught exceptions and continue serving potentially corrupt state. Preserve data integrity and use the runtime's recovery mechanism.
"""

VERIFY_FULL = """\
Verify before you finish: `npm run build` in frontend/, start the backend with `ARC_EXTRA_PORTS=0 PORT={smoke} npm start`, one curl per new endpoint (one success, one error case), stop the server. The harness runs the acceptance specs for this node right after your turn and hands you the failures; when a node has no spec of its own, write a short check for it instead of assuming it works.
"""

VERIFY_MINIMAL = """\
You have no shell in this turn. The harness runs `npm run build`, starts the backend and runs the acceptance specs after your turn, then supplies any failures. Work within the configured request and output budgets. Preserve the existing application structure and create or edit the files needed by the requirements; do not combine unrelated modules merely to reduce file count. Use the supplied file listing and source evidence first, and inspect additional files when needed to resolve uncertainty. Batch independent small edits where practical, split changes that would exceed the response budget, and avoid rereading unchanged files without a reason. Check syntax and imports before finishing, then give a brief summary.
"""

PORT_RULES = """\
Ports: run your own smoke servers ONLY with `ARC_EXTRA_PORTS=0 PORT={smoke} npm start` (port {smoke}). NEVER bind port {port} — the runner watches it and terminates the run. Stop every server you started before you finish. Do not run git; the harness commits.
For a background server in a one-shot shell, redirect the entire command group, including stdin, so descendants cannot hold the tool's capture pipes open:
```sh
(cd backend && exec env ARC_EXTRA_PORTS=0 PORT={smoke} npm start) < /dev/null > smoke-server.log 2>&1 &
echo $!
```
Set the shell tool workdir to the application directory and run this command unchanged. Keep every cd inside the redirected parentheses; prepending cd ... && outside them creates another background shell that retains the capture pipes. Retain the printed PID for cleanup; inspect smoke-server.log and confirm HTTP readiness before testing. Do not assume a successful background launch means the app is ready. Rebuild frontend/ after source changes. Stop your server before ending the turn so the harness can start its own.
"""


NODE_PROMPT = """\
{preamble}
{node_spec}
{design}{ancestors}
{tests}
{ui}{performance}
{verify}
""" + PORT_RULES

NODE_PREAMBLE_EXTEND = """\
Implement requirement node {node_id} in the existing application (frontend/ built by `npm run build` into frontend/dist/; zero-dependency Node backend in backend/, `npm start`, PORT env var). Extend the app; do not rewrite or break existing features.
"""

NODE_PREAMBLE_CREATE = """\
Build a full-stack web application in the current working directory that implements requirement node {node_id} (the whole requirement tree is at {req_dir}; further nodes, if any, come in later turns — leave room for them but implement only this one).

""" + ARCHITECTURE_CONTRACT + """
Mandatory files (all in this turn): frontend/package.json (with the `build` script), the frontend page sources plus the tiny build script that fills frontend/dist/, backend/package.json (with the `start` script, empty dependencies) and backend/server.js.
"""


REPAIR_PROMPT = """\
The acceptance specs for requirement node {node_id} just ran against your app: {passed}/{total} passed. Failing tests (Feature / where it failed / what was observed / the last steps before failure):
{failures}
{test_location}
{corrections}{slow}{sources}
Fix frontend/ and/or backend/ so these tests pass without breaking the passing ones. Work within the configured request budget. Use the supplied evidence to identify the cause, read relevant sources when needed, and make focused edits. For a failed post-action assertion, trace the preceding actions and identify the element and record actually acted on. With repeated controls, inspect locator scope, ordering, visibility, and hover/focus state before assuming a storage or rendering failure. A strict mode violation is a different fault and needs a different repair: it says the locator resolved to more than one element, which is a fact about the document rather than about visibility -- elements hidden with display:none still match, so hiding, re-labelling or re-scoping the duplicate cannot clear it. Exactly one element in the document may match, so remove the duplicate element or empty its text, even though that changes a rendered element. Preserve keyboard access and the required interaction semantics when resolving ambiguity. Preserve behavior beyond the tested inputs. The harness rebuilds and re-runs these specs right after your turn, and the spec files are read-only ground truth.
For persistent data, initialize required records only for a new store or an explicit migration. Later startups must preserve user edits, deletions and archive state; a missing record does not mean the store is new. Reset data only when the requirements explicitly demand it.
""" + PORT_RULES

FINAL_CHECK_PROMPT = """\
Final end-to-end check of the web application in the current directory:
1. `npm run build` in frontend/ — fix any error.
2. Kill leftover servers, start the backend with `ARC_EXTRA_PORTS=0 PORT={smoke} npm start`, confirm `curl http://127.0.0.1:{smoke}/` serves the app and every API endpoint answers (success and error cases).
3. Audit required flows and states against the contracts below. Check accessible names, unique IDs and correct label associations. Resolve observed locator ambiguity in its intended scope; repeated text and destinations can be legitimate.
{tests}
{ui}{performance}
""" + PORT_RULES

REHEARSAL_REPAIR_PROMPT = """\
The app failed the pre-grading startup rehearsal. The runner executes exactly:
1. cd frontend && npm install && npm run build   (must exit 0)
2. cd backend && npm install && npm start        (must bind PORT and stay up)
Rehearsal error:
{error}
Fix the project so this sequence works (typical causes: a require() path that does not match a real file, a file referenced but never written, a startup syntax error, a dependency missing from package.json). Verify: build the frontend, start the backend with `ARC_EXTRA_PORTS=0 PORT={smoke} npm start`, confirm it binds, stop it. Never bind {port}. Write the fix now.\
"""

# 「official」「public」这类词在正赛上是假的：卷子可能是 harness 按需求树自己出的
# （见 author_acceptance_suite）。**提示词里一句不成立的话，模型会照它行动** ——
# 老的 VERIFY_FULL 承诺「官方测试随后会跑」并禁止自建测试，于是每个节点都在等一个
# 永远不来的裁判。这里只说卷子在哪、它规定了什么、不许改。
ACCEPTANCE_TESTS_PROMPT = """\
ACCEPTANCE SPECS (the behavior this build is judged on; when they and the requirement text disagree, report the conflict instead of silently discarding the requirement) live under {tests_dir}. Files: {files}. They define routes, hrefs, accessible names, option labels, exact texts, error wording and action order. Never modify, copy or delete them.
"""

INLINE_SPEC_HEADER = """\
The spec files are quoted below in full — do NOT spend tool calls reading them or the requirement again:
"""


def inline_spec_text(tests_dir: Path, files: list[str], max_chars: int) -> str:
    """Quote spec + helper files into the prompt (bounded). Each read_file the
    model would otherwise issue is a full-context round trip (~11k tokens)."""
    parts = []
    total = 0
    for rel in files:
        path = tests_dir / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if total + len(text) > max_chars:
            return ""  # too big to inline; let the model read selectively
        total += len(text)
        parts.append(f"--- {rel} ---\n{text.rstrip()}\n")
    return INLINE_SPEC_HEADER + "".join(parts) if parts else ""


def locate_acceptance_tests(tree: dict, bundle_dir: Path) -> Path | None:
    """ARCBENCH_TESTS_DIR, then the runner's /workspace/tests, then the public
    specs shipped in the bundle (matched by requirement root name)."""
    candidates: list[Path] = []
    env_dir = os.environ.get("ARCBENCH_TESTS_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.append(Path("/workspace/tests"))
    bundled = bundle_dir / "public-tests"
    manifest = bundled / "manifest.json"
    if manifest.is_file():
        try:
            mapping = json.loads(manifest.read_text(encoding="utf-8"))
            root_name = str(tree.get("name", "")).strip()
            for req_id, title in mapping.items():
                if str(title).strip() == root_name and (bundled / req_id).is_dir():
                    candidates.append(bundled / req_id)
        except Exception as exc:  # noqa: BLE001
            log(f"[tests] manifest unreadable: {exc}")
    for cand in candidates:
        try:
            if cand.is_dir() and any(cand.rglob("*.spec.ts")):
                return cand.resolve()
            log(f"[tests] candidate {cand}: {'no *.spec.ts' if cand.is_dir() else 'absent'}")
        except Exception as exc:  # noqa: BLE001
            log(f"[tests] candidate {cand} unreadable: {exc}")
    return None


TRUNCATION_CORRECTION = (
    "Your previous response was cut off by the output limit and nothing was saved. "
    "Write exactly ONE file per response, complete, and stop -- do not try to emit "
    "several files in one turn."
)


def truncation_correction(ok: bool, text: str) -> str | None:
    """Tell the next round that the last one died on the output limit.

    The implement turn already retries once on truncation ("one file per response").
    Repair and rewrite turns had no handling at all, so a truncated repair burned the
    round silently and the node then ran out of budget.

    Seen on ticket-booking with glm-5.3-flash: the rewrite turn ran 582s and came back
    `output_truncated: Model output was truncated (max_tokens); the response is
    incomplete`, with `completion_tokens` exactly 32768 -- the proxy's floor, which the
    kernel's own 4096 is raised to. Nothing was written, REQ-1 never passed, and the
    node hit its 1500s cap. A reasoning model spends part of that budget on reasoning,
    so the room left for the file is smaller than the number suggests.

    This adds no retry (the round budget is already spent); it makes the *next* round
    aware, which is the cheap half of the fix.
    """
    if ok or "truncated" not in (text or "").lower():
        return None
    return TRUNCATION_CORRECTION


def phase_for_label(label: str) -> str:
    """Which routing phase a turn belongs to, from the label it was given.

    This decides which model a request is sent to whenever model routes are
    configured, and it was an inline conditional with no test of its own -- the
    routing rules were covered, the thing that feeds them was not. A rule that
    says `"phases": ["repair"]` is worth exactly as much as this function's
    agreement about what a repair is called.

    The labels are the ones the flow actually emits: `REQ-3.2 implement`,
    `REQ-3.2 repair 1/5`, `REQ-3.2 rewrite (repair 1)`, `full-suite repair 1/3`,
    `final check`, `design`.
    """
    if any(word in label for word in ("repair", "rewrite")):
        return "repair"
    if "final check" in label:
        return "verify"
    if "design" in label:
        return "design"
    return "implement"


def spec_base_ports(tests_dir: Path | None) -> list[int]:
    """Ports the specs hard-code as their default base URL (e.g. 3301)."""
    if not tests_dir:
        return []
    ports: set[int] = set()
    for path in tests_dir.rglob("*.ts"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"https?://(?:127\.0\.0\.1|localhost):(\d{2,5})", text):
            ports.add(int(m.group(1)))
    return sorted(ports)


def acceptance_tests_prompt(tests_dir: Path | None, web_port: int, smoke_port: int,
                            files: list[str] | None = None, inline: bool = False) -> str:
    if not tests_dir:
        return ""
    if files is None:
        files = sorted(str(p.relative_to(tests_dir)) for p in tests_dir.rglob("*.ts"))
    text = ACCEPTANCE_TESTS_PROMPT.format(tests_dir=tests_dir, files=", ".join(files[:40]) or "(none)")
    if inline:
        text += inline_spec_text(tests_dir, files, int(os.environ.get("OCTOS_ARC_INLINE_SPEC_CHARS", "24000")))
    extra = [p for p in spec_base_ports(tests_dir) if p != web_port]
    if extra:
        ports = ", ".join(map(str, extra))
        text += (f"PORT CONTRACT (mandatory): the specs default to port(s) {ports} while the grader starts the "
                 f"backend with PORT={web_port}. Serve the identical app on BOTH the PORT value and port(s) {ports}: "
                 f"create a SEPARATE http.createServer(handler) for each port (one Server can listen only once — "
                 f"calling listen() twice throws ERR_SERVER_ALREADY_LISTEN and the process dies), binding the extra "
                 f"port(s) only when process.env.ARC_EXTRA_PORTS is not '0'. The grader sets ONLY PORT, so the "
                 f"extra port(s) ARE bound during grading.\n")
    return text


# ---------------------------------------------------------------- authoring the acceptance suite

# 正赛没有官方卷子：`/workspace/tests` 不存在，bundle 里也没有 public-tests，于是
# `locate_acceptance_tests()` 返回 None，整套验收机制（每节点跑 spec → 失败清单 →
# 修复回合 → 全站体检）全部空转，叶子节点因为「没有 spec」被判成通过。
# 这就是本地练习（自带 public-tests）与正赛（卷子隐藏）之间唯一的差别。
#
# 这一段是补上那个缺口的：**让模型自己按需求树出一份卷子**，出完之后它就是一个
# 普通的 `tests_dir`，下游一行都不用改。
#
# 为什么是「先出卷、再施工」而不是「一边施工一边出卷」：卷子出完才算得上一份
# 独立的判据。如果实现的人自己决定考什么，考的就会是他已经写出来的东西。
#
# 出卷的依据**只有需求原文**（`acceptance_digest` 搬的就是 requirements.yaml 里
# 的字段），一个字的题目都没有写死在这里 —— 同一份代码在 sheet 和 github 上跑的
# 是同一段提示词，产出两份完全不同的卷子。
ACCEPTANCE_AUTHOR_PROMPT = """\
You are writing the acceptance suite for a project that DOES NOT EXIST YET. These \
Playwright specs are the only machine check this build will ever get: whatever you do \
not assert here, nobody checks; and any assertion the requirement text does not support \
sends the builders chasing a defect that is not there. Both mistakes are expensive, so \
when the text does not pin something down, assert less and assert it exactly.

Read every digest file listed below before you write anything. Then write ONE spec file \
per requirement node, in {out_dir}, named EXACTLY after the node id: `<NODE_ID>.spec.ts` \
(for example `REQ-1-2-1.spec.ts`). Use write_file, ONE file per response, and write \
nothing but those spec files.

Rules that decide whether a spec survives contact with a real build:
- `import {{ test, expect }} from '@playwright/test';` and always navigate relatively, \
`await page.goto('/')` — never an absolute host or port. The runner supplies the base URL.
- A digest quotes the enclosing requirements too, not just the node: a contract stated at \
an outer layer (an accessible name, a role, a persistence rule) binds this node as well, \
even where this node does not repeat it. Assert those.
- These requirements name things exactly — accessible names, labels, roles, column \
headers, button text, error wording, the order of items. Where the text names it, assert \
that exact thing, with `exact: true` on every name match: \
`page.getByRole('button', {{ name: 'Create account', exact: true }})`, \
`page.getByRole('grid', {{ name: 'Worksheet grid', exact: true }})`, \
`expect(locator).toHaveAttribute('aria-selected', 'true')`. A bare `name:` matches \
substrings, so it silently resolves to any element whose label merely contains the \
wording; `exact: true` makes that collision fail loudly instead of quietly testing the \
wrong element.
- Roles are exactly as granular as the text names them. `textbox`, `searchbox` and \
`combobox` are three different roles: a plain text input exposes `textbox` whatever its \
purpose, so a `searchbox` assertion demands a role the text never asked anyone to expose. \
Where the text itself offers alternatives — 'A' or 'B' — accept those alternatives, and \
only those: every branch of an alternation (`.or()`, a regex, a list of names) is a shape \
the implementation is licensed to expose, so a branch the text does not name is an \
assertion about a DOM nobody was asked to build. Where the text names one shape, pin that \
one — never add `.or()` as a fallback for 'the build might do it differently'.
- When the same wording can appear in more than one place, scope the locator to its \
container — `locator.filter({{ hasText: ... }})`, a `getByRole` region, the form it lives \
in — instead of matching on the whole page, and prefer role plus name over bare text.
- Where the text does NOT pin a string down, assert the weakest thing that still shows \
the behavior exists — visible, countable, a URL — and never invent a message. Where the \
text DOES pin a string down, assert that exact string with `toHaveText` on the element \
that carries it; `toContainText` is only for wording the requirement itself describes as \
containing something (a summary that includes a name, a message that lists items) — never \
a looser substitute for an exact string, which lets a wrong wording pass. Never assert \
the text of a whole page or a large container (`innerText`, `body`/`main` with \
`toHaveText`): dynamic dates, counters and previews live in there, and one changed digit \
fails the spec while the feature works. Assert the specific element that carries the text. \
And never assert a rendering the text does not state: a digit pattern (`/[0-9]+ files/`), \
a word order, a separator. The text saying a count exists is not the text saying how it is \
rendered.
- Several small independent `test(...)` blocks per file, one per scenario or distinct \
behavior. No test may depend on another file's state, and no test may depend on the one \
before it: each starts from a fresh page and creates what it needs, with values unique to \
this run (a fresh email, an appended suffix) unless the text fixes the literal name. Never \
assume the store starts empty, that a previous test's rows are absent, or that a default \
branch is still untouched. An assertion that something does NOT exist must be scoped to \
what this test created — its row, its list, its form — never the whole page.
- **One test, one state.** Every `test(...)` block creates the objects it works on, under \
names only that test uses. Two specs must never write to the same fixed resource — the same \
workbook or sheet name, the same account, the same record: whichever one runs first changes \
it, and the other then fails for good with neither spec wrong, because they are contending \
for a single thing. Where the requirement text fixes a literal name because the system \
pre-provisions that object, that object is **shared**: read it and assert on it, but never \
leave it in a state your own test produced — if your test has to change it, put it back the \
way you found it. **Putting it back is part of the test, not politeness**: the password you \
changed, the status you moved forward, the membership you revoked, the record you renamed — \
each alters an object that existed before your test. Tests in one file run one after another \
against one server, and a second clean run starts from the system as it was: a change left \
behind becomes the next test's wrong starting state, and the failure lands on a spec that \
did nothing wrong. Two specs that each need an object of the same kind each create their own.
- **The starting state is one, and the text can describe it inconsistently.** Two nodes may \
describe the same pre-provisioned object — the seeded workbook, the seeded account, its rows — \
in ways that cannot both be true in a single run. Assert the starting state only as far as your \
own node's text states it, and never borrow another node's description of what is already \
there: a check the shipped starting state cannot satisfy fails every build forever, no matter \
what the implementers do.
- **A name you did not create must have a source.** Every fixed name a test reads or \
asserts — a seeded workbook, a branch, a pre-provisioned account — must appear in the \
digest verbatim; a name patterned on one that does (`…-s1` quoted does not license `…-s2`) \
asks the system for an object nobody was told to have. Where a test needs an object of its \
own, it creates it under a name unique to that test.
- Drive the app the way a user would (`fill`, `click`, `press`, `goto`), then assert what \
the user sees.
- A helper that builds a locator is a PLAIN function, never `async` — `function cell(page) \
{{ return page.getByRole('gridcell', {{ name: 'A1', exact: true }}); }}`. An `async` helper \
called inside `expect()` passes a Promise to the matcher and Playwright rejects it outright \
("toBeVisible can be only used with Locator object"): that assertion then fails for **every** \
build, however correct the app, and no fix in the application can end it. If a helper must \
be `async`, `await` it before handing the result to `expect()`.

Five further checks are mandatory. Each one is invisible to every other kind of test when \
it is missing, and each one is where a plausible build quietly diverges from the text:
- Persistence is checked from a NEW browser session, never from the page that wrote it. \
Wherever the text says a result persists, is stored on the server, survives, remains \
visible, or is seen by another account, open a second context in the test \
(`const other = await page.context().browser().newContext();`), sign in there if the text \
names who sees it, and assert that what the first session produced is there. Inside one \
session, a value that never left that session looks exactly like one that reached the server.
- A scenario that names two accounts or two roles gets TWO sessions, one per account, and \
asserts what the OTHER one sees — never a second account used only as a name inside the \
first session's data.
- Before committing to any assertion, ask what the requirement text physically allows the \
DOM to show, and assert only that. A native `<select>`'s options are not rendered while \
its popup is closed — assert the select's value or the selected option's text, never that \
the option element is visible; the control's own text is every option's label concatenated, \
so it equals no single option — assert its value (`toHaveValue`, `inputValue()`), never \
`toHaveText` on the control itself. A cell is not an input either: put text into a cell the \
way the editor the text names is driven — type into the inline edit box or the named box, \
and drive the cell itself with a click or a double-click. Something the text places in one \
state (a create button on \
the list page, a chip that appears after filtering) must not also be asserted absent in \
another state unless the text itself says so. An assertion no compliant implementation can \
satisfy fails the build forever, and no amount of fixing the app can end it.
- Every rule with numbers or a format (a length, a range, allowed or forbidden characters) \
gets boundary cases, one test per case: the shortest allowed value, the longest allowed \
value, one just past a limit, and one that breaks the format. The example the text happens \
to quote is not a boundary case.
- Every name the text quotes is asserted as the exact accessible name. Every error message \
the text states for a field is asserted by its exact text; the `aria-invalid` / \
`aria-describedby` wiring is asserted only where the text itself requires the message to \
be tied to its field — a message merely rendered somewhere on the page does not satisfy \
that, but do not invent the requirement where the text is silent. Do not add structural \
requirements of your own anywhere: tag names, routes, aria attributes, the internal layout \
of a control. If the requirement text does not state it, an assertion about it is a defect \
report about something nobody was asked to build.
- Whenever a flow creates or imports something the user keeps — an account, a record, a \
document — at least one test creates or imports it and then immediately uses it the way the \
text intends: writes into it, renames it, deletes part of it — reloads, and asserts the \
effect survived. Asserting that the creation dialog opened is not using what it made: a \
build whose creation path leaves the object half-initialized passes every creation check and \
fails the first real use.
- Where the text states what a role may not do — 'visitors cannot…', 'a Read user cannot…' — \
at least one test attempts exactly that act from the wrong side of the line and asserts the \
refusal the text states: the message it quotes, or the state staying unchanged. A \
restriction no test exercises is a sentence of the requirement with nothing behind it.
- The specs you write are one suite, not a pile of independent files: nothing you assert \
may contradict anything else you assert, across files included. If one test registers an \
account and a later test signs in with that address, both must expect success; if a test \
deletes a row, no test may still expect it present afterwards. Before you finish each file, \
re-read the earlier ones and hunt for pairs of opposite expectations — at most one of any \
pair can be right.
- Assert an exact message only where THIS node's text states it. Wording quoted in some \
other flow's requirement is that flow's business; borrowing it makes this node's spec \
demand a message this feature was never asked to produce.

Nodes you are writing specs for, in order — you are responsible for all of them:
{index}

Digest files, one per node (read the matching one before writing its spec):
{files}

Write every listed spec now, one file per response, and stop when the last one is written.\
"""

# B 档：独立找茬回合。出卷收尾后，换一个**审计视角**再过一遍 —— 它看不到实现
# （那时候实现还不存在），也看不到出卷人的推理，只有需求原文和卷子本身。它要回答
# 的问题只有一个：**逐句读，如果实现方对这句话偷懒，现在这份卷子抓得住吗？**
#
# 和出了卷就冻结相比，多这一道是有依据的：出卷人和答卷人是同一个模型，它的卷子
# 会系统性地避开它自己实现里会打折扣的维度 —— 2026-10-01 的正赛实测是 169 个
# 用例里只有 1 个跨浏览器会话的检查（全是单会话内的断言），而同一个模型的实现
# 恰好写了「每个会话一份平行世界」的存储。缺的不是认真，是**科目**：单会话的
# 卷子结构上测不出会话隔离，这条盲区靠出卷人自觉补不齐，要靠一个只对文字负责的
# 审计回合去查。
#
# 提示词里的盲区清单是通用测试技法（跨会话、多账号、边界值、可访问名与错误
# 关联），不是任何任务的字段 —— 同一段文字在 sheet 和 github 上抓出来的东西
# 不一样，规则本身一个字的任务数据都没有。
ACCEPTANCE_SUITE_AUDIT_PROMPT = """\
You are auditing an acceptance suite that somebody else wrote, BEFORE the application it \
describes is built. You will not see any implementation — your only material is the \
requirement digests and the spec files, and that is deliberate: your job is to find the \
sentences the specs fail to look at, not to explain failures.

For every node listed below, read its digest and its spec side by side, sentence by \
sentence. A sentence is covered only when some assertion in the spec would FAIL if an \
implementation quietly skipped that sentence or faked it. Where a sentence is not covered, \
append a small, independent `test(...)` block that asserts exactly what the text states — \
and no more: an assertion the text does not support sends the builders chasing a defect \
that is not there, which is just as expensive as a missing one.

Hold every file against these questions, in order:
1. Would each assertion still pass against an implementation that does exactly what the \
digest states, in the plainest DOM a browser allows — and would it fail if that \
implementation quietly skipped the sentence?
2. Does every test end with each pre-existing object exactly as it found it — password \
unchanged, status unchanged, membership unchanged? Tests in one file run one after another \
against one server, and a clean second run must pass: a test that leaves a shared object \
altered hands everything after it a starting state that no longer exists.
3. Would this file pass if it ran again straight after itself? Any fixed name it creates \
(an account, a repository, a row) must be unique to the run, not left over from a previous one.
4. Does every fixed name the test reads or asserts have a source in the digest, quoted \
verbatim — helper arguments included? A name patterned on a quoted one (`…-s2` from `…-s1`) \
is not quoted by it.
5. Is every quoted string byte-identical to the text; every role the role the text names (a \
`searchbox` where the text says a text field bets on a DOM shape nothing asked for); every \
offered alternative ('A' or 'B') accepted in both forms; every asserted rendering (a digit \
format, a color, a separator) one the text states?
6. Where the text states what a role may not do, does some test attempt exactly that and \
assert the refusal the text states?
7. Does every sentence that orders acts in time or describes a lifecycle — 'saves it, then \
reopens', 'closes and shows…', 'after X, Y happens' — appear as the ordered pair of acts \
with the state the sentence ends in asserted? A spec that performs the first act and \
asserts only its immediate effect leaves the ending state unverified, and the text's \
'closes' / 'returns' / 'reopens' is a requirement about that state, not decoration.
8. Does any assertion accept more than one shape — `.or()`, a regex alternation, a list of \
alternative names, roles or texts? Each accepted branch is a shape the implementation is \
licensed to expose: list them, and keep only the branches the text itself names. Where the \
text names one shape, pin exactly that one.
9. Where the text describes a control or an interaction without naming it (no accessible \
name, no role, no label), does the spec assert the behaviour the text states instead of a \
name, role or tag of the spec's own choosing? Where the text names the control by its \
wording, assert that exact wording — the implementer takes names from the text, never from \
the spec.

Look hardest at the checks that are invisible from inside a single browser session or from \
inside a single spec file, because an author who pictures the happy path misses them first:
- a result the text says persists, is stored, survives or is seen by another account, \
verified only in the session that produced it. Verify it from a fresh context \
(`await page.context().browser().newContext()`), signed in as whoever the text says sees it.
- a scenario that names two accounts or two roles driven as one session — give each account \
its own context and assert what the other one sees.
- something the app lets the user create or import (an account, a record, a document) whose \
spec only drives the creation flow and never uses what was created in the same test — write \
into it, rename it, delete part of it, reload and assert the effect. A creation path wired to \
a half-initialized object passes every creation-dialog check and fails the first real use.
- a rule with numbers or a format (a length, a range, allowed or forbidden characters) \
where the spec only tests the shape of the example the text quotes — add the boundary \
values: shortest allowed, longest allowed, one just past a limit, one breaking the format.
- a name the text quotes (button, label, heading, column header, role) that no assertion \
ever checks; an error message the text states that the spec only checks loosely — assert \
the exact message text, and the `aria-invalid` / `aria-describedby` wiring only where the \
text itself requires the message to be tied to its field.
- a step in the scenario with no assertion after it at all.
- a test that changes an object the text pre-provisions — a seeded account's password, a \
seeded record's state — and never restores it: the tests after it, and every clean rerun, \
start from a state that no longer exists. Add the restore step, or have the test work on an \
object it creates itself.
- a restriction the text states ('visitors cannot…', 'a Read user cannot…') that no test \
ever attempts from the forbidden side — the refusal the text names is a sentence of the \
requirement with no check behind it.

An assertion the text does not support is a defect in the suite itself, and the run cannot \
end while one stands — check every file against these shapes and rewrite the offending \
assertion (keep the test, drop or scope the assertion):
- an assertion about wiring or structure no digest mentions — aria attributes, tag names, \
routes, the internal layout of a control;
- an assertion no compliant implementation can satisfy: a native `<select>`'s option \
asserted visible while its popup is closed; something the text says exists asserted absent \
somewhere else; two assertions, in this file or across files, that demand opposite \
outcomes of the same act — at most one of the pair can be right, remove the wrong one;
- a name match without `exact: true`, where a longer label could contain the wording;
- the text of a whole page or a large container (`innerText`, `body`/`main` with \
`toHaveText`) where dynamic values live — scope it to the element that carries the text;
- "nothing else exists" or count assertions that assume an empty store or untouched prior \
state instead of the rows this test itself created;
- exact wording borrowed from a flow other than the node's own digest.

Separately, flag — and think twice about — every bet on a DOM shape no digest fixes. These \
do not always have a rewrite, so weigh them against the text instead of deleting them:
- `selectOption(` on a control no digest states as a native select: a custom dropdown does \
not implement it and the call throws;
- `.fill()` / `.type()` straight onto a grid cell where the text puts the editor elsewhere \
(an inline edit box, a formula bar) — a cell need not be an input;
- `toHaveCSS` with a color value no digest states — a style name like 'Red fill' is not an \
RGB value;
- a digit format (`/[0-9]+ files/`), a word order or a separator the text never shows;
- a role more specific than the text names — `searchbox` where the text says a text field, \
`menuitem` where it says an option: the text's role is what the implementer was asked to \
expose;
- a fixed name no digest contains and the test does not create — one extrapolated from a \
quoted name (`…-s2` from `…-s1`) belongs to an object the system was never asked to have.

Mechanical pre-scan of this batch (it only counts and matches wording; you decide what it \
means):
{mechanical}

The missing-check list inside the pre-scan is mandatory, not advisory: before this batch is \
finished, every item it names must leave with a disposition — either the missing check is \
added to the spec, or a waiver with a reason is written to the waivers file, in the exact \
form the pre-scan asks for. An item left unaddressed and unwaived is the one failure this \
audit exists to prevent.

Rules for your edits:
- Never delete, weaken or rename an existing test. Add; leave what is already covered alone.
- Each added test stands alone: fresh page, its own data, no dependency on another test.
- Write back ONLY the files you actually changed, with write_file, ONE file per response, \
the COMPLETE updated file — keep every existing line of it.
- If a node below has no spec file yet, write one now covering its digest: this is the last \
chance before the suite is frozen.
- When a node's spec already covers its digest, leave the file untouched and say so.

Nodes in this batch, with the files that belong to them:
{index}

Files (read each digest and each existing spec before judging it):
{files}
"""

# 缺账清单的「追账」小回合（2026-10-07，硬任务升级；同日并入结构性缺陷）：找茬回合
# 跑完，清单上还有条目没有下文 —— 缺考没补检查、过不了的断言没修，也都没写豁免理由。
# 这一轮只干一件事：逐条要个交代（修，或写明为什么不修）。**只追一次，不循环**：排练
# 实测软提示下模型补 21 条测试只消掉 1 条缺考；把「每条必须有交代」讲成回合的存在理由，
# 再要不到就如实进报告和完成信号。
ACCEPTANCE_DISPOSITION_PROMPT = """\
This acceptance suite is about to be frozen. A mechanical pre-scan flagged items that the \
audit rounds left with no disposition yet — neither fixed, nor waived with a written reason. \
An item is either a missing check the requirement text implies, or an assertion no \
implementation can pass (a structural defect in the spec). Every item below must be answered \
now, one way or the other: the list is mandatory, and finishing with an item silently \
un-addressed is not allowed.

For each item, either:
- fix it: add the missing check — a small, independent `test(...)` block asserting what the \
digest states and no more — when the text really states it and it is checkable; or repair \
the assertion so that a correct implementation can pass it; or
- judge it un-checkable, not actually required by the text, or not a real defect (the scanner \
is mechanical), and record a waiver with a reason in the waivers file.

Waivers file (write_file; if it already exists, keep every existing line and add yours):
{waivers}
One line per waived item, exactly `- <node id>: <reason>`. A waiver without a reason is not \
a disposition: say why the text does not support the check, why no mechanical check can \
pin it down, or why the flagged assertion is not actually a defect.

Items with no disposition yet:
{items}

Rules: never delete, weaken or rename an existing test; write back ONLY files you actually \
changed, each with its COMPLETE content; if an item turns out to be covered by a check you \
find when you re-read the file, leave the file untouched and say so.
"""

DIGEST_INTRO = """\
Everything below is requirement text, quoted verbatim. The requirements nest: a contract \
stated at an enclosing layer (an accessible name, a role, a persistence rule) binds this \
node too, even where this node does not repeat it.
"""

# 自产卷子的落脚点（相对 output_dir）。放在应用目录里而不是 `.arc/` 底下，有两个
# 原因：`.arc/` 是 harness 的私有区、写它会被 TurnMonitor 记成「改了受保护文件」；
# 而 `acceptance/` 是一个真实项目本来就会有的目录，出现在交付物里不算异物。
AUTHOR_SUITE_DIR = "acceptance"


def acceptance_digest(layers, node_id: str, cap: int = 20000) -> str:
    """一个节点的出卷依据：祖先链（根 → 自己）+ 自己，全部原文。

    与 `orchestrator._ancestors_text()` 同一个理由（那里是给施工的人看，这里是给
    出卷的人看）：跨节点的硬性契约写在**它适用的那一层**的描述里。出卷的人只看
    叶子，卷子里就不会有那条断言；而卷子里没有的东西，实现的模型没有任何理由去
    满足 —— 「工作表网格必须叫 Worksheet grid」这种规矩一旦漏掉，24 个叶子会各
    自发明一个名字。

    不做筛选是刻意的，和 `_ancestors_text()` 一样：抽取「哪句重要」是主观判断，
    判断错了没有任何测试会发现；搬运原文没有这个问题。
    """
    if node_id not in layers.by_id:
        return ""
    parts = []
    for anc in layers.chain(node_id):
        info = layers.by_id.get(anc)
        if info is None:
            continue
        head = "## This node" if anc == node_id else f"## Enclosing requirement {anc}"
        body = describe_node(info.node).strip()
        if len(body) > cap:
            body = body[:cap] + "\n[... requirement text truncated ...]"
        parts.append(f"{head}\n{body}")
    return DIGEST_INTRO + "\n\n" + "\n\n".join(parts) + "\n"


def practice_stage() -> str:
    """排练开关：`OCTOS_ARC_PRACTICE_STAGE=specs` → 这一轮在**出卷这一站之后**收工。

    单独练出卷（拿真实需求跑模型出卷 + 卷面体检）时，后面的蓝图/施工不练也
    照样要花几小时和真金白银 —— 这个开关只决定「跑完哪一站停」。
    正赛平台从不设置它；设置了也只是提前收工，不碰任何判据。
    """
    return os.environ.get("OCTOS_ARC_PRACTICE_STAGE", "").strip().lower()


# 决赛快跑节奏（2026-10-08 首领拍板）：平台形态下默认预算的封顶 —— 小树 6h、
# 大树 10h。40 是 sheet 决赛（28 节点）与 github 决赛（70 节点）两棵实测树之间的
# 分界。显式 `OCTOS_TIME_BUDGET` 与本地长跑（削平关成 0）都不经过这里。
FAST_PACING_SMALL_TREE_MAX_NODES = 40
FAST_PACING_SMALL_TREE_SECONDS = 6 * 3600
FAST_PACING_LARGE_TREE_SECONDS = 10 * 3600


def effective_budget(budget: int, *, explicit: bool, node_count: int, seconds_per_node: int,
                     max_wall_seconds: int, platform_wall_seconds: int) -> int:
    """这次跑多久收工。**三个方向：先按节点数抬高，再按决赛节奏封顶，最后按平台墙钟削平。**

    抬高那半：默认 3600s 是给冒烟用的，32 个节点的树要的是小时级。

    封顶那半（2026-10-08 首领拍板，决赛要求快跑）：平台形态下按树体量给默认
    预算封顶 —— ≤40 节点的树（sheet 体量）6h、更大的树（github 体量）10h；
    原公式 1500×节点数 会给 11.7h / 29.2h。到点不再开新回合、直接封箱「已验证的
    最好状态」：分数不会翻车，只可能少赚。显式 `OCTOS_TIME_BUDGET`（本地排练与
    冒烟都走这条）不经这里；本地长跑把墙钟削平关成 0 时也跳过 —— 那两个都不是
    平台形态。

    削平那半是一条**外部事实**，不是我们的选择：平台 22 小时之后会杀掉进程。
    杀掉意味着没有终检、没有彩排、没有上报 —— 交上去的是一个半成品，而报告里
    看不出区别。削了顶多是每个节点的份额小一点（份额是 `remaining/nodes_left`
    现算的，会自动摊到每个节点上），相比之下削平永远是划算的。

    实测的两个数：sheet 42 节点 → 63,000s（17.5h，不用削）；github 65 节点 →
    97,500s（27h，**必须削**）。

    ★ 78000s（21.7h）而不是 79200s（22h）：`remaining()` 是从进程启动算起的真实
    墙钟，而**最后一轮回合是不受预算打断的**（一个 implement 回合最长 2400s）。
    留 1800s 给收尾（终检 + 彩排 + 上报）。

    `max_wall_seconds=0` 关掉削平（本地长跑用得到）。
    """
    if not explicit:
        budget = max(budget, seconds_per_node * node_count)
        if max_wall_seconds:
            cap = (FAST_PACING_SMALL_TREE_SECONDS if node_count <= FAST_PACING_SMALL_TREE_MAX_NODES
                   else FAST_PACING_LARGE_TREE_SECONDS)
            if budget > cap:
                log(f"[flow] finals pacing: {budget}s ({budget / 3600:.1f}h) capped at "
                    f"{cap}s ({cap / 3600:.1f}h) for a {node_count}-node tree — the run "
                    f"wraps up with the best verified state it has by then")
                budget = cap
    if max_wall_seconds and budget > max_wall_seconds:
        log(f"[flow] budget {budget}s ({budget / 3600:.1f}h) exceeds the "
            f"{max_wall_seconds}s ({max_wall_seconds / 3600:.1f}h) wall-clock "
            f"ceiling (platform allows {platform_wall_seconds / 3600:.1f}h); "
            f"clamping so the run can still wrap up on its own")
        return max_wall_seconds
    return budget


def authored_spec_map(suite_dir: Path, node_ids: list[str], leaves: list[str]) -> dict:
    """自产卷子 → 节点：**按我们要求的文件名直接建表**。

    官方卷子那条路需要 `map_specs_to_nodes` 去**解析文件名**（`spec_node_id`），
    因为那些文件名是平台定的，我们只掌握约定。自产卷子不一样：文件名是我们
    自己指定的（`<NODE_ID>.spec.ts`，见 `ACCEPTANCE_AUTHOR_PROMPT`），所以
    「哪个文件属于哪个节点」是已知的，不需要反推。

    反推的代价实测过。`spec_node_id` 的正则要求 id 以 `REQ-` 开头，三种编号
    各试一遍的结果：

        REQ-1-2-1   → 全部认出来     ✅ 正常
        ROOT / 1.1  → 一份都不认     ❌ 每个节点 0 份卷子
        TASK-A      → 一份都不认     ❌ 每个节点 0 份卷子

    而解析失败的卷子不是归到兜底集合，是**被直接丢掉**。「节点名下的卷子数是
    0」在 `_accept` 里的意思又是**直接判通过**（`own_ok = not specs`）。最坏的
    情况于是长这样：磁盘上躺着 24 份卷子、日志一行异常都没有、报告写着 24/24
    全部通过 —— **而一个断言都没执行过**。连「出卷失败，退回没有验收」那条
    兜底日志都不会触发，因为文件确实写出来了。

    按文件名建表把这条路径整个绕开：它与 id 长什么形状无关。

    名字对不上任何节点的文件不丢，放进 `None`（回归集）—— 它们在
    `final_acceptance` 的全量体检里仍然会跑。
    """
    mapping: dict = {nid: [] for nid in node_ids}
    mapping[None] = []
    wanted = set(leaves)
    for path in sorted(suite_dir.rglob("*.spec.ts")):
        rel = str(path.relative_to(suite_dir))
        stem = path.name[: -len(".spec.ts")]
        if stem in wanted:
            mapping[stem].append(rel)
        else:
            mapping[None].append(rel)
    return mapping


def authoring_coverage(spec_map: dict, leaves: list[str]) -> tuple[int, list[str]]:
    """(有几个叶子真的拿到了卷子, 没拿到的那些)。

    判据是**磁盘上的文件**，不是模型说了什么。出卷回合失败、写了一半、写到别的
    目录 —— 都会在这里变成「没覆盖」，而不是变成一次静默的自动通过。
    """
    covered = [i for i in leaves if spec_map.get(i)]
    missing = [i for i in leaves if not spec_map.get(i)]
    return len(covered), missing


def adopt_suite_on_disk(authored: dict, suite_dir: Path, node_ids: list[str],
                        leaves: list[str]) -> dict:
    """出卷交回空表时，把**盘上现成的**卷子按文件名挂回节点。

    这条路的两个来客：本地演练把 `OCTOS_ARC_AUTHOR_TESTS=0` 打开（从卷子阶段
    接力），或者出卷回合还没开就被预算闸门拦下 —— 两种情况下 `acceptance/`
    里的文件都来自上一轮，而它们是这个工作区里**唯一的卷子**。

    空表原样交回去的后果，和 `authored_spec_map` 里写的是同一条：`spec_map`
    为空 → `authoring_coverage` 0/N → 每个节点在 `own_ok = not specs` 下
    **自动通过**，日志与报告却全部正常。挂回同样按文件名直接建表，不解析。

    一份都挂不到**节点**上时交回的仍是**空表**，不是一个「全是空列表的真话表」——
    调用方用 `if mapping:` 判断「出卷成没成」（同一约定见
    `author_acceptance_suite` 末尾），那个判断必须继续读得对。只落进回归
    桶（名字对不上任何节点）的文件不算数：那是上一轮的旧账，挂上它等于让
    全量体检拿旧需求反复判红。
    """
    if authored:
        return authored
    on_disk = authored_spec_map(suite_dir, node_ids, leaves)
    return on_disk if any(on_disk.get(nid) for nid in node_ids) else {}


def authoring_groups(layers, leaves: list[str], max_groups: int = 8) -> list[list[str]]:
    """把叶子按**一级分支**分组，一组一个出卷回合。

    为什么要分回合：一份卷子要 24-47 个文件、每个文件要求先读 8-15KB 的原文，
    挤在一个回合里是 70k+ token 的上下文加几十次工具调用 —— 一出错整个改造一
    就什么都没产出。按分支切开，每回合只管 4-12 个节点，而且**上一个回合写完的
    文件是落盘的**：后面某个回合失败，前面那些卷子照样能用。

    超过 `max_groups` 个分支时把尾部并进最后一组 —— 回合数是有上限的，
    因为出卷用的是和施工同一个时间预算。
    """
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for leaf in leaves:
        chain = layers.chain(leaf)
        key = chain[1] if len(chain) > 1 else chain[0]
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(leaf)
    if len(order) <= max_groups:
        return [groups[k] for k in order]
    head = [groups[k] for k in order[:max_groups - 1]]
    tail = [leaf for k in order[max_groups - 1:] for leaf in groups[k]]
    return head + [tail]


# ---------------------------------------------------------------- 卷面机械体检（suite audit）

# A 档的「验收器配套」：卷子出完之后（找茬回合前后各一次）做一遍**机械抽查** ——
# 只读需求原文和卷子文本，数三样东西：每个节点的句型触发了哪些必考科目、卷子里
# 有没有对应的考法、`test(...)` 的密度。它判不了「写得对不对」（那是找茬回合的
# 事），但它能判「有没有整类缺考」—— 2026-10-01 的正赛实测就是 169 个用例里只有
# 1 个跨浏览器会话的检查，而同一个模型的实现恰好写了「每个会话一份平行世界」的
# 存储：单会话的卷子结构上测不出会话隔离，这类盲区必须先被**认出来**。
#
# 触发词全部是英文需求文本里的**通用措辞**（"persisted on the server"、"signed in
# as …"、"between X and Y characters" 这类句型），不是任何任务的字段。同一份代码
# 在 sheet 和 github 上触发的东西不一样，规则本身一个字的任务数据都没有。
#
# ★ 「缺考」的牙齿在**冻结之前**（2026-10-07 起升为硬任务）：机械启发式不配把
# 交付直接卡死（卷子马上就冻结），但清单不再是「提示」—— 找茬回合必须**逐条交代**：
# 补上检查，或把带理由的豁免写进 `_audit/suite-audit-waivers.md`。跑完还漏交代的
# 条目再追一个小回合（排练实测：软提示下模型补 21 条测试只消掉 1 条缺考）；仍然
# 没交代的进报告和完成信号如实点名（见 `suite_audit_completion_note`），
# 「自家卷子全绿」不再是唯一的完工口径。
# ★ 用词干不用词形：「persists」曾经从 "persisted" 和 "persist on the server" 两个
# 条目中间漏过去（句型测试钉住了这一课）。宁可多触发：触发只产生一条提示，漏掉
# 的是一整类科目。
FRESH_SESSION_HINTS = (
    "persist", "stored", "saved on the server",
    "survive", "still there", "still visible", "still present",
    "remains visible", "visible to another", "seen by another",
    "other users can see", "other accounts can see",
    "fresh session", "new session", "another session", "other session", "next session",
    "separate session", "across sessions", "durably",
)
MULTI_ACTOR_HINTS = (
    "another account", "another user", "other account", "other user", "second account",
    "second user", "different account", "different user", "each account", "both accounts",
    "as a visitor", "anonymous visitor", "unauthenticated visitor",
)
# "signed in as <x>" 出现**两个不同身份**就是多账号场景 —— 不写死任何名字。
_SIGNED_IN_AS_RE = re.compile(r"signed in as\s+([A-Za-z0-9_.\-]+)", re.I)
# 带数字或格式的规则："between 12 and 128 characters"、"at least one uppercase"。
_FORMAT_RULE_RE = re.compile(
    r"(?i)\b(between\s+\d+\s+and\s+\d+"
    r"|at least\s+(?:one|two|three|\d+)"
    r"|at most\s+\d+"
    r"|no (?:more|fewer|less) than\s+\d+"
    r"|minimum of\s+\d+|maximum of\s+\d+"
    # 数字区间必须带单位词尾 —— 裸的 `\d+-\d+` 会把 "REQ-3-4" 这样的节点编号
    # 也算成格式规则（校准实验实测：47/47 全是编号假触发）。
    r"|\d+\s*(?:-|–|—|to)\s*\d+\s*(?:characters?|chars?|letters?|digits?|items?|words?|symbols?|numbers?)"
    r"|(?:uppercase|lowercase|special character|digit|letter)s?\b"
    r"|must (?:contain|match|start|end|be at least|be at most))")
_TEST_CALL_RE = re.compile(r"\btest\s*\(\s*[\"'`]")
_SECOND_CONTEXT_RE = re.compile(r"\bnewContext\s*\(")

# 卷子自己的**结构性**缺陷：`expect(helper(...))` 而 helper 是同文件里 `async`
# 声明的 —— 进 expect() 的是 Promise，不是 Locator。Playwright 对 Promise 直接
# 拒绝任何 locator 断言（"toBeVisible can be only used with Locator object, was
# called with Promise ..."），**任何实现都救不了这条断言**：实现方没做错，卷子
# 自己永远红。2026-10-06 决赛排练实测（真模型，sheet 输入）：7 份卷子里 1 份
# 这么写（REQ-6-1-1，3 处），本地用 Playwright 1.63.0 复现确认。
# 它和「缺考/过分断言」不同 —— 那条是判断题，这条是机器判的硬缺陷。
_ASYNC_FN_RE = re.compile(r"async\s+function\s+(\w+)\s*\(")
_ASYNC_ARROW_RE = re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*async\b")
_UNAWAITED_IN_EXPECT_RE = re.compile(r"expect\s*\(\s*(?!await\b)(\w+)\s*\(")

# 「建了要用」的两组动作词法（第五类科目，全套级检查）。判据在两张正赛真实
# 卷面上校准过（github 47 张、sheet 24 张）：
#   creators = 主实体出生 —— button/link 名字含 new/create/add/import/upload 的
#              引用，或 setInputFiles（上传即导入）。**menuitem 不算**：菜单项是
#              对已有实体下的命令（「创建筛选器/透视表」挂在旧工作簿上），不是
#              实体出生。校准实测：不排除 menuitem 时 sheet 的 REQ-5-1-2
#              「建筛选器＋写数据」会造出一座假桥，把真正的缺口盖住。
#   writers  = 真往表单/输入框/格子里写值（fill / keyboard.type / pressSequentially
#              / .type）；只按 Enter 不算。
_JOURNEY_CREATE_RE = re.compile(
    r"getByRole\(\s*['\"](?:button|link)['\"]\s*,\s*\{\s*name:\s*"
    r"(?:['\"][^'\"\n]*\b(?:new|create|add|import|upload)\b[^'\"\n]*['\"]"
    r"|/[^/\n]*(?:new|create|add|import|upload)[^/\n]*/i)"
    r"|setInputFiles\s*\(",
    re.IGNORECASE)
_JOURNEY_DATA_RE = re.compile(r"\.fill\s*\(|keyboard\.type\s*\(|pressSequentially\s*\(|\.type\s*\(")

# 反向体检：卷子断得比需求多的三个通用形状（2026-10-03 加，判据在 two 张正赛
# 真实卷面上校准过）。三条全是测试技法词，没有任务字段 —— 同一条正则抓的是
# 「这个断言有没有需求依据」，不是「这个任务该考什么」。
#   aria  —— 卷子断言 aria-* 关联，而该节点的需求原文一个字都没提（G1 偏严
#            家族的来源：需求里 0 处 aria- 而卷子按出题提示词硬补）。
#   text  —— `locator('body'/'main')` 上的 innerText / to(Have|Contain)Text：
#            整页文本全等，动态日期、计数一变就红（实测 3 处误伤）。
#   name  —— `name:` 匹配次数多于 `exact: true` 次数：子串匹配撞名，strict
#            mode 下解析到多个元素即误杀（实测 ~15 处）。
# 三条都只**提示**（进找茬回合的机械清单），不直接判缺陷 —— 需求真要求 aria
# 或真要求整页文本时它们是合法断言，由读卷子的人定夺。
_ARIA_ASSERT_RE = re.compile(r"aria-(?:invalid|describedby)", re.IGNORECASE)
_WHOLE_PAGE_TEXT_RE = re.compile(
    r"innerText"
    r"|locator\(\s*['\"](?:body|main)['\"]\s*\)[^\n]{0,80}\.to(?:Have|Contain)Text",
    re.IGNORECASE)
_NAME_MATCH_RE = re.compile(r"\bname\s*:", re.IGNORECASE)
_EXACT_TRUE_RE = re.compile(r"\bexact\s*:\s*true", re.IGNORECASE)
# 正则形式的 name（`name: /pattern/i`）自带精确语义，不需要 exact: true。
_NAME_REGEX_RE = re.compile(r"\bname\s*:\s*/", re.IGNORECASE)
# `expect(getByRole('option', ...)).toBeVisible()`：原生 <select> 收起时 option
# 不渲染，这条断言对合规实现永不可满足（2026-10-01 github 实锤铁案之一，实测
# 用 select_option 才能通过）。只有渲染出 option 元素的自定义 combobox 才可能
# 合法 —— 机械层分不清两者，所以只提示：「核对这个控件是不是原生 select」。
_OPTION_VISIBILITY_RE = re.compile(
    r"expect\([^\n]{0,120}getByRole\(\s*['\"]option['\"][^\n]{0,160}\.toBeVisible\s*\(",
    re.IGNORECASE)
# 阈值 3：在两张正赛卷面上校准（github 47 张、sheet 24 张）—— 1~2 处裸 name
# 是零星写法（6 张文件），≥3 才是成片的裸 name 习惯（github 35 张、sheet 9 张），
# 提示该聚焦的是后者。计数是近似（多行 locator 写法差一两处），只进提示。
_LOOSE_NAME_MIN = 3

# 技法赌注（2026-10-07 加）：卷子断得比需求多的**第二族形状** —— 赌 DOM 形状。
# overreach 那一族的措辞是「不能就这么结束」（aria 没背书就删、整页文本就收窄）；
# 这一族**未必有替代写法**（原生 select 和自定义下拉、格子和内联编辑器，都合规），
# 所以单列一档、措辞轻一档：对着原文掂量，不是判缺陷。
# 六条判据全是通用测试技法词，没有任务字段。校准命中（真实排练卷面）：
#   sheet REQ-5-2-1 —— 对 gridcell 直接 .fill()（基线契约要求双击出 "Edit <coord>"）
#   sheet REQ-7-2-1 —— selectOption 赌原生 select（原文未钉控件类型；同批 REQ-5-1-2
#                      对同类控件走的 option 路线，两种假设至多一种能过）
#   github REQ-6-3-1 —— /[0-9]+\s+commit/ 数字格式（原文只说 commit information）
#   github REQ-5-5   —— evo-reaction-repository-s2/s3（原文只引了 s1）
_ROLE_BACKING = {
    # 卷子用到的角色 → 需求里能给它背书的一族说法（命中任一即算有据，不判）。
    "searchbox": ("searchbox", "search box", "search field", "search input", "search bar"),
    "spinbutton": ("spinbutton", "spin button", "stepper"),
    "slider": ("slider",),
    "switch": ("switch", "toggle"),
    "listbox": ("listbox", "list box"),
    "menuitem": ("menuitem", "menu item", "menu"),
    "treeitem": ("treeitem", "tree item", "tree"),
    "tabpanel": ("tabpanel", "tab panel", "tab"),
}
_ROLE_CALL_RE = re.compile(r"getByRole\(\s*['\"]([a-z]+)['\"]")
# 带可访问名的角色调用：`getByRole('searchbox', { name: 'Search assignees' })`。
# 名字用来分辨「这个控件本 digest 管」还是「全局钉死、只是路过」—— 见下方说明。
_ROLE_NAMED_CALL_RE = re.compile(
    r"getByRole\(\s*['\"]([a-z]+)['\"]\s*,\s*\{[^}]{0,160}?name:\s*(['\"])([^'\"]+)\2")
# selectOption 只对原生 <select> 成立；需求里点名过控件类型的说法算有据（dropdown
# 家族都算 —— 两轮真卷面上用 selectOption 的 12 份里 9 份的原文都点名了控件类型）。
_SELECT_BACKING = ("native select", "<select", "select element", "select control",
                   "dropdown", "drop-down", "drop down", "combobox", "combo box")
_SELECT_OPTION_RE = re.compile(r"\.selectOption\s*\(", re.IGNORECASE)
# toHaveCSS 断的是**计算样式**：文字给了具体色值（rgb(...) / #hex）才算有据；只给
# 样式名（"Red fill"）时，具体 RGB 是实现的自由（sheet REQ-7-2-1 的原文给了 rgb，
# 所以不亮；只写 "Red fill" 而断言 rgb(...) 才亮）。
_STYLE_CALL_RE = re.compile(
    r"toHaveCSS\(\s*['\"](?:background|background-color|color|border-color)['\"]",
    re.IGNORECASE)
_STYLE_BACKING_RE = re.compile(r"rgb(?:a)?\s*\(|#[0-9a-fA-F]{3,8}\b")
# 数字格式正则：正则字面量里带 [0-9] 或 \d；需求原文一个数字都没有时才亮（原文给了
# 数字样例 —— "3 files" —— 就不亮）。
_DIGIT_REGEX_RE = re.compile(r"/(?:\\.|[^/\n])*?(?:\[0-9\]|\\d)(?:\\.|[^/\n])*?/[a-z]*")
# 判「摘要里有没有数字」时要先把需求编号（REQ-6-3-1、ROOT）和情景编号
# （"scenario 1"）剥掉 —— 摘要引用别的节点、给情景排号时满篇都是数字，不剥
# 这一条永远不亮（校准踩过两回）。
_REQ_ID_RE = re.compile(r"\b(?:req|root)(?:-[0-9a-z]+)*\b"
                        r"|\b(?:scenarios?|cases?|steps?)\s+[0-9a-z]+\b",
                        re.IGNORECASE)
_GRIDCELL_DIRECT_FILL_RE = re.compile(
    r"getByRole\(\s*['\"](?:gridcell|cell)['\"][^\n;]{0,200}?"
    r"\.(?:fill|type|pressSequentially)\s*\(", re.IGNORECASE)
_TECHNIQUE_NAME_RE = re.compile(r"['\"]([a-z][a-z0-9]*(?:[-_][a-z0-9]+)+)['\"]")
_TECHNIQUE_NAME_MIN = 8
# 属性/样式/协议类词汇：它们是技法词，不是需求里的对象名（`aria-selected`
# `background-color` 这类在卷面上满地都是，不排掉这一条会淹掉真信号）。
_TECHNIQUE_NAME_STOP = {
    "background-color", "border-color", "border-radius", "font-size", "font-weight",
    "font-family", "line-height", "text-align", "text-decoration", "letter-spacing",
    "box-shadow", "pointer-events", "white-space", "word-break", "z-index",
    "min-width", "min-height", "max-width", "max-height", "flex-direction",
    "table-cell", "inline-block", "pre-wrap", "no-cache", "no-store", "utf-8",
    "application-json", "text-plain", "same-origin",
    # Playwright 权限/API 字面量（grantPermissions([...])），不是需求对象名。
    "clipboard-read", "clipboard-write",
}


def _quoted_in_digest(name: str, digest_low: str) -> bool:
    """名字在需求原文里是否作为**被引用的固定名**出现（反引号或引号包着）。

    裸词不算：「Search」在摘要里满篇是动词，只有 `Search assignees` 这种引起来
    的才是「原文在说这个控件」。这条把「全局钉死的控件只是路过」和「本节点的
    控件被按另一种角色断言」分开 —— 校准：github 15 处 searchbox 里 14 处是
    全局搜索框（名字 'Search'，摘要里没引），1 处是 `Search assignees`（摘要引了，
    但给的角色是 textbox），只有后者是真赌注。
    """
    n = name.lower()
    return (f"`{n}`" in digest_low or f'"{n}"' in digest_low or f"'{n}'" in digest_low
            or f"“{n}”" in digest_low or f"‘{n}’" in digest_low)


def _gridcell_handler_names(text: str) -> set[str]:
    """文件里「拿着 gridcell/cell locator」的名字（定位助手，或直接赋值的变量）。

    真实卷面把格子包进小助手（`function cell(page, coord) { return grid(page)
    .getByRole('gridcell', ...) }`），对格子的写入要顺着助手找，否则 `.fill()`
    藏在 `cell(...)` 后面就漏了（sheet REQ-5-2-1 的形状）。
    """
    names: set[str] = set()
    for pattern in (
            # `function cell(page) { return grid(page).getByRole('gridcell', ...) }`
            r"function\s+(\w+)\s*\([^)]*\)\s*(?::[^={]*)?\{[^}]{0,400}?"
            r"getByRole\(\s*['\"](?:gridcell|cell)['\"]",
            # `const cell = (page) => grid(page).getByRole('gridcell', ...)`
            r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|\w+)\s*=>[^;]{0,300}?"
            r"getByRole\(\s*['\"](?:gridcell|cell)['\"]",
            # `const c = page.getByRole('gridcell', ...)`
            r"(?:const|let|var)\s+(\w+)\s*=\s*[^;=\n]{0,80}?"
            r"getByRole\(\s*['\"](?:gridcell|cell)['\"]"):
        names |= {m.group(1) for m in re.finditer(pattern, text)}
    # 变量取自助手：`const c = cell(page, 'A1')`（两遍，够一层转手）。
    for _ in range(2):
        for m in re.finditer(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:await\s+)?(\w+)\s*\(", text):
            if m.group(2) in names:
                names.add(m.group(1))
    return names


# 双击出编辑框：需求原文自己交代了「怎么进格子」时（双击 → 内联输入框），卷子对
# 格子元素直接 fill 就不是没据的赌注，而是跟原文对着干 —— 原文说的进格子方式是
# 双击后在框里输，格子本身不是输入框。校准（2026-10-07）：全排练 sheet 系 8 套 ×
# 1 节点命中，全是出卷审计已判定的强条件项；github 系（无网格）0 命中。词表是
# 通用 UI 说法，不含任务字段。
_GRID_EDITOR_WORDS = ("inline", "text box", "textbox", "edit box", "input box",
                      "input field", "editor")
_DOUBLE_CLICK_RE = re.compile(r"double[- ]click")


def _grid_editor_stated(corpus_low: str) -> bool:
    """原文有没有交代「双击格子出内联编辑框」这条进格子方式。

    看双击字眼周围 ±一段窗口里有没有编辑框族的词 —— 两条凑齐才算原文交代了
    进格子的方式；只提双击（其余用途）不算。
    """
    low = corpus_low or ""
    for m in _DOUBLE_CLICK_RE.finditer(low):
        window = low[max(0, m.start() - 160): m.end() + 200]
        if any(w in window for w in _GRID_EDITOR_WORDS):
            return True
    return False


def _gridcell_fill_count(text: str) -> int:
    """对格子直接 fill/type 的次数（直连链 + 经助手/变量的间接写法）。"""
    count = len(_GRIDCELL_DIRECT_FILL_RE.findall(text))
    for name in _gridcell_handler_names(text):
        count += len(re.findall(
            r"\b" + re.escape(name) + r"\b[^\n;]{0,120}?\.(?:fill|type|pressSequentially)\s*\(",
            text))
    return count


def _candidate_names(text: str):
    """卷面里「像固定名」的写法（引号包着的小写连字符词），逐行去噪后逐个产出。

    只认「小写 + 短横线/下划线连起来」的写法（`evo-reaction-repository-s2`）：
    邮箱（含 @ 和点）、口令（大写开头）、CSS/ARIA 属性词都进不来。带运行期插值的
    行整行跳过 —— 那上面的名字是拼出来的，不是写死的。
    """
    for line in (text or "").splitlines():
        if "${" in line or "Date.now" in line or "randomUUID" in line:
            continue
        for match in _TECHNIQUE_NAME_RE.finditer(line):
            name = match.group(1)
            if len(name) < _TECHNIQUE_NAME_MIN or name in _TECHNIQUE_NAME_STOP:
                continue
            if name.startswith(("aria-", "data-", "webkit-", "-webkit-")):
                continue
            yield name


def _unbacked_names(text: str, digest_low: str, corpus: str | None = None,
                    limit: int = 6) -> list[str]:
    """卷子里写死的、需求原文没有出处、也不是运行期生成的名字。

    名字在需求原文里出现过（大小写不敏感）就不算 —— `corpus` 给了整套摘要的并集时
    按并集算（种子对象常写在别的节点摘要里，本节点只是引用）；剩下的要么是本测试
    自己写进去/建出来的（允许），要么是凭空推断出来的对象，后者是这条要抓的形状
    （提示层：里面混着不少自建数据的噪音，硬任务只取 `_extrapolated_names` 那一档）。
    """
    seen_low = (corpus if corpus is not None else digest_low) or ""
    out: list[str] = []
    for name in _candidate_names(text):
        if name in out or name in seen_low:
            continue
        out.append(name)
        if len(out) >= limit:
            break
    return out


_VARIANT_TAIL_RE = re.compile(r"[-_ ]s?\d+$")


def _extrapolated_names(text: str, digest_low: str, corpus: str | None = None,
                        limit: int = 6) -> dict[str, str]:
    """卷子把原文引用的名字**外推**出来的变体：原文钉 `x-s1`，卷子写 `x-s2`。

    判据：这个名字本身在需求原文里没有出处，但去掉末尾的变体标记（`-s2` / `-2` /
    空格 2）之后的基名在原文里 —— 名字是从被引用的那个推导出来的，而**推导出来的
    名字不是被引用的名字**。返回 {卷子里的名字: 原文引用的基名}。

    这是 `_unbacked_names` 里能升硬任务的那一档（校准：全排练 356 份真卷命中 6 处，
    全是真的 —— REQ-5-5 的 `…-s2/s3`，原文只引了 `…-s1`）；被测试自己 fill/type
    写进去的数据和自建对象的普通未背书名都够不着这条：它们的基名不在原文里。
    """
    seen_low = (corpus if corpus is not None else digest_low) or ""
    out: dict[str, str] = {}
    for name in _candidate_names(text):
        if name in out or name in seen_low:
            continue
        base = _VARIANT_TAIL_RE.sub("", name)
        if base == name or len(base) < _TECHNIQUE_NAME_MIN:
            continue
        if base in seen_low:
            out[name] = base
            if len(out) >= limit:
                break
    return out


def spec_technique_bets(text: str, digest: str, corpus: str = "") -> dict:
    """一份卷子赌了哪些需求没背书的 DOM 形状 → {形状: 计数或名字列表}。

    只提示、不判缺陷（见上方注释）：赌注未必错，但每一条都要对着需求原文掂量。
    `corpus` 是**整套**摘要的并集（可选）：固定名字只要在任何一份摘要里有出处
    就算合法（种子对象常出现在别的节点的摘要里，本节点只是引用）；不传时退回
    只看本节点的 digest。噪音在两轮真实排练卷面上校准过（见文件头注释）。

    例外是 `extrapolated_names`（名字从被引用的那个外推出来）、`role_unbacked`
    与 `cell_fill_contradicted`（原文交代了双击出编辑框、卷子却直接对格子 fill）
    这三档：2026-10-07 校准后升为**硬任务**（进 `suite_audit` 的处置账本，见
    `gap_disposition`）—— 三者在真卷上都是 0 误报，赌注未必错但必须有下文。
    """
    text = text or ""
    if not text:
        return {}
    digest_low = (digest or "").lower()
    out: dict = {}
    fills = _gridcell_fill_count(text)
    if fills:
        out["cell_fill"] = fills
        if _grid_editor_stated((corpus or digest_low)):
            out["cell_fill_contradicted"] = fills
    if _SELECT_OPTION_RE.search(text) and not any(w in digest_low for w in _SELECT_BACKING):
        out["select_option"] = len(_SELECT_OPTION_RE.findall(text))
    if _STYLE_CALL_RE.search(text) and not _STYLE_BACKING_RE.search(digest_low):
        out["style_color"] = len(_STYLE_CALL_RE.findall(text))
    if _DIGIT_REGEX_RE.search(text) and not re.search(
            r"\d", _REQ_ID_RE.sub(" ", digest_low)):
        out["format_regex"] = len(_DIGIT_REGEX_RE.findall(text))
    all_calls = list(_ROLE_CALL_RE.finditer(text))
    named_calls = list(_ROLE_NAMED_CALL_RE.finditer(text))
    unnamed: list[str] = []
    for role in sorted({m.group(1) for m in all_calls} & set(_ROLE_BACKING)):
        if any(word in digest_low for word in _ROLE_BACKING[role]):
            continue  # 本节点摘要给这个角色背了书
        calls = [m for m in named_calls if m.group(1) == role]
        bare = len([m for m in all_calls if m.group(1) == role]) - len(calls)
        # 带名字的调用：名字在本节点摘要里**没被引用**时放过 —— 那个控件是全局
        # 钉死的（它的角色写在别的节点的摘要里），本节点只是路过它去干自己的事。
        # 名字被本摘要引用了（或压根没带名字）才是真赌注：摘要明明在说这个控件，
        # 卷子却按一个它没给过的角色去定位。
        if bare or any(_quoted_in_digest(m.group(3), digest_low) for m in calls):
            unnamed.append(role)
    if unnamed:
        out["role_unbacked"] = unnamed
    names = _unbacked_names(text, digest_low, corpus=(corpus or digest).lower())
    if names:
        out["fixed_names"] = names
    extra = _extrapolated_names(text, digest_low, corpus=(corpus or digest).lower())
    if extra:
        out["extrapolated_names"] = extra
    return out

# ------------------------------------------------- 实现侧接口对账（2026-10-08，只提示）
#
# 26/30 归因的产物：4 条失分里有 2 条的形状是**实现无谓地丢掉了自家的语义** ——
# 原生 <button> 上盖 role="menuitem"（判分方按 role 找人，牌子被自己摘了）、
# 装饰字形当文本节点（精确文本断言被一个 ▸ 弄红）。卷面已有的 `role_unbacked`
# 是**卷子→需求**的单向对账；这一族反过来对：**实现暴露的接口 vs 卷子要查的
# 接口**。和 `spec_technique_bets` 同一档：只提示、不判缺陷、不进处置账本、
# 不阻塞任何回合、不新增任何测量 —— 它只是把静态读盘看得见的东西说出口。
#
# 三条判据全是通用词法（HTML 标签名、ARIA 角色名、几何字形），没有任务字段：
#   role_overrides —— 原生语义元素上带 role= 覆盖（role 值是原生 role、或
#                     none/presentation 的不算）—— 「牌子被自己摘了」的形状；
#   glyph_text     —— 装饰字形出现在源码里 —— 当成文本节点渲染时进 textContent；
#   role_missing   —— 卷子按角色 R 找人、需求原文点名过 R，而 frontend/ 源码里
#                     一个字都没提过 R 一族（连'可能渲染出它'的线索都没有）。
_IMPL_SKIP_DIRS = {"node_modules", "dist", "build", ".next", "coverage", ".git",
                   "__pycache__", "out", "vendor", "tmp"}
_IMPL_EXTS = {".jsx", ".tsx", ".js", ".ts", ".vue", ".svelte", ".html", ".astro"}
_IMPL_MAX_FILES = 600
_IMPL_MAX_BYTES = 400_000

# 原生元素 → 它的隐式 role（`role=` 盖上去之前，浏览器和 Playwright 看到的）。
_IMPL_NATIVE_ROLE = {
    "button": "button", "select": "combobox", "textarea": "textbox",
    "nav": "navigation", "main": "main", "header": "banner", "footer": "contentinfo",
    "aside": "complementary", "ul": "list", "ol": "list", "li": "listitem",
    "table": "table", "tr": "row", "td": "cell", "th": "columnheader", "form": "form",
    "img": "img", "dialog": "dialog", "details": "group", "summary": "button",
    "option": "option", "fieldset": "group", "figure": "figure", "hr": "separator",
    "h1": "heading", "h2": "heading", "h3": "heading",
    "h4": "heading", "h5": "heading", "h6": "heading",
    "progress": "progressbar", "meter": "meter", "output": "status",
}
# input 的隐式 role 由 type 决定；拿不到字面 type 时不判（认不准就不说）。
_IMPL_INPUT_ROLE = {
    "text": "textbox", "search": "searchbox", "email": "textbox", "url": "textbox",
    "tel": "textbox", "checkbox": "checkbox", "radio": "radio", "number": "spinbutton",
    "range": "slider", "button": "button", "submit": "button", "reset": "button",
}
# 起止成对的引号内不跨标签：一次抓下一个完整的开标签，属性里找 role/type/href。
_IMPL_TAG_RE = re.compile(
    r"<\s*([a-zA-Z][a-zA-Z0-9-]*)\b((?:\"[^\"]*\"|'[^']*'|[^<>\"'])*)>")
_IMPL_ROLE_ATTR_RE = re.compile(r"\brole\s*=\s*[\"']([a-zA-Z]+)[\"']")
_IMPL_TYPE_ATTR_RE = re.compile(r"\btype\s*=\s*[\"']([a-zA-Z-]+)[\"']")
# role="none"/"presentation" 是标准 ARIA 的「摘语义」值（菜单里 <li role="none">
# 是正统写法），不在这条判据里说话。
_IMPL_ROLE_NOOP = {"none", "presentation"}
# 标准 ARIA 本来就该这么写的组合同样不说（两张官方种子盘上校准过）：
#   <form role="search"> —— 搜索地标，ARIA in HTML 明确允许的 form 角色（种子盘
#     github 侧 3 处，是正统写法，不是「牌子被摘了」）；
#   <ul|ol|li role="listbox|menu|option|…"> —— APG 的 listbox/menu/tablist 模式
#     本来就是「容器带 widget 角色、条目带 item 角色」（种子盘 github 侧
#     <li role="option">），原生 list/listitem 角色在这个模式里没有人查。
_IMPL_ROLE_SANCTIONED = {
    "form": {"search"},
    "ul": {"listbox", "menu", "menubar", "tablist", "tree", "radiogroup", "grid", "treegrid", "group"},
    "ol": {"listbox", "menu", "menubar", "tablist", "tree", "radiogroup", "grid", "treegrid", "group"},
    "li": {"option", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "treeitem",
           "radio", "checkbox", "switch", "tree", "listbox", "group", "row"},
}
# 几何装饰字形（三角形/箭头/圆点/对勾/叉号）：它们出现在文本位置就是 textContent
# 污染源。常见的排版符号（« » · — 等）不列 —— 那是正文的一部分，警示会淹掉信号。
_IMPL_GLYPH_RE = re.compile(r"[▸▾▴◂◀▶►◄▲▼△▽◆◇●○★☆✓✔✗✕✖]")
# 「源码里一个字都没提过」判据只对显式角色成立（必须有 role= 或对应控件才渲染得
# 出来，判据两边都说得清）。词表同时是两侧的口径：需求点名过 R 一族 **且**
# 源码里连 R 一族的一个词都没有 —— 两个条件同源，不各说各话。
_IMPL_MISSING_ROLES = {
    "menuitem": ("menuitem", "menu"),
    "tabpanel": ("tabpanel", "tab"),
    "treeitem": ("treeitem", "tree item", "tree"),
    "listbox": ("listbox", "list box"),
    "switch": ("switch", "toggle"),
    "slider": ("slider",),
    "spinbutton": ("spinbutton", "spin button", "stepper"),
    "searchbox": ("searchbox", "search box", "search field", "search input", "search bar"),
    "gridcell": ("gridcell", "grid cell", "cell"),
}


def _impl_source_files(frontend) -> list:
    found: list = []
    try:
        walker = sorted(frontend.rglob("*"))
    except OSError:
        return found
    for path in walker:
        if len(found) >= _IMPL_MAX_FILES:
            break
        try:
            if not path.is_file() or path.suffix.lower() not in _IMPL_EXTS:
                continue
            if any(part in _IMPL_SKIP_DIRS for part in path.parts):
                continue
            if path.stat().st_size > _IMPL_MAX_BYTES:
                continue
        except OSError:
            continue
        found.append(path)
    return found


def impl_interface_warnings(app_dir, specs: dict, requirement_text: str = "") -> dict:
    """实现→卷子 的机械对账（只提示）：见上方文件头说明。

    `specs` 是**这次要看的**卷子（{文件名: 文本}，调用方给失败的批 —— 范围越小
    提示越相关）；`requirement_text` 是节点需求原文。返回三个键，值都是写好的
    整句（可以直接贴进提示词），没有发现时三个键都在、都是空表。
    """
    out: dict = {"role_overrides": [], "glyph_text": [], "role_missing": []}
    root = Path(str(app_dir)) / "frontend"
    if not root.is_dir():
        return out
    files = _impl_source_files(root)
    if not files:
        return out
    blobs: dict = {}
    for path in files:
        try:
            blobs[path] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    if not blobs:
        return out
    whole_low = "\n".join(blobs.values()).lower()

    def rel(path) -> str:
        try:
            return str(path.relative_to(Path(str(app_dir))))
        except ValueError:
            return str(path)

    # 判据一：原生元素上的 role 覆盖（牌子被自己摘了）。
    seen_overrides: set = set()
    for path, text in blobs.items():
        for m in _IMPL_TAG_RE.finditer(text):
            tag, attrs = m.group(1).lower(), m.group(2)
            role_m = _IMPL_ROLE_ATTR_RE.search(attrs)
            if not role_m:
                continue
            role = role_m.group(1).lower()
            if role in _IMPL_ROLE_NOOP or role in _IMPL_ROLE_SANCTIONED.get(tag, ()):
                continue
            if tag == "input":
                type_m = _IMPL_TYPE_ATTR_RE.search(attrs)
                native = _IMPL_INPUT_ROLE.get(type_m.group(1).lower(), "") if type_m else ""
            elif tag == "a":
                native = "link" if re.search(r"\bhref\b", attrs) else ""
            else:
                native = _IMPL_NATIVE_ROLE.get(tag, "")
            if not native or native == role:
                continue
            key = (rel(path), tag, role, native)
            if key in seen_overrides:
                continue
            seen_overrides.add(key)
            where = key[0]
            out["role_overrides"].append(
                f"`{where}`: a real <{tag}> carries role=\"{role}\" — an explicit role "
                f"replaces the native one, so it no longer exposes role `{native}`, and a "
                f"role lookup for `{native}` finds nothing. If both roles may be looked up, "
                f"nest instead of overriding: put the ARIA role on a wrapper around the real "
                f"control (or on a child inside it), not on the control itself.")

    # 判据二：装饰字形当文本节点（进 textContent）。
    seen_glyphs: set = set()
    for path, text in blobs.items():
        for glyph in sorted(set(_IMPL_GLYPH_RE.findall(text))):
            key = (rel(path), glyph)
            if key in seen_glyphs:
                continue
            seen_glyphs.add(key)
            where = key[0]
            out["glyph_text"].append(
                f"`{where}`: the placeholder glyph `{glyph}` appears in the source — "
                f"rendered as a text node it joins its element's text content (`aria-hidden` "
                f"and `display:none` do not remove it from `textContent`), and an exact-text "
                f"assertion then fails while the feature works. Draw it with a CSS "
                f"pseudo-element or an SVG icon instead.")

    # 判据三：卷子按角色找人、需求点名过，而源码一个字都没提过这一族。
    spec_roles: dict = {}
    for name, text in (specs or {}).items():
        for m in _ROLE_CALL_RE.finditer(text or ""):
            spec_roles.setdefault(m.group(1).lower(), set()).add(str(name))
    req_low = (requirement_text or "").lower()
    for role in sorted(spec_roles):
        family = _IMPL_MISSING_ROLES.get(role)
        if not family:
            continue
        if not any(re.search(rf"\b{re.escape(w)}\b", req_low) for w in family):
            continue  # 需求原文没点名过 —— 那是卷子过界，由 role_unbacked 那条线管
        if any(re.search(rf"\b{re.escape(w)}\b", whole_low) for w in family):
            continue  # 源码里提过这一族 —— 静态判不出真假，不说
        who = ", ".join(sorted(spec_roles[role])[:4])
        out["role_missing"].append(
            f"{who}: the spec locates a control by role `{role}`, the requirement text names "
            f"it, and no source file under frontend/ mentions the `{role}` family at all — if "
            f"the failure being chased is a locator that resolves to nothing, nothing in the "
            f"build currently exposes that role.")
    return out


SUITE_AUDIT_DIR = "_audit"
SUITE_AUDIT_FILE = "suite-audit.md"
SUITE_SHARED_FILE = "suite-shared-objects.md"
SUITE_WAIVER_FILE = "suite-audit-waivers.md"


def spec_structural_defects(text: str) -> list[str]:
    """卷子里**任何实现都满足不了**的结构性缺陷（语言层面，不是行为层面）。

    目前只认一条：`expect(helper(...))` 而 helper 是同文件里 `async` 声明的
    （或 `const helper = async (...) =>`）。expect 拿到的是 Promise，Playwright
    对 locator 断言直接报 "can be only used with Locator object"，这条断言对
    **每一个**实现都红——它不是「实现没做到」，是卷子写错了。

    `expect(await helper(...))` 与 `expect(fn(...)).resolves…` 不在此列。
    返回一行行给人看的说明，空列表 = 没查出这条。
    """
    text = text or ""
    helpers = {m.group(1) for m in _ASYNC_FN_RE.finditer(text)}
    helpers |= {m.group(1) for m in _ASYNC_ARROW_RE.finditer(text)}
    if not helpers:
        return []
    bad: list[str] = []
    for match in _UNAWAITED_IN_EXPECT_RE.finditer(text):
        if match.group(1) not in helpers:
            continue
        # `.resolves` / `.rejects` 是 expect(Promise) 的合法写法，放过。
        tail = text[match.end():match.end() + 120]
        if ".resolves" in tail or ".rejects" in tail:
            continue
        bad.append(match.group(1))
    if not bad:
        return []
    names = ", ".join(sorted(set(bad)))
    return [f"expect(...) is handed a Promise: `{names}` is declared `async` but called "
            f"inside expect() without await — Playwright rejects every locator matcher on "
            f"it, so this assertion fails for **every** implementation. Make the helper a "
            f"plain function that returns the locator, or await it before expect()."]


def suite_audit_triggers(digest_text: str) -> dict:
    """一段需求原文触发了哪些「必须这么考」的句型。判据只有文本本身。

    `DIGEST_INTRO` 是出卷依据的固定开场白（给写卷人看的说明，不是需求原文），
    它自己就带着「a persistence rule」这样的词眼 —— 先切掉再匹配，否则每个
    节点都会因为这段我们自己写的话被标上持久化科目（校准实验实测：切掉之前
    47/47 全中，其中只此一条是假信号）。
    """
    text = digest_text or ""
    if DIGEST_INTRO in text:
        text = text.split(DIGEST_INTRO, 1)[1]
    low = text.lower()
    identities = {m.group(1).lower() for m in _SIGNED_IN_AS_RE.finditer(text)}
    return {
        "fresh_session": any(h in low for h in FRESH_SESSION_HINTS),
        "multi_actor": len(identities) >= 2 or any(p in low for p in MULTI_ACTOR_HINTS),
        "format_rule": bool(_FORMAT_RULE_RE.search(text)),
    }


def read_suite_material(suite_dir: Path, leaves: list[str],
                        digest_rel: dict[str, str]) -> tuple[dict, dict]:
    """体检素材：节点 → 需求原文、节点 → 卷子文本。读不到的就不出现在表里。

    和 `authored_spec_map` 同一套盘点口径（按文件名认，不解析内容），区别只是
    这里要的是文本本身。体检判据全部来自这两张表，没有任何外部状态。
    """
    specs_by_stem: dict[str, Path] = {}
    for path in sorted(suite_dir.rglob("*.spec.ts")):
        specs_by_stem.setdefault(path.name[: -len(".spec.ts")], path)
    digests: dict[str, str] = {}
    specs: dict[str, str] = {}
    for leaf in leaves:
        rel = digest_rel.get(leaf)
        if rel:
            try:
                digests[leaf] = (suite_dir / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        path = specs_by_stem.get(leaf)
        if path is not None:
            try:
                specs[leaf] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
    return digests, specs


def suite_audit(leaves: list[str], digests: dict, specs: dict) -> dict:
    """卷面机械体检：每个节点的题型画像 + 缺考清单。

    「缺考」的判据是全套里最硬的一条：需求原文带着持久化/多账号的句型，而卷子
    从头到尾没有开过第二个浏览器上下文（`newContext(`）。单会话的断言对这种
    句型**结构上**不构成验证 —— 这正是 2026-10-01 github 3.22 分那次的形状。
    `format_rule` 触发只做提示（边界密度没法机械判定对错），进找茬提示词，不进
    缺考清单。

    第二条检查是全套级的（不属于任何单个节点）：「建了要用」。创建/导入动作与
    写数据动作两边都非空、却没有任何一张卷子同时占两样 —— 主实体建出来没人
    用过，正是 2026-10-01 sheet 47.54 那次的形状（新建工作簿→写单元格命中
    undoStack 崩溃，官方评分期 500 连发，自家卷子 138/138 全绿：建卷不写字、
    写卷不创建，交集为空）。两个集合的判据全是通用词法，没有一个任务字段。

    第三条是反向的（`overreach`）：卷子**断得比需求多** —— aria 关联而需求
    没提、整页文本全等、name 匹配不带 exact。和缺考一样进找茬回合的机械
    清单；不同的是这三条只在需求确实没背书时才成立（aria 要对着 digest 比），
    所以只提示、不判缺陷。

    第四条是 `technique`：卷子赌了需求没背书的 **DOM 形状**（对格子直接 fill、
    对非原生 select 调 selectOption、断没写明的色值/数字格式、用 digest 没点名
    的角色、写 digest 没有出处的固定名字）。同样只提示；语气比 overreach 轻
    一档 —— 这类赌注未必有替代写法，和原文对一对就好（见 spec_technique_bets）。

    第五条是 `literals`（字面量对账，2026-10-07）：technique 里校准下来 0 误报的
    三档 —— 外推名（原文引 `x-s1`、卷子写 `x-s2`）、没背书的角色、原文交代了
    双击出编辑框却直接对格子 fill —— 升为硬任务，和缺考、结构性缺陷一样进处置
    账本（见 `gap_disposition`）。
    """
    nodes: dict[str, dict] = {}
    gap_nodes: list[str] = []
    corpus_low = "\n".join(digests.get(leaf) or "" for leaf in leaves).lower()
    for leaf in leaves:
        text = specs.get(leaf, "")
        trig = suite_audit_triggers(digests.get(leaf, ""))
        has_ctx = bool(_SECOND_CONTEXT_RE.search(text))
        gaps: list[str] = []
        if leaf not in specs:
            gaps.append("no spec file on disk")
        elif (trig["fresh_session"] or trig["multi_actor"]) and not has_ctx:
            reasons = []
            if trig["fresh_session"]:
                reasons.append("persistence/storage language")
            if trig["multi_actor"]:
                reasons.append("more than one account")
            gaps.append(" and ".join(reasons)
                        + " stated, but no check ever leaves the first browser session")
        if gaps:
            gap_nodes.append(leaf)
        digest_low = (digests.get(leaf) or "").lower()
        aria_without_requirement = bool(_ARIA_ASSERT_RE.search(text)) and "aria-" not in digest_low
        whole_page_text = bool(_WHOLE_PAGE_TEXT_RE.search(text))
        loose_raw = (len(_NAME_MATCH_RE.findall(text))
                     - len(_EXACT_TRUE_RE.findall(text))
                     - len(_NAME_REGEX_RE.findall(text)))
        loose_names = loose_raw if loose_raw >= _LOOSE_NAME_MIN else 0
        option_visibility = bool(_OPTION_VISIBILITY_RE.search(text))
        defects = spec_structural_defects(text)
        # 字面量对账（#2，2026-10-07）：卷内钉死的名字/角色/交互对不上需求原文的
        # 三档 —— 外推名（原文引 `x-s1`、卷子写 `x-s2`）、没背书的角色、原文交代了
        # 进格子方式（双击出编辑框）却直接对格子 fill。三档校准 0 误报，和缺考、
        # 结构性缺陷汇入同一本处置账（`gap_disposition`）。
        tech = spec_technique_bets(text, digests.get(leaf, ""), corpus_low)
        literals: list[str] = []
        for role in tech.get("role_unbacked") or []:
            literals.append(f"locates a control by role `{role}` that this node's requirement "
                            f"text never backs — the text's role is what the implementer was "
                            f"asked to expose; use the role the text names")
        for name, base in (tech.get("extrapolated_names") or {}).items():
            literals.append(f"writes the name `{name}` while the requirement text only quotes "
                            f"the `{base}` family — a name extrapolated from a quoted one is "
                            f"not quoted; use only names the text quotes, or create the record "
                            f"inside the test")
        if tech.get("cell_fill_contradicted"):
            literals.append("fills a gridcell directly while the requirement text routes cell "
                            "entry through a double-click inline editor — the text's way is "
                            "what the implementer was asked to expose; use the documented "
                            "interaction, or set the value without typing into the cell")
        nodes[leaf] = {
            "spec": leaf in specs,
            "tests": len(_TEST_CALL_RE.findall(text)),
            "second_context": has_ctx,
            "triggers": trig,
            "gaps": gaps,
            "defects": defects,
            "overreach": {
                "aria_without_requirement": aria_without_requirement,
                "whole_page_text": whole_page_text,
                "loose_name_matches": loose_names,
                "option_visibility": option_visibility,
            },
            "technique": tech,
            "literals": literals,
        }
    creators = [leaf for leaf in leaves if _JOURNEY_CREATE_RE.search(specs.get(leaf, ""))]
    writers = [leaf for leaf in leaves if _JOURNEY_DATA_RE.search(specs.get(leaf, ""))]
    bridging = [leaf for leaf in creators if leaf in writers]
    overreach_nodes = [leaf for leaf in leaves
                       if nodes[leaf]["overreach"]["aria_without_requirement"]
                       or nodes[leaf]["overreach"]["whole_page_text"]
                       or nodes[leaf]["overreach"]["loose_name_matches"]
                       or nodes[leaf]["overreach"]["option_visibility"]]
    technique_nodes = [leaf for leaf in leaves if nodes[leaf]["technique"]]
    technique_kinds = ("cell_fill", "select_option", "style_color", "format_regex",
                       "role_unbacked", "fixed_names")
    return {
        "nodes": nodes,
        "gap_nodes": gap_nodes,
        "defect_nodes": [l for l in leaves if nodes[l]["defects"]],
        "literal_nodes": [l for l in leaves if nodes[l]["literals"]],
        "missing_specs": [l for l in leaves if l not in specs],
        "with_second_context": [l for l in leaves if nodes[l]["second_context"]],
        "journey": {
            "creators": creators,
            "writers": writers,
            "bridging": bridging,
            "gap": bool(creators and writers and not bridging),
        },
        "overreach": {
            "nodes": overreach_nodes,
            "aria_without_requirement": [l for l in leaves
                                         if nodes[l]["overreach"]["aria_without_requirement"]],
            "whole_page_text": [l for l in leaves if nodes[l]["overreach"]["whole_page_text"]],
            "loose_name_matches": {l: nodes[l]["overreach"]["loose_name_matches"]
                                   for l in leaves if nodes[l]["overreach"]["loose_name_matches"]},
            "option_visibility": [l for l in leaves
                                  if nodes[l]["overreach"]["option_visibility"]],
        },
        "technique": {
            "nodes": technique_nodes,
            **{kind: [l for l in leaves if kind in nodes[l]["technique"]]
               for kind in technique_kinds},
        },
        "totals": {
            "nodes": len(leaves),
            "specs": sum(1 for l in leaves if nodes[l]["spec"]),
            "tests": sum(n["tests"] for n in nodes.values()),
            "triggered": sum(1 for n in nodes.values() if any(n["triggers"].values())),
            "overreach": len(overreach_nodes),
            "technique": len(technique_nodes),
        },
    }


def suite_audit_line(audit: dict) -> str:
    """一行总览 —— 日志和找茬回合里都用它。"""
    t = audit.get("totals") or {}
    line = (f"{t.get('specs', 0)}/{t.get('nodes', 0)} spec(s), "
            f"{t.get('tests', 0)} test() block(s), "
            f"{len(audit.get('with_second_context') or [])} spec(s) opening a second browser context, "
            f"{len(audit.get('gap_nodes') or [])} node(s) with a missing check category")
    if (audit.get("journey") or {}).get("gap"):
        line += ", no spec creating or importing an entity and then using it"
    defects = audit.get("defect_nodes") or []
    if defects:
        line += (f", **{len(defects)} spec(s) with an assertion no implementation can pass** "
                 f"({', '.join(defects[:4])})")
    over = (audit.get("overreach") or {}).get("nodes") or []
    if over:
        line += f", {len(over)} node(s) asserting beyond the requirement text"
    tech = (audit.get("technique") or {}).get("nodes") or []
    if tech:
        line += (f", {len(tech)} node(s) betting on a DOM shape the requirement text "
                 f"does not fix")
    return line


def suite_audit_hints(audit: dict, group: list[str]) -> str:
    """找茬回合的机械提示：只列这一组里被标出来的节点。"""
    lines: list[str] = []
    rows = audit.get("nodes") or {}
    for leaf in group:
        row = rows.get(leaf)
        if not row:
            continue
        notes = list(row.get("gaps") or [])
        if row.get("defects"):
            notes.append("the spec hands expect() a Promise (an `async` helper called inside "
                         "expect() without await): Playwright rejects every locator matcher on "
                         "it and **no implementation can pass this assertion** — make the "
                         "helper a plain function returning the locator, or await it before "
                         "expect(). Fix this before anything else.")
        if not notes and (row.get("triggers") or {}).get("format_rule"):
            notes.append("the text states rules with numbers or a format; count the boundary "
                         "cases the spec really makes")
        over = row.get("overreach") or {}
        if over.get("aria_without_requirement"):
            notes.append("asserts aria-invalid/aria-describedby wiring the digest never "
                         "mentions — drop it unless the text really requires the tie")
        if over.get("whole_page_text"):
            notes.append("asserts whole-page text (innerText or body/main to*Text) — dynamic "
                         "values live there; scope it to the element that carries the text")
        loose = over.get("loose_name_matches") or 0
        if loose:
            notes.append(f"{loose} name match(es) without `exact: true` — a longer label "
                         "containing the wording resolves to the wrong element")
        if over.get("option_visibility"):
            notes.append("asserts a `getByRole('option')` element is visible — a native "
                         "select renders no options while its popup is closed; keep this "
                         "only if the control renders its options itself")
        tech_row = row.get("technique") or {}
        if tech_row.get("cell_fill"):
            notes.append(f"{tech_row['cell_fill']} write(s) go straight onto a grid cell "
                         "(`.fill()`/`.type()` on a cell locator) — a cell is not an input "
                         "unless the text says typing goes into the cell itself; type where "
                         "the text puts the editor (an inline edit box, a named box), or "
                         "double-click into the cell first")
        if tech_row.get("select_option"):
            notes.append("calls `.selectOption(` on a control no digest fixes as a native "
                         "select — a custom dropdown does not implement it and the call "
                         "throws; put the control-type bet against the text")
        if tech_row.get("style_color"):
            notes.append("asserts a computed color with `toHaveCSS` that no digest states — "
                         "a style name ('Red fill') is not an RGB value; assert the value "
                         "the text gives, or the style the text names")
        if tech_row.get("format_regex"):
            notes.append("asserts a digit format (a regex containing [0-9]/\\d) while the "
                         "digest contains no digits at all — the text says a value exists, "
                         "not how it renders")
        if tech_row.get("role_unbacked"):
            roles = ", ".join(tech_row["role_unbacked"])
            notes.append(f"resolves a control by a role the digest never names ({roles}) — "
                         "the text's role is what the implementer was asked to expose; use "
                         "the role the text names")
        if tech_row.get("fixed_names"):
            names = ", ".join(f"`{n}`" for n in tech_row["fixed_names"])
            notes.append(f"uses fixed name(s) the digest never contains ({names}) — unless "
                         "the test creates them itself, the system was never asked to have "
                         "them (a name extrapolated from a quoted one is not quoted)")
        if notes:
            lines.append(f"  - {leaf}: " + "; ".join(notes))
    journey = audit.get("journey") or {}
    if journey.get("gap"):
        here = [leaf for leaf in group if leaf in (journey.get("creators") or [])]
        if here:
            lines.append(
                "  - suite-level: no spec in the whole suite creates or imports an entity and "
                f"then uses it; the creators in this batch ({', '.join(here)}) never operate on "
                "what they make. At least one test here must create or import the thing, then "
                "write into it (or rename it, or delete part of it), reload, and assert the "
                "effect survived.")
    return "\n".join(lines) if lines else "  (nothing flagged mechanically for this batch — judge by reading)"


# ---------------------------------------------------------------- 缺考处置（硬任务，2026-10-07）


# 排练实测：缺考清单只当「提示」时，模型补了 21 条测试、只消掉 1 条缺考。升成硬任务
# 之后的判据是**逐条交代**：补上检查（前后两遍体检的盘面差就是证据），或把带理由的
# 豁免写进 `_audit/suite-audit-waivers.md`（文件里有没有这个 ID，就是豁免的证据）。
# 都没做到 → 追一个小回合；再要不到就如实进报告和完成信号。
_WAIVER_LINE_RE = re.compile(
    r"^\s*[-*]*\s*`?(?:\*\*)?([A-Za-z][A-Za-z0-9_.-]*)(?:\*\*)?`?\s*[:：]\s*(\S.*)$")


def read_gap_waivers(suite_dir) -> dict[str, str]:
    """缺考条目的书面豁免：每行 `<节点ID>：<理由>`（`-` 和 `**加粗**` 都收）。

    没有理由的行不算数 —— 豁免的判据是「写了为什么」，不是「提到了这个节点」。
    文件不存在 / 读不到都返回空表（= 一条都没交代）。ID `suite` 保留给全套级的
    「建了要用」缺考。
    """
    path = Path(suite_dir) / SUITE_AUDIT_DIR / SUITE_WAIVER_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    waivers: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _WAIVER_LINE_RE.match(stripped)
        if not match:
            continue
        reason = match.group(2).strip().strip("`").strip()
        if len(reason) < 3:
            continue
        waivers[match.group(1)] = reason
    return waivers


def gap_disposition(before: dict, final: dict, waivers: dict) -> dict:
    """每条欠账的最终交代：修掉了（`filled`）/ 书面豁免（`waived`）/ 没交代（`undisposed`）。

    `filled` 取前后两遍体检的**盘面差** —— 修没修不看回合说了什么，看盘上少了谁。
    头一遍就没欠、后面也不欠的节点不进这里（它本来就不欠账）。`suite` 是保留 ID：
    「建了要用」的全套级缺考。

    2026-10-07 起「欠账」= 缺考（`gap_nodes`）∪ **结构性缺陷**（`defect_nodes`：
    任何实现都过不了的断言，见 `spec_structural_defects`）∪ **字面量对不上**
    （`literal_nodes`：外推名、没背书的角色、原文交代了进格子方式却直接对格子
    fill，见 `_extrapolated_names` 与 `_grid_editor_stated`）。三者走
    **同一条**处置流水线：修掉看盘面差；修不掉要么修卷子、要么在豁免文件里写明
    理由（误报的出口也在这里）；都不做就点名进报告和完成信号。
    """
    def flags(d: dict) -> list[str]:
        return list(dict.fromkeys(list(d.get("gap_nodes") or [])
                                  + list(d.get("defect_nodes") or [])
                                  + list(d.get("literal_nodes") or [])))
    before_flags = set(flags(before))
    final_flags = flags(final)
    filled = sorted(before_flags - set(final_flags))
    waived = {leaf: waivers[leaf] for leaf in final_flags if leaf in waivers}
    undisposed = [leaf for leaf in final_flags if leaf not in waivers]
    if (before.get("journey") or {}).get("gap") and not (final.get("journey") or {}).get("gap"):
        filled.append("suite")
    elif (final.get("journey") or {}).get("gap"):
        if "suite" in waivers:
            waived["suite"] = waivers["suite"]
        else:
            undisposed.append("suite")
    return {"filled": filled, "waived": waived, "undisposed": undisposed}


def suite_gap_disposition_note(audit: dict, group: list[str], suite_dir) -> str:
    """找茬提示词尾巴上的「必须逐条交代」清单：本组的欠账 + 豁免文件路径。

    提示词里本来就有机械提示（`suite_audit_hints`）；这里再加的是**规矩**：清单上
    的每一条在这个回合里必须有下文。欠账 = 缺考 + 结构性缺陷（过不了的断言，
    扫描器的原话就是修法）。本组没有欠账就返回空串（别添噪音）。
    """
    rows = audit.get("nodes") or {}
    owed: list[tuple[str, list[str]]] = []
    for leaf in group:
        row = rows.get(leaf) or {}
        items = (list(row.get("gaps") or []) + list(row.get("defects") or [])
                 + list(row.get("literals") or []))
        if items:
            owed.append((leaf, items))
    journey = audit.get("journey") or {}
    journey_here = bool(journey.get("gap")) and any(
        leaf in (journey.get("creators") or []) for leaf in group)
    if not owed and not journey_here:
        return ""
    lines = ["",
             "Items in this batch that MUST be dispositioned before you finish "
             "(fix the spec or add the check, or write a waiver with a reason):"]
    for leaf, items in owed:
        lines.append(f"  - {leaf}: " + "; ".join(items))
    if journey_here:
        lines.append("  - suite (use the id `suite` in the waivers file): no spec creates or "
                     "imports an entity and then uses it — at least one test here must do "
                     "that, or waive it with a reason.")
    lines.append("Waivers file (write_file; keep the lines already in it): "
                 f"{Path(suite_dir) / SUITE_AUDIT_DIR / SUITE_WAIVER_FILE}")
    lines.append("A waiver line is `- <node id>: <reason>` — a waiver without a reason is not "
                 "a disposition.")
    return "\n".join(lines)


def disposition_items(final: dict, undisposed: list[str], suite_dir) -> str:
    """追账回合的条目清单：节点 ID + 欠的是什么（缺的科目 / 过不了的断言）+ 卷子路径。"""
    rows = final.get("nodes") or {}
    journey = final.get("journey") or {}
    lines: list[str] = []
    for leaf in undisposed:
        if leaf == "suite":
            creators = ", ".join(journey.get("creators") or []) or "(none)"
            lines.append("- suite: no spec creates or imports an entity and then uses it — at "
                         "least one test must create or import something and then use what it "
                         f"made (or waive it with the id `suite`). Creator spec(s): {creators}")
            continue
        spec = Path(suite_dir) / f"{leaf}.spec.ts"
        where = str(spec) if spec.is_file() else "(none yet — write one for this node)"
        row = rows.get(leaf) or {}
        owe = "; ".join(list(row.get("gaps") or []) + list(row.get("defects") or [])
                        + list(row.get("literals") or []))
        lines.append(f"- {leaf}: {owe or 're-read the digest against the spec'}  (spec: {where})")
    return "\n".join(lines)


def render_suite_audit(audit: dict) -> str:
    """落盘的体检报告（markdown）—— 复盘时看的是它，不是日志里那一行。"""
    t = audit.get("totals") or {}
    rows = audit.get("nodes") or {}

    def mark(flag) -> str:
        return "yes" if flag else "—"

    journey = audit.get("journey") or {}
    lines = [
        "# acceptance suite audit (mechanical)",
        "",
        f"- nodes: {t.get('nodes', 0)}; specs on disk: {t.get('specs', 0)}; "
        f"test() blocks counted: {t.get('tests', 0)}; nodes with any trigger: {t.get('triggered', 0)}",
        f"- specs opening a second browser context: {len(audit.get('with_second_context') or [])}",
        f"- nodes whose requirement text implies a check the spec does not make: "
        f"{len(audit.get('gap_nodes') or [])}",
        f"- specs that create or import an entity: {len(journey.get('creators') or [])}; of "
        f"those, specs that then use it in the same file: {len(journey.get('bridging') or [])}",
    ]
    defect_nodes = audit.get("defect_nodes") or []
    if defect_nodes:
        lines.append(f"- **specs with an assertion no implementation can pass**: "
                     f"{len(defect_nodes)} ({', '.join(defect_nodes)})")
    over = audit.get("overreach") or {}
    if over.get("nodes"):
        lines.append(
            f"- nodes asserting beyond the requirement text: {len(over.get('nodes') or [])} "
            f"(aria w/o requirement: {len(over.get('aria_without_requirement') or [])}; "
            f"whole-page text: {len(over.get('whole_page_text') or [])}; "
            f"name matches without exact: {sum((over.get('loose_name_matches') or {}).values())}; "
            f"option-visibility: {len(over.get('option_visibility') or [])})")
    tech = audit.get("technique") or {}
    tech_nodes = tech.get("nodes") or []
    if tech_nodes:
        kinds = "; ".join(
            f"{label}: {len(tech.get(key) or [])}"
            for label, key in (("cell fill", "cell_fill"),
                               ("selectOption", "select_option"),
                               ("computed color", "style_color"),
                               ("digit format", "format_regex"),
                               ("role not in digest", "role_unbacked"),
                               ("fixed name w/o source", "fixed_names"))
            if tech.get(key))
        lines.append(f"- nodes betting on a DOM shape the requirement text does not fix: "
                     f"{len(tech_nodes)} ({kinds}) — hints, not verdicts")
    if journey.get("gap"):
        lines.append("- **journey gap**: no spec creates or imports an entity and then uses it "
                     f"(creator spec(s): {', '.join(journey.get('creators') or [])})")
    disp = audit.get("gap_disposition")
    if disp:
        # 硬任务升级（2026-10-07）：每条缺考必须有交代 —— 补上、或书面豁免；
        # 分开列，没交代的加粗点名（和完成信号、日志同一个口径）。
        lines += ["", "## flagged-item dispositions (every item must be answered): missing "
                      "checks, names, roles or interactions the text does not back, and "
                      "assertions no implementation can pass", ""]
        filled = disp.get("filled") or []
        if filled:
            lines.append(f"- filled during the audit: {len(filled)} ({', '.join(filled)})")
        else:
            lines.append("- filled during the audit: none")
        waived = disp.get("waived") or {}
        if waived:
            lines.append(f"- waived with a written reason: {len(waived)}")
            for node_id, reason in waived.items():
                lines.append(f"  - {node_id}: {reason}")
        else:
            lines.append("- waived with a written reason: none")
        missing = disp.get("undisposed") or []
        if missing:
            lines.append(f"- **no disposition at all — neither a check nor a written reason "
                         f"({len(missing)})**: {', '.join(missing)}")
        else:
            lines.append("- no disposition at all: none — every item on the list is answered")
    lines.append("")
    if rows:
        lines += ["| node | tests | 2nd ctx | persistence | multi-actor | format rule | beyond text | gaps |",
                  "|---|---|---|---|---|---|---|---|"]
        for leaf, row in rows.items():
            trig = row.get("triggers") or {}
            over_row = row.get("overreach") or {}
            over_mark = mark(over_row.get("aria_without_requirement")
                             or over_row.get("whole_page_text")
                             or over_row.get("loose_name_matches")
                             or over_row.get("option_visibility"))
            lines.append(
                f"| {leaf} | {row.get('tests', 0)} | {mark(row.get('second_context'))} | "
                f"{mark(trig.get('fresh_session'))} | {mark(trig.get('multi_actor'))} | "
                f"{mark(trig.get('format_rule'))} | {over_mark} | "
                f"{'; '.join(row.get('gaps') or []) or ''} |")
    defects = [(leaf, row.get("defects") or []) for leaf, row in rows.items() if row.get("defects")]
    if defects:
        lines += ["", "## assertions no implementation can pass (structural spec defects)", ""]
        for leaf, items in defects:
            for item in items:
                lines.append(f"- [{leaf}] {item}")
    if tech_nodes:
        lines += ["", "## DOM-shape bets the text does not fix (hints, not verdicts)", ""]
        for leaf in tech_nodes:
            bets = (rows.get(leaf) or {}).get("technique") or {}
            bits: list[str] = []
            if bets.get("cell_fill"):
                bits.append(f"cell fill ×{bets['cell_fill']}")
            if bets.get("select_option"):
                bits.append(f"selectOption ×{bets['select_option']} "
                            "(no native select in the digest)")
            if bets.get("style_color"):
                bits.append(f"toHaveCSS color ×{bets['style_color']} "
                            "(no color value in the digest)")
            if bets.get("format_regex"):
                bits.append(f"digit format ×{bets['format_regex']} (digest has no digits)")
            if bets.get("role_unbacked"):
                bits.append("role not named: " + ", ".join(bets["role_unbacked"]))
            if bets.get("fixed_names"):
                bits.append("names w/o source: "
                            + ", ".join(f"`{n}`" for n in bets["fixed_names"]))
            lines.append(f"- {leaf}: " + "; ".join(bits))
    return "\n".join(lines) + "\n"


def suite_audit_completion_note(audit: dict | None) -> str:
    """收尾上报里的体检注记 —— 卷子全绿也不许含糊成「全都验过了」。"""
    if not audit:
        return ""
    parts: list[str] = []
    gaps = audit.get("gap_nodes") or []
    defects = audit.get("defect_nodes") or []
    literals = audit.get("literal_nodes") or []
    disp = audit.get("gap_disposition")
    if disp:
        # 硬任务升级（2026-10-07）：欠账按「交代没交代」分开说 —— 写了理由的算交代过，
        # 剩下的是没交代的。旧的措辞只在没有处置信息时使用。结构性缺陷同日并入这本
        # 账：修掉了看盘面差，没修的要么是白纸黑字的豁免、要么点名。
        missing = [i for i in disp.get("undisposed") or [] if i != "suite"]
        waived = [i for i in (disp.get("waived") or {}) if i != "suite"]
        missing_broken = [i for i in missing if i in defects]
        missing_lit = [i for i in missing if i not in missing_broken and i in literals]
        missing = [i for i in missing if i not in missing_broken and i not in missing_lit]
        waived_broken = [i for i in waived if i in defects]
        waived_lit = [i for i in waived if i not in waived_broken and i in literals]
        waived = [i for i in waived if i not in waived_broken and i not in waived_lit]
        if missing:
            shown = ", ".join(missing[:4]) + ("…" if len(missing) > 4 else "")
            parts.append(f"{len(missing)} missing-check item(s) got neither a check nor a "
                         f"written reason ({shown})")
        if missing_broken:
            shown = ", ".join(missing_broken[:4]) + ("…" if len(missing_broken) > 4 else "")
            parts.append(f"{len(missing_broken)} spec(s) still hand expect() a Promise — an "
                         f"assertion no implementation can pass — with neither a fix nor a "
                         f"written reason ({shown})")
        if waived:
            shown = ", ".join(waived[:4]) + ("…" if len(waived) > 4 else "")
            parts.append(f"{len(waived)} missing-check item(s) waived with a written reason "
                         f"({shown})")
        if waived_broken:
            shown = ", ".join(waived_broken[:4]) + ("…" if len(waived_broken) > 4 else "")
            parts.append(f"{len(waived_broken)} spec defect(s) kept on a written reason "
                         f"({shown})")
        if missing_lit:
            shown = ", ".join(missing_lit[:4]) + ("…" if len(missing_lit) > 4 else "")
            parts.append(f"{len(missing_lit)} spec(s) use a name, role or interaction the "
                         f"requirement text does not back ({shown}) — with neither a fix nor "
                         f"a written reason")
        if waived_lit:
            shown = ", ".join(waived_lit[:4]) + ("…" if len(waived_lit) > 4 else "")
            parts.append(f"{len(waived_lit)} unbacked name/role/interaction item(s) kept on "
                         f"a written reason ({shown})")
    elif gaps:
        total = (audit.get("totals") or {}).get("nodes", len(audit.get("nodes") or {}))
        shown = ", ".join(gaps[:4]) + ("…" if len(gaps) > 4 else "")
        parts.append(f"{len(gaps)}/{total} node(s) still lack a check their requirement "
                     f"text implies ({shown})")
    if not disp and defects:
        # 审计关掉 = 只报不追：缺陷如实进完成信号（和缺考的旧口径同一条）。
        shown = ", ".join(defects[:4]) + ("…" if len(defects) > 4 else "")
        parts.append(f"{len(defects)} spec(s) contain an assertion no implementation can pass "
                     f"({shown})")
    if not disp and literals:
        shown = ", ".join(literals[:4]) + ("…" if len(literals) > 4 else "")
        parts.append(f"{len(literals)} spec(s) use a name, role or interaction the requirement "
                     f"text does not back ({shown})")
    if (audit.get("journey") or {}).get("gap"):
        if disp and "suite" in (disp.get("waived") or {}):
            parts.append("no spec creates or imports an entity and then uses it "
                         "(waived with a written reason)")
        elif disp and "suite" in (disp.get("undisposed") or []):
            parts.append("no spec creates or imports an entity and then uses it, and no "
                         "written reason was given")
        else:
            parts.append("no spec creates or imports an entity and then uses it")
    over = (audit.get("overreach") or {}).get("nodes") or []
    if over:
        shown = ", ".join(over[:4]) + ("…" if len(over) > 4 else "")
        parts.append(f"{len(over)} node(s) assert beyond the requirement text, risking "
                     f"unwinnable checks ({shown})")
    tech = (audit.get("technique") or {}).get("nodes") or []
    if tech:
        shown = ", ".join(tech[:4]) + ("…" if len(tech) > 4 else "")
        parts.append(f"{len(tech)} node(s) bet on a DOM shape their text does not fix "
                     f"({shown})")
    shared = audit.get("shared_flags") or []
    if shared:
        shown = ", ".join(flag["spec"] for flag in shared[:3]) + ("…" if len(shared) > 3 else "")
        parts.append(f"{len(shared)} spec(s) change a shared, pre-provisioned object without "
                     f"restoring it ({shown})")
    if not parts:
        return ""
    return "; suite audit: " + "; ".join(parts)


# ---------------------------------------------------------------- v5 增量维护


# 出卷时的「写作」动作：填充/输入/选择的具体值。冲突扫描就看这些字面量。
#
# 两种写法都要认，因为它们在盘上都真实存在：
#   · locator 写法   `page.getByLabel('Name').fill('X')`        —— 第一个字面量就是值
#   · page 写法      `page.fill('#name', 'X')`                  —— 第一个是**选择器**，
#                                                                 第二个才是值
# 只认第一种的话，page 写法会被当成「选择器是资源名」，真冲突反而漏掉。
# `.press('Enter')` 不收：按键在几十份卷子里天然重复，收进来全是噪音。
_WRITE_CALL = re.compile(
    r"\.(?:fill|type|selectOption)\(\s*"
    r"(?:['\"][^'\"\\\n]{1,200}['\"]\s*,\s*)?"          # 可选的 page 写法选择器
    r"(?P<quote>['\"])(?P<val>(?:(?!(?P=quote)).){1,120})(?P=quote)")
# 算源码体量时认的后缀（.ts/.tsx/.js/.jsx/.json/.css/.html/.vue/.mjs/.cjs）。
_SOURCE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".json", ".css", ".html", ".vue", ".mjs", ".cjs"}
# 数体量时跳过的目录。
_SOURCE_SKIP = {"node_modules", "dist", "build", "coverage", ".git", ".next", "out"}


def scaffold_like(output_dir: Path, max_bytes: int | None = None) -> bool:
    """这个「现成的 app」是不是一个空架子（毛坯）。

    判据只有一条：`frontend/` + `backend/` 里的源码**总字节数**小到不像一个做过
    活的 app。模板骨架（占位页 + 空路由）落在这个线以下；正赛跑出来的产物
    （几十个页面 + 几十条路由）远在它以上。

    为什么按大小而不按内容：内容是任意的（什么技术栈都可能），体量是客观的。
    误判的代价是不对称的 —— 把真 app 误当毛坯会把它**拆了从零建**（正赛实测
    代价 40-80 倍），所以阈值要低到只掐真正的空壳；`ARCBENCH_SCAFFOLD_MAX_BYTES`
    可以调，`ARCBENCH_KEEP_TEMPLATE=1` 直接关掉「拆」这个动作。
    """
    if max_bytes is None:
        try:
            max_bytes = int(os.environ.get("ARCBENCH_SCAFFOLD_MAX_BYTES", "65536"))
        except ValueError:
            max_bytes = 65536
    total = 0
    seen = False
    for name in ("frontend", "backend"):
        root = Path(output_dir) / name
        if not root.is_dir():
            return False          # 两半都得在，缺一半不算「像样的 app」
        seen = True
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in _SOURCE_EXTS:
                continue
            if any(part in _SOURCE_SKIP for part in path.parts):
                continue
            try:
                total += path.stat().st_size
            except OSError:
                continue
            if total >= max_bytes:
                return False      # 已经超线，不必数完
    return seen and total < max_bytes


def discard_allowed(output_dir: Path) -> tuple[bool, str]:
    """**默认永不清场**（v5 的起跑纪律）。返回 (允不允许拆, 为什么)。

    正赛那次「体检不合格就炸掉」的代价是：一个**大部分能用**的继承 app 被
    整个搬走、56 个节点从零重建，然后超时、然后分数还不如原地维护。所以
    拆楼的许可被收到只剩两种情形：

      · 环境变量 `ARCBENCH_KEEP_TEMPLATE=1` —— **强制保留**，任何情况都不拆
        （人在现场判断「这楼不能拆」时用的总闸）；
      · 真的毛坯（`scaffold_like`）—— 没有可维护的东西，拆不拆都一样。

    其余一律保留：占位也好、半成品也好、有一半是坏的也好，都是在原地改。
    旧行为由显式开关承担，不是默认。
    """
    if os.environ.get("ARCBENCH_KEEP_TEMPLATE", "0") == "1":
        return False, "ARCBENCH_KEEP_TEMPLATE=1 — the existing app is kept, whatever the probe says"
    if scaffold_like(output_dir):
        return True, "the app on disk is an empty scaffold — nothing worth keeping"
    return False, ("the app on disk is real work (it has substance); it is kept and maintained "
                   "in place, however red the probe came back")


def quarantine_foreign_specs(suite_dir: Path, keep: set[str], dest: Path) -> list[str]:
    """把**不是这一轮出的**卷子请出 `suite_dir`（移到 `dest`，不删）。

    为什么必须动它：`_all_specs()` 是对着卷子目录 rglob 的，收尾全卷体检和
    全卷对账都拿它当分母。继承下来的官方卷子有一部分测的是**本轮不再要求**的
    行为（决赛文档改了文本、删了节点），它们永远红、永远修不动 —— 留在盘上
    就是给每一次全卷测量放一个「注定失败」的用例，把「修不动」的噪音灌进
    修复队列。

    移走不是删掉：文件在 `.arc/inherited-specs/` 里还在，日志里报数量，
    事后可查。返回被移走的相对路径列表。
    """
    moved: list[str] = []
    root = Path(suite_dir)
    if not root.is_dir():
        return moved
    for path in sorted(root.rglob("*.spec.ts")):
        if path.name[: -len(".spec.ts")] in keep:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = Path(path.name)
        target = Path(dest) / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target))
            moved.append(rel.as_posix())
        except OSError as exc:  # noqa: BLE001 — 移不动不能打断出卷
            log(f"[author] could not set aside the stale spec {rel}: {exc}")
    return moved


def scan_suite_conflicts(suite_dir: Path) -> dict[str, list[str]]:
    """冲突扫描：**同一个固定字面量被两份以上的卷子写进去**。

    正赛实测的那种死局（「A 需求和 B 需求不可能共存」）在文字层有一条可机械
    检出的影子：两份卷子往**同一个名字**上写（同一个工作簿名、同一个账号、
    同一个资源）。谁先跑谁把它改成另一个样子，后跑的断言「它现在是 X」当场
    失败，而且**永远**失败 —— 双方都没写错，是它们抢同一件东西。

    扫描的边界要说清楚：它认的是**写死的字面量**（`.fill('X')`、`.fill('#sel', 'X')`、
    `.selectOption(...)` 里的那个值），不是变量、不是运行期生成的名字。所以它
    只抓得住「两份卷子抢同一个固定资源」这一半；「两个需求在语义上互斥」
    那一半机器判不了，仍然靠出卷提示词里的一题一状态规则（见
    `ACCEPTANCE_AUTHOR_PROMPT`）。抓得住的这一半必须抓住 —— 它最容易发生，
    也最难在日志里认出来。

    返回 {字面量: [卷子文件:行, …]}，只留出现在 ≥2 份**不同**卷子里的字面量。
    """
    root = Path(suite_dir)
    hits: dict[str, list[str]] = {}
    files: dict[str, set[str]] = {}
    if not root.is_dir():
        return hits
    for path in sorted(root.rglob("*.spec.ts")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        for number, line in enumerate(text.splitlines(), 1):
            for match in _WRITE_CALL.finditer(line):
                val = (match.group("val") or "").strip()
                # 空串/单字符/明显是模板占位的短值不算资源名。
                if len(val) < 3 or val.startswith("${"):
                    continue
                hits.setdefault(val, []).append(f"{rel}:{number}")
                files.setdefault(val, set()).add(rel)
    return {val: where for val, where in hits.items() if len(files.get(val, ())) >= 2}


def conflict_hints(conflicts: dict[str, list[str]], group: list[str],
                   limit: int = 8) -> str:
    """把冲突扫描的结果变成找茬提示词的素材（按组过滤）。"""
    mine = set(group)
    rows = []
    for val, where in sorted(conflicts.items()):
        stems = {Path(w.split(":")[0]).name[: -len(".spec.ts")] for w in where}
        if not (stems & mine):
            continue
        rows.append(f"  - {val!r} is written by: {', '.join(sorted(where)[:6])}")
        if len(rows) >= limit:
            break
    if not rows:
        return ""
    return ("\nFIXED LITERALS WRITTEN BY MORE THAN ONE SPEC FILE (mechanical scan of the suite on "
            "disk — two specs sharing one name will fight over it, and one of them will fail "
            "forever):\n" + "\n".join(rows) + "\nEvery spec has to create its own object under a "
            "name only it uses (or reset a pre-provisioned one it shares), so that no two specs "
            "compete for the same resource.\n")


# ------------------------------------------------ v5.1 公共对象点名（卷子自证）

# 「公共对象」：判分环境**本来就有、而且只有一个**的东西 —— 种子工作簿、预置账号、
# 已经被占用的名字。谁都可以读它、断言它；但一份卷子改过不还原，后面所有依赖它
# 的卷子看到的就是另一个世界 —— 两边都没写错，却注定有一边红（正赛 sheet 的
# 死局正是这个形状：套用到的固定工作簿只有一本，24 份卷子里 17 份都往上面写）。
#
# 清单**不写死在 harness 里**（那是任务数据，换一个任务就废），从两处点名：
#   · 需求原文里带「种子/预置/已有」的句子里**用反引号包起来**的名字 —— 高置信，
#     判据只用这一份。反引号是需求作者自己给的「这是个名字/代码」标记；别的引号
#     （"…"、“…”）里多半是报错文案和按钮标签（“Username already exists”），把
#     它们也算成对象，判据会被稀释成「几乎每份卷子都碰过公共对象」——两轮真实文本
#     里需要点名的对象**全部**带反引号，所以这条规则够用而且干净；
#   · 盘上种子/初始化代码里出现的名字 —— 与上一条重合的标 app；只在盘上出现、
#     需求没点到的，进报告单列一节（只报告，不判）。
# 然后拿这份清单去照每份卷子：动过公共对象、又没有任何**可看见的**复原动作的，
# 点名到文件。边界和冲突扫描同一条：认不出「手工把原值写回去」那种复原。
_SHARED_SENTENCE = re.compile(
    r"\b(?:seed(?:ed|s)?|(?:pre-?)?provision(?:s|ed|ing)?|pre-?sets?|pre-?exists?ing|"
    r"already\s+exists?|"
    r"initial\s+(?:data|state|content)|starts?\s+with|ships?\s+with|comes?\s+with)\b"
    r"|种子|预置|已有|初始|自带|原有",
    re.IGNORECASE)
# 引号里的一小段就是一个候选名字（`Q3 Sales`、`Sheet1`、`A1:C6`…）。五个组按引号
# 种类分：1=反引号（判据只用它），其余是弯引号和 ASCII 引号（进报告、不判）。
# 宽度用 {1,80} 而不是 {2,80}：单字符的引号串（`` `6` ``）如果匹配不上，后面所有
# 引号的配对就会整体错位 —— `values `6` and `14`` 会把中间的 " and " 当成名字。
_SHARED_TOKEN = re.compile(r"`([^`\n]{1,80})`|'([^'\n]{1,80})'|\"([^\"\n]{1,80})\""
                           r"|“([^”\n]{1,80})”|‘([^’\n]{1,80})’")
# 「复原动作」的机械影子：生命周期钩子，或一个 restore/reset/reseed 形状的调用。
_SHARED_ISOLATION = re.compile(
    r"\b(?:beforeEach|afterEach|beforeAll|afterAll)\b"
    r"|\.(?:post|put|patch|delete|get)\s*\(\s*[`'\"][^`'\"\n]*(?:restore|reset|reseed)",
    re.IGNORECASE)
# 盘上扫种子代码时不要的东西：代码标识符（snake_case/camelCase）、常量、时间戳、
# 协议串。名字是给人看的，不是给编译器看的。
_SHARED_APP_STOPWORDS = {
    "utf-8", "us-ascii", "application/json", "text/html", "text/plain",
    "localhost", "undefined", "localhost:3000",
}


def _shared_token_ok(tok: str) -> bool:
    tok = tok.strip().strip(".,;:!?")
    if not (2 <= len(tok) <= 60):
        return False
    if len(tok.split()) > 4:                      # 一句话不是名字
        return False
    if any(ch in tok for ch in "{}<>$\n\\"):
        return False
    if tok.startswith(("http", "//", "/")):
        return False
    if not re.search(r"[^\W\d_]", tok, re.UNICODE):  # 至少一个字母/汉字
        return False
    if not tok[0].isalnum() or any(ch in tok for ch in "()="):
        # 公式（`=A1+B1`、`AVERAGE(B2:B10)`）、CLI 开关（`--workers=1`）、错误码
        # （`#REF!`）都在种子脚本的引号里成群出现 —— 它们是代码，不是对象名字。
        return False
    return True


def _shared_name_in(name: str, text: str) -> bool:
    """名字出现在这一行里（按词边界，`Sheet1` 不命中 `Sheet10`）。"""
    return re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text) is not None


def requirement_shared_names(req_dir: Path) -> tuple[dict[str, str], dict[str, str]]:
    """需求原文里「种子/预置/已有」的句子中点名的对象。

    返回 `(对象名单, 别的引号里的串)`：
      · 对象名单 = **反引号**包起来的名字 → {名字: 出处}，判据只用这一份；
      · 别的引号（ASCII/弯引号）里的串多半是报错文案、按钮标签、示例值 ——
        {串: 出处}，进报告单列一节，只报告不判。
    """
    names: dict[str, str] = {}
    quoted: dict[str, str] = {}
    root = Path(req_dir)
    if not root.is_dir():
        return names, quoted
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in (".yaml", ".yml", ".md", ".json", ".txt"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        for number, line in enumerate(text.splitlines(), 1):
            if not _SHARED_SENTENCE.search(line):
                continue
            for match in _SHARED_TOKEN.finditer(line):
                tok = next((g for g in match.groups() if g), "").strip()
                if not _shared_token_ok(tok) or tok in _SHARED_APP_STOPWORDS:
                    continue
                tok = tok.strip().strip(".,;:!?")
                if match.group(1) is not None:            # 反引号：这是个名字
                    names.setdefault(tok, f"{rel}:{number}")
                else:
                    quoted.setdefault(tok, f"{rel}:{number}")
    return names, quoted


def app_shared_names(app_dir: Path) -> set[str]:
    """盘上种子/初始化代码里出现的名字（高置信与否由 `scan_shared_objects` 定）。"""
    out: set[str] = set()
    root = Path(app_dir) if app_dir else None
    if root is None or not root.is_dir():
        return out
    candidates: list[Path] = []
    for pattern in ("*seed*", "*bootstrap*", "*initial*", "data.json"):
        for path in root.rglob(pattern):
            if not path.is_file() or path.suffix.lower() not in _SOURCE_EXTS:
                continue
            if any(part in _SOURCE_SKIP or part in (".arc", ".git") for part in path.parts):
                continue
            candidates.append(path)
    for path in sorted(candidates)[:40]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:200000]
        except OSError:
            continue
        for match in _SHARED_TOKEN.finditer(text):
            tok = next((g for g in match.groups() if g), "").strip()
            if not _shared_token_ok(tok) or tok in _SHARED_APP_STOPWORDS:
                continue
            if re.match(r"^\d{4}-\d{2}-\d{2}", tok):        # 时间戳
                continue
            if re.match(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)+$", tok):   # snake_case 标识符
                continue
            if re.match(r"^[a-z]+[A-Z][A-Za-z0-9]*$", tok):         # camelCase 标识符
                continue
            if re.match(r"^[0-9a-fA-F]{16,}$", tok):                # 十六进制摘要
                continue
            out.add(tok.strip().strip(".,;:!?"))
    return out


def scan_shared_objects(suite_dir: Path, req_dir: Path | None = None,
                        app_dir: Path | None = None) -> dict:
    """公共对象点名 + 每份卷子的自证（动过、且没有可见复原动作 → 点出来）。

    返回 `{"objects": {名字: {...}}, "flags": [...], "app_only": [...]}`。
    判据（哪些卷子被点）只用**需求原文点名**的那一份清单：盘上扫出来的名字做
    对照 —— 高置信的标准是「需求也提到它」；只有盘上有的，进报告不判。
    """
    objects: dict[str, dict] = {}
    quoted_only: list[str] = []
    if req_dir:
        names, quoted = requirement_shared_names(req_dir)
        quoted_only = sorted(quoted)
        for name, where in names.items():
            objects[name] = {"sources": ["requirement"], "where": where, "specs": {}}
    app_names = app_shared_names(app_dir) if app_dir else set()
    for name, info in objects.items():
        if name in app_names:
            info["sources"].append("app")
    app_only = sorted(app_names - set(objects))

    flags: list[dict] = []
    root = Path(suite_dir)
    if root.is_dir():
        for path in sorted(root.rglob("*.spec.ts")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = path.name
            lines = text.splitlines()
            touched: dict[str, list[int]] = {}
            for name in objects:
                hits = [n for n, line in enumerate(lines, 1) if _shared_name_in(name, line)]
                if hits:
                    touched[name] = hits
                    objects[name]["specs"][rel] = hits
            if not touched:
                continue
            writes = []
            for line in lines:
                for match in _WRITE_CALL.finditer(line):
                    val = (match.group("val") or "").strip()
                    if len(val) >= 3 and not val.startswith("${"):
                        writes.append(val)
            if not writes or _SHARED_ISOLATION.search(text):
                continue
            flags.append({"spec": rel, "objects": sorted(touched), "writes": len(writes),
                          "shared_writes": sorted(set(writes) & set(objects))})
    flags.sort(key=lambda f: (-len(f["shared_writes"]), f["spec"]))
    return {"objects": objects, "flags": flags, "app_only": app_only,
            "quoted_only": quoted_only}


def render_shared_objects(scan: dict) -> str:
    """点名报告（落盘 `<suite>/_audit/suite-shared-objects.md`）。"""
    objects = scan.get("objects") or {}
    flags = scan.get("flags") or []
    lines = ["# 公共对象点名（判分环境本来就有的东西）", "",
             "这些对象只有一个：谁都可以读它、断言它；一份卷子改过不还原，后面所有依赖",
             "它的卷子看到的就是另一个世界 —— 两边都没写错，却注定有一边红。清单从需求",
             "原文的「种子/预置」句子 + 盘上种子代码里点名，不写死在任何地方。", ""]
    if not objects:
        lines.append("（需求原文里没有点到任何种子对象 —— 这份清单为空。）")
    for name, info in sorted(objects.items()):
        src = "、".join({"requirement": "需求原文", "app": "盘上种子代码"}.get(s, s)
                        for s in info.get("sources") or [])
        refs = ", ".join(sorted(info.get("specs") or {})) or "（没有卷子引用它）"
        lines.append(f"- `{name}` —— 出处：{src}（{info.get('where', '')}）")
        lines.append(f"  - 引用它的卷子：{refs}")
    if flags:
        lines += ["", "## 动过公共对象、又没见到复原动作的卷子", "",
                  "（机械扫描只认得出生命周期钩子（beforeEach/afterEach）和 restore/reset 形状",
                  "的调用；「手工把原值写回去」这种复原告不好认 —— 认得出的都列在这里，",
                  "认不出的靠找茬回合逐份读。）", ""]
        for flag in flags:
            extra = ("；其中写死了公共名：" + ", ".join(f"`{v}`" for v in flag["shared_writes"])
                     if flag.get("shared_writes") else "")
            lines.append(f"- `{flag['spec']}` —— 引用 "
                         + ", ".join(f"`{o}`" for o in flag["objects"][:6])
                         + f"；写了 {flag['writes']} 处固定值{extra}，没见到复原动作")
    else:
        lines += ["", "没有卷子在不带复原动作的情况下动公共对象。"]
    if scan.get("app_only") or scan.get("quoted_only"):
        lines += ["", "## 只报告、不判的候选（判据不用它们）", ""]
    if scan.get("quoted_only"):
        lines += ["- 需求句子里用别的引号（不是反引号）包起来的串 —— 多半是报错文案/"
                  "按钮标签/示例值，不算对象名：" + ", ".join(f"`{n}`" for n in scan["quoted_only"][:24])]
    if scan.get("app_only"):
        lines += ["",
                  "（多半是实现自己挑的说法；判分方认哪一种，需求文本说了不算。）", "",
                  "- 盘上种子代码里出现、需求原文没点到的名字：" +
                  ", ".join(f"`{n}`" for n in scan["app_only"][:24])]
    return "\n".join(lines) + "\n"


def shared_object_hints(scan: dict, group: list[str], limit: int = 8) -> str:
    """把公共对象点名变成找茬提示词的素材（按组过滤）。"""
    mine = set(group)
    rows = []
    for flag in scan.get("flags") or []:
        stem = Path(flag["spec"]).name[: -len(".spec.ts")]
        if stem not in mine:
            continue
        rows.append(f"  - {flag['spec']} touches "
                    + ", ".join(flag["objects"][:6])
                    + f"; it writes {flag['writes']} fixed value(s) and shows no restore step")
        if len(rows) >= limit:
            break
    names = sorted(scan.get("objects") or {})
    if not names:
        return ""
    head = ("\nSHARED, PRE-PROVISIONED OBJECTS: the text names objects the system already has ("
            + ", ".join(names[:8]) + ("…" if len(names) > 8 else "")
            + "). There is exactly one of each and this suite drives one server: read them and "
            "assert on them, but a spec that changes one must put it back before it ends.\n")
    if not rows:
        return head
    return (head + "Specs in this batch that change a shared object with no restore step the "
            "mechanical scan can see:\n" + "\n".join(rows) + "\n")


# ---------------------------------------------------------------- flow


class Flow:
    def __init__(self, args, output_dir: Path, req_dir: Path) -> None:
        self.output_dir = output_dir
        self.req_dir = req_dir
        self.web_port = args.web_port
        self.smoke_port = int(os.environ.get("OCTOS_SMOKE_PORT", "3100"))
        if self.smoke_port == self.web_port:
            self.smoke_port += 1
        # 1200 → 2400（2026-10-01）。这一个数字是**单个回合的硬闸门**，而且
        # 超时不重放（`_transient` 对 "turn timed out" 返回 False）——被砍断的
        # 回合没有第二次机会。
        #
        # 实现回合本身不跑验收（跑验收的是它后面的机器循环），但**修复回合要**：
        # 拿到失败清单之后要读代码、改、再自己跑一遍隔离验收，而
        # `verify_app.py` 建副本 + 装依赖 + 起服务 + 开浏览器本身就要几分钟。
        # 20 分钟里「改完再验完」很紧，而修复被砍断留下的可能是改了一半的树
        # （cloud 6e82a7ff571c：修复在 1200s 被砍，下一轮从 27/32 掉到 17/32，
        # 后面几轮都在修它造成的损伤）。
        #
        # 上限仍在，`remaining / nodes_left` 继续保护后面的节点。
        self.node_timeout = int(os.environ.get("OCTOS_NODE_TIMEOUT", "2400"))
        self.budget = int(os.environ["OCTOS_TIME_BUDGET"]) if os.environ.get("OCTOS_TIME_BUDGET") else 3600
        self.budget_explicit = bool(os.environ.get("OCTOS_TIME_BUDGET"))
        # keep-local-3 (workflow C): with 480 s/node, 16 of 17 implement/repair
        # turns were cut at 283 s; Web nodes need 10-20 min of implementation.
        self.seconds_per_node = int(os.environ.get("OCTOS_SECONDS_PER_NODE", "1500"))
        # 平台的墙钟硬顶。**这是一个外部事实，不是我们的选择**：22 小时之后进程会被
        # 杀掉，杀掉意味着没有终检、没有彩排、没有上报 —— 交上去的是一个半成品。
        #
        # 为什么必须有这条：预算原本是 `1500 × 节点数` 算出来的，65 个节点 = 27h，
        # **比平台允许的还长**。于是最坏情况不是「跑得慢」，是「跑到 22 小时被从中间
        # 掐断」——而那正好是我们最不希望发生的形态：所有修复都花掉了，收尾全没做。
        #
        # 削到哪儿？**由「预算管不到的那两段」倒推**，不是一个拍出来的整数：
        #
        #   平台红线
        #     − 一个在飞的回合          预算是「不再开新回合」，不是「打断正在跑的」。
        #                              最后一个回合可以在 remaining≈0 时起步，跑满
        #                              单回合闸门（`node_timeout`，2400s）才轮到下一句
        #                              `time_up()`。
        #     − 收尾                    `final_acceptance_passes()` 是**不设闸门**的
        #                              （它必须跑完：终检是最后一道体检），后面还有彩排、
        #                              postflight、上报。1800s 是这个尾巴在旧注释里的
        #                              估值，沿用。
        #   = 预算上限
        #
        # 79200 − 2400 − 1800 = 75000（20.8h）。写死一个 78000 也能跑，但它是对的
        # 纯属巧合：谁把 `node_timeout` 调大一点，那个数字就悄悄越线了，而这种越线的
        # 后果（收尾被杀）在报告里看不出来。
        #
        # `OCTOS_ARC_MAX_WALL_SECONDS=0` 关掉它（本地长跑用得到）。
        self.platform_wall_seconds = int(os.environ.get("OCTOS_ARC_PLATFORM_WALL_SECONDS", "79200"))
        self.tail_seconds = int(os.environ.get("OCTOS_ARC_TAIL_SECONDS", "1800"))
        self.max_wall_seconds = int(os.environ.get(
            "OCTOS_ARC_MAX_WALL_SECONDS",
            str(max(0, self.platform_wall_seconds - self.node_timeout - self.tail_seconds))))
        # 300 → 450（2026-10-01）。这是修复轮的**时间地板**，也是
        # `repair_needs()` 在没有历史样本时的初值（实际值取最近若干轮的中位数 ×1.1）。
        # 单回合闸门抬到 2400 之后，一轮「读失败 → 改 → 重跑隔离验收」的正常
        # 耗时跟着上去了，地板得跟着抬——地板猜小了的后果不是保守，是反过来：
        # 剩 300–600 秒时它会放行一轮注定被砍断的修复，那一轮什么都留不下。
        self.min_repair_seconds = int(os.environ.get("OCTOS_MIN_REPAIR_SECONDS", "450"))
        # 1500 → 3000。这个上限的作用是「别让一个节点吃掉全部预算」，
        # 但那件事 `remaining / nodes_left` 已经在做了——它每个节点重算一次，
        # 超支会自动把后面的份额压下去。而这个 **硬上限** 做的是另一件事：
        # 它把快节点省下来的余额**锁住不让慢节点用**。
        #
        # keep @ D 的实测，直接说明代价（2026-09-19）：
        #     全程预算 48,000s，实际用掉 30,240s —— **剩 17,760s（37%）没用**
        #     而六个失败节点各自只超出单节点上限 13–18 秒就被砍断：
        #     implement 900s（0.6 × 1500 的上限）+ 验收循环 613s = 1513s > 1500s
        #     于是 `left < min_repair_seconds` 成立，修复停在 round 1，节点判负。
        # 六个节点在 37% 的余额面前，因为差十几秒而失败。
        #
        # 抬到 3000 之后，`node_timeout`（1200）会先卡住 implement 轮，
        # 剩下的 ≥1800s 归验收循环——够两到三轮修复，而不是恰好一轮。
        # 上限仍在（不是取消），且 `remaining / nodes_left` 继续保护后面的节点。
        #
        # 3000 → 6000（2026-10-01）：同一个道理再走一步。单回合闸门抬到 2400
        # 之后，一个节点「实现 + 两三轮修复」的正常形状就是 2400 + 3×1200 ≈ 6000s。
        # 停在 3000 的话，实现回合吃掉 2400 之后就只剩 600s 给修复——又回到
        # 上面那段记录的老毛病：节点在余额充足的情况下因为差十几秒被判负。
        # 上限仍在，`remaining / nodes_left` 继续保护后面的节点。
        self.node_budget_cap = int(os.environ.get("OCTOS_NODE_TIME_BUDGET", "6000"))
        # 5 → 8（2026-10-01）。轮数是**天花板不是承诺**：真正卡住的是节点份额
        # （6000s）和剩余时间，每一轮开跑前都会问一次「剩下的时间够不够开这一轮」
        # （`remaining < 240` / `< min_repair_seconds × 3`）。所以抬高它只是
        # 不再人为地提前判负，不会让一个节点无限修下去。
        self.repair_rounds = int(os.environ.get("OCTOS_REPAIR_ROUNDS", "8"))
        self.repair_rounds_explicit = bool(os.environ.get("OCTOS_REPAIR_ROUNDS"))
        # -- 交付保底（封箱）的账 -------------------------------------------
        # 整个 run 量到过的最好全卷版本（「成绩卡」）。封箱时交付的就是它，
        # 而不是「盘上恰好剩下的版本」——见 `seal_delivery`。
        self._best_state: dict | None = None
        self._best_summary = None
        self._best_grouped = None
        # 最近一次全卷测量的结果，以及「自那之后动过手吗」。任何一个模型回合
        # 都可能改到应用文件（`turn()` 里置脏），全卷测量清零；封箱拿这两个
        # 判断「当前树配不配当交付物」。
        self._last_full_measure: dict | None = None
        self._tree_maybe_edited = True
        # Run-wide cost guard. Defaults scale with the tree and sit ~3x above a normal run
        # (calibration: cloud keep 2224a9013528, 32 nodes, PASSED 32/32, 9,038 s, ¥16.58 ≈ 26M
        # platform tokens ≈ 0.8M tokens and ~1.1 turns per node), so they never truncate a
        # healthy run; they only stop repair loops that have gone pathological. Explicit env
        # values override (0 = off). OCTOS_ARC_MAX_TOTAL_TOKENS_ABS is the optional absolute
        # ceiling for a per-run spend rule (e.g. ¥50 ≈ 75M tokens at the observed ¥0.63/M).
        self.max_total_tokens = int(os.environ.get("OCTOS_ARC_MAX_TOTAL_TOKENS", "-1"))
        self.max_turns = int(os.environ.get("OCTOS_ARC_MAX_TURNS", "-1"))
        self.max_total_tokens_abs = int(os.environ.get("OCTOS_ARC_MAX_TOTAL_TOKENS_ABS", "0"))
        self.turn_count = 0
        self._wound_down_logged = False
        # 配额等待的累计账（见 `Flow.hold_for_quota`）：整跑上限跨回合生效，
        # 免得配额反复打爆时一等等掉半个预算。
        self.quota_waited_total = 0.0
        self.small_task_nodes = int(os.environ.get("OCTOS_SMALL_TASK_NODES", "2"))
        self.implement_fraction = float(os.environ.get("OCTOS_IMPLEMENT_FRACTION", "0.6"))
        self.alias_states = os.environ.get("OCTOS_ARC_ALIAS_SPEC_IDS", "1") != "0"
        self.perf_contract = os.environ.get("OCTOS_PERF_CONTRACT", "1") != "0"
        self.guard_enabled = os.environ.get("OCTOS_GUARD", "1") != "0"
        self.t_start = time.time()
        self.runtime = None
        self.events = None
        self.driver: OctosDriver | LocalDriver | ToolDriver | None = None
        self.tests_dir: Path | None = None
        # 卷子是不是我们自己写的（见 `author_acceptance_suite`）。**默认 False，
        # 也就是「官方卷子」** —— 那个方向上的错误是安全的：官方卷子只是少一条
        # 出口，自产卷子被误当成官方卷子也只是少一条出口，两者都不会让判据变松。
        self.authored_suite = False
        self.spec_map: dict = {None: []}
        self.probe_summaries: dict = {}
        self.probe_count = 0  # nodes actually probed against the existing app
        # v5 体检表：每个探过的要求点一行 —— 过几个/共几个/第一个失败长什么样。
        # 它是「施工清单」（`write_construction_plan`）和勘察回合的原料：
        # 模型开场先看这份事实，再决定哪些沿用、哪些改造、哪些新增。
        self.probe_health: dict = {}
        # 模板探针只生效一次。它在两个地方被调用（见 `probe_existing_app`）：官方卷子
        # 那条路和自产卷子那条路，而后者的卷子要等到出卷回合跑完才存在。
        self._probed = False
        self.aliases: dict[str, str] = {}
        self.runner: AcceptanceRunner | None = None
        self.test_verdict: dict[str, bool | None] = {}
        self.impl_failed: list[str] = []
        self.pending_corrections: list[str] = []
        self.evolution = False
        self.folder_children: dict[str, list[str]] = {}

    # -- helpers ----------------------------------------------------------
    def wound_down(self) -> bool:
        """True once the run has spent its token or turn allowance: no more repair
        turns, remaining nodes get one implement turn each, one final suite, done."""
        proxy = getattr(self, "llm_proxy", None)
        tokens = proxy.total_tokens if proxy is not None else 0
        over = (self.max_total_tokens > 0 and tokens >= self.max_total_tokens) or \
               (self.max_turns > 0 and self.turn_count >= self.max_turns) or \
               (self.max_total_tokens_abs > 0 and tokens >= self.max_total_tokens_abs)
        if over and not self._wound_down_logged:
            self._wound_down_logged = True
            log(f"[guard] cost guard tripped: {tokens} billable tokens, {self.turn_count} turns "
                f"(limits {self.max_total_tokens} / {self.max_turns} / abs {self.max_total_tokens_abs}); no further repair turns")
        return bool(over)

    def remaining(self) -> float:
        return self.budget - (time.time() - self.t_start)

    def time_up(self) -> bool:
        return self.remaining() <= 0

    def mark(self, kind: str, node_id: str, message: str | None = None) -> None:
        fn = getattr(self.events, f"mark_{kind}")
        fn(node_id, message)
        if self.alias_states:
            for alias, target in self.aliases.items():
                if target == node_id:
                    fn(alias, message)

    def protected_prefixes(self) -> list[str]:
        prefixes = [".arc/", str(self.output_dir / ".arc"), "requirements/", str(self.req_dir)]
        if self.tests_dir:
            prefixes.append(str(self.tests_dir))
        return prefixes

    def inline_source_chars(self) -> int:
        """How much of the app to quote into a repair turn.

        This is an input budget and the context window bounds it.

        The default is unchanged. Whether a larger share of a 1048576 token
        window helps a repair more than it dilutes it is not something the
        harness can answer offline; it needs a cloud run against the same task.

        What is measured, on the four large tasks of 2026-09-16, each about
        halfway through its nodes:

            task           nodes    files  source   quoted  omitted  seen
            stackoverflow  32/66    14     152050   4       10       63%
            prestashop     44/86    15     115636   7        8       79%
            12306          48/117   10     110394   5        5       86%
            ctrip          58/125    7     109559   3        4       86%

        Every repair prompt on those tasks already hides four to ten files, and
        the sources roughly double again by the last node. The budget they are
        competing for is about 22000 tokens of a 1048576 token window.

        An implement turn is given no quoted source, and it is not left blind
        either: it has the tool tier, so it lists the tree itself (`list_dir` /
        `glob`) and reads what it needs -- `NODE_PROMPT` tells it to read
        `backend/server.js` and the page it extends. That is deliberate, and the
        tool events match it exactly, so do not "fix" implement turns by quoting
        sources into them.
        """
        return int(os.environ.get("OCTOS_ARC_INLINE_SOURCE_CHARS", "90000"))

    def sources_text(self) -> str:
        # A repair has to understand the code before editing it, so it cannot be
        # quoted less than the turn that wrote the code was.
        limit = self.inline_source_chars()
        return inline_sources(self.output_dir, limit) + "\n" if limit > 0 else ""

    def corrections_text(self) -> str:
        if not self.pending_corrections:
            return ""
        text = "Corrections from the harness:\n" + "\n".join(f"- {c}" for c in self.pending_corrections) + "\n"
        self.pending_corrections = []
        return text

    def turn(self, prompt: str, timeout: int, label: str, expect_verification: bool = True,
             request_budget: int | None = None, record_duration: bool = True,
             read_only: bool = False) -> tuple[bool, str]:
        proxy = getattr(self, "llm_proxy", None)
        # `verify_text` has the proxy drop the shell tools in minimal mode. A turn
        # that cannot run a command must not then be told off for not running one.
        no_shell = read_only or bool(getattr(proxy, "extra_drop_tools", None))
        monitor = TurnMonitor(self.protected_prefixes(),
                              expect_verification=expect_verification and not no_shell,
                              allowed_prefixes=[".arc/design/", str(self.output_dir / ".arc" / "design")])
        if proxy is not None:
            # Per-turn reasoning: OCTOS_ARC_IMPLEMENT_REASONING (e.g. "none") applies
            # to first implement turns of small tasks; rewrite/repair keep the base mode.
            base_mode = getattr(self, "base_reasoning_mode", proxy.mode)
            impl_mode = os.environ.get("OCTOS_ARC_IMPLEMENT_REASONING", "")  # auto already gives "none" to 1-node tasks
            is_implement = label.endswith(" implement") or label.startswith("skeleton")
            proxy.mode = impl_mode if (impl_mode and is_implement and self.minimal_mode(getattr(self, "n_nodes", 99))) else base_mode
            if request_budget is None:
                # A repair has to understand a failure before it can edit, so it
                # cannot need less room than the turn that wrote the code. Both
                # follow the same default; the per-turn timeout and the cost
                # guard still bound them. Cloud 746c81a2b5aa and 3ffe9702bf15 hit
                # the old fixed cap of 10 on 17% and 23% of their repair turns —
                # including the last repair of every node that stayed broken —
                # while spending 7% of the token budget and 7% of the time.
                default = "20" if self.minimal_mode(getattr(self, "n_nodes", 99)) else "0"
                request_budget = int(os.environ.get("OCTOS_ARC_REPAIR_REQUESTS", default)) if "repair" in label else \
                    int(os.environ.get("OCTOS_ARC_IMPLEMENT_REQUESTS", default))
            proxy.phase = phase_for_label(label)
            proxy.begin_turn(request_budget)
        # A model turn may change application files, even when it later fails.
        getattr(self, "probe_summaries", {}).clear()
        # 记进「动过手」的账：封箱时这棵树要么重新过秤，要么退回成绩卡上的
        # 已验证版本 —— 没量过的编辑不留到交付物里。
        self._tree_maybe_edited = True
        t0 = time.time()
        self.turn_count += 1
        # 只读回合（诊断）：把写类和 shell 工具从**请求里**拿掉 —— 提示词说「别改」
        # 是可被忽略的建议；工具面里没有它才是做不到。回合结束原样还回去
        # （`extra_drop_tools` 是流程级状态，`verify_text` 也在用它）。
        prev_drop = set(proxy.extra_drop_tools or ()) if (read_only and proxy is not None) else None
        if prev_drop is not None:
            proxy.extra_drop_tools = prev_drop | self.WRITE_TOOLS | self.SHELL_TOOLS
        try:
            ok, text = self.driver.run(prompt, max(60, int(timeout)), monitor)
            # 配额打爆（整段接口按小时不可用）不是这个回合的失败 —— 停手等，回合原样
            # 重跑，修复轮次一个不扣。见 `hold_for_quota`。
            held = 0.0
            if not ok and quota_exhausted(text):
                ok, text, held = self.hold_for_quota(prompt, max(60, int(timeout)), label, monitor, text)
        finally:
            if prev_drop is not None:
                proxy.extra_drop_tools = prev_drop
        elapsed = time.time() - t0
        log(f"[flow] {label} {'ok' if ok else 'FAILED'} in {elapsed:.0f}s"
            + (f" (held {held:.0f}s on platform quota)" if held else "")
            + f" (tools={monitor.tool_calls} wrote={monitor.wrote_files} verified={monitor.verified}): {text[-240:]!r}")
        # 记下修复轮真实花了多久。`repair_needs()` 用它判断「还够不够再来一轮」——
        # 那个判断原先用一个固定的 300 秒，实测一轮约 600 秒。
        # 只留最近 12 轮：模型和题目会变，很久以前的耗时不该继续影响现在的决定。
        #
        # `record_duration=False`：一个回合里的**第二次**调用（引文编辑重问）不是
        # 一个回合。记进去会把每轮的耗时中位数砍半，`repair_needs()` 于是低估，
        # 放行一轮注定被 deadline 砍断的修复 —— 正是那个函数的存在意义。
        if record_duration and ("repair" in label or "rewrite" in label):
            # 批修回合**单独记账**：一批只修 1~3 份卷子，耗时中位数远低于整段修复。
            # 混进 `repair_durations` 会把「一轮修复要多久」的观察值拉低，
            # `repair_needs()` 于是低估、放行注定被砍断的回合 —— 正是那个函数
            # 存在的意义被反过来用。批修的账只服务 `batch_repair_needs()`。
            bucket = "batch_repair_durations" if "batch repair" in label else "repair_durations"
            seen = getattr(self, bucket, None)
            if not isinstance(seen, list):
                seen = []
            # 等待配额的时间不算「修复耗时」—— `repair_needs()` 拿它估下一轮该开
            # 多大，把平台停摆算进去会把以后的修复闸门越修越短。
            setattr(self, bucket, (seen + [max(0.0, elapsed - held)])[-12:])
        if not ok and permanent_provider_error(text):
            raise PermanentProviderError(text[:1000])
        if proxy is not None and proxy.turn_budget and proxy.turn_requests > proxy.turn_budget:
            log(f"[guard] {label}: request budget {proxy.turn_budget} hit; turn forced to finish")
        for c in monitor.corrections():
            log(f"[guard] {label}: {c[:160]}")
            if self.guard_enabled:
                self.pending_corrections.append(c)
        restored = self.restore_protected()
        if restored:
            self.pending_corrections.append(
                "You changed official test/requirement files; the harness restored them: "
                + ", ".join(restored[:5]) + ". They are read-only ground truth — fix the app instead.")
        return ok, text

    def hold_for_quota(self, prompt: str, timeout: int, label: str,
                       monitor: TurnMonitor, text: str) -> tuple[bool, str, float]:
        """配额打爆时**停手等**，而不是把失败记到节点账上（2026-10-01 正赛复盘）。

        配额和普通限流的区别：普通限流几秒就过（那是 `OctosDriver._transient`
        的退避重试管的）；配额耗尽则是整段接口按小时不可用，退避重试没有意义，
        而每失败一次都在扣节点的修复轮次 —— github 的 REQ-1 分支就是这么被
        打死的。这里做的是熔断：不再开新回合，隔 `probe` 秒把同一个回合原样再
        跑一次当探活（接口死着时每次只烧几秒、0 个工具、0 轮次），接口一活这个
        回合就直接正常完成。

        三重上限防死等：单次等待、整跑累计等待、以及「必须给收尾留出
        `tail_seconds + node_timeout`」的底线。等的是不稀缺的墙钟，省的是稀缺
        的修复轮次；等不动了就照常失败，行为和从前一致。

        返回 `(ok, text, waited)`；`waited` 供调用方把等待时间从回合耗时里扣除。
        """
        if os.environ.get("OCTOS_ARC_QUOTA_HOLD", "1") == "0":
            return False, text, 0.0
        probe = max(5, int(os.environ.get("OCTOS_ARC_QUOTA_PROBE_SECONDS", "240")))
        per_wait = max(0, int(os.environ.get("OCTOS_ARC_QUOTA_WAIT_SECONDS", "5400")))
        total_cap = max(0, int(os.environ.get("OCTOS_ARC_QUOTA_TOTAL_WAIT_SECONDS", "7200")))
        reserve = float(getattr(self, "tail_seconds", 1800) + getattr(self, "node_timeout", 2400))
        waited = 0.0
        while True:
            used = getattr(self, "quota_waited_total", 0.0)
            allowed = min(per_wait - waited, total_cap - used, self.remaining() - reserve)
            if allowed <= 0 or self.wound_down():
                log(f"[quota] not holding '{label}': waited {waited:.0f}s this turn, "
                    f"{used:.0f}s of the run's hold budget used, {self.remaining():.0f}s of run left: "
                    f"{text[-160:]!r}")
                break
            nap = min(probe, allowed)
            log(f"[quota] platform quota exhausted during '{label}'; holding {nap:.0f}s and "
                f"re-running the same turn ({self.remaining():.0f}s left; "
                f"{total_cap - used:.0f}s of hold budget left): {text[-160:]!r}")
            sleep_logging(nap)
            waited += nap
            self.quota_waited_total = used + nap
            ok, text = self.driver.run(prompt, timeout, monitor)
            if ok or not quota_exhausted(text):
                log(f"[quota] hold over after {waited:.0f}s: "
                    + ("recovered; the turn completed" if ok else "the error is no longer quota-class"))
                return ok, text, waited
        return False, text, waited

    def perf_text(self) -> str:
        return PERFORMANCE_CONTRACT if self.perf_contract and self.needs_session else ""

    def classify_tree(self, tree: dict) -> None:
        """Keyword-gate the optional contract blocks so a counter never reads
        session/performance rules; derive behavior from the supplied requirements."""
        text = json.dumps(tree, ensure_ascii=False).lower()
        self.needs_session = bool(re.search(r"login|log in|sign in|password|session|register|注册|登录|密码|会话", text))
        self.needs_data = bool(re.search(r"seed|published|fixture|option|select|dropdown|nationalit|车次|train|选项|下拉|预置", text))

    def ui_contract(self) -> str:
        blocks = [UI_CONTRACT_CORE]
        if getattr(self, "needs_data", True):
            blocks.append(UI_CONTRACT_DATA)
        if getattr(self, "needs_session", True):
            blocks.append(UI_CONTRACT_SESSION)
        return "".join(blocks)

    # 工具名有**两个命名面**，收窄工具面时都得列（见 kernel/bridge/toolMap.ts）：
    # 事件与守卫那面是 octos 名（`read_file`/`shell`，`guard.py` 的集合就是它），
    # 而**请求体里出现的是 CCB 名**（`Read`/`Write`/`Edit`/`Bash` = EXPOSED_ORDER，
    # 桥用 `toOctosName()` 把模型调的名字翻成 octos 名才发给事件流）。
    # drop 名单是「名字不在里面就删」—— 多列一个零成本，漏一个就是「以为收窄了、
    # 其实一个都没删」的静默失明。实测：`SHELL_TOOLS` 原先只有 octos 名，
    # 精简化回合的「拿走 shell」一直是空转（`Bash` 一直还看得见）。
    SHELL_TOOLS = {"bash", "shell", "exec_command", "Bash"}
    # 写类的两个面同样都列：CCB 名（Write/Edit/MultiEdit/NotebookEdit）+ 守卫
    # 认识的 octos 名（`guard.py:_WRITE_TOOLS` / v3 `WRITE_TOOL_FALLBACK`）。
    WRITE_TOOLS = {"write_file", "edit_file", "diff_edit", "apply_patch", "create_file",
                   "append_file", "Write", "Edit", "MultiEdit", "NotebookEdit"}

    def minimal_mode(self, total_nodes: int) -> bool:
        mode = os.environ.get("OCTOS_VERIFY_MODE", "auto")
        return mode == "minimal" or (mode != "full" and total_nodes <= self.small_task_nodes)

    def verify_text(self, total_nodes: int) -> str:
        minimal = self.minimal_mode(total_nodes)
        # Prompt budgets alone are ignored often enough (v9-tb-a: 41 tool calls
        # incl. servers in a "no shell" repair turn); in minimal mode the proxy
        # removes the shell tools so commands are impossible, the harness builds.
        proxy = getattr(self, "llm_proxy", None)
        if proxy is not None and os.environ.get("OCTOS_ARC_DROP_SHELL", "1") != "0":
            proxy.extra_drop_tools = set(self.SHELL_TOOLS) if minimal else set()
        return VERIFY_MINIMAL if minimal else VERIFY_FULL.format(smoke=self.smoke_port)

    def tool_tier_available(self) -> bool:
        """工具档还在不在。

        原版的 59MB 内核自带读写工具；精简版用 urllib 顶掉了内核，没有工具就没有这一档。
        凡是要模型自己去翻文件、自己改的地方都得先问这一句，否则它会被推上一条不存在的路
        —— 提示词里写着工具，模型手里却一个都没有。
        """
        # getattr：测试会绕过 __init__ 造 Flow，这个判断不能是那个把它们弄崩的东西。
        return not isinstance(getattr(self, "driver", None), LocalDriver)

    def maybe_probe(self, node_ids: list[str]) -> None:
        """Endpoint probe policy: none in dry runs; otherwise the token-free
        GET /models probe with a minimal fallback."""
        if os.environ.get("OCTOS_ARC_DRYRUN") == "1":
            log("[probe] skipped (OCTOS_ARC_DRYRUN=1)")
        else:
            probe_endpoint()


    def repair_requirements(self, node_id: str | None = None) -> str:
        nodes = getattr(self, "requirement_nodes", {})
        selected = [nodes[node_id]] if node_id in nodes else ([] if node_id is not None else nodes.values())
        text = "\n\n".join(describe_node(node) for node in selected)
        if not text:
            return ""
        return ("Original requirements (read-only; preserve details even when acceptance does not assert them):\n"
                + text + "\n")

    def repair_test_location(self, specs: list[str] | None = None) -> str:
        if not self.tests_dir:
            return ""
        context = (f"Application directory: {self.output_dir.resolve()}. "
                   f"Read-only acceptance directory: {self.tests_dir.resolve()}. "
                   "Relative spec paths in failure reports refer to this directory. "
                   "Read relevant specs and helpers here when needed.\n")
        runner = getattr(self, "runner", None)
        if runner:
            helper = BUNDLE_DIR / "verify_app.py"
            binary = runner.root / "node_modules" / ".bin" / "playwright"
            if helper.is_file() and binary.is_file():
                args = ["env"]
                browsers = getattr(runner, "env_extra", {}).get("PLAYWRIGHT_BROWSERS_PATH")
                if browsers is not None:
                    args.append(f"PLAYWRIGHT_BROWSERS_PATH={browsers}")
                args.extend([sys.executable, str(helper), "--app", str(self.output_dir.resolve()),
                             "--tests", str(self.tests_dir.resolve()), "--playwright", str(runner.root)])
                workers = runner.workers if specs else final_workers(getattr(self, "mem_limit", None))
                args.extend(["--workers", str(workers)])
                for spec in specs or []:
                    args.extend(["--spec", spec])
                context += ("Isolated acceptance entry (builds and starts a disposable application copy):\n"
                            f"```sh\n{shlex.join(args)}\n```\n"
                            "Use this command for acceptance checks so test writes do not alter the source application's data. "
                            "Edit the source application, not the disposable copy or read-only tests. "
                            "The command prints failures and a report path. The harness re-runs acceptance after your edits.\n")
        return context

    def tests_prompt_for(self, node_id: str | None, skeleton: bool = False) -> str:
        if not self.tests_dir:
            return ""
        if skeleton:
            support = sorted(str(p.relative_to(self.tests_dir)) for p in self.tests_dir.rglob("*.ts")
                             if not p.name.endswith(".spec.ts"))
            n_specs = len(list(self.tests_dir.rglob("*.spec.ts")))
            return (f"The acceptance specs ({n_specs} files) live under {self.tests_dir}; each later turn "
                    f"receives the spec files for its own node. In THIS turn read only the shared helpers "
                    f"({', '.join(support[:10]) or 'none'}) and at most two spec files to learn the base URL, "
                    f"navigation and header conventions; do not implement the features yet.\n"
                    + acceptance_tests_prompt(self.tests_dir, self.web_port, self.smoke_port, []).split("\n", 1)[-1])
        files = list(self.spec_map.get(node_id) or [])
        support = sorted(str(p.relative_to(self.tests_dir)) for p in self.tests_dir.rglob("*.ts")
                         if not p.name.endswith(".spec.ts"))
        if not files:
            # **没有挂到自己名下的 spec ≠ 全部 spec 都是你的评分标准。**
            #
            # 这里原先是「把它当成没有 spec 的节点，那就把全部 spec 列给它看」。在
            # 官方卷子那条路上那是好意（总比什么都不给强），在自产卷子这条路上它是
            # 一句**不成立的话**：后台对它的断言是 0 条（`_accept` 里 `own_ok = not specs`），
            # 却告诉它「这批卷子就是评判你的依据」。模型于是去满足一堆跟它无关的断言，
            # 花掉的是这个节点的回合预算。
            return (f"There is no acceptance spec for this node ({node_id}) in this run — nothing "
                    f"here is a machine check on your work. Build exactly what the requirement text "
                    f"above asks for, and make it consistent with the architecture contract. "
                    f"Other nodes' specs exist under {self.tests_dir}; read one only if you need the "
                    f"conventions (base URL, navigation, headers), never as a checklist for this node.\n")
        return acceptance_tests_prompt(self.tests_dir, self.web_port, self.smoke_port, files + support,
                                       inline=os.environ.get("OCTOS_ARC_INLINE_SPECS", "1") != "0")


    # -- git --------------------------------------------------------------
    def head(self) -> str | None:
        return self.runtime.git.current_head()

    def commit(self, message: str) -> bool:
        try:
            return self.runtime.git.commit(message)
        except Exception as exc:  # noqa: BLE001
            log(f"[git] commit failed: {exc}")
            return False

    def last_repair_diff(self, max_chars: int = 900) -> str:
        """What the previous repair commit actually changed.

        The harness commits after every repair, so when a round reproduces the
        round before it, git can say whether anything moved and where. Cloud
        e767e871a6c6 ran four rounds over eleven failures that never budged, and
        the in-repo record of 91aaecaf31af and 5747e6bcf530 is that repeated
        repairs re-emit the same files. "You changed these and nothing moved" is
        a different instruction from "it failed again".
        """
        git = getattr(getattr(self, "runtime", None), "git", None)
        if git is None:
            return ""  # a diagnostic must never be what breaks the repair loop
        try:
            result = git.run(["diff", "HEAD~1", "HEAD", "--stat", "--", "frontend", "backend"], check=False)
        except Exception as exc:  # noqa: BLE001
            log(f"[git] could not read the last repair: {exc}")
            return ""
        stat = (getattr(result, "stdout", "") or "").strip()
        if not stat:
            return ("\n\nThe previous repair left frontend/ and backend/ unchanged, so this result is the "
                    "same code measured twice. Make an edit this time.")
        return ("\n\nThe previous repair changed this and the failures did not move:\n"
                + clip_ends(stat, max_chars))

    def unfinished_repair_note(self, text: str, max_chars: int = 700) -> str:
        """Hand the previous repair turn's closing words to the next round.

        A repair turn can end before it edits anything: reading one large source
        file eats the turn, or the per-turn timeout lands mid-plan. Cloud
        e767e871a6c6 lost all three full-suite rounds that way and finished at
        20/32 -- "No files were modified ... Next step is to apply those edits to
        frontend/src/index.html", then "Need one more turn to read the note-action
        click handlers". That run was capped at ten requests per repair, a cap
        since lifted, but the waste it exposed is not about the cap: each round
        said exactly where it had got to, the message was dropped, and the next
        round paid to read the same file again. Hand it forward. The conclusion
        sits at the end of the message, so keep the tail.
        """
        text = (text or "").strip()
        if not text:
            return ""
        if len(text) > max_chars:
            text = "… " + text[-max_chars:].lstrip()
        return ("\n\nWhere the last repair attempt stopped, in its own closing words. It ran on a tool "
                "budget and may have run out before it could edit anything, so treat this as work already "
                "done: continue from it rather than reading the same files again.\n" + text)

    def restore_app(self, sha: str) -> None:
        git = self.runtime.git
        for part in ("frontend", "backend"):
            if (self.output_dir / part).exists():
                git.run(["checkout", sha, "--", part], check=False)
        git.run(["clean", "-fd", "-e", "node_modules", "-e", "dist", "--", "frontend", "backend"], check=False)
        log(f"[flow] restored frontend/ and backend/ to best commit {sha[:8]}")

    # -- 交付保底：快照 / 成绩卡 / 封箱 -----------------------------------

    def snapshot_state(self, note: str = "snapshot") -> str | None:
        """把工作区此刻的状态钉成一个提交对象（含未提交改动与新建文件），不动 HEAD。

        用途是「把这一刀整个退回」：修复回合被切断时盘上可能留着改了一半的
        文件，没有快照就只能让它留在那儿污染下一刀。做法是全量入索引后
        `write-tree` + `commit-tree`；提交对象挂在 `refs/arc/last-snapshot`
        下面防回收。**任何一步失败都返回 None** —— 调用方要的是「能退就退」，
        退不了就保持旧行为，辅助动作绝不变成新的故障点。
        """
        git = getattr(getattr(self, "runtime", None), "git", None)
        if git is None:
            return None
        try:
            git.run(["add", "-A"], check=False)
            tree = git.run(["write-tree"], check=False)
            tree_sha = (getattr(tree, "stdout", "") or "").strip()
            if getattr(tree, "returncode", 1) != 0 or not tree_sha:
                return None
            head = self.head() or ""
            args = ["commit-tree", tree_sha, "-m", f"arc snapshot: {note[:80]}"]
            if head:
                args += ["-p", head]
            made = git.run(args, check=False)
            sha = (getattr(made, "stdout", "") or "").strip()
            if getattr(made, "returncode", 1) != 0 or not sha:
                return None
            git.run(["update-ref", "refs/arc/last-snapshot", sha], check=False)
            return sha
        except Exception as exc:  # noqa: BLE001
            log(f"[snapshot] could not snapshot the workspace: {exc}")
            return None

    def snapshot_is_current(self, sha: str) -> bool:
        """快照 `sha` 是不是**现在这个 HEAD** 钉的（父提交 == HEAD）。

        `snapshot_state` 建快照时把当时的 HEAD 记成父提交 —— 所以这个等式为真
        等价于「卡片之后没有再落过提交」。封箱在**当前树成绩未知**（量不动）时
        要问这一句：卡片是某一刻那棵树的像，卡片之后落的提交都不在卡里，退回
        就是拿更早的树覆盖之后的一切；好坏未知的那棵树与已量过的卡之间，只有
        「钉在现在这个 HEAD 上」（卡描述的就是盘上这棵树）才值得退。任何一步
        说不清（没有 sha、命令失败、没有父提交、不是快照）都返回 False ——
        退回是拿交付物在赌，说不清就不赌。
        """
        sha = (sha or "").strip()
        if not sha:
            return False
        git = getattr(getattr(self, "runtime", None), "git", None)
        if git is None:
            return False
        try:
            got = git.run(["rev-parse", f"{sha}^"], check=False)
            parent = (getattr(got, "stdout", "") or "").strip()
            return (getattr(got, "returncode", 1) == 0 and bool(parent)
                    and parent == (self.head() or ""))
        except Exception:  # noqa: BLE001
            return False

    def best_candidate(self) -> dict | None:
        return getattr(self, "_best_state", None)

    def record_best(self, summary, note: str) -> str | None:
        """把「刚刚量到的这一版」登记进全局成绩卡 —— 比卡片上的差就不登记。

        卡片是整个 run 的交付锚点，比 `final_acceptance` 里那个 pass 内的
        `best` 高一层：多次 final pass、被掐停的收尾、最后一次没量过的编辑，
        全都归它管。**登记时先快照**，卡片上的 sha 因此精确等于被测的那棵树
        （旧实现记的是 `head()`，attempt 0 时工作区里还有未提交改动，两者
        并不相等）。落盘 `.arc/best_state.json` 并钉在 `refs/arc/best` 上 ——
        进程被杀之后，恢复命令跟着卡片一起留在盘上。

        返回登记下来的 sha；这次测量没进卡（有更好的）时返回 None。
        """
        if summary is None or getattr(summary, "error", None) or not getattr(summary, "results", None):
            return None
        passed = int(getattr(summary, "passed", 0) or 0)
        total = int(getattr(summary, "total", 0) or 0)
        cur = getattr(self, "_best_state", None)
        if cur is not None and passed <= int(cur.get("passed", -1)):
            return None
        sha = self.snapshot_state(f"best {passed}/{total}") or self.head()
        state = {
            "passed": passed,
            "total": total,
            "sha": sha,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": note,
        }
        if sha:
            state["recover"] = (f"git checkout {sha} -- frontend backend && "
                                f"git clean -fd -e node_modules -e dist -- frontend backend")
        self._best_state = state
        git = getattr(getattr(self, "runtime", None), "git", None)
        if git is not None and sha:
            try:
                git.run(["update-ref", "refs/arc/best", sha], check=False)
            except Exception:  # noqa: BLE001
                pass
        try:
            path = self.output_dir / ".arc" / "best_state.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:  # noqa: BLE001
            log(f"[delivery] could not persist best_state.json: {exc}")
        log(f"[delivery] best verified state so far: {passed}/{total} @ {(sha or '?')[:8]} ({note})")
        return sha

    def seal_delivery(self, allow_measure: bool = True) -> None:
        """封箱：把交付物钉在「整个 run 量到过的最好版本」上。

        三闸里的最后一闸，和预算怎么结束无关 —— 收尾正常走完要过它，快要
        超支也要过它（退快照和提交只要几秒，允许几秒级的溢出）；进程真的
        被杀则由 `refs/arc/best` + `.arc/best_state.json` 里的恢复命令兜底。

        规则只有一条：
          · 自上次全卷测量后动过手、且还量得动 → 先量一次（量的是**当前**树）；
          · 当前树不是（或不配是）最好的 → 退回卡片上的最好版本；
          · 例外只有一条（2026-10-08）：当前树的成绩**未知**（量不动）而卡片钉
            的是**更早的 HEAD** —— 退回会把卡片之后落盘的每一刀提交静默丢掉
            （中途武装的卡可能差着几小时的工作），按现状交付，提交说明如实写明；
          · 最后提交一记 `final delivery`，交付状态在 git 历史里可见、可恢复。
        """
        if os.environ.get("OCTOS_SEAL_DELIVERY", "1") == "0":
            return
        dirty = bool(getattr(self, "_tree_maybe_edited", True))
        last = getattr(self, "_last_full_measure", None)
        measured_now = False
        if allow_measure and dirty and self.runner is not None and self.tests_dir:
            all_specs = sorted(str(p.relative_to(self.tests_dir)) for p in self.tests_dir.rglob("*.spec.ts"))
            need = float(os.environ.get("OCTOS_SEAL_MEASURE_MIN", "600"))
            if all_specs and self.remaining() >= need:
                workers = final_workers(getattr(self, "mem_limit", None))
                log(f"[delivery] sealing: measuring the current tree once ({self.remaining():.0f}s left)")
                summary = self.run_specs(all_specs, workers=workers, grader_like=True)
                if summary.error and summary.killed and workers > 1:
                    workers = max(1, workers // 2)
                    log(f"[delivery] sealing: runner was killed; retrying with {workers} worker(s)")
                    summary = self.run_specs(all_specs, workers=workers, grader_like=True)
                if not summary.error and summary.results:
                    measured_now = True
                    self._tree_maybe_edited = False
                    last = self._last_full_measure = {"passed": summary.passed, "total": summary.total}
                    grouped = nodes_for_failures(summary.results, self.spec_map)
                    self.record_full_suite(summary, grouped)
                    self.commit(f"chore: final measurement {summary.passed}/{summary.total}")
                    if self.record_best(summary, "seal measurement"):
                        self._best_summary, self._best_grouped = summary, grouped
                else:
                    why = str(summary.error or "no test results")
                    log(f"[delivery] sealing: the final measurement could not run ({why[:120]})")
        best = getattr(self, "_best_state", None)
        if best is None or not best.get("sha"):
            log("[delivery] no verified full-suite state on record; delivering the current tree as-is")
            self.commit("chore: final delivery (no verified state on record)")
            return
        tree_is_best = (not dirty or measured_now) and last is not None and last["passed"] >= best["passed"]
        if not tree_is_best:
            if (dirty and not measured_now
                    and not self.snapshot_is_current(str(best.get("sha") or ""))):
                log(f"[delivery] sealing: the score card {best['passed']}/{best['total']} @ "
                    f"{str(best['sha'])[:8]} was pinned before the current HEAD and this tree "
                    "could not be measured — not rolling back (that would silently drop every "
                    "commit landed since the card); delivering the current tree as-is")
                self.commit("chore: final delivery (card pinned before HEAD; current tree unmeasured)")
                return
            log(f"[delivery] sealing: restoring the best verified state {best['passed']}/{best['total']} "
                f"@ {str(best['sha'])[:8]}")
            self.restore_app(best["sha"])
            if getattr(self, "_best_summary", None) is not None:
                self.record_full_suite(self._best_summary, self._best_grouped)
        self.commit(f"chore: final delivery: best verified state {best['passed']}/{best['total']}")
        log(f"[delivery] sealed at {best['passed']}/{best['total']} @ {str(best['sha'])[:8]}; to recover "
            f"exactly this state: {best.get('recover', 'git checkout ' + str(best['sha']))}")

    # -- acceptance -------------------------------------------------------
    def author_acceptance_suite(self, tree: dict, node_ids: list[str]) -> None:
        """没有官方卷子的时候，自己出一份。

        正赛把测试藏起来了：`/workspace/tests` 不存在，bundle 里也没有
        public-tests，`tests_dir` 就是 None。那之后发生的事没有一行是显式的 ——
        `tests_prompt_for()` 返回空串、`setup_playwright()` 提前返回、
        `own_ok = not specs` 让**每个叶子自动通过**。24 个叶子 24 个实现回合
        0 个修复回合，一次过，没有任何返工的机会。这不是模型发挥失常，是验收
        机制整个没接上。

        这个方法把它接上：让模型按需求树写一份 Playwright 卷子，写完放进
        `output_dir/acceptance/`，然后**当成官方卷子用** —— `spec_map` 走
        `map_specs_to_nodes`，每节点实现完跑它自己那份，失败进修复回合，最后
        整套跑一遍（`final_acceptance`，全站体检）。下游一行都不用改。

        出卷先于施工，顺序是刻意的：卷子必须是一份**独立的判据**。谁实现谁出题，
        出的就会是他已经写出来的东西。

        出卷用的时间来自同一个总预算，所以它有上限（`_authoring_timeout`），并且
        在剩余时间跌破一半时立刻停手 —— 一份不完整的卷子仍然比没有强，但一个
        没时间施工的卷子没有意义。

        返回 `node_id -> [spec 相对路径]`，**按我们要求的文件名直接建表，从不反过来
        解析文件名**。两者的区别是这件事的要害，见 `authored_spec_map`。
        """
        if os.environ.get("OCTOS_ARC_AUTHOR_TESTS", "1") == "0":
            log("[author] OCTOS_ARC_AUTHOR_TESTS=0: skipping suite authoring"
                " (a suite already on disk will be adopted as-is)")
            return {}
        layers = layerize(tree)
        wanted = set(node_ids)
        leaves = [i for i in layers.leaves if i in wanted]
        if not leaves:
            log("[author] no atomic nodes to author specs for")
            return {}

        suite_dir = self.output_dir / AUTHOR_SUITE_DIR
        # -- v5：上一轮的旧卷子请出这个目录（不判分、不修复）------------------
        # 继承工作区里躺着上一轮跑出来的卷子（`acceptance/*.spec.ts`）。它们是
        # **上一轮需求的判据**：这一轮文本改了、节点没了，它们就永远红。留在盘上
        # 会被 `_all_specs()` 收进全卷体检，成为「修不动」的常驻噪音（正赛实测：
        # 一整轮里大量修复回合花在注定失败的旧账上）。移走不删，见
        # `quarantine_foreign_specs`。
        stale = quarantine_foreign_specs(
            suite_dir, set(leaves), self.output_dir / ".arc" / "inherited-specs")
        if stale:
            log(f"[author] set aside {len(stale)} spec file(s) from an earlier round "
                f"(they are not judged this round): {', '.join(stale[:6])}"
                + (" …" if len(stale) > 6 else ""))
        digest_dir = suite_dir / "_requirements"
        digest_dir.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        for leaf in leaves:
            rel = f"_requirements/{leaf}.md"
            try:
                (suite_dir / rel).write_text(acceptance_digest(layers, leaf), encoding="utf-8")
            except OSError as exc:  # noqa: BLE001
                log(f"[author] could not write digest for {leaf}: {exc}")
                continue
            files[leaf] = rel
        if not files:
            log("[author] no digests written; authoring skipped")
            return {}

        log(f"[author] {len(files)} atomic nodes, digests in {digest_dir}")
        authored: set[str] = set()
        groups = authoring_groups(layers, [i for i in leaves if i in files])
        for done, group in enumerate(groups):
            budget = self._authoring_timeout()
            if budget <= 0:
                left = sum(len(g) for g in groups[done:])
                log(f"[author] stopping with {left} node(s) un-authored: {self.remaining():.0f}s left")
                break
            # 摘要列**绝对路径**。原来是 `_requirements/…` 相对路径，而模型的 cwd
            # 是工作区——从它的位置那个路径根本不存在，每个出题组都要先撞 4-12 次
            # "File does not exist"、靠护栏逼着它换法子才找到（2026-10-01 正赛日志，
            # 每一组都撞）。`as_posix()` 在 Linux（平台）和 Windows（本地）上都是
            # 可读的绝对路径；钉住这段文案的测试断言的是 `_requirements/<leaf>.md`
            # 子串，仍然成立。
            prompt = ACCEPTANCE_AUTHOR_PROMPT.format(
                out_dir=suite_dir,
                index="\n".join(f"  {i}  {str(layers.by_id[i].node.get('name', ''))}"
                                for i in group),
                files="\n".join("  " + (suite_dir / files[i]).as_posix()
                                for i in group if i in files))
            label = f"author specs {group[0]}..{group[-1]} ({len(group)})"
            self.turn(prompt, budget, label, expect_verification=False)
            got = [i for i in group if (suite_dir / f"{i}.spec.ts").is_file()]
            authored.update(got)
            log(f"[author] {label}: {len(got)}/{len(group)} spec file(s) written")
        # ---- A 档机械体检 + B 档找茬回合 ----
        # 顺序是刻意的：先体检（缺考清单进找茬提示词），找茬补完再体检一遍；报告
        # 存进 `self.suite_audit`，收尾的完成信号会带上它 —— 「自家卷子全绿」不再
        # 是唯一的完工口径。体检绝不能把出卷拖崩：任何异常都只是少一份报告，
        # 卷子照常交下去。
        try:
            self.suite_audit = self.audit_authored_suite(
                layers, [i for i in leaves if i in files], suite_dir, files, groups)
        except Exception as exc:  # noqa: BLE001 — 审计是增值，不是前置条件
            self.suite_audit = None
            log(f"[audit] suite audit failed to run: {exc!r}; the suite itself is unaffected")
        # 卷子是 harness 盘点的，不是模型自报的：模型说写完了不算，文件在才算。
        # 盘点放在审计之后：找茬回合补出来/补完整的卷子同样算数。
        spec_map = authored_spec_map(suite_dir, node_ids, leaves)
        total = sum(len(v) for v in spec_map.values())
        covered = sum(1 for i in leaves if spec_map.get(i))
        log(f"[author] suite ready: {total} spec file(s) covering "
            f"{covered}/{len(leaves)} atomic node(s) in {suite_dir} "
            f"({len(authored)} from authoring turns, before the audit)")
        if not total:
            # **一份都没出成 → 交回空表，不是一个「十个键、全是空列表」的表。**
            # 后者是真话（确实一份都没挂上），但它有十个键，`if mapping:` 是**真** ——
            # 调用方只要用真值判断「出卷成没成」，就会读成「成了」。这一段的全部
            # 意义就是让这种退化状态**一眼可辨**，所以空表要和上面几条提前返回一致。
            return {}
        return spec_map

    def audit_authored_suite(self, layers, leaves: list[str], suite_dir: Path,
                             digest_rel: dict[str, str], groups: list[list[str]]) -> dict:
        """B 档找茬回合 + A 档机械体检 —— 卷子冻结之前的最后一道。

        为什么要有这一道：出卷人和答卷人是同一个模型，它的卷子会系统性地避开
        它自己实现里会打折扣的维度。2026-10-01 的正赛实测：169 个用例里只有
        1 个跨浏览器会话的检查（全是单会话内的断言），而同一个模型的实现恰好
        写了「每个会话一份平行世界」的存储 —— 单会话的卷子结构上测不出会话
        隔离。缺的不是认真，是**科目**；补科目靠出卷人自觉会失败，靠一个只对
        文字负责的独立审计回合才抓得住（见 `ACCEPTANCE_SUITE_AUDIT_PROMPT`）。

        流程（都发生在卷子进受保护名单之前，所以这里改得动它）：
          1. 机械体检一遍（`suite_audit`），缺考清单 + 结构性缺陷（`spec_structural_defects`：
             任何实现都过不了的断言）+ 字面量对账（外推名 / 没背书的角色 / 原文交代了
             进格子方式却直接对格子 fill，`literal_nodes`）
             当**必须逐条交代的硬任务**素材；
          2. 按出卷时的同一个分组，一组一个找茬回合 —— 审计只看需求原文和卷子，
             看不到实现，也看不到出卷人的推理；补的检查直接写回卷子文件，补不了的
             把带理由的豁免写进 `_audit/suite-audit-waivers.md`；
          3. 再体检一遍；还漏交代的条目追**一个小回合**（只追一次，不循环），然后
             报告落盘 `<suite>/_audit/suite-audit.md`，补上/豁免/漏交代都进完成信号。

        时间闸门与出卷共用（`_authoring_timeout`）：预算跌破一半就跳过剩余审计
        组 —— 卷子已经能用，施工的时间不能被审计吃光。`OCTOS_ARC_AUDIT_TESTS=0`
        关掉找茬回合本身；机械体检是纯文本分析（几毫秒），无论开关都会跑。

        返回最终体检报告（同时存在 `self.suite_audit` 上，完成信号用它）。
        """
        digests, specs = read_suite_material(suite_dir, leaves, digest_rel)
        before = suite_audit(leaves, digests, specs)
        log(f"[audit] mechanical pre-scan: {suite_audit_line(before)}")
        # v5 冲突扫描：两份卷子抢同一个固定资源（写死的同一个名字）。抢同一件东西
        # 的两个用例里，后跑的那个永远失败，而且双方都没写错 —— 正赛里「A 需求和
        # B 需求不可能共存」的机械影子。认的是写死的字面量，不是变量（边界见
        # `scan_suite_conflicts`）。
        try:
            conflicts = scan_suite_conflicts(suite_dir)
        except Exception as exc:  # noqa: BLE001 — 扫描是增值，不是前置条件
            conflicts = {}
            log(f"[audit] the shared-literal scan failed: {exc!r}")
        if conflicts:
            log(f"[audit] shared-literal scan: {len(conflicts)} literal(s) written by more than "
                f"one spec — {', '.join(sorted(conflicts)[:6])}"
                + (" …" if len(conflicts) > 6 else ""))
            try:
                audit_dir = suite_dir / SUITE_AUDIT_DIR
                audit_dir.mkdir(parents=True, exist_ok=True)
                lines = ["# 卷子冲突扫描（同一个固定字面量被两份以上的卷子写进去）", "",
                         "抢同一件东西的用例里，后跑的那个永远失败。每条都该改卷：",
                         "让每份卷子用只有它自己用的名字自建对象，或把共用对象重置回原样。", ""]
                for val, where in sorted(conflicts.items()):
                    lines.append(f"- `{val}` ← {', '.join(sorted(where)[:8])}")
                (audit_dir / "suite-conflicts.md").write_text("\n".join(lines) + "\n",
                                                              encoding="utf-8")
            except OSError as exc:  # noqa: BLE001
                log(f"[audit] could not write the conflict report: {exc}")
        else:
            log("[audit] shared-literal scan: no fixed literal is written by two different specs")
        # v5.1 公共对象点名：判分环境本来就有的东西（种子工作簿、预置账号、已经被占用的
        # 名字）。清单从需求原文的「种子/预置」句子 + 盘上种子代码点名 —— 不写死；动过
        # 公共对象、又没有可见复原动作的卷子点出来，进报告、进找茬提示词、进完成信号。
        try:
            shared = scan_shared_objects(suite_dir, getattr(self, "req_dir", None),
                                         self.output_dir)
        except Exception as exc:  # noqa: BLE001 — 点名是增值，不是前置条件
            shared = {"objects": {}, "flags": [], "app_only": []}
            log(f"[audit] the shared-object roll-call failed: {exc!r}")
        if shared["objects"]:
            names = ", ".join(sorted(shared["objects"])[:6])
            log(f"[audit] shared-object roll-call: {len(shared['objects'])} pre-provisioned "
                f"object(s) named by the text — {names}"
                + (" …" if len(shared["objects"]) > 6 else ""))
        if shared["flags"]:
            log(f"[audit] **WARNING**: {len(shared['flags'])} spec(s) touch a shared object and "
                f"write fixed values with no visible restore step — "
                + ", ".join(f["spec"] for f in shared["flags"][:6]))
        try:
            audit_dir = suite_dir / SUITE_AUDIT_DIR
            audit_dir.mkdir(parents=True, exist_ok=True)
            (audit_dir / SUITE_SHARED_FILE).write_text(render_shared_objects(shared),
                                                       encoding="utf-8")
        except OSError as exc:  # noqa: BLE001
            log(f"[audit] could not write the shared-object report: {exc}")
        if os.environ.get("OCTOS_ARC_AUDIT_TESTS", "1") == "0":
            log("[audit] OCTOS_ARC_AUDIT_TESTS=0: audit turns skipped; the mechanical scan still ran")
        else:
            for done, group in enumerate(groups):
                budget = self._authoring_timeout()
                if budget <= 0:
                    left = sum(len(g) for g in groups[done:])
                    log(f"[audit] stopping with {left} node(s) un-audited: {self.remaining():.0f}s left")
                    break
                index = "\n".join(f"  {i}  {str(layers.by_id[i].node.get('name', ''))}"
                                  for i in group)
                files = "\n".join(
                    f"  {i}\n    digest: {suite_dir / digest_rel[i]}\n    spec: "
                    + (f"{suite_dir / (i + '.spec.ts')}"
                       if (suite_dir / f"{i}.spec.ts").is_file()
                       else "(none yet — write one for this node)")
                    for i in group if i in digest_rel)
                prompt = ACCEPTANCE_SUITE_AUDIT_PROMPT.format(
                    index=index, files=files,
                    mechanical=suite_audit_hints(before, group)
                    + conflict_hints(conflicts, group)
                    + shared_object_hints(shared, group)
                    + suite_gap_disposition_note(before, group, suite_dir))
                label = f"audit specs {group[0]}..{group[-1]} ({len(group)})"
                self.turn(prompt, budget, label, expect_verification=False)
        # 找茬回合会改卷子（起点一致性、复原动作都靠这时候补上）—— 点名报告和完成
        # 信号必须是**改完之后**的盘面（和下面 `suite_audit` 的重扫同一时刻）；
        # 上面那份是回合开始前的快照，只用于本批提示词。追账回合也改卷子，所以
        # 重扫是一个可重复的动作。
        def _rescan_shared() -> None:
            nonlocal shared
            try:
                shared = scan_shared_objects(suite_dir, getattr(self, "req_dir", None),
                                             self.output_dir)
                (suite_dir / SUITE_AUDIT_DIR / SUITE_SHARED_FILE).write_text(
                    render_shared_objects(shared), encoding="utf-8")
                if shared["flags"]:
                    log(f"[audit] after the audit turns, {len(shared['flags'])} spec(s) still change "
                        f"a shared object with no visible restore step: "
                        + ", ".join(f["spec"] for f in shared["flags"][:6]))
            except Exception as exc:  # noqa: BLE001 — 重扫失败就留着第一版报告
                log(f"[audit] the shared-object re-scan failed: {exc!r}")

        if os.environ.get("OCTOS_ARC_AUDIT_TESTS", "1") != "0":
            _rescan_shared()
        digests, specs = read_suite_material(suite_dir, leaves, digest_rel)
        final = suite_audit(leaves, digests, specs)
        final["shared_flags"] = shared["flags"]  # 完成信号要用（`suite_audit_completion_note`）
        # 缺考处置（硬任务，2026-10-07，见文件头那块注释）：清单上的每条必须交代 ——
        # 补上检查，或把带理由的豁免写进 waivers 文件。`before → final` 的盘面差是
        # 「补了没有」的证据；文件里有没有这个 ID，是「豁免了没有」的证据。审计关掉
        # 时整段不启用，回到「只报不追」的老行为。
        disp: dict = {"filled": [], "waived": {}, "undisposed": []}
        if os.environ.get("OCTOS_ARC_AUDIT_TESTS", "1") != "0":
            disp = gap_disposition(before, final, read_gap_waivers(suite_dir))
            # 挂账条件含结构性缺陷与字面量欠账：只出这两类、没有缺考的卷面同样要有
            # 下文（#1 扫描器进站 / #2 字面量对账）。
            if (before["gap_nodes"] or before["defect_nodes"] or before["literal_nodes"]
                    or (before["journey"] or {}).get("gap")
                    or final["gap_nodes"] or final["defect_nodes"] or final["literal_nodes"]
                    or (final["journey"] or {}).get("gap")):
                final["gap_disposition"] = disp
            if disp["undisposed"]:
                budget = self._authoring_timeout()
                if budget <= 0:
                    log(f"[audit] {len(disp['undisposed'])} flagged item(s) still without a "
                        f"disposition, and no time left to chase them: {self.remaining():.0f}s left")
                else:
                    n = len(disp["undisposed"])
                    prompt = ACCEPTANCE_DISPOSITION_PROMPT.format(
                        waivers=suite_dir / SUITE_AUDIT_DIR / SUITE_WAIVER_FILE,
                        items=disposition_items(final, disp["undisposed"], suite_dir))
                    log(f"[audit] disposition round: {n} flagged item(s) got neither a fix nor a "
                        f"written reason — asking once, item by item")
                    # 只追一次，不循环：重扫之后剩下的直接进报告和完成信号。
                    self.turn(prompt, budget, f"disposition round ({n} item(s))",
                              expect_verification=False)
                    _rescan_shared()
                    digests, specs = read_suite_material(suite_dir, leaves, digest_rel)
                    final = suite_audit(leaves, digests, specs)
                    final["shared_flags"] = shared["flags"]
                    disp = gap_disposition(before, final, read_gap_waivers(suite_dir))
                    final["gap_disposition"] = disp
        try:
            audit_dir = suite_dir / SUITE_AUDIT_DIR
            audit_dir.mkdir(parents=True, exist_ok=True)
            (audit_dir / SUITE_AUDIT_FILE).write_text(render_suite_audit(final), encoding="utf-8")
        except OSError as exc:  # noqa: BLE001 — 报告写不了不能打断出卷
            log(f"[audit] could not write the audit report: {exc}")
        log(f"[audit] mechanical re-scan: {suite_audit_line(final)}")
        if disp["filled"]:
            shown = ", ".join(disp["filled"][:8]) + (" …" if len(disp["filled"]) > 8 else "")
            log(f"[audit] flagged items fixed during the audit: {len(disp['filled'])} ({shown})")
        if disp["waived"]:
            log(f"[audit] flagged items waived in writing: {len(disp['waived'])} "
                f"({', '.join(list(disp['waived'])[:8])})")
        if disp["undisposed"]:
            shown = ", ".join(disp["undisposed"][:8]) + (" …" if len(disp["undisposed"]) > 8 else "")
            log(f"[audit] **WARNING**: {len(disp['undisposed'])} flagged item(s) got neither "
                f"a fix nor a written reason: {shown}. The suite is about to be frozen, and this "
                f"note rides the completion signal.")
        elif os.environ.get("OCTOS_ARC_AUDIT_TESTS", "1") == "0" and (
                final["gap_nodes"] or final["defect_nodes"] or final["literal_nodes"]):
            bits: list[str] = []
            if final["gap_nodes"]:
                shown = ", ".join(final["gap_nodes"][:8]) + (
                    " …" if len(final["gap_nodes"]) > 8 else "")
                bits.append(f"{len(final['gap_nodes'])} node(s) whose requirement text implies a "
                            f"check the suite does not make: {shown}")
            if final["defect_nodes"]:
                shown = ", ".join(final["defect_nodes"][:8]) + (
                    " …" if len(final["defect_nodes"]) > 8 else "")
                bits.append(f"{len(final['defect_nodes'])} spec(s) with an assertion no "
                            f"implementation can pass: {shown}")
            if final["literal_nodes"]:
                shown = ", ".join(final["literal_nodes"][:8]) + (
                    " …" if len(final["literal_nodes"]) > 8 else "")
                bits.append(f"{len(final['literal_nodes'])} spec(s) using a name, role or "
                            f"interaction the requirement text does not back: {shown}")
            log("[audit] **WARNING**: " + "; ".join(bits) + ". Reported, not enforced — the "
                "suite is about to be frozen, and this note rides the completion signal.")
        return final

    def smoke_check_authored_suite(self, specs: list[str]) -> None:
        """自产卷子的第二道体检：**它们能跑吗** —— 只收集，不执行。

        为什么官方卷子不需要这一道而自产卷子需要：官方卷子的内容是既定的、跑了
        十几轮没变过；自产卷子是刚写出来的 TypeScript，**没有任何人编译过它**。
        语法错、import 错、用了不存在的 API —— 这些会让整个文件一条用例都收集
        不到，而收集失败在 `_accept` 里的下场是 `summary.error` → 「spec run
        failed」：不修复、不回滚、节点直接判负。同一个错误还会在**每个**节点上
        重演，因为每个节点都要跑自己那几份卷子。

        这一趟不开浏览器，几秒钟。发现坏文件就点名报出来，让日志里出现的是
        「哪几份卷子是坏的」，而不是二十几个节点各自失败一次、看起来像模型不行。

        它只报告，**不改判据**：坏卷子仍然会让节点失败（那是诚实的），但从这一刻
        起至少有人知道原因。
        """
        if self.runner is None or not specs:
            if self.runner is None:
                log("[author] no Playwright available; the authored suite cannot be collected")
            return
        try:
            total, error = self.runner.collect(specs)
        except Exception as exc:  # noqa: BLE001 — 体检本身绝不能打断一轮
            log(f"[author] suite collection check failed to run: {exc}")
            return
        if error:
            log("[author] **WARNING**: the authored suite does not collect — every node whose specs "
                "are in this set will fail with 'spec run failed', with no repair round. "
                f"Detail: {error[:800]}")
        elif total == 0:
            log(f"[author] **WARNING**: the authored suite collected 0 tests across "
                f"{len(specs)} file(s) — nothing in it would ever be asserted")
        else:
            log(f"[author] authored suite collects cleanly: {total} test(s) in {len(specs)} file(s)")

    def _authoring_timeout(self) -> float:
        """一个出卷回合能有多长。

        上限是单回合闸门（`node_timeout`，超时不重放）；下限是 240s，比这个还短
        的回合写不完一组 spec，开它只是浪费一次调用。中间按剩余时间的比例走 ——
        出卷发生在全部施工之前，`remaining()` 此刻接近满额，所以真正起作用的是
        闸门本身。

        `remaining()` 少于总预算一半时返回 0：那时候该去施工了。卷子不完整仍然
        能用（没卷子的节点退回今天的行为：自动通过），没时间施工才是什么都没有。
        """
        if self.remaining() < self.budget * 0.5:
            return 0.0
        return max(240.0, min(float(self.node_timeout), self.remaining() * 0.2))

    def setup_playwright(self) -> None:
        """Prefer the Playwright already on the machine (the runner image ships
        one). A private install is the last resort and never touches shared
        state: own npm cache, own browser dir, pinned version, removed at exit.
        Run da9a64b32c09: an unisolated install made the platform's own
        `npx playwright test` resolve a different version whose chromium build
        was missing, and every graded test failed."""
        if not self.tests_dir:
            return
        env_extra: dict = {}
        root = find_playwright_root(playwright_candidates(BUNDLE_DIR, self.tests_dir, self.output_dir))
        if root is None:
            root = find_playwright_by_search(log)
        if root is None and os.environ.get("OCTOS_ARC_INSTALL_PLAYWRIGHT", "1") != "0":
            version = playwright_version_hint(self.tests_dir)
            log(f"[acceptance] no preinstalled Playwright found; private install of @playwright/test@{version}")
            self.private_playwright = _temp_dir("octos-arc-playwright-")
            installed = ensure_playwright(self.private_playwright, log, version=version)
            if installed:
                root, env_extra = installed
        if root is None:
            log("[acceptance] Playwright unavailable; nodes will be judged by the final check only")
            return
        limit = container_memory_limit()
        self.mem_limit = limit
        workers = workers_for_memory(limit, int(os.environ.get("OCTOS_ARC_TEST_WORKERS", "2")))
        self.runner = AcceptanceRunner(root, self.tests_dir, acceptance_work_dir(root), log,
                                       timeout_ms=int(os.environ.get("OCTOS_ARC_TEST_TIMEOUT_MS", "10000")),
                                       workers=workers, env_extra=env_extra)
        log(f"[acceptance] using Playwright at {root}; workers={workers}"
            + (f" (container memory limit {limit // (1024 * 1024)} MiB)" if limit else ""))

    def snapshot_protected(self) -> None:
        """Copy the official tests dir (and requirements) so any edit the model
        sneaks past the hook (e.g. via a shell redirect) is undone after the
        turn — the platform grades with THESE files."""
        self.protected_snapshots = []
        for live in (self.tests_dir, self.req_dir):
            if not live or not live.is_dir():
                continue
            snap = _temp_dir("octos-protected-")
            shutil.copytree(live, snap / "tree", ignore=shutil.ignore_patterns("node_modules"))
            self.protected_snapshots.append((live, snap / "tree", tree_digest(live)))

    def restore_protected(self) -> list[str]:
        fixed_all: list[str] = []
        for live, snap, digest in getattr(self, "protected_snapshots", []):
            try:
                fixed = restore_tree(live, snap, digest)
            except OSError as exc:
                log(f"[guard] could not restore {live}: {exc}")
                continue
            if fixed:
                log(f"[guard] restored {len(fixed)} protected file(s) under {live}: {fixed[:5]}")
                fixed_all.extend(f"{live}/{rel}" for rel in fixed)
        return fixed_all

    def start_llm_proxy(self) -> None:
        """Front the model endpoint with llm_proxy so DeepSeek reasoning is
        capped (`OCTOS_ARC_REASONING`: low (default) | medium | high | none |
        passthrough) and exact per-request usage lands in .arc/llm-usage.jsonl."""
        mode = os.environ.get("OCTOS_ARC_REASONING", "auto")
        if mode == "auto":
            # Thinking off is safe for one-node builds and one-node evolutions
            # (v10: Counter/Dice/Evolution all pass, completion 0.5-1.9k tokens)
            # but TB repairs without thinking looped 22 calls with no write.
            #
            # 判据必须是「任务有多少需求节点」，不是「有多少待实现节点」。
            # evolution 运行里所有节点都已存在 → nodes_to_implement 恒为 0 →
            # 被判成 none → **每一个修复回合都被关掉了思维链**，而那正是最需要
            # 思考的一类回合。作者自己的注释就写着 TB repairs without thinking
            # looped 22 calls with no write —— 他知道，只是判据变量用错了。
            # n_nodes 在下方 3696 行赋值，本函数在 3767 行才被调用，读得到。
            mode = "none" if getattr(self, "n_nodes", 2) <= 1 else "low"
        upstream = os.environ.get("OPENAI_BASE_URL", "")
        routes_configured = bool(json.loads(configured_model_routes() or "[]"))
        if mode == "passthrough" and not routes_configured:
            return
        if not upstream.startswith("http"):
            if routes_configured:
                raise ValueError("model routing requires an HTTP provider endpoint")
            return
        try:
            dump = (self.output_dir / ".arc" / "llm-requests") if os.environ.get("OCTOS_ARC_PROXY_DUMP") == "1" else None
            self.llm_proxy = LlmProxy(upstream, mode, self.output_dir / ".arc" / "llm-usage.jsonl", dump_dir=dump,
                                      dump_limit=int(os.environ.get("OCTOS_ARC_PROXY_DUMP_LIMIT", "3")),
                                      destream=os.environ.get("OCTOS_ARC_DESTREAM", "1") != "0",
                                      trim=os.environ.get("OCTOS_ARC_TRIM_PROMPT", "1") != "0",
                                      min_max_tokens=int(os.environ.get("OCTOS_ARC_MAX_TOKENS", "32768"))).start()
        except OSError as exc:
            if routes_configured:
                raise
            log(f"[proxy] could not start local LLM proxy ({exc}); using the endpoint directly")
            return
        self.base_reasoning_mode = mode
        os.environ["OPENAI_BASE_URL"] = self.llm_proxy.base_url
        log(f"[proxy] LLM requests via {self.llm_proxy.base_url} -> {upstream} (reasoning={mode}, "
            f"destream={'on' if self.llm_proxy.destream else 'off'}, trim={'on' if self.llm_proxy.trim else 'off'})")

    def stop_llm_proxy(self) -> None:
        proxy = getattr(self, "llm_proxy", None)
        if proxy:
            proxy.stop()
        self.log_usage_summary()

    def log_usage_summary(self) -> None:
        """Provider-reported usage totals (same numbers the platform bills on),
        printed so the runner log carries them even when .arc/ is not exported."""
        path = self.output_dir / ".arc" / "llm-usage.jsonl"
        if not path.is_file():
            return
        tot = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
               "prompt_cache_hit_tokens": 0, "total_tokens": 0, "request_bytes": 0, "response_bytes": 0,
               "sse_chunks": 0}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            tot["requests"] += 1
            for k in list(tot)[1:]:
                tot[k] += int(rec.get(k) or 0)
        log(f"[usage] provider totals: {json.dumps(tot)}")

    def cleanup_playwright(self) -> None:
        private = getattr(self, "private_playwright", None)
        if private and Path(private).exists():
            shutil.rmtree(private, ignore_errors=True)
            # 已经在退出流程里删过了就从登记表里摘掉，免得 atexit 再删一次。
            if Path(private) in _TEMP_DIRS:
                _TEMP_DIRS.remove(Path(private))
            log(f"[acceptance] removed private Playwright install {private}")

    def app_server(self, grader_like: bool) -> AppServer:
        return AppServer(self.output_dir, self.smoke_port, log, grader_like=grader_like,
                         extra_ports=[p for p in spec_base_ports(self.tests_dir) if p != self.web_port])

    def run_specs(self, specs: list[str], workers: int | None = None, grader_like: bool = False,
                  fail_fast: bool = False) -> RunSummary:
        """Build, start, run the specs, then undo whatever the test run mutated
        (a persisted counter at -1 would otherwise be committed as the seed).
        `grader_like` starts the backend with only PORT set, as the platform does.
        `fail_fast` 判红即停（透传到 runner，2026-10-08 提速批）—— 只给日常
        体检（对账）用；成绩/净胜判定的测量不挂它（见 `_run_reconcile`）。"""
        if len(specs) > 1 and os.environ.get("OCTOS_ARC_FILE_ISOLATION", "1") != "0":
            return self.run_specs_isolated(specs, workers=workers, grader_like=grader_like,
                                           fail_fast=fail_fast)
        git_run = lambda args: self.runtime.git.run(args, check=False)  # noqa: E731
        snapshot_worktree(git_run)
        server = self.app_server(grader_like)
        summary: RunSummary | None = None
        try:
            err = server.build()
            if err is None:
                err = server.start()
            if err is not None:
                return RunSummary(error=err, startup_error=True)
            summary = self.runner.run(specs, f"http://127.0.0.1:{self.smoke_port}", workers=workers,
                                      fail_fast=fail_fast)
            return summary
        finally:
            server.stop()
            # Ask before restoring: afterwards there is nothing left to compare.
            if summary is not None:
                summary.stores_written = mutated_by_tests(git_run)
            restore_worktree(git_run)

    def run_specs_isolated(self, specs: list[str], workers: int | None = None,
                           grader_like: bool = False, fail_fast: bool = False) -> RunSummary:
        """逐份文件跑卷子：每份之间重启应用、还原工作区。

        动机（实测形状）：一批文件共用一个服务器时，一份卷子留下没还原的状态
        （改掉的密码、前进的工单状态、多出来的成员）会成为后面每一份文件的
        错误起点 —— REQ-1-3 的密码改完不还原，同批两份后续卷子拿旧密码登录
        全红，调查了半天才发现卷子自己没错。文件级隔离把这份外溢砍掉：每份
        文件开工时，应用和工作区都回到快照原点。

        边界要说清（不许含糊）：粒度是**文件级，不是用例级**。同一文件内部
        两个 test 之间照样共用一个服务器 —— 用例级要求「每个 test 前重新灌
        种子」的应用重置入口，题目不保证有；文件内部的自洁由出卷提示词的
        「改了就必须还原」铁律负责。重启进程顺带清掉应用自身的内存状态。

        代价也要说清：文件串行跑（一份文件一个服务器，没法并行），整批墙钟
        时间比一次并发跑长；换来的是任何一份卷子的烂摊子只可能烂它自己。
        `OCTOS_ARC_FILE_ISOLATION=0` 退回整批一次跑（对照用）。

        卷子工作目录整批只准备一次（2026-10-08 提速批）：清目录+重拷全卷+
        逐份改写原来每份文件都做一遍（52 份卷子的批次 = 52 遍），买到的只是
        「报告不串台」—— 现在只对第一份做，其余份由 runner 清报告文件。

        `fail_fast` 透传给每一份文件的测量：判红即停**在文件内部**生效
        （第一条红就收工），文件与文件之间照跑不误 —— 体检要的是逐份的红
        名单，这正是它比整批一次跑多买到的那点东西。
        """
        git_run = lambda args: self.runtime.git.run(args, check=False)  # noqa: E731
        snapshot_worktree(git_run)
        server = self.app_server(grader_like)
        combined = RunSummary()
        err = server.build()
        if err is not None:
            restore_worktree(git_run)
            return RunSummary(error=err, startup_error=True)
        log(f"[acceptance] file isolation: {len(specs)} spec file(s), one server start each")
        try:
            for i, spec in enumerate(specs, start=1):
                summary: RunSummary | None = None
                try:
                    start_err = server.start()
                    if start_err is not None:
                        # 部分结果照常并进 combined，但整批标记为未完成 —— 调用方
                        # （封箱测量、终局修复）据此不会把半截测量当成全量结论。
                        # 启动失败与构建失败同属环境病（见 RunSummary.startup_error）。
                        combined.error = f"server did not start before {spec}: {start_err}"
                        combined.startup_error = True
                        break
                    summary = self.runner.run([spec], f"http://127.0.0.1:{self.smoke_port}",
                                              workers=1, reprepare=(i == 1),
                                              fail_fast=fail_fast)
                finally:
                    server.stop()
                    if summary is not None:
                        combined.stores_written += mutated_by_tests(git_run)
                        combined.results += summary.results
                        combined.load_errors += summary.load_errors
                        if summary.stdout_tail:
                            combined.stdout_tail = summary.stdout_tail
                    restore_worktree(git_run)
                if summary is None:
                    break
                if summary.killed:
                    # 被杀多半是 OOM：下一份只会更糟，停在这里让调用方按既有
                    # 「减 worker 重试」路径处理（见 seal_delivery）。
                    combined.killed = True
                    combined.error = summary.error or f"runner killed at {spec}"
                    break
                if summary.error:
                    # 单份文件的基建错（收集不到用例、报告读不出）不拖垮整批：
                    # 并进 load_errors 继续跑，其余文件的结果仍然有效。
                    combined.load_errors.append(f"{spec}: {summary.error}")
            combined.total = len(combined.results)
            combined.passed = sum(1 for r in combined.results if r.ok)
            return combined
        finally:
            server.stop()
            restore_worktree(git_run)

    def record_tests(self, node_id: str, specs: list[str], summary: RunSummary) -> None:
        try:
            for r in summary.results:
                test_id = re.sub(r"[^A-Za-z0-9._-]+", "-", r.title)[:120]
                self.runtime.traceability.upsert_test(test_id=test_id, req_id=node_id, type="e2e",
                                                      file_path=r.file or None, passed=r.ok, emit_event=False)
        except Exception as exc:  # noqa: BLE001
            log(f"[trace] test rows not recorded: {exc}")


    def repair_needs(self) -> float:
        """再来一轮修复，实际需要多少秒——**按这个模型自己的表现算**，不是按一个猜的常数。

        原先这里是固定的 `min_repair_seconds`（默认 300）。意图是对的，代码注释写得很清楚：
        「只剩几分钟才开始的修复轮同样会超时，不如保住当前最好的状态」。
        但那个数字是猜的，而 keep @ D 的实测是**一轮修复约 600 秒**——整整两倍。
        猜小了的后果不是保守，是**反过来**：在只剩 300–600 秒时它会放行一轮注定被砍断的修复，
        那一轮什么都留不下（甚至可能留下改了一半的文件），时间也没了。

        所以改成用观察值：取最近若干轮的中位数、留 10% 余量，并且**不低于**配置的下限
        （下限仍然是 `OCTOS_MIN_REPAIR_SECONDS`，所以这条改动只会让判断更严、不会更松）。
        还没跑过修复轮时就用下限，行为与原先一致。

        只留最近 12 轮：模型和题目会变，很久以前的耗时不该继续影响现在的决定。
        """
        seen = [d for d in (getattr(self, "repair_durations", None) or []) if d and d > 0]
        if not seen:
            return float(self.min_repair_seconds)
        seen = sorted(seen)
        median = seen[len(seen) // 2] if len(seen) % 2 else (seen[len(seen) // 2 - 1] + seen[len(seen) // 2]) / 2
        return max(float(self.min_repair_seconds), median * 1.1)

    def batch_repair_needs(self) -> float:
        """一批（1~3 份卷子）的修复实际要多久 —— `repair_needs` 的批修版。

        观察值来自 `batch_repair_durations`（`turn()` 里按 label 分开记账的），
        所以整段修复的耗时不会和批修的混在一起互相污染。还没跑过批修回合时
        退回 `min_repair_seconds`，行为和旧路径一致。
        """
        seen = [d for d in (getattr(self, "batch_repair_durations", None) or []) if d and d > 0]
        if not seen:
            return float(self.min_repair_seconds)
        seen = sorted(seen)
        median = seen[len(seen) // 2] if len(seen) % 2 else (seen[len(seen) // 2 - 1] + seen[len(seen) // 2]) / 2
        return max(float(self.min_repair_seconds), median * 1.1)

    def batch_repair_timeout(self) -> int:
        """一批修复的单回合上限。

        一批最多 3 份卷子，正常情况下比整段修复短得多；封顶
        `OCTOS_BATCH_REPAIR_TIMEOUT_MAX`（默认 1200s）防的是失控 —— 一个
        回合作到 20 分钟还没完，多半不是活多，是它跑偏了，让快照把它退回来
        比让它继续写更便宜。
        """
        cap = int(os.environ.get("OCTOS_BATCH_REPAIR_TIMEOUT_MAX", "1200"))
        try:
            need = self.batch_repair_needs()
        except Exception:  # noqa: BLE001
            need = float(getattr(self, "min_repair_seconds", 300))
        return max(300, int(min(self.node_timeout, min(cap, need * 1.5))))

    def banked_surplus(self, index: int, total: int) -> float:
        """已经**省下来**的预算里，可以拿给这个节点用的部分。

        `remaining / nodes_left` 假设剩下每个节点都花一样多。实际不是：大多数节点几秒就过，
        少数难节点要好几分钟。keep @ D 全程只用掉预算的 63%，而它丢掉的六个节点里有三个
        在**开局附近**——那时余额还没攒出来，份额恰好 1500，离它们需要的 1813 差 287 秒，
        于是修复停在第一轮。开局保守的代价，就是早期的难节点拿不到它们需要的时间。
        （抬高 `node_budget_cap` 救不了这三个：第 4 个节点的份额只有 1592，上限根本没顶到。）

        这里只动用**已经证明省下来的**那部分，不向未来借钱：

            按进度本该花掉的 = 总预算 × 已完成节点数 / 总节点数
            结余             = max(0, 本该花掉的 − 实际花掉的)

        进度落后时结余为 0，行为逐字不变——所以一个已经超支的运行不会因此继续超支。
        只拿结余的一半，另一半留给后面，免得开局几个难节点把余额吃光、把尾巴饿到地板。

        四个约束是叠加的不是替换的：`remaining / nodes_left` 仍是基数、
        `node_budget_cap` 仍封顶、240 秒地板仍在。
        """
        budget = getattr(self, "budget", 0) or 0
        if budget <= 0 or total <= 0:
            return 0.0
        done = max(0, index - 1)
        should_have_used = budget * done / total
        actually_used = budget - self.remaining()
        return max(0.0, should_have_used - actually_used) / 2.0


    def discard_template(self) -> Path | None:
        """Move frontend/ and backend/ of a non-working existing app to
        .arc/template-discarded/ so the fresh build starts from our own layout."""
        dest = self.output_dir / ".arc" / "template-discarded"
        try:
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True, exist_ok=True)
            moved = []
            for name in ("frontend", "backend"):
                src = self.output_dir / name
                if src.exists():
                    shutil.move(str(src), str(dest / name))
                    moved.append(name)
            log(f"[flow] existing app passes no spec; moved {moved} to {dest.relative_to(self.output_dir)} and building fresh")
            return dest
        except OSError as exc:
            log(f"[flow] could not set the existing app aside: {exc}")
            return None

    def already_passing_nodes(self, node_ids: list[str]) -> set[str]:
        """Evolution probe: run each candidate node's specs against the existing app
        (no LLM); nodes that fully pass need no implementation turn."""
        out: set[str] = set()
        for node_id in node_ids:
            specs = list(self.spec_map.get(node_id) or [])
            if not specs:
                continue
            summary = self.run_specs(specs)
            self.probe_count += 1
            if summary.error:
                # 绿 / 黄 / 红三档：跑不起来是红，而且原因要留下来（施工清单要用）。
                self.probe_health[node_id] = {"passed": 0, "total": len(specs), "green": False,
                                              "error": str(summary.error)[:200]}
                log(f"[acceptance] probe {node_id}: existing app does not build/start/serve ({summary.error[:160]})")
                continue
            if not summary.total:
                continue
            self.probe_health[node_id] = {
                "passed": summary.passed, "total": summary.total,
                "green": bool(summary.all_passed),
                "first_failure": self._first_failure_line(summary)}
            log(f"[acceptance] probe {node_id}: {summary.passed}/{summary.total} against the existing app")
            if summary.all_passed:
                out.add(node_id)
                self.probe_summaries[node_id] = summary  # regression_cycle reuses it
        return out

    @staticmethod
    def _first_failure_line(summary) -> str:
        """探针里第一个失败的用例长什么样 —— 一行，给施工清单当证据。"""
        for result in getattr(summary, "results", []) or []:
            if getattr(result, "ok", True):
                continue
            title = (getattr(result, "title", "") or "").strip()
            message = " ".join((getattr(result, "message", "") or "").split())
            return f"{title}: {message[:160]}" if title else message[:200]
        return ""

    def write_construction_plan(self, node_ids: list[str]) -> None:
        """**施工清单**：每个要求点今天是什么状态，绿/黄/红 + 一句为什么。

        「起跑先勘察、绝不乱拆」落到文件上的那一份：模型开场读它（勘察回合），
        人复盘也读它。它只讲**探针量到的事实**，不替模型做取舍 —— 取舍在图纸
        （沿用/改造/新增）那一节里，那一步是模型的活。
        """
        try:
            path = self.output_dir / ".arc" / "construction-plan.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            rows = ["# 施工清单（开工前体检）", "",
                    "探针把每个要求点的卷子直接打在**现成的 app** 上跑了一遍，"
                    "不经过模型。绿 = 已经满足，黄 = 做到一半，红 = 还不满足。", "",
                    "| 要求点 | 状态 | 说明 | 第一个失败 |",
                    "| --- | --- | --- | --- |"]
            counts = {"green": 0, "yellow": 0, "red": 0}
            for node_id in node_ids:
                health = self.probe_health.get(node_id)
                if not health:
                    state, why, first = "未探测", "这份卷子没有跑到（没有卷子或已跳过）", ""
                elif health.get("error"):
                    state, why, first = "红", "跑不起来：" + health["error"][:120], ""
                elif health.get("green"):
                    state, why, first = "绿", f"{health.get('passed', 0)}/{health.get('total', 0)} 全过", ""
                elif health.get("total"):
                    state = "黄"
                    why = f"{health.get('passed', 0)}/{health.get('total', 0)} 通过"
                    first = health.get("first_failure", "")
                else:
                    state, why, first = "红", "没有可跑的用例", ""
                if state == "绿":
                    counts["green"] += 1
                elif state == "黄":
                    counts["yellow"] += 1
                elif state == "红":
                    counts["red"] += 1
                rows.append("| {} | {} | {} | {} |".format(
                    node_id, state, str(why).replace("|", "/"), str(first).replace("|", "/")))
            rows += ["", f"绿 {counts['green']} 个；黄 {counts['yellow']} 个；红 {counts['red']} 个。",
                     "绿的是**沿用**的地基（别动它）；黄的要按新需求**改造**；红的按新需求做**新增**。"]
            path.write_text("\n".join(rows) + "\n", encoding="utf-8")
            log(f"[flow] construction plan written: {path} "
                f"(green {counts['green']}, partial {counts['yellow']}, red {counts['red']})")
        except OSError as exc:
            log(f"[flow] could not write the construction plan: {exc}")

    def probe_existing_app(self, node_ids: list[str], unchanged: set[str]) -> set[str]:
        """模板探针：拿卷子量一遍现成的 app，哪些节点已经天然满足。

        **它必须在「已经拿到卷子」之后跑，而这件事只在一个地方曾经成立。**
        官方卷子在构造驱动之前就位，自产卷子不是 —— 出卷是一个模型回合，排在驱动
        之后（见 `author_acceptance_suite`）。于是正赛上这里会看到 `runner is None`
        （`setup_playwright()` 在没有卷子时提前返回），整个探针空转。

        空转的代价不是「少省几个回合」。下面那个 `discard_template()` 也就不会触发：
        一个空壳模板会被当成正经地基，每个节点都拿到 `NODE_PREAMBLE_EXTEND`
        （「在现有基础上扩展，不要重写」），而它其实什么都不满足 —— 注释里记的实测
        代价是**fresh build 的 40-80 倍**。

        所以它在两个调用点各跑一次，靠 `_probed` 只生效一次：官方卷子那条路在
        构造驱动之前，自产卷子那条路在出卷之后。
        """
        if not self.evolution or self.runner is None or self._probed:
            return unchanged
        self._probed = True
        unchanged = set(unchanged)
        # The platform's template app carries no traceability records, so
        # fingerprints cannot tell what is new. A node whose specs already
        # pass against the existing app is unchanged — no LLM turn for it.
        unchanged |= self.already_passing_nodes([n for n in node_ids if n not in unchanged])
        # 体检表先落盘 —— 勘察回合（和事后复盘）读的就是它。写在拆/不拆的判断之前：
        # 无论结论是什么，这份事实都该留下来。
        self.write_construction_plan(list(node_ids))
        if self.probe_count and not unchanged:
            # 一个都不满足。**v5：这件事不再自己决定拆楼** —— 探针只知道「今天没有
            # 一条卷子全过」，它不知道这楼里有多少能用。正赛实测的代价是：一个大部分
            # 能用的继承 app 被整个搬走、从零重建，然后超时。所以「拆」的许可收到
            # `discard_allowed` 里（真毛坯，或人按了强制开关）；其余的**保留 + 就地
            # 维护**，红的那些由施工清单点名，按节点一个个改。
            allowed, why = discard_allowed(self.output_dir)
            if allowed:
                # cloud c30b29eab45b/10b04d36f704: implement-then-rewrite on a placeholder
                # cost 40-80x the fresh build.
                self.discard_template()
                self.evolution = False
            else:
                self.evolution = True
                log(f"[flow] the probe found nothing green, but the app stays: {why}")
                log("[flow] it is maintained in place — the construction plan names what is red")
        self.nodes_to_implement = len([n for n in node_ids if n not in unchanged])
        log(f"[flow] {'evolution' if self.evolution else 'fresh build'} after probing the "
            f"existing app: unchanged {sorted(unchanged)}, "
            f"to implement {[i for i in node_ids if i not in unchanged]}")
        return unchanged

    def degrade_to_no_acceptance(self) -> bool:
        """卷子在盘上、却一份都跑不起来 —— 退回「没有验收机制」那一条路。

        见调用点的长注释：这是正赛上最坏的一种半配置状态（`setup_playwright()` 的
        三层兜底全失败），而它原来的症状是**全树判死**外加一句说反了的话。

        判据是「`tests_dir` 有、`runner` 没有」：前者说明本轮**本该**有验收，
        后者说明它跑不了。两个都成立才是降级，其它组合什么都不做 ——
        没有卷子的时候 `runner` 本来就是 None（那是正常路径，不是故障）。

        返回是否降级了。**清空 `spec_map` 是就地改**，不换新 dict：那个引用
        可能已经交出去过（`Orchestrator` 构造用的是 `flow.spec_map`）。
        """
        if not self.tests_dir or self.runner is not None:
            return False
        log("[acceptance] " + "*" * 74)
        log("[acceptance] **Playwright is unavailable: NOTHING in this run can be verified.**")
        log("[acceptance] Every node will be built and accepted on zero assertions. The suite on disk")
        log("[acceptance] is still protected and still listed, but it will never be run — so a passing")
        log("[acceptance] report from this run means nothing. The delivered app is judged by the")
        log("[acceptance] grader's own Playwright, so the run continues and delivers it.")
        log("[acceptance] " + "*" * 74)
        for key in list(self.spec_map):
            self.spec_map[key] = []
        return True

    def spec_basis(self, node_id: str, passed: bool) -> str:
        """这个节点的验收结论**站在什么证据上** —— 就是上报给平台的那句话。

        原先这里是一句写死的话（「acceptance specs pass (node run and full parallel
        suite)」）。在官方卷子那条路上它是真的：每个节点都有一份卷子、都被跑过。
        自产卷子这条路上它不是：出卷失败时的叶子、以及**全部文件夹节点**，名下一份
        卷子都没有，而 `_accept` 里那句 `own_ok = not specs` 让它们一条断言都没跑
        就通过。于是 42 个节点上报 42 句一模一样的「卷子通过」，其中一半没跑过卷子。

        平台的打分不读这些事件，所以这句话不影响名次。它骗的是**我们自己** ——
        事后想复盘「哪些节点是真验过的」，读到的是一排没有区别的通过。

        ★ 这里只改措辞，不改 `test_verdict` 的取值：判据仍然是编排说了算。
        """
        # **跑都跑不起来的一趟，不能说自己验过。**
        #
        # 卷子在盘上、`spec_map` 却在 `run()` 里被清空（Playwright 三层兜底全失败，
        # 见那一处的说明）时，下面每一条分支都会说谎：叶子那条说「nothing asserted
        # it」还算贴边，**文件夹节点那条会说「aggregate: the full baseline ran and
        # stayed green」—— 而一次全量基线都没跑过**。所以这一条排在最前面。
        if self.tests_dir and getattr(self, "runner", None) is None:
            return ("Playwright was unavailable in this run: no acceptance suite could be run at all"
                    if passed else
                    "Playwright was unavailable in this run: nothing verified this node")
        if not self.tests_dir:
            return ("no acceptance suite ran in this run; the requirement text and the contract "
                    "are the only evidence" if passed else
                    "no acceptance suite ran in this run; nothing verified this node")
        if self.spec_map.get(node_id):
            return ("acceptance specs pass (node run and full parallel suite)" if passed
                    else "acceptance specs failing")
        info = getattr(getattr(self, "layers", None), "by_id", {}).get(node_id)
        if getattr(info, "children", None):
            # 文件夹节点：自己名下没有卷子，但聚合验收跑了全量基线。
            return ("aggregate: this node owns no spec; the full baseline ran and stayed green "
                    "at this level" if passed else
                    "aggregate: this node owns no spec; the baseline at this level did not pass")
        return ("no acceptance spec is mapped to this node — nothing asserted it directly" if passed
                else "no acceptance spec is mapped to this node; it failed on the requirement text")

    def final_acceptance(self) -> None:
        """Run EVERY spec file together against one server with the configured workers.
        Per-node runs cannot see cross-node interference through shared server
        state; this pass can, and it repairs the nodes whose tests fail.

        P2（`OCTOS_ATOMIC_FINAL`，默认开）：有 orchestrator 时整个交给
        `Orchestrator.final_acceptance_batched` —— 修复按批（1~3 份卷子一批）、
        每批全卷过秤才提交、没过秤退回。`=0` 或没有 orchestrator 时逐字走
        下面的旧 body（一把修全部红节点、边修边无条件提交）。"""
        if self.runner is None or not self.tests_dir:
            return
        if _atomic_final():
            orch = getattr(self, "orchestrator", None)
            if orch is not None and getattr(orch, "layers", None) is not None:
                orch.final_acceptance_batched()
                return
        all_specs = sorted(str(p.relative_to(self.tests_dir)) for p in self.tests_dir.rglob("*.spec.ts"))
        unverified = [n for n, v in self.test_verdict.items() if v is not True] or \
            [n for n in self.spec_map if n and self.spec_map[n] and n not in self.test_verdict]
        if len(all_specs) < 2 and not unverified:
            return  # single spec already judged by the node run
        # One more round than the identical-failure escalation needs, so the
        # changed approach actually gets to run.
        rounds = int(os.environ.get("OCTOS_FINAL_REPAIR_ROUNDS", "3"))
        workers = final_workers(getattr(self, "mem_limit", None))
        previous_failing: frozenset | None = None
        repeated = False  # the last round reproduced the round before it
        # Which behaviours the per-node runs judged good, before this pass starts
        # overwriting the verdicts with full-suite ones.
        passed_alone = {node for node, verdict in self.test_verdict.items() if verdict is True}
        passed_a_round: set = set()  # nodes this pass has already seen pass once
        # Measured, not yet acted on: a repair killed at the per-turn timeout can
        # leave the tree part edited. Cloud 6e82a7ff571c went 27/32, had its
        # repair cut at 1200s, and measured 17/32 next round, so the rounds after
        # it repair the damage rather than the five failures it started with. The
        # restore below still delivers the best round, so only the intervening
        # rounds are spent. Repairing from `best` instead was tried and reverted:
        # the evidence then comes from a round where the flaky specs passed, which
        # silently drops the intermittent note. Any retry needs to keep both.
        best: dict | None = None  # L17: best full-suite round (passed, sha, summary, grouped)
        regressions = 0  # consecutive rounds behind the best one
        last_passed = -1
        unfinished = ""  # what the previous repair turn said it had left to do
        wrote_last = False  # whether that turn got as far as committing an edit
        for attempt in range(rounds + 1):
            summary = self.run_specs(all_specs, workers=workers, grader_like=True)
            while summary.error and summary.killed and workers > 1:
                # Cloud 29c840566f36: the runner was OOM-killed under a 512 MiB
                # cgroup. The memory a suite needs is not known before running it,
                # so give the box a count it can hold instead of abandoning the
                # repairs on the first kill.
                workers = max(1, workers // 2)
                log(f"[acceptance] full suite runner was killed; retrying with {workers} worker(s)")
                summary = self.run_specs(all_specs, workers=workers, grader_like=True)
            if summary.error and summary.killed:
                log(f"[acceptance] full suite could not run ({summary.error[:120]}); keeping per-node verdicts")
                break
            if summary.error:
                # The app does not even start the way the grader starts it: every node fails.
                log(f"[acceptance] full suite (grader-like start) failed: {summary.error[:300]}")
                for node_id in self.spec_map:
                    if node_id:
                        self.test_verdict[node_id] = False
                grouped = {None: []}
                failures = (f"- Feature: application startup exactly as the grader runs it (only PORT set)\n"
                            f"  Failed at: npm start\n  Observation: {startup_error_digest(summary.error, 700)}\n"
                            f"  Steps: npm run build -> npm start")
                summary = RunSummary(passed=0, total=len(all_specs))
            else:
                grouped = nodes_for_failures(summary.results, self.spec_map)
                failures = failure_summaries(summary) + failure_source_context(summary, self.tests_dir)
                failures += self.interference_note(grouped, passed_alone, summary.stores_written)
                failures += self.intermittent_note(grouped, passed_a_round)
                failures += self.worker_parity_note(workers)
                owners = {Path(path).name: node for node, paths in self.spec_map.items()
                          for path in (paths or [])}
                passed_a_round |= {owners.get(Path(r.file or "").name)
                                   for r in summary.results if r.ok} - {None}
            log(f"[acceptance] full suite round {attempt}: {summary.passed}/{summary.total}; failing nodes "
                f"{sorted(k for k in grouped if k) or ('all' if None in grouped and not summary.results else [])}")
            self.record_full_suite(summary, grouped)
            # This round measured the tree as it stands, so the seal no longer
            # has to treat it as possibly-edited -- unless a later turn edits it
            # again, which flips the flag back in `turn()`.
            self._tree_maybe_edited = False
            self._last_full_measure = {"passed": summary.passed, "total": summary.total}
            last_passed = summary.passed
            if best is None or summary.passed > best["passed"]:
                if attempt > 0:
                    self.commit(f"chore: full acceptance suite {summary.passed}/{summary.total} (best so far)")
                best = {"passed": summary.passed, "sha": self.head(), "summary": summary, "grouped": grouped}
                card_sha = self.record_best(summary, f"final acceptance round {attempt}")
                if card_sha:
                    # The snapshot is the exact tree that was measured; `head()`
                    # is not, while a round ends with uncommitted edits.
                    best["sha"] = card_sha
                    self._best_summary, self._best_grouped = summary, grouped
                regressions = 0
            elif summary.passed < best["passed"]:
                # The per-node loop already does this; the full-suite pass did not.
                # A repair cut at the per-turn timeout leaves the tree part
                # written: cloud 6e82a7ff571c went 27/32 -> repair killed at 1200s
                # -> 17/32, and the rounds after it repair that instead of the
                # five failures the pass began with. Two rounds behind the best
                # is a trend rather than one flaky spec, which is the same
                # threshold the node loop uses.
                #
                # Two and not one, and the same run shows why: 6e82a7ff571c went
                # 27/32, lost a repair to the timeout, measured 17/32, lost the
                # next repair to the timeout as well -- and then measured 28/32,
                # past the best it had. A rollback on the first dip would have
                # returned to 27 and never reached 28. Damage from a cut turn is
                # recoverable, so this waits for a trend and the pass still
                # delivers its best round either way.
                regressions += 1
                if regressions >= 2 and best["sha"]:
                    self.restore_app(best["sha"])
                    self.pending_corrections.append(
                        f"Your last two full-suite repairs made the tests worse; the harness restored frontend/ and "
                        f"backend/ to the best state ({best['passed']}/{best['summary'].total}). Start from that code.")
                    log(f"[acceptance] full suite: two rounds behind {best['passed']}/{best['summary'].total}; "
                        f"restored the best state")
                    # The tree is the best round's again, so the verdicts have to be
                    # too, and the restore at the end of the pass has nothing left
                    # to do.
                    self.record_full_suite(best["summary"], best["grouped"])
                    last_passed = best["passed"]
                    regressions = 0
            if not grouped:
                self.commit(f"chore: full acceptance suite {summary.passed}/{summary.total} pass (full suite)")
                return
            # A spec that has passed once in this pass and fails now is unstable;
            # letting it count as progress hides a stall in everything else.
            unstable = frozenset(spec for node in passed_a_round
                                 for spec in (self.spec_map.get(node) or []))
            failing_signature = failure_signature(summary, unstable)
            if previous_failing is not None and failing_signature == previous_failing:
                if repeated:
                    log("[acceptance] full suite: failures unchanged after a changed approach; stopping repairs")
                    break
                # Cloud 3ffe9702bf15: the suite stalled at 26/32 and the run ended
                # with most of its budget unspent. One identical round means the
                # repair missed the cause, not that the cause cannot be fixed --
                # tell it so, the way the per-node path already does, and retry.
                repeated = True
                failures += self.last_repair_diff()
                # A repair that ran out before editing has an unfinished plan worth
                # continuing; one that edited and moved nothing does not -- there the
                # instruction below is to change the cause, so carrying its reasoning
                # forward would argue against the correction in the same prompt.
                if wrote_last:
                    unfinished = ""
                    log("[acceptance] full suite: same failures as the previous round; changing repair approach")
                    self.pending_corrections.append(
                        'Repeated attempts produced the same observed failure. Recheck the assumptions behind the repair: inspect expected and received values, preceding actions, locator scope, and actual application state. Change the cause supported by this evidence. Do not manufacture the expected output or bypass the underlying operation; preserve behavior for other inputs.')
                else:
                    # The previous repair committed nothing, so the suite just measured the
                    # same code twice and identical failures are guaranteed -- they are not
                    # evidence that the repair missed the cause. `last_repair_diff()` above
                    # already added the accurate line for this case ("left frontend/ and
                    # backend/ unchanged ... Make an edit this time"), and `unfinished` is
                    # deliberately kept so the half-finished plan carries forward. Adding
                    # "recheck the assumptions behind the repair" on top of that argues
                    # against it in the same prompt: one line says finish what you started,
                    # the other says abandon your reasoning and change the cause.
                    # Same defect, and the same fix, as the per-node identical-failure path.
                    log("[acceptance] full suite: same failures, but the previous repair committed "
                        "nothing -- the suite measured identical code, so this repeat is not "
                        "evidence about the repair; keeping the unfinished plan")
            else:
                repeated = False
            previous_failing = failing_signature
            if attempt == rounds or self.remaining() < 240 or self.wound_down():
                break
            failing = sorted(k for k in grouped if k) or ["all nodes"]
            failures += self.unfinished_repair_note(unfinished)
            prompt = REPAIR_PROMPT.format(
                node_id=", ".join(failing), passed=summary.passed, total=summary.total, failures=failures,
                test_location=self.repair_test_location(),
                sources=self.repair_requirements() + self.sources_text(),
                corrections=self.corrections_text() + "The full suite runs all spec files against one "
                "server; tests from different files must not interfere through shared server state "
                "(e.g. a counter that every browser session shares). Keep persisted data only where the "
                "requirement demands persistence.\n",
                slow="", smoke=self.smoke_port, port=self.web_port)
            _, unfinished = self.turn(
                prompt, min(self.suite_repair_timeout(), max(120, self.remaining() - 200)),
                f"full-suite repair {attempt + 1}/{rounds}")
            wrote_last = self.commit(f"fix: full-suite repair {attempt + 1}")
        # L17 (ported from the Rust harness): deliver the best full-suite round, not the last one.
        if best is not None and best["sha"] and last_passed < best["passed"]:
            log(f"[acceptance] full suite: last round {last_passed} < best {best['passed']}; restoring the best state")
            self.restore_app(best["sha"])
            self.record_full_suite(best["summary"], best["grouped"])
            self.commit(f"chore: keep best full-suite state {best['passed']}/{best['summary'].total}")

    def suite_repair_timeout(self) -> int:
        """How long a repair that answers the whole suite at once may take.

        `node_timeout` bounds a turn about one node; this bounds a turn about
        every failing node together, and the two are not the same size of job.
        They default to the same number. Measured on the runs of 2026-09-16, an
        hour apart, because the first reading was misleading. At 207 node-phase
        turns not one had reached the 1200s cap, longest 1049s, while both of
        6e82a7ff571c's full-suite repairs were cut at it -- which looked like
        the suite repairs being the long ones. By 261 turns prestashop had a
        node-phase turn cut at 1200s on a 2 GiB container, and six turns sat
        past 890s. So the long turns are not particular to the full suite, nor
        to 6e82a7ff571c's 512 MiB box; six runs sharing one provider queue
        behind each other. Keep the two separate anyway, so the suite one can
        move without shortening or lengthening every node turn with it.
        """
        return int(os.environ.get("OCTOS_SUITE_REPAIR_TIMEOUT", str(self.node_timeout)))

    def final_acceptance_passes(self) -> None:
        """Repeat the full-suite pass while it still fails and the budget allows.

        Cloud 3ffe9702bf15 delivered 25/32 after spending 5754 s of a 48000 s
        budget: one pass ran, its repairs stalled, and the run ended with the
        rest of the time unused. Every pass keeps its own best state, so a
        repeat starts from a state at least as good as the one before it.
        """
        passes = int(os.environ.get("OCTOS_FINAL_SUITE_PASSES", "3"))
        for attempt in range(passes):
            if self.time_up() or self.remaining() < self.min_repair_seconds * 3:
                break
            if attempt:
                if all(verdict is not False for verdict in self.test_verdict.values()):
                    break
                log(f"[flow] full suite still failing with {self.remaining():.0f}s left; "
                    f"pass {attempt + 1}/{passes}")
            self.final_acceptance()
            if self.driver:
                self.driver.end_scope("node")

    GRADER_WORKERS = 1  # the grading config: workers: 1, fullyParallel: false

    @classmethod
    def worker_parity_note(cls, workers: int) -> str:
        """Say so when the suite could not be run the way it will be graded.

        The suite shares one server and one persisted store, so a run at N
        workers measures a different app than the graded one. The default is now
        the grader's own count (`acceptance.GRADER_WORKERS`, 1); this note fires
        only when that was raised on purpose (`OCTOS_ARC_FINAL_WORKERS`).

        An earlier version of this note claimed the count decides *which* tests
        fail, from two runs at each of one and four workers. More repeats
        disproved it: the same two nodes swap places between repeats at a fixed
        count as well, so that difference is the flakiness #175 reports, not the
        worker count. What is left is the parity gap itself, which is reason
        enough not to read the list as the set that will be scored.
        """
        if workers == cls.GRADER_WORKERS:
            return ""
        return (f"\n\nThis suite ran with {workers} worker(s); grading runs {cls.GRADER_WORKERS} against one "
                "server, so this is not the run that scores the app. Fix the cause these failures share "
                "rather than the exact list, which a differently loaded run need not reproduce.")

    @staticmethod
    def intermittent_note(grouped: dict, passed_a_round: set) -> str:
        """Name the behaviours that already passed once in this pass.

        Running the same 32 specs three times over one unchanged app gave 25,
        24 and 25: REQ-2.7.1 failed once and passed twice. A repair told only
        that it failed goes looking for a missing feature, when what it has is a
        race — and the grader runs with retries off, so it stays worth fixing.
        """
        flaky = sorted(node for node in grouped if node and node in passed_a_round)
        if not flaky:
            return ""
        return ("\n\nThese already passed in an earlier round of this same pass and fail now: "
                f"{', '.join(flaky)}. The behaviour exists; what is missing is that it settles "
                "reliably. Look for a race, an unawaited update or state left over from another "
                "session rather than for an unimplemented feature.")

    @staticmethod
    def interference_note(grouped: dict, passed_alone: set, stores_written: list | None = None) -> str:
        """Name the behaviours that only fail in company.

        Cloud 746c81a2b5aa lost REQ-5.1 this way: it passed on its own and failed
        in the suite, where every spec drives the same server. Saying which
        failures are of that kind separates "the behaviour is wrong" from "the
        behaviour does not survive another session touching the same state" —
        the evidence for both looks identical otherwise.

        The claim is only as fresh as the per-node verdict it rests on, and later
        nodes change the app. Measured on one delivered app, seven of its eight
        suite failures also failed with the rest of the suite removed, so the note
        asks for the check rather than asserting the conclusion.
        """
        solo = sorted(node for node in grouped if node and node in passed_alone)
        if not solo:
            return ""
        note = ("\n\nThese passed their own node run earlier and fail now that every spec drives one "
                f"server: {', '.join(solo)}. That earlier result is from before the later nodes were "
                "built, so check the behaviour still works on its own: if it does, the difference is "
                "state shared with the other sessions; if it does not, it was broken since.")
        if stores_written:
            note += (" The suite run left these files changed on disk: "
                     f"{', '.join(stores_written)} — whatever one session writes there is still there for "
                     "the next.")
        return note

    def record_full_suite(self, summary: RunSummary, grouped: dict) -> None:
        """Per-node verdicts and traceability from one full-suite round."""
        for node_id, specs in self.spec_map.items():
            if node_id and specs and summary.results:
                self.record_tests(node_id, specs, RunSummary(results=[r for r in summary.results
                                  if Path(r.file or "").name in {Path(p).name for p in specs}]))
                self.test_verdict[node_id] = node_id not in grouped


    def has_app(self) -> bool:
        return (self.output_dir / "frontend" / "package.json").is_file() and \
            (self.output_dir / "backend" / "package.json").is_file()

    # -- final ------------------------------------------------------------
    def rehearsal(self) -> bool:
        """构建 + 启动一次，确认交付物起得来。**这是收尾里最后一件会花时间的事。**

        它原来不看表：唯一的闸门 `remaining() < -600` 是**构建失败之后**才问的，
        而且允许先超支 600s、再去开一个上限 `node_timeout`(2400s) 的修复回合。

        预算是 `remaining()`，平台红线在预算之外只留了 `node_timeout + tail`
        （见 `max_wall_seconds`）—— 那段留白是给**已经在飞的回合**和 `postflight()`
        的，彩排每多花一秒都在吃它。吃穿的代价不是「少跑一会儿」：`postflight()`
        在 `finally` 里，进程被平台在 22h 一刀杀掉就没机会跑，模型留在后台的服务器
        进程活到判分，四个 Chromium 在同一个内存笼子里 OOM，**整套隐藏测试被杀**
        （注释里记着的 746c81a2b5aa 就是这一次）。

        所以修复回合的时长按剩余时间削 —— 和 `final_acceptance` 里那句是同一个写法
        —— 而且剩余时间不够一次修复就不再开新的。构建 + 启动本身照跑：它便宜
        （分钟级）、而且「app 起不来」是唯一必须知道的结论。
        """
        for attempt in range(1, 4):
            log(f"[rehearsal] startup rehearsal {attempt}/3 (smoke port {self.smoke_port}, grader-like env)")
            server = self.app_server(grader_like=True)
            err = server.build() or server.start()
            server.stop()
            if err is None:
                log("[rehearsal] app builds and starts cleanly")
                return True
            log(f"[rehearsal] FAILED: {err.splitlines()[0][:200]}")
            if attempt == 3:
                log("[rehearsal] giving up; submitting as-is")
                return False
            if self.remaining() < self.min_repair_seconds:
                log(f"[rehearsal] giving up with {self.remaining():.0f}s left: not enough for a repair "
                    f"turn ({self.min_repair_seconds:.0f}s) plus the build/start that must follow it")
                return False
            # Capture and log, but deliberately DO NOT append a correction here.
            # REHEARSAL_REPAIR_PROMPT takes error/port/smoke only -- it has no
            # `corrections` placeholder, so it never drains the corrections channel.
            # An append would therefore miss the next rehearsal attempt entirely and
            # instead surface in whatever unrelated prompt drains the channel next,
            # telling that turn about a truncation that had nothing to do with it.
            # The loop re-measures with a real build/start either way, so the log line
            # is the whole value: it separates "the repair turn never finished" from
            # "the repair ran and the app still will not start".
            reh_prompt = REHEARSAL_REPAIR_PROMPT.format(error=clip_ends(err, 1200), port=self.web_port,
                                                        smoke=self.smoke_port)
            reh_ok, reh_text = self.turn(
                reh_prompt, min(self.node_timeout, max(120, self.remaining() - 200)),
                f"rehearsal repair {attempt}")
            if not reh_ok:
                log(f"[rehearsal] repair {attempt} did not complete ({str(reh_text)[-160:]}); "
                    f"the next attempt re-measures with a real build/start")
            elif truncation_correction(False, reh_text):
                log(f"[rehearsal] repair {attempt} hit the output limit; "
                    f"its edit may be partial and the next build will say so")
            self.commit("fix: startup rehearsal repair")
        return False

    def _practice_stop(self) -> bool:
        """出卷站的排练出口 —— 见 `practice_stage()`。

        走到这一句时卷子已经：落盘、挂到节点上（`spec_map`）、进保护名单、
        做过收集体检 —— 盘上的状态和正式跑到这里时**一模一样**。所以练出来的
        卷子是真的，只是后面的段落不跑。收尾走 `postflight()`（关驱动/代理/
        Playwright、收进程），和正常收工同一个动作。
        """
        if practice_stage() != "specs":
            return False
        log(f"[practice] OCTOS_ARC_PRACTICE_STAGE=specs: stopping after suite authoring — "
            f"{len(getattr(self, 'spec_map', None) or {})} node(s) with specs at {self.tests_dir}")
        self.events.mark_run_completed("practice run: stopped after suite authoring")
        self.postflight()
        return True

    def postflight(self) -> None:
        """Hand the box back before grading starts.

        The grader launches four Chromium workers in this cgroup the moment the
        harness returns. Cloud 746c81a2b5aa left 512 MiB to share: the suite the
        harness had just measured at 30/32 was killed four tests in and all 32
        scored as skipped. Servers a model turn left running are memory we can
        still give back, and reaping only between nodes leaves the last ones
        alive for exactly the run that cannot afford them.
        """
        if self.driver:
            self.driver.close()
        self.cleanup_playwright()
        self.stop_llm_proxy()
        strays = reap_workspace_processes(self.output_dir, log)
        if strays:
            log(f"[flow] reaped {strays} leftover process(es) before grading")

    # -- run --------------------------------------------------------------
    def run(self) -> int:
        self.runtime = AgentRuntime.from_env(project_dir=str(self.output_dir))
        self.events = self.runtime.events
        self.events.mark_run_started("octos bundle started")
        ordered: list[dict] = []
        watchdog_stop = threading.Event()
        try:
            previous = previous_requirement_records(self.output_dir)
            tree = load_requirement_tree(self.req_dir)
            self.runtime.traceability.store_requirement_tree(tree)
            # 需求树不再被压平成一张 DFS 的任务清单 —— 树的形状**就是**编排。
            # 深度 = 精度层级，非叶节点既产出契约（实现时先于子节点）又聚合验收
            # （验收时后于子节点）。`ordered` 现在只是「全部节点，文档序」，
            # 供下面那些与编排无关的账目（预算、指纹、事件上报、spec 归属）使用。
            self.layers = layerize(tree)
            ordered = [self.layers.by_id[n].node for n in self.layers.order]
            self.requirement_nodes = {str(node.get("id")): node for node in ordered}
            if not ordered:
                raise ValueError("no requirement nodes found")
            self.classify_tree(tree)
            node_ids = [str(n.get("id")) for n in ordered]
            self.budget = effective_budget(
                self.budget, explicit=self.budget_explicit, node_count=len(ordered),
                seconds_per_node=self.seconds_per_node, max_wall_seconds=self.max_wall_seconds,
                platform_wall_seconds=self.platform_wall_seconds)
            log(f"[flow] {len(ordered)} atomic nodes in dependency order: {node_ids}; time budget "
                f"{self.budget}s ({self.budget / 3600:.1f}h, ends at "
                f"{self.budget / max(1, len(ordered)):.0f}s/node)")
            self.folder_children = folder_descendants(tree)

            self.evolution = self.has_app()
            unchanged: set[str] = set()
            if self.evolution:
                unchanged = unchanged_node_ids(ordered, previous)
                log(f"[flow] evolution mode: existing app detected; unchanged nodes {sorted(unchanged)}, "
                    f"to implement {[i for i in node_ids if i not in unchanged]}")
            self.nodes_to_implement = len([n for n in node_ids if n not in unchanged])
            self.n_nodes = len(ordered)
            if not self.repair_rounds_explicit and self.n_nodes > 2:
                # 默认仍是 3，行为不变。做成可调是因为这条与上面那条「思维链判据」
                # 在同一个已验证运行里同时生效，归因是混淆的 —— 单独效果从未被
                # 隔离测量过。可调是为了能关掉它再做一次对照，不是先猜答案。
                self.repair_rounds = int(os.environ.get("OCTOS_ARC_REPAIR_ROUNDS_LARGE_TREE", "3"))
            if self.max_total_tokens < 0:
                # Raised from 2.5M/node to 8M/node. The token limb of this guard exists to
                # bound *cost*, and that is no longer a thing it can buy: the platform meters
                # its own access key, a submission that brings its own key records 0, and
                # `_cost_efficiency` returns None for cost <= 0 -- so spend has no effect on
                # the leaderboard at all. Meanwhile tripping it is expensive in the only
                # currency left: `wound_down()` shuts off every repair turn for the rest of
                # the run, including the checkpoint repairs that recovered 8 of submission A's
                # 32 keep nodes.
                #
                # And the old margin was razor thin, measured not guessed: A's keep finished
                # on 78,211,655 tokens against an 80,000,000 limit -- 97.8%, i.e. it did not
                # trip by 2.2%. Any run slightly less frugal than the best one we have would
                # have lost all of its repairs to a guard protecting nothing.
                #
                # Safe to raise because wall-clock is guarded separately and does not depend
                # on this number: every repair site already gates on `self.remaining()` /
                # `time_up()` against OCTOS_TIME_BUDGET (main.py 2467, 2826, 2891, 3087, 3138).
                # 8M/node is 10x the calibrated 0.8M/node and 3.3x A's measured 2.44M/node.
                self.max_total_tokens = max(6_000_000, 8_000_000 * self.n_nodes)
            if self.max_turns < 0:
                # Turns are left alone: unlike tokens, turn count is a proxy for wall-clock
                # rather than for money, and that is still a real constraint.
                self.max_turns = max(24, 4 * self.n_nodes)                          # ~3.5x the calibrated 1.1/node
            log(f"[guard] cost guard: {self.max_total_tokens} tokens / {self.max_turns} turns"
                + (f" / absolute {self.max_total_tokens_abs}" if self.max_total_tokens_abs else ""))

            self.tests_dir = locate_acceptance_tests(tree, BUNDLE_DIR)
            if self.tests_dir:
                specs = sorted(str(p.relative_to(self.tests_dir)) for p in self.tests_dir.rglob("*.spec.ts"))
                self.spec_map, self.aliases = map_specs_to_nodes(specs, node_ids)
                log(f"[tests] {len(specs)} spec files at {self.tests_dir}; mapping "
                    f"{ {k: v for k, v in self.spec_map.items() if v} }; aliases {self.aliases}")
            else:
                log("[tests] no acceptance specs found; building from requirement text only")

            self.maybe_probe(node_ids)
            self.runtime.git.ensure_repo()
            self.setup_playwright()
            # 官方卷子这条路：此刻卷子已经就位，探针当场就能跑。
            unchanged = self.probe_existing_app(node_ids, unchanged)

            dry_run = os.environ.get("OCTOS_ARC_DRYRUN") == "1"
            if dry_run:
                octos_bin = "(dry run: no kernel)"
                log("[octos] OCTOS_ARC_DRYRUN=1: fixed placeholder replies, no model calls")
            else:
                # 精简版：不再下载 59MB 的 Linux 内核二进制。内核只剩「把文本发给
                # 模型、把回文本交回来」这一件事，urllib 足够。工具能力由
                # ToolDriver 提供，不依赖那个二进制。
                octos_bin = "(local driver: no kernel binary)"
            log(f"[octos] {octos_bin}")
            data_dir = _temp_dir("octos-data-")
            protected = [p for p in (self.tests_dir, self.req_dir) if p and p.is_dir()]
            config_dir = _temp_dir("octos-config-")
            self.start_llm_proxy()
            env = build_octos_env(config_dir, protected)
            write_profile_defaults(data_dir, config_dir, protected_hooks(protected))
            self.snapshot_protected()
            env["PORT"] = str(self.smoke_port)  # a bare `npm start` inside a turn must not hit the grading port
            # 驱动选择。**默认走 v4 的 CCB 内核**（`ccb_driver.CcbDriver`）；
            # 两个显式退路留给 A/B 对照，三个驱动接口相同，调换不动其他任何东西：
            #     OCTOS_ARC_DRIVER=local → v2 的一次请求驱动
            #     OCTOS_ARC_DRIVER=tool  → v3 的 Python 工具档
            #
            # CcbDriver 的基类就是下面的 OctosDriver —— 它就是那个「起了子进程走
            # JSON-RPC」的实现，v3 里因为找不到 octos 二进制而是死代码，v4 正好
            # 把那个二进制换成我们自己的内核。所以回合循环 / 会话作用域 / 心跳
            # 包装 / 重试全都不用重写：它们走的是 profile 与协议，桥两者都说。
            #
            # ToolDriver **故意不继承 LocalDriver**：tool_tier_available() 的判据是
            # `not isinstance(driver, LocalDriver)`。旧的 DFS 节点循环里那段
            # 「local driver has no tool mode」的短路判也是同一个 isinstance，
            # 现在那段没了，但这层继承关系仍然要维持 —— 层级编排整个是工具模式，
            # 驱动一旦被认成 LocalDriver，tool_tier_available() 就报 False，
            # Orchestrator.run() 的启动守卫会当场喊出来。CcbDriver 同理。
            _driver_name = os.environ.get("OCTOS_ARC_DRIVER", "ccb")
            driver_class = {"local": LocalDriver, "tool": ToolDriver}.get(_driver_name)
            if driver_class is None:
                # 惰性 import：万一 ccb_driver.py 没打进包，`local` / `tool` 两个
                # 退路仍然可用（顶层 import 的话会一起炸）。
                from ccb_driver import make_driver
                driver_class = make_driver(OctosDriver)
            self.driver = DryRunDriver() if dry_run else driver_class(
                self.output_dir, env, data_dir, int(os.environ.get("OCTOS_MAX_ITERATIONS", "500")),
                events_log=self.output_dir / ".arc" / "octos-events.jsonl")
            self.driver.hooks = protected_hooks(protected)
            # 两个判据都要，因为这两种驱动的属性形态不同：
            #   ToolDriver —— `protected_dirs` 从不在 __init__ 里建，只有
            #                 `getattr(self, "protected_dirs", None)` 读，
            #                 所以 hasattr 是 False，只能靠 isinstance 认出来；
            #   CcbDriver  —— 预建了一个空 list，hasattr 是 True。
            # 漏掉任一边的后果是「受保护目录静默失效」——那种失败没有任何症状。
            if isinstance(self.driver, ToolDriver) or hasattr(self.driver, "protected_dirs"):
                # 受保护目录本来能从 hooks 里解析出来。再显式交一次手，是怕将来
                # hooks 的形状变了之后 _deny 静默退化成「没有受保护目录」。
                self.driver.protected_dirs = protected

            # 改造一/二：**没有官方卷子就自己出一份**（正赛的测试是隐藏的，
            # `/workspace/tests` 不存在，所以这条路在正赛上是常态而不是例外）。
            # 出完的卷子从这一刻起就是 `tests_dir`，和官方卷子走完全相同的下游：
            # `spec_map` → 每节点跑自己那份 → 失败进修复 → `final_acceptance`
            # 全站体检。没有这一段，正赛上每个叶子都是「没有 spec ∴ 通过」。
            #
            # 位置在驱动构造之后：出卷本身是一个模型回合，需要驱动；而在
            # `Orchestrator` 之前：蓝图就要读 `flow.tests_dir`。
            #
            # 出卷期间 `tests_dir` 仍是 None，所以 `protected` 里没有它 —— 模型
            # 必须能写这些文件。出完之后立刻把它补进保护名单并重拍快照，此后它
            # 和官方卷子一样只读：模型改不动（hook 拒绝写 + 每回合结束恢复）。
            if not self.tests_dir:
                authored = self.author_acceptance_suite(tree, node_ids)
                suite_dir = self.output_dir / AUTHOR_SUITE_DIR
                specs = sorted(str(p.relative_to(suite_dir)) for p in suite_dir.rglob("*.spec.ts")) \
                    if suite_dir.is_dir() else []
                if specs:
                    self.tests_dir = suite_dir.resolve()
                    # **这份卷子是我们自己写的。** 下游有一处依赖这个事实：
                    # 编排允许节点在修复若干轮无果之后主张「这份卷子判错了」并把它
                    # 作废（`_maybe_void_spec`）。官方卷子**不许**走那条路 ——
                    # 官方卷子是判分机在跑的另一份拷贝，本地作废它只是把自己弄瞎，
                    # 隐藏测试照样会跑、照样会判错。自产卷子才是我们自己的决定：
                    # 它写错了，该作废就作废，否则节点永远过不去，而 `_abandon_region`
                    # 会把整个区域一起判死。
                    self.authored_suite = True
                    # **不调用 `map_specs_to_nodes`。** 见 `authored_spec_map`：那条路
                    # 要解析文件名，而 id 一旦不是 `REQ-数字` 的形状，每一份卷子都会
                    # 解析失败并被丢掉 —— 于是每个节点名下 0 份卷子、每个节点自动通过，
                    # 而日志和报告都显示一切正常。自产卷子的归属是已知的，不要反推。
                    leaves = [i for i in layerize(tree).leaves if i in set(node_ids)]
                    adopted = adopt_suite_on_disk(authored, suite_dir, node_ids, leaves)
                    if not authored and adopted:
                        # 出卷一个回合都没开（接力开关，或预算闸门拦下）：盘上的
                        # 卷子来自上一轮，它是这个工作区里唯一的卷子，照用不误。
                        log(f"[tests] authoring returned no mapping; adopting "
                            f"{sum(len(v) for v in adopted.values())} spec file(s) already on disk")
                    self.spec_map, self.aliases = adopted, {}
                    covered, missing = authoring_coverage(adopted, leaves)
                    log(f"[tests] authored suite: {len(specs)} spec file(s) at {self.tests_dir}; "
                        f"{covered} node(s) have one, {len(missing)} do not")
                    if not covered:
                        # 文件写出来了、但一份都没挂到节点上。这不是「验收弱一点」，
                        # 这是**没有验收**：每个节点都会在 `own_ok = not specs` 下
                        # 直接判通过。必须喊出来 —— 这个失败形态本身没有任何症状。
                        log("[tests] **WARNING**: not one authored spec is mapped to a node. Every "
                            "node will be marked passed without running a single assertion. "
                            f"Spec files on disk: {specs[:5]}")
                    elif missing:
                        log(f"[tests] WARNING: {len(missing)} atomic node(s) have no spec and will be "
                            f"accepted on the baseline check alone: {missing[:8]}"
                            + (" …" if len(missing) > 8 else ""))
                    protected = [p for p in (self.tests_dir, self.req_dir) if p and p.is_dir()]
                    # 就地更新而不是重建：驱动拿的是**同一个 dict 的引用**
                    # （`OctosDriver.__init__` 里 `self.env = env`），重新赋值会让
                    # 驱动继续看着旧的那份，而 `config.json` 里的 hooks 已经变了。
                    env.update(build_octos_env(config_dir, protected))
                    # **必须重新钉一次。** `build_octos_env` 是从 `os.environ.copy()`
                    # 起步的，而平台上父进程带着 `PORT=<判分口>` 出生；上面那条
                    # `env.update(...)` 于是把 `env["PORT"]` 从冒烟口改回判分口，
                    # 把 2468 行那句「回合里裸跑 `npm start` 不能落到判分口上」的
                    # 保证原地撤销 —— 无声无息，因为两边的值都是合法的端口号。
                    env["PORT"] = str(self.smoke_port)
                    write_profile_defaults(data_dir, config_dir, protected_hooks(protected))
                    self.driver.hooks = protected_hooks(protected)
                    if isinstance(self.driver, ToolDriver) or hasattr(self.driver, "protected_dirs"):
                        self.driver.protected_dirs = protected
                    self.snapshot_protected()
                    self.setup_playwright()
                    self.smoke_check_authored_suite(specs)
                else:
                    # 一个 spec 都没写出来：退回改造之前的行为（没有验收，
                    # 每个叶子自动通过），而不是把 `tests_dir` 指到一个空目录
                    # —— 那会让 `setup_playwright()` 建出一个跑不出任何东西的
                    # runner，比没有更糟。
                    log("[tests] authoring produced no specs; falling back to no acceptance suite")

            # **名下有卷子、却一份都跑不起来 —— 这是正赛上最坏的一种半配置状态。**
            #
            # `setup_playwright()` 的三层兜底（机器自带的、全盘搜、自己装一个）可能全失败，
            # 那时它留下 `tests_dir` 有、`runner` 没有，并打一句「nodes will be judged by
            # the final check only」。**那句话在这个架构里是反的。**
            #
            # `_accept` 不会去问 runner 在不在：它拿到 `spec_map` 里挂着的卷子就去跑，
            # 跑的时候 `run_specs` 里 `self.runner.run(...)` 当场 AttributeError，被
            # `_run_specs` 的宽 except 吃掉，节点收到的是「specs could not run」—— 而这句
            # 话的处理是**不修复、不回滚、当场判负**。每个叶子都要跑自己那份卷子，于是
            # 二十几个叶子同一个死法，`_abandon_region` 再把整个区域连坐：一次 Playwright
            # 找不到 = 全树零分，而报告里翻半天只看得到「某个节点的卷子跑不起来」。
            #
            # 但「我们本地验不了」和「这个 app 不行」是两件事 —— 判分机跑隐藏测试用的是
            # **它自己的** Playwright。所以正确的降级是照样把 app 做出来交上去，而不是
            # 每一区都判死。这里退回和「出卷一份都没成」（上面那条 else）**完全相同**的
            # 那条路：空 `spec_map` = 没有验收机制。`spec_basis()` 会照实上报
            # 「Playwright was unavailable … never verified」，不会含糊成「卷子通过」。
            self.degrade_to_no_acceptance()

            # 排练开关（本地演练专用，平台从不设置）：出卷这一站已经完整走完
            # ——卷子注册、保护、收集体检都做过了——就按正常收工的方式收尾。
            if self._practice_stop():
                return 0

            # **探针的第二站。** 上面那一段（官方卷子或自产卷子）跑完之后，卷子
            # 才真正就位；而第一站（`setup_playwright()` 之后那次调用）在正赛上
            # 看到的是 `runner is None`，只能空转。没有这一次，模板场景下
            # `discard_template()` 永远不会触发 —— 一个空壳模板被当成正经地基，
            # 每个节点都在「在现有基础上扩展」，代价是 fresh build 的 40-80 倍。
            #
            # **#9 接盘归一 + 冷启动预检插在它前面**（自产/认回的卷子才走，官方卷
            # 路径不下水）：探针会拿整份卷子量现成的 app、把结果写进施工清单 ——
            # 接盘态要是起不来，先让预检把病治了（或至少把话喊出来），别让一趟
            # 「全树红」的探针结果污染蓝图的事实底料。编排对象在这里提前构造，
            # 下面的 `run()` 复用同一个（`pre_passed` 等探针结论出来再补）。
            self.orchestrator = Orchestrator(self)
            self.orchestrator.preflight()
            unchanged = self.probe_existing_app(node_ids, unchanged)

            threading.Thread(target=_port_watchdog, args=(self.web_port, self.output_dir, watchdog_stop),
                             daemon=True).start()
            try:
                # 编排整个换成了层级式 —— 见 ORCHESTRATION.md。
                # 旧的 `skeleton()` 门控和 `node_cycle` 节点循环都不在了：
                # 它们的世界观是 DFS（需求是任务清单，做完一个划掉一个），
                # 而这里的世界观是精度层级（每层都是完整 app，只是越来越细）。
                #
                # 编排自己管：L0 蓝图 → L1 骨架 → 逐层展开契约 → 叶子实现 → 聚合验收。
                # 它产出的 verdict 写回 `self.test_verdict`，下面的事件上报、
                # `mark_folders()`、postflight 一行都不用改 —— 那些是**报告层**，
                # 和怎么编排没关系。
                #
                # 对象在接盘站就构造好了（#9 预检要先用它），这里补上探针的结论。
                self.orchestrator.pre_passed = set(unchanged)
                verdicts = self.orchestrator.run(tree)
                for node_id, ok in verdicts.items():
                    self.test_verdict[node_id] = ok
                skipped = [i for i in node_ids if i not in verdicts]
                for node_id in skipped:
                    log(f"[flow] time budget exhausted; skipping {node_id}")
                    self.mark("implementation_started", node_id)
                    self.mark("implementation_failed", node_id, "skipped: time budget exhausted")
                    self.impl_failed.append(node_id)

                self.final_acceptance_passes()
                undecided = [i for i in node_ids if self.test_verdict.get(i) is None and i not in self.impl_failed]
                final_ok = None
                if undecided and not self.time_up():
                    log(f"[flow] final check turn for nodes without a local verdict: {undecided}")
                    final_ok, _ = self.turn(FINAL_CHECK_PROMPT.format(smoke=self.smoke_port, port=self.web_port,
                                                                      tests=self.tests_prompt_for(None),
                                                                      performance=self.perf_text(), ui=self.ui_contract()),
                                            self.node_timeout, "final check")
                    self.commit("chore: final verification pass")
                rehearsed = self.rehearsal()
                for node_id in undecided:
                    if rehearsed and final_ok is not False:
                        self.mark("test_passed", node_id, "final check and startup rehearsal passed")
                    else:
                        self.mark("test_failed", node_id, "final check or startup rehearsal failed")
                    self.test_verdict[node_id] = bool(rehearsed and final_ok is not False)
                # 封箱（保底三闸之三）：交付物钉在**整个 run 量到过的最好版本**上，
                # 而不是「盘上恰好剩下的版本」。自上次全卷测量后动过手就先量一次；
                # 当前树不配当交付物就退回成绩卡。必须跑在下面的逐节点 verdict 上报
                # **之前** —— 退回会重写 `test_verdict`，上报要按交付的那棵树来。
                # 封箱自己出任何问题都只记日志：它是保底，不是新的故障点。
                #
                # **位置在 `finally` 里的 `postflight()` 之前，这是要害**：
                # postflight 会把私有装的 Playwright 删掉（那是给评分器腾内存），
                # 而封箱的「先量一次」正要拿它跑全卷。v5.1 之前封箱写在 finally
                # 之后，于是「量一次」这条分支在真机上从来没跑成过 —— 每次都是
                # 「could not run」，脏树上永远直接退回旧快照，最后一段时间做出的
                # 改进**全部静默丢掉**（2026-10-06 本地冒烟实测：removed private
                # Playwright install → sealing: could not run (WinError 2)）。
                try:
                    self.seal_delivery()
                except Exception as exc:  # noqa: BLE001
                    log(f"[delivery] seal failed: {exc!r}")
            finally:
                watchdog_stop.set()
                self.postflight()
            for node_id in node_ids:  # final per-node verdicts (full-suite run may have changed them)
                if self.test_verdict.get(node_id) is True:
                    self.mark("test_passed", node_id, self.spec_basis(node_id, passed=True))
                elif self.test_verdict.get(node_id) is False:
                    self.mark("test_failed", node_id, self.spec_basis(node_id, passed=False))
            self.mark_folders()
            self.commit("chore: traceability and acceptance state")
            failed = [i for i in node_ids if self.test_verdict.get(i) is not True]
            # 「自家卷子全绿」不是唯一的完工口径：卷面机械体检留下的缺考注记
            # 跟着完成事件一起上报（见 `audit_authored_suite`）。没有体检
            # （官方卷子 / 出卷开关关闭 / 一份都没出成）时注记是空串，口径不变。
            audit_note = suite_audit_completion_note(getattr(self, "suite_audit", None))
            if failed:
                self.events.mark_run_completed(
                    f"completed; nodes not verified: {', '.join(failed)}{audit_note}")
            else:
                self.events.mark_run_completed(
                    "all requirement nodes implemented and verified" + audit_note)
            _reap_stray_processes("postflight", self.output_dir)
            _postflight_structure_check(self.output_dir)
            _free_web_port(self.web_port, self.output_dir)
            self.write_preview_ready()
            return 0
        except Exception as exc:  # the platform judges by events, not exit code
            log(f"[flow] aborted: {exc!r}")
            # The runner state event carries only the message; without the stack a
            # crash here is a one-line riddle in a 25-minute run. stdout only.
            traceback.print_exc()
            watchdog_stop.set()
            if self.driver:
                self.driver.close()
            self.cleanup_playwright()
            self.stop_llm_proxy()
            for node in ordered:
                node_id = str(node.get("id"))
                if node_id not in self.test_verdict:
                    self.mark("test_failed", node_id, f"run aborted: {str(exc)[:200]}")
            try:
                self.mark_folders()
            except Exception:  # noqa: BLE001
                pass
            _reap_stray_processes("exception", self.output_dir)
            _postflight_structure_check(self.output_dir)
            _free_web_port(self.web_port, self.output_dir)
            # 封箱的异常路径：崩溃也是结束的一种，交付物同样钉在成绩卡上。
            # 只退快照 + 提交（不量卷）—— 都崩了就别再花几分钟跑套件了，
            # 而且保持几秒级，不影响 `mark_run_failed` 的收尾。
            try:
                self.seal_delivery(allow_measure=False)
            except Exception as seal_exc:  # noqa: BLE001
                log(f"[delivery] seal failed: {seal_exc!r}")
            self.events.mark_run_failed(str(exc)[:1000])
            return 1 if isinstance(exc, PermanentProviderError) else 0

    def mark_folders(self) -> None:
        """The platform counts FOLDER nodes as requirements too ("45 requirements
        and 32 scenarios" for a 32-leaf tree); derive their state from their
        atomic descendants so the functional-rate denominator is covered."""
        for folder_id, leaves in self.folder_children.items():
            if not leaves:
                continue
            verdicts = [self.test_verdict.get(leaf) for leaf in leaves]
            self.events.mark_design_started(folder_id)
            self.events.mark_design_done(folder_id, f"{len(leaves)} atomic children designed")
            self.events.mark_implementation_started(folder_id)
            if all(v is not None for v in verdicts) or any(leaf in self.impl_failed for leaf in leaves):
                done = [leaf for leaf in leaves if leaf not in self.impl_failed]
                if done:
                    self.events.mark_implementation_done(folder_id, f"{len(done)}/{len(leaves)} atomic children implemented")
                else:
                    self.events.mark_implementation_failed(folder_id, "no atomic child implemented")
            if all(v is True for v in verdicts):
                self.events.mark_test_passed(folder_id, f"all {len(leaves)} atomic children pass")
            else:
                failing = [leaf for leaf, v in zip(leaves, verdicts) if v is not True]
                self.events.mark_test_failed(folder_id, f"children not verified: {', '.join(failing)}")

    def write_preview_ready(self) -> None:
        artifacts_dir = os.environ.get("ARCBENCH_ARTIFACTS_DIR")
        if artifacts_dir:
            try:
                Path(artifacts_dir).mkdir(parents=True, exist_ok=True)
                (Path(artifacts_dir) / "preview-ready.json").write_text(
                    json.dumps({"ready": True, "reason": "octos bundle completed"}) + "\n", encoding="utf-8")
            except OSError:
                pass


# ---------------------------------------------------------------- main

def minimal_probe_body(model: str) -> bytes:
    """Fallback chat probe that cannot bill reasoning: thinking disabled, one output token."""
    return json.dumps({"model": model, "messages": [{"role": "user", "content": "OK"}], "max_tokens": 1,
                       "thinking": {"type": "disabled"}}).encode()


def endpoint_is_up(status: int) -> bool:
    """Any non-5xx HTTP answer proves the endpoint is reachable (401/404 included)."""
    return status < 500


def probe_endpoint() -> None:
    """Wait out endpoint/proxy outages (up to 10 min) without spending tokens:
    GET /models first (unbilled; any non-5xx answer = up). Only if that never
    answers, one chat request with thinking disabled and max_tokens=1.
    The old probe ("Reply with exactly: OK", max_tokens=4) let the model reason
    before its 4-token answer — about ¥0.0008 per run, a third of a Smoke task."""
    key = os.environ.get("OPENAI_API_KEY", "")
    base = os.environ.get("OPENAI_BASE_URL")
    if not (key and base):
        return
    import urllib.request as _ur
    import urllib.error as _ue
    from llm_proxy import open_upstream  # loopback must bypass the system proxy (6f41b052)
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    deadline = time.time() + 600
    attempt = 0
    while True:
        attempt += 1
        try:
            with open_upstream(_ur.Request(base.rstrip("/") + "/models", headers=headers), 30, base) as resp:
                log(f"[probe] GET /models -> HTTP {resp.status} (endpoint up, no tokens spent)")
                return
        except _ue.HTTPError as exc:
            if endpoint_is_up(exc.code):
                log(f"[probe] GET /models -> HTTP {exc.code} (endpoint up, no tokens spent)")
                return
            log(f"[probe] attempt {attempt}: GET /models -> HTTP {exc.code}")
        except Exception as exc:  # noqa: BLE001
            log(f"[probe] attempt {attempt}: GET /models -> {exc}")
            # Some gateways expose only chat/completions: one minimal, reasoning-free request.
            try:
                req = _ur.Request(base.rstrip("/") + "/chat/completions", headers=headers, method="POST",
                                  data=minimal_probe_body(os.environ.get("MODEL", "deepseek-chat")))
                with open_upstream(req, 60, base) as resp:
                    log(f"[probe] minimal chat probe -> HTTP {resp.status}")
                    return
            except _ue.HTTPError as exc2:
                if endpoint_is_up(exc2.code):
                    log(f"[probe] minimal chat probe -> HTTP {exc2.code} (endpoint up)")
                    return
                log(f"[probe] attempt {attempt}: chat -> HTTP {exc2.code}")
            except Exception as exc2:  # noqa: BLE001
                log(f"[probe] attempt {attempt}: chat -> {exc2}")
        if time.time() >= deadline:
            log("[probe] endpoint still failing after 10min; proceeding anyway")
            return
        time.sleep(30)


def load_project_env() -> None:
    """把 ARC 源目录旁的 .env 读进环境变量，已有的一律不覆盖。

    官方 harness 就是这么拿配置的：`core/workflow.py` 一导入就调 `load_project_env()`，
    读的是 `Path.cwd()/.env`；Lab04 也写明「插件会在运行 ARC 前，将相关配置同步到 ARC
    源目录的 .env 文件」。我们本地一直是 run-local.py 把变量直接喂进子进程，所以这条
    路从没被走到过 —— 但判分机上 main.py 是被直接拉起来的，没有 run-local.py 这层。

    顺序上环境变量优先（`k not in os.environ` 才写），平台真给了就一个字节也改不动。
    键名两套写法都认：官方的 OPENAI_* 与 Lab02 `.env.example` 的 api_key/base_url/model。
    """
    pairs = (("api_key", "OPENAI_API_KEY"), ("OPENAI_KEY", "OPENAI_API_KEY"),
             ("base_url", "OPENAI_BASE_URL"), ("OPENAI_API_BASE", "OPENAI_BASE_URL"),
             ("model", "MODEL"), ("api_mode", "ARC_OPENAI_API_MODE"))
    seen = set()
    for cand in (Path(__file__).resolve().parent / ".env", Path.cwd() / ".env"):
        if cand in seen or not cand.is_file():
            continue
        seen.add(cand)
        for raw in cand.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            name, value = name.strip(), value.strip().strip('"').strip("'")
            if name and value and name not in os.environ:
                os.environ[name] = value
    for src, dst in pairs:
        if os.environ.get(src) and not os.environ.get(dst):
            os.environ[dst] = os.environ[src]


def main() -> int:
    load_project_env()
    _sweep_temp()  # 上一次没走到退出留下的残骸，趁这一轮还没开始先收掉
    parser = argparse.ArgumentParser(description="Octos agent bundle for ARC-Bench")
    parser.add_argument("requirement_path", nargs="?", default=os.environ.get("ARCBENCH_TASK_DIR", "/workspace/task"))
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--type", "--app-type", dest="app_type", default="web")
    parser.add_argument("--web-port", type=int,
                        default=int(os.environ.get("ARCBENCH_WEB_PORT", os.environ.get("ARC_WEB_PORT", "3000"))))
    args = parser.parse_args()

    key = os.environ.get("OPENAI_API_KEY", "")
    print(f"[env] OPENAI_BASE_URL={os.environ.get('OPENAI_BASE_URL', '<unset>')}", flush=True)
    print(f"[env] MODEL={os.environ.get('MODEL', '<unset>')}", flush=True)
    print(f"[env] OPENAI_API_KEY={'set(len=%d)' % len(key) if key else '<unset>'}", flush=True)
    print(f"[env] ARCBENCH_TEMPLATE_DIR={os.environ.get('ARCBENCH_TEMPLATE_DIR', '<unset>')}", flush=True)
    print(f"[env] ARCBENCH_TASK_DIR={os.environ.get('ARCBENCH_TASK_DIR', '<unset>')}", flush=True)
    print(f"[env] argv requirement_path={args.requirement_path}", flush=True)
    req_src = Path(args.requirement_path).resolve()
    if args.output_dir:
        output_dir = Path(args.output_dir).resolve()
    elif os.environ.get("ARCBENCH_TEMPLATE_DIR"):
        output_dir = Path(os.environ["ARCBENCH_TEMPLATE_DIR"]).resolve()
    else:
        output_dir = Path.cwd() / "workspace" / f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    output_dir.mkdir(parents=True, exist_ok=True)

    on_platform = bool(os.environ.get("ARCBENCH_TEMPLATE_DIR"))
    if on_platform:
        req_dir = req_src
    else:
        req_dir = output_dir / "requirements"
        if req_dir.exists():
            shutil.rmtree(req_dir)
        shutil.copytree(req_src, req_dir)
    return Flow(args, output_dir, req_dir).run()


if __name__ == "__main__":
    sys.exit(main())
