"""Local acceptance testing for the ARC adapter.

Runs the public (or container-provided) Playwright specs that belong to one
requirement node against the generated app, and turns failures into the
compact four-field summary (Feature / Failed at / Observation / Steps) fed back
to the model. Spec discovery, spec->node mapping and report parsing are pure
functions covered by arc/tests/test_acceptance.py; process handling lives in
`AcceptanceRunner`.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# 需求 id 有两种写法：老任务是点分（`REQ-1.2.3`），现在的任务用短横线
# （`REQ-1-2-1`）。原先只认点分，于是 `REQ-1-2-1.spec.ts` 被解析成 `REQ-1` ——
# 叶子节点的 spec 全部挂到根节点上，每个节点拿到的都是别人的 spec。
# 两种都要认，且短横线那种要吃到最深（`REQ-1-2-1` 而不是 `REQ-1`）。
_SPEC_ID = re.compile(r"^(REQ-\d+(?:[.\-]\d+)*)(?=[.\-_ ]|$)")


def npm_exe() -> str:
    """npm 的可执行名。

    Windows 上 npm 是 `npm.cmd`，裸名 "npm" 只有经过 shell 才找得到 ——
    直接交给 subprocess 会得到 [WinError 2]。Linux 上 which 命中的就是 npm
    本身，所以线上行为与改动前逐字一致。
    """
    return shutil.which("npm") or "npm"


def local_bin(name: str, bin_dir: Path) -> str:
    """node_modules/.bin 里的可执行文件（Windows 上带 .cmd/.ps1 后缀）。"""
    found = shutil.which(name, path=str(bin_dir))
    if found:
        return found
    if sys.platform == "win32":
        # which 靠 PATHEXT 找；它没命中时剩下的无后缀文件是给 sh 用的脚本，
        # Windows 直接执行会 [WinError 193]。显式再试一遍后缀。
        for ext in (".cmd", ".exe", ".bat"):
            candidate = bin_dir / (name + ext)
            if candidate.is_file():
                return str(candidate)
    return str(bin_dir / name)


_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def kill_process_group(proc: subprocess.Popen) -> None:
    """整组杀掉后台起来的应用进程。

    Linux 上走 killpg(getpgid(pid), SIGKILL)：应用是 start_new_session=True 起的，
    不整组杀的话它的 node 子进程会留着占住端口，下一轮就起不来了。
    Windows 既没有 os.killpg 也没有 SIGKILL，退化成杀那一个进程 —— 本地够用。
    判分机走的仍是原来那条 killpg 路径，行为逐字不变。
    """
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        try:
            killpg(os.getpgid(proc.pid), _SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            return
    # Windows：proc.kill() 只杀 npm 那一层，它拉起来的 node server.js 会留下
    # 变成孤儿占着端口 —— 上一轮就是这么攒下两个进程的。taskkill /T 才是
    # 这里要的「连子树一起杀」。
    try:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, timeout=15)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    proc.kill()


def windows_copy_build(frontend: Path) -> bool:
    """Windows 专用：`npm run build` 跑不动时，代它做同一件事。

    生成的应用的 build 是一条 `node -e "..."`，内容是「把 src 原样拷成 dist」。
    在判分机（Linux）上 npm 交给 sh 执行，这是参考实现原样跑通的路径，没有问题。
    在 Windows 上 npm 改用 cmd.exe，而 cmd 在那对引号上直接崩掉（实测 rc=3221226505、
    零输出）；改派 Git 的 sh 也过不去 cmd→sh 的引号边界（实测 rc=127）。

    所以这里只在 Windows 上补一次等价动作，让本地能跑出真实判分。判分机永远
    走不到这个分支（非 win32 直接返回 False），线上行为逐字不变。

    只认那一条拷贝命令：build 一旦是别的东西就返回 False，让真正的构建失败照常报错。
    """
    if sys.platform != "win32":
        return False
    src, dist = frontend / "src", frontend / "dist"
    try:
        script = (json.loads((frontend / "package.json").read_text(encoding="utf-8", errors="replace")).get("scripts") or {}).get("build") or ""
    except (OSError, ValueError):
        return False
    if "cpSync('src'" not in script.replace('"', "'") or not src.is_dir():
        return False
    try:
        shutil.rmtree(dist, ignore_errors=True)
        shutil.copytree(src, dist)
    except OSError:
        return False
    return True


def npm_script_shell_env() -> dict:
    """Windows 专用：让 npm 用 Git 的 sh 跑 package.json 里的脚本。

    判分机是 Linux，npm 一直在用 /bin/sh 跑脚本；模型写的 build/start 也都是那套
    写法（run7 实测生成了 `mkdir -p dist && cp src/*.html dist/`）。Windows 上 npm
    改用 cmd.exe，这类脚本当场就废 —— 于是本地报出一个判分机上根本不会发生的
    构建失败，自修复轮还会拿它当真，花一整轮去"修"一个不存在的问题。

    把 script-shell 指到 Git 的 sh，脚本就按它被写出来的样子跑。判分机怎么跑，
    本地就怎么跑：**在 Windows 的 sh 下跑不过的脚本，在 Linux 上也跑不过**，
    本地因此不会比判分机更宽松、也不会更严。

    非 win32 返回空字典，线上行为逐字不变。
    """
    if sys.platform != "win32":
        return {}
    for cand in (r"C:\Program Files\Git\bin\sh.exe",
                 r"C:\Program Files\Git\usr\bin\sh.exe",
                 r"C:\Program Files (x86)\Git\bin\sh.exe",
                 shutil.which("sh")):
        if cand and Path(cand).is_file():
            return {"npm_config_script_shell": cand}
    return {}


def spec_node_id(rel_path: str) -> str | None:
    """`REQ-1.2-user-login.spec.ts` -> `REQ-1.2`; non-spec files -> None."""
    name = Path(rel_path).name
    if not name.endswith(".spec.ts"):
        return None
    m = _SPEC_ID.match(name)
    return m.group(1) if m else None


def _version_key(req_id: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", req_id))


def map_specs_to_nodes(spec_paths: list[str], node_ids: list[str]) -> tuple[dict, dict]:
    """Assign spec files to requirement nodes.

    Returns (mapping, aliases): mapping[node_id] -> [spec paths] with the key
    None holding specs that belong to no single node (regression set);
    aliases[spec_id] -> node_id for spec ids that are not literal node ids.

    Order of preference: literal id match; when the remaining distinct spec
    ids and the remaining nodes have the same count, pair them in numeric /
    document order; otherwise attach `REQ-1.x` to an existing `REQ-1` parent.
    """
    mapping: dict = {nid: [] for nid in node_ids}
    mapping[None] = []
    aliases: dict[str, str] = {}
    by_spec_id: dict[str, list[str]] = {}
    for path in spec_paths:
        sid = spec_node_id(path)
        if sid is None:
            continue
        by_spec_id.setdefault(sid, []).append(path)
    unmatched_ids = []
    for sid in sorted(by_spec_id, key=_version_key):
        if sid in mapping:
            mapping[sid].extend(by_spec_id[sid])
        else:
            unmatched_ids.append(sid)
    free_nodes = [nid for nid in node_ids if not mapping[nid]]
    if unmatched_ids and len(unmatched_ids) == len(free_nodes):
        for sid, nid in zip(unmatched_ids, free_nodes):
            mapping[nid].extend(by_spec_id[sid])
            aliases[sid] = nid
        return mapping, aliases
    for sid in unmatched_ids:
        parent = sid
        target = None
        # 往上找祖先要按 id **自己**的分隔符走。混着走会让 `REQ-1.2` 在
        # 短横线树上匹配到一个不该匹配的 `REQ-1`（反过来也成立）。
        sep = "." if "." in sid else "-"
        while sep in parent:
            parent = parent.rsplit(sep, 1)[0]
            if parent in mapping:
                target = parent
                break
        if target is None:
            mapping[None].extend(by_spec_id[sid])
        else:
            mapping[target].extend(by_spec_id[sid])
            aliases[sid] = target
    return mapping, aliases


@dataclass
class TestOutcome:
    title: str
    ok: bool
    status: str
    duration_ms: int
    file: str = ""          # the SPEC file the test lives in (node ownership)
    line: int | None = None
    location: str = ""      # where the error was raised (may be a helper file)
    message: str = ""
    steps: list[str] = field(default_factory=list)
    action_errors: list[str] = field(default_factory=list)
    context_path: str = ""  # Playwright's error-context.md for this failure
    rendered_page: str = ""  # its accessibility snapshot, filled in by the runner


@dataclass
class RunSummary:
    passed: int = 0
    total: int = 0
    results: list[TestOutcome] = field(default_factory=list)
    stdout_tail: str = ""
    error: str | None = None  # infrastructure error (no report)
    killed: bool = False      # the test runner itself was killed (OOM); not a verdict
    # 构建/启动失败：`error` 说的是「应用起不来」，不是「卷子红」。带上这个标志，
    # 调用方才能把「环境病」与「实现病」分开处置（#5a 就地抢修 / #5b 三分类）——
    # 这是**性质**不是**严重程度**：同样一个 error，含义可以差着两条修复路径。
    startup_error: bool = False
    load_errors: list[str] = field(default_factory=list)  # Playwright top-level errors
    stores_written: list[str] = field(default_factory=list)  # files this run left changed on disk

    def slow(self, threshold_ms: int) -> list[str]:
        return [r.title for r in self.results if r.duration_ms >= threshold_ms]

    @property
    def all_passed(self) -> bool:
        return self.total > 0 and self.passed == self.total


def summarize_report(report: dict) -> RunSummary:
    """Collapse a Playwright JSON report into per-test outcomes."""
    summary = RunSummary()

    def walk(suites, parent_file=""):
        for suite in suites or []:
            file = suite.get("file") or parent_file
            for spec in suite.get("specs", []):
                tests = spec.get("tests", [])
                results = [r for t in tests for r in t.get("results", [])]
                last = results[-1] if results else {}
                ok = bool(tests) and all(t.get("status") == "expected" or t.get("ok") for t in tests)
                errors = [e for e in [last.get("error"), *(last.get("errors") or [])] if isinstance(e, dict)]
                # A test-level timeout may precede the actionable locator error.
                # Prefer its call log and source location over the generic deadline.
                err = max(errors, key=lambda e: (
                    "Call log:" in str(e.get("message", "")),
                    "Locator:" in str(e.get("message", "")),
                    bool(e.get("location"))), default={})
                loc = err.get("location") or {}
                steps = [s.get("title", "") for s in last.get("steps", []) if s.get("title")]
                loc_file = Path(loc.get("file") or "").name
                # keep-local-4: failures raised inside support/e2e.ts were
                # attributed to the helper, so no node owned them.
                summary.results.append(TestOutcome(
                    title=spec.get("title", "?"), ok=ok, status=last.get("status", "unknown"),
                    duration_ms=int(sum(r.get("duration", 0) for r in results)),
                    file=Path(spec.get("file") or file or loc_file or "").name,
                    line=loc.get("line"),
                    location=f"{loc_file}:{loc.get('line')}" if loc_file and loc.get("line") else loc_file,
                    message=_ANSI.sub("", str(err.get("message") or "") + "\n" + "\n".join(
                        line for line in str(err.get("stack") or "").splitlines() if line.strip().startswith("at "))).strip(),
                    steps=steps, action_errors=[_ANSI.sub("", e)[:2000] for e in
                        (report.get("action_errors", {}).get(spec.get("id"), []) or [])[:8] if isinstance(e, str)],
                    context_path=next((str(a.get("path") or "") for a in (last.get("attachments") or [])
                                       if isinstance(a, dict) and a.get("name") == "error-context"), "")))
            walk(suite.get("suites", []), file)

    walk(report.get("suites", []))
    summary.load_errors = [_ANSI.sub("", str(e.get("message") or e))[:600] for e in report.get("errors") or []]
    summary.total = len(summary.results)
    summary.passed = sum(1 for r in summary.results if r.ok)
    return summary


def nodes_for_failures(results: list[TestOutcome], spec_map: dict) -> dict[str, list[TestOutcome]]:
    """Group failed outcomes by the requirement node that owns their spec file
    (matched on the spec file's basename); unmapped files land under None."""
    owner: dict[str, object] = {}
    for node_id, paths in spec_map.items():
        for path in paths or []:
            owner[Path(path).name] = node_id
    grouped: dict = {}
    for r in results:
        if r.ok:
            continue
        grouped.setdefault(owner.get(Path(r.file or "").name), []).append(r)
    return grouped


def _call_log_steps(message: str) -> list[str]:
    """Playwright's `Call log:` lines are the closest thing to a step trace
    when the spec has no test.step() blocks."""
    steps = []
    seen = False
    for line in message.splitlines():
        if line.strip().lower().startswith("call log"):
            seen = True
            continue
        if seen:
            stripped = line.strip().lstrip("-").strip()
            if not stripped:
                break
            steps.append(stripped[:120])
    return steps


# The snapshot sits under a `# Page snapshot` heading after an action failure and
# inline in `# Error details` after a failed expect(); it is the only YAML in the
# document either way (the spec source is fenced as ```ts).
_PAGE_SNAPSHOT = re.compile(r"(?m)^```yaml\n(.*?)\n```", re.S)


def page_snapshot(error_context: str, max_chars: int = 4000) -> str:
    """The accessibility tree of the page as it stood when the test failed.

    Playwright writes `test-results/<test>/error-context.md` for every failure
    and records the rendered page in it. Without that a repair only sees the
    locator the test waited for and has to guess which roles and accessible
    names the app actually produced. Only the snapshot is kept: the error and
    the spec source are already summarised elsewhere.
    """
    match = _PAGE_SNAPSHOT.search(error_context)
    if not match:
        return ""
    return _clip_lines(match.group(1), max_chars)


def clip_ends(text: str, max_chars: int) -> str:
    """Keep both ends of a tool log.

    npm and the bundlers print the cause first and then a wall of exit
    boilerplate, so a tail-only excerpt of a failed build can be nothing but log
    paths — the line naming the missing module is the first thing dropped.
    """
    text = text.strip()
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3
    elided = len(text) - max_chars
    return f"{text[:head]}\n… {elided} characters elided …\n{text[head - max_chars:]}"


def _clip_lines(text: str, max_chars: int) -> str:
    """Keep whole lines only: a half-line of YAML reads as a different tree."""
    kept: list[str] = []
    budget = max_chars
    for line in text.splitlines():
        if len(line) + 1 > budget:
            kept.append("… snapshot truncated")
            break
        kept.append(line)
        budget -= len(line) + 1
    return "\n".join(kept).strip()


_STARTUP_NOISE = ("Failed to load the ES module", "--trace-warnings", "npm notice")
_STARTUP_ERROR_MARKERS = ("SyntaxError", "ReferenceError", "TypeError", "RangeError",
                          "Cannot find module", "MODULE_NOT_FOUND", "EADDRINUSE",
                          "error TS", "Error:", "throw ")


def startup_error_digest(text: str, limit: int) -> str:
    """The slice of a build/start failure that actually names the cause.

    When a CommonJS file fails to parse, Node prints `Warning: Failed to load the ES
    module <abs path>. Make sure to set "type": "module" ...` *before* the real error,
    and `npm start` adds its own two-line preamble. The warning is a wrong lead --
    setting "type": "module" would break the require() calls the harness pins -- and
    with an absolute path it spends roughly 220 of a 600-character observation budget
    that is meant for the stack.

    Measured on the local TB REQ-1 round-0 capture (2026-09-17, cause `Identifier
    '__dirname' has already been declared`): the error line sat at character 530, so a
    600-character head slice did still reach it -- this is not a fix for a lost error.
    What it does is drop the misleading lead and hand the whole budget to the stack,
    which also leaves room when the path is longer or npm prints more first.

    So: drop the lines known to mislead, start at the first line that names an error,
    and fall back to the tail (where a stack trace ends up) when nothing matches.
    """
    lines = [l for l in text.splitlines() if not any(n in l for n in _STARTUP_NOISE)]
    header = lines[0] if lines and ("exited early" in lines[0] or "could not launch" in lines[0]) else None
    body = lines[1:] if header else lines
    start = next((i for i, l in enumerate(body) if any(m in l for m in _STARTUP_ERROR_MARKERS)), None)
    # Budget the header first: clipping the joined result at the end would slice from
    # the front and throw away the very tail this falls back to.
    room = limit - (len(header) + 1) if header else limit
    if room <= 0:
        return (header or "")[:limit]
    kept = "\n".join(body[start:] if start is not None else body).strip()
    kept = kept[:room] if start is not None else kept[-room:]
    return f"{header}\n{kept}".strip() if header else kept


def failure_summaries(summary: RunSummary, max_steps: int = 8, max_observation: int = 900,
                      max_snapshots: int = 18000, files: set[str] | None = None) -> str:
    """Four-field digest of every failed test — the only thing the model sees.

    `files` (basenames) narrows the digest to one repair batch / scope: a prompt
    that asks for 1-3 spec files must not carry the failure detail of the other
    twenty, or the model repairs the whole suite's worth anyway and the batch
    granularity exists only in the harness's bookkeeping.
    """
    results = summary.results if not files else [r for r in summary.results
                                                 if Path(r.file or "").name in files]
    blocks = []
    # A full suite can fail on many nodes at once; share the snapshot budget so a
    # long first tree cannot crowd the later failures out of the repair prompt.
    # The share has to clear the page chrome — header, sidebar, banner run over a
    # thousand characters before the content the test was actually looking at.
    failing = sum(1 for r in results if not r.ok) or 1
    # A share below the page chrome -- header, sidebar, banner run over a thousand
    # characters before the content the test looked at -- shows none of what the
    # test could see, so the share has a floor. Above roughly twenty failures the
    # floor wins every time and `max_snapshots` stops bounding anything: a
    # 125-spec suite failing wholesale produced 100000 characters of trees under
    # an 18000 budget, and a 236000-character prompt. That prompt still fits the
    # model -- deepseek-v4-flash carries a 1048576 token window, and 236000
    # characters is about 59000 -- so this bounds cost and noise, not a context
    # overflow; do not reintroduce the floor believing the prompt would be
    # rejected. Keep the floor, and spend it
    # on as many failures as the budget really covers; the rest still report their
    # feature, location, observation and steps, which is what names the cause.
    per_snapshot = max(800, max_snapshots // failing)
    snapshots_left = max(1, max_snapshots // per_snapshot)
    for r in results:
        if r.ok:
            continue
        observation = r.message.strip() or f"status {r.status}"
        if r.status == "timedOut" or "timeout" in observation.lower()[:120]:
            observation = (f"TIMED OUT after {r.duration_ms} ms. Inspect the failed operation below; "
                           f"a missing or hidden element can also cause a timeout. " + observation)
        observation = observation[:max_observation]
        where = r.location or r.file or "?"
        if r.location and r.file and not r.location.startswith(r.file):
            where = f"{r.location} (called from {r.file})"
        steps_src = r.steps or _call_log_steps(r.message)
        steps = " -> ".join(steps_src[-max_steps:]) if steps_src else "(no step trace)"
        blocks.append(f"- Feature: {r.title}\n  Failed at: {where}\n  Observation: {observation}\n  Steps: {steps}")
        if r.rendered_page and snapshots_left > 0:
            snapshots_left -= 1
            indented = "\n".join("    " + line for line in _clip_lines(r.rendered_page, per_snapshot).splitlines())
            blocks[-1] += ("\n  Page at failure (what the app actually rendered; roles and accessible "
                           "names the test could see):\n" + indented)
        if r.action_errors:
            blocks[-1] += "\n  Browser diagnostics (helpers may have recovered; correlate with the final failure):\n" + "\n".join(r.action_errors)[:4000]
    return "\n".join(blocks)


def failure_source_context(summary: RunSummary, tests_dir: Path | None, max_chars: int = 4000,
                           want: set[str] | None = None) -> str:
    """Quote bounded, read-only source around actual failure lines; never guess ambiguous files.

    `want` (basenames) narrows the quoted failures to one batch / scope, same as
    `failure_summaries`.
    """
    if not tests_dir or max_chars <= 0:
        return ""
    root = tests_dir.resolve()
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git") and not (Path(directory)/d).is_symlink()]
        files.extend(Path(directory)/n for n in names if n.endswith('.ts'))
    blocks, seen = [], set()
    locations = []
    for result in summary.results:
        if want and Path(result.file or "").name not in want:
            continue
        locations.append(result)
        for match in re.finditer(r"(?m)^\s*at (?:[^ (][^(]*\()?([^\n()]+\.ts):(\d+):\d+\)?\s*$", result.message):
            locations.append(replace(result, location=f"{match[1]}:{match[2]}", line=int(match[2])))
    for r in locations:
        if r.ok or not r.line or r.line < 1:
            continue
        name = (r.location.rsplit(':', 1)[0] if r.location else r.file)
        candidates = [p for p in files if p.name == Path(name).name]
        if len(candidates) != 1:
            continue
        path = candidates[0]
        if not path.resolve().is_relative_to(root) or (path, r.line) in seen:
            continue
        seen.add((path, r.line))
        try:
            if path.stat().st_size > 1_000_000:
                continue
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeError):
            continue
        if r.line > len(lines):
            continue
        excerpt = '\n'.join(f"{'>' if n+1 == r.line else ' '} {n+1}: {lines[n][:400]}" for n in range(max(0, r.line-5), min(len(lines), r.line+4)))
        blocks.append(f"Read-only failure source: {path.relative_to(root)}\n{excerpt}")
    return ('\n\n' + '\n\n'.join(blocks))[:max_chars] if blocks else ''


def failure_signature(summary: RunSummary, unstable: frozenset[str] = frozenset()) -> frozenset[tuple[str, ...]]:
    """Detect a stalled repair by the observation, not just the test name.

    Ignore elapsed milliseconds and retry counts, which vary without a code
    change, but retain locator text and source location to detect progress.

    `unstable` names spec files already seen to pass and fail within the same
    pass. Cloud e767e871a6c6 spent four full-suite rounds on eleven failures
    that never moved, because a twelfth flaked in and out and made every round
    look different from the one before it, so the stall was never noticed.
    """
    def normalize(text: str) -> str:
        text = _ANSI.sub("", text)
        text = re.sub(r"\b\d+(?:\.\d+)?\s*ms\b", "<time>", text)
        text = re.sub(r"\b\d+\s*×", "<repeats>", text)
        return " ".join(text.split())

    return frozenset((r.file, r.title, r.status, r.location,
                      normalize(r.message), normalize(" | ".join(r.steps)))
                     for r in summary.results
                     if not r.ok and Path(r.file or "").name not in unstable)


# ---------------------------------------------------------------- processes

def find_playwright_root(candidates: list[Path]) -> Path | None:
    """First directory that has @playwright/test installed."""
    for cand in candidates:
        if cand and (cand / "node_modules" / "@playwright" / "test").is_dir():
            return cand.resolve()
    return None


def acceptance_work_dir(root: Path) -> Path:
    """A writable scratch dir under the Playwright root (module resolution),
    falling back to the system temp dir."""
    preferred = root / ".octos-acceptance" / f"run-{os.getpid()}"
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        return Path(tempfile.mkdtemp(prefix="octos-acceptance-")) / "run"


def playwright_candidates(bundle_dir: Path, tests_dir: Path | None, output_dir: Path) -> list[Path]:
    """Places an existing @playwright/test may live, most specific first. The
    runner image ships one ("Using preinstalled Playwright package from runner
    image"); finding it matters because installing our own must never change
    what the platform's own `npx playwright test` resolves later."""
    cands: list[Path] = []
    env_root = os.environ.get("OCTOS_ARC_PLAYWRIGHT_ROOT")
    if env_root:
        cands.append(Path(env_root))
    cands.append(bundle_dir / "local-grader")
    if tests_dir:
        cands.extend([tests_dir, *tests_dir.parents][:4])
    cands.extend([output_dir, Path("/workspace"), Path("/workspace/tests"), Path("/app"), Path("/runner"),
                  Path("/opt/playwright"), Path("/ms-playwright"), Path.home()])
    try:  # global npm root: /usr/local/lib/node_modules -> parent holds node_modules/
        root = subprocess.run([npm_exe(), "root", "-g"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20).stdout.strip()
        if root:
            cands.append(Path(root).parent)
    except (OSError, subprocess.TimeoutExpired):
        pass
    cands.extend([Path("/usr/local/lib"), Path("/usr/lib"), Path("/usr/local"), Path("/opt")])
    return cands


def find_playwright_by_search(log: Callable[[str], None], max_depth: int = 6, timeout: int = 25) -> Path | None:
    """Bounded filesystem search for a preinstalled @playwright/test package."""
    cmd = ["find", "/", "-maxdepth", str(max_depth), "-type", "d", "-path", "*/node_modules/@playwright/test",
           "-not", "-path", "/proc/*", "-not", "-path", "/sys/*", "-not", "-path", "/tmp/*"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return None
    for hit in sorted(out, key=len):
        root = Path(hit).parents[2]  # <root>/node_modules/@playwright/test
        if (root / "node_modules" / "@playwright" / "test" / "package.json").is_file():
            log(f"[acceptance] found preinstalled Playwright at {root}")
            return root
    return None


def playwright_version_hint(tests_dir: Path | None, fallback: str = "1.63.0") -> str:
    """Version to install when we must: the one the tests declare, else a pin.
    Never `latest` — an unpinned install is what broke cloud grading."""
    for base in ([tests_dir, *tests_dir.parents][:3] if tests_dir else []):
        for name in ("package-lock.json", "package.json"):
            path = base / name
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            pkg = (data.get("packages") or {}).get("node_modules/@playwright/test") or {}
            version = pkg.get("version")
            if not version:
                for section in ("devDependencies", "dependencies"):
                    version = (data.get(section) or {}).get("@playwright/test")
                    if version:
                        break
            if version:
                return str(version).lstrip("^~=v")
    return fallback


def isolated_install_env(private_root: Path) -> dict:
    """Environment for a self-contained Playwright install: private npm cache
    and browser directory, mirrors for the runner network. Nothing outside
    `private_root` is written, so the platform's own Playwright is untouched."""
    env = dict(os.environ)
    env.update(
        npm_config_registry="https://registry.npmmirror.com",
        npm_config_cache=str(private_root / "npm-cache"),
        NPM_CONFIG_CACHE=str(private_root / "npm-cache"),
        npm_config_update_notifier="false",
        PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright",
        PLAYWRIGHT_BROWSERS_PATH=str(private_root / "browsers"),
    )
    return env


def ensure_playwright(install_root: Path, log: Callable[[str], None], timeout: int = 540,
                      version: str = "1.63.0") -> tuple[Path, dict] | None:
    """Self-contained install of @playwright/test@<version> + chromium under
    `install_root`. Returns (root, env_extra) — env_extra must be passed to
    every run that uses this install (private PLAYWRIGHT_BROWSERS_PATH)."""
    install_root.mkdir(parents=True, exist_ok=True)
    env = isolated_install_env(install_root)
    (install_root / "package.json").write_text(json.dumps({"name": "octos-arc-acceptance", "private": True}))
    t0 = time.time()
    # 两步都写成 thunk，执行时才展开。第二步必须在 npm install **之后**才解析：
    # 装之前 node_modules/.bin 还不存在，local_bin 里的 which 找不到东西，会退回
    # 无后缀的 `playwright` —— 那是给 sh 用的脚本，Windows 上交给 subprocess 就是
    # [WinError 193]「不是有效的 Win32 应用程序」。Linux 上两种时机解析结果相同。
    def install_step() -> list[str]:
        return [npm_exe(), "install", "--no-audit", "--no-fund", "--no-package-lock",
                f"@playwright/test@{version}"]

    def chromium_step() -> list[str]:
        return [local_bin("playwright", install_root / "node_modules" / ".bin"), "install", "chromium"]

    steps = [install_step, chromium_step]
    for step in steps:
        cmd = step()
        remaining = timeout - (time.time() - t0)
        if remaining <= 0:
            log("[acceptance] playwright install timed out")
            return None
        try:
            r = subprocess.run(cmd, cwd=install_root, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=remaining)
        except (subprocess.TimeoutExpired, OSError) as exc:
            log(f"[acceptance] playwright install step {Path(cmd[0]).name} {cmd[1]} failed: {exc}")
            return None
        if r.returncode != 0:
            log(f"[acceptance] playwright install step {Path(cmd[0]).name} {cmd[1]} rc={r.returncode}: "
                f"{(r.stderr or r.stdout)[-300:]}")
            return None
    root = find_playwright_root([install_root])
    if root is None:
        return None
    log(f"[acceptance] playwright {version} installed privately into {install_root} in {time.time()-t0:.0f}s "
        f"(browsers under {env['PLAYWRIGHT_BROWSERS_PATH']})")
    return root, {"PLAYWRIGHT_BROWSERS_PATH": env["PLAYWRIGHT_BROWSERS_PATH"]}


# ------------------------------------------------------------------ Windows
# 判分机上「谁占着端口、这进程是不是我们的」靠 lsof / ps / /proc，三样都齐。
# Windows 上三样都没有：lsof 和 free 根本不存在，ps 是 Git 自带的、读不到 cwd，
# /proc 不存在。于是下面四个清理函数在本地全是**空转** —— 正常返回、一个进程都不
# 杀，日志里也看不出区别。tb-v3-tool-c 节点 1 的
# `Failed at: build/start / code: 'EADDRINUSE'` 就是这么来的：回合里起的自测服务器
# 占着 43101，验收测试要在同一个端口起服务器，而「开跑前先腾空端口」那一步是空的，
# 节点拿 0 分 —— 它的功能根本没被测过。
#
# 这里给同一件事补上本地口径，做法沿用本文件 `kill_process_group` 已有的那套：
# PowerShell 取进程表/端口表，taskkill /T 连子树一起杀。每个分支都以 win32 为条件，
# 非 Windows 直接走原来那条路，线上行为逐字不变。


def _powershell() -> str:
    return shutil.which("powershell") or shutil.which("pwsh") or "powershell"


def _win_run(cmd: list[str], timeout: int = 25) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _win_csv_rows(script: str) -> list[list[str]]:
    """跑一段 PowerShell，按 CSV 解析输出。

    用 CSV 而不是自定义分隔符：CommandLine 里有引号、空格、反斜杠、中文路径，
    手写切分必碎；ConvertTo-Csv 会把它们正确转义，csv 模块正好读得回来。
    脚本里一个双引号都不出现，省掉 subprocess 那一层的转义问题。
    """
    out = _win_run([_powershell(), "-NoProfile", "-NonInteractive", "-Command", script])
    if not out.strip():
        return []
    try:
        return [row for row in csv.reader(io.StringIO(out)) if row and any(c.strip() for c in row)]
    except csv.Error:
        return []


_WIN_PROCESS_SCRIPT = ("Get-CimInstance Win32_Process | "
                       "Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CommandLine | "
                       "ConvertTo-Csv -NoTypeInformation")


def win_process_table() -> list[tuple[int, int, str, str, str]]:
    """整张进程表：(pid, ppid, name, exe, cmdline)。

    取不到就返回空表 —— 调用方一律「拿不到就当不是我们的」。取数失败时宁可漏杀，
    绝不因为读不到信息去杀一个不认识的进程（判分机是共享主机）。
    """
    rows = _win_csv_rows(_WIN_PROCESS_SCRIPT)
    if len(rows) < 2:
        return []
    header = [h.strip().lower() for h in rows[0]]
    try:
        i_pid = header.index("processid")
        i_ppid = header.index("parentprocessid")
        i_name = header.index("name")
        i_exe = header.index("executablepath")
        i_cmd = header.index("commandline")
    except ValueError:
        return []
    need = max(i_pid, i_ppid, i_name, i_exe, i_cmd) + 1
    table: list[tuple[int, int, str, str, str]] = []
    for row in rows[1:]:
        row = row + [""] * (need - len(row))
        try:
            table.append((int(row[i_pid]), int(row[i_ppid]), row[i_name] or "",
                          row[i_exe] or "", row[i_cmd] or ""))
        except ValueError:
            continue
    return table


_WIN_LISTEN_SCRIPT = ("Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
                      "Select-Object LocalPort,OwningProcess | ConvertTo-Csv -NoTypeInformation")


def win_listening_pids(port: int) -> list[int]:
    """监听 `port` 的进程号。先问 Get-NetTCPConnection，空手再退回 netstat。"""
    pids: list[int] = []
    rows = _win_csv_rows(_WIN_LISTEN_SCRIPT)
    if len(rows) >= 2:
        header = [h.strip().lower() for h in rows[0]]
        if "localport" in header and "owningprocess" in header:
            i_port, i_own = header.index("localport"), header.index("owningprocess")
            need = max(i_port, i_own) + 1
            for row in rows[1:]:
                row = row + [""] * (need - len(row))
                try:
                    if int(row[i_port]) == port:
                        pids.append(int(row[i_own]))
                except ValueError:
                    continue
            if pids:
                return sorted(set(pids))
    # netstat 兜底：`  TCP    127.0.0.1:43101    0.0.0.0:0    LISTENING    12345`
    for line in _win_run(["netstat", "-ano", "-p", "tcp"]).splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        local = parts[1].rsplit(":", 1)
        if len(local) == 2 and local[1].isdigit() and int(local[1]) == port and parts[4].isdigit():
            pids.append(int(parts[4]))
    return sorted(set(pids))


def win_kill_tree(pid: int) -> bool:
    """taskkill /T 连子树一起杀 —— 和 `kill_process_group` 同一条路子。

    只杀一层会留下孤儿：npm 被杀了，它拉起来的 node server.js 还占着端口，
    下一个轮到验收测试时就 EADDRINUSE。
    """
    if pid <= 0 or pid in (os.getpid(), os.getppid()):
        return False
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def win_owns_process(pid: int, root: Path,
                     table: list[tuple[int, int, str, str, str]] | None = None) -> bool:
    """这个进程是不是从我们的工作目录里起来的。

    Windows 拿不到工作目录（没有 /proc/<pid>/cwd，也没有 lsof），所以用三个替代
    信号，命中任意一个就算我们的：

      1. 命令行里出现工作目录       —— 例如 `node C:\\...\\tb-v3-tool-c\\backend\\server.js`
      2. 可执行文件在工作目录下     —— node_modules 里的本地二进制
      3. 父进程链能走到我们自己     —— 工具回合起的 `sh -c "npm start"` → npm → node
                                      这条链的顶端就是 main.py

    三个都不命中就当作外来进程。宁可漏杀，不可错杀。
    """
    rows = win_process_table() if table is None else table
    if not rows:
        return False
    root_key = str(Path(root).resolve()).replace("/", "\\").lower()
    by_pid = {row[0]: row for row in rows}
    me, parent = os.getpid(), os.getppid()
    seen: set[int] = set()
    walk = pid
    for _ in range(32):
        if walk in seen or walk <= 0 or walk not in by_pid:
            return False
        seen.add(walk)
        if walk in (me, parent):
            return True
        _, ppid, _, exe, cmd = by_pid[walk][:5]
        for text in (exe, cmd):
            if text and root_key in text.replace("/", "\\").lower():
                return True
        walk = ppid
    return False


def listening_pids(port: int) -> list[int]:
    """监听 `port` 的进程号，两个平台都认。"""
    if sys.platform == "win32":
        return win_listening_pids(port)
    try:
        out = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return []
    pids = []
    for item in out:
        try:
            pids.append(int(item))
        except ValueError:
            pass
    return pids


def owned_by_workspace(pid: int, root: Path) -> bool:
    """这个进程是不是从 `root` 里起来的，两个平台都认。

    Linux 走「工作目录在 root 下」这条既有判据；Windows 走 `win_owns_process`
    那三个替代信号（见其 docstring）。
    """
    if sys.platform == "win32":
        return win_owns_process(pid, root)
    return workspace_contains(process_cwd(pid), root)


def free_port(port: int) -> None:
    if sys.platform == "win32":
        # Linux 这条路径本来就不问归属：端口是我们的，上面的监听者一律清掉。
        # 本地照同一口径办，两边才等价。
        for pid in win_listening_pids(port):
            win_kill_tree(pid)
        return
    for pid in listening_pids(port):
        try:
            os.kill(pid, _SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def container_memory_limit() -> int | None:
    """cgroup memory limit in bytes (v2 memory.max or v1 limit_in_bytes), None
    when unlimited/unknown. The ARC runner container has 512 MiB (cloud
    29c840566f36: memory.max=536870912, 4 Chromium workers -> OOM, rc=-9)."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if raw.isdigit():
            value = int(raw)
            if value < 1 << 50:  # v1 reports a huge number when unlimited
                return value
    return None


def workers_for_memory(limit: int | None, requested: int) -> int:
    """One Chromium worker per ~700 MiB of container memory, at least 1."""
    if not limit:
        return requested
    return max(1, min(requested, limit // (700 * 1024 * 1024)))


def workers_for_final(limit: int | None, requested: int) -> int:
    """Full-suite workers, capped by memory (~450 MiB per Chromium worker + app).
    2 GiB -> 4, 512 MiB -> 1. The *default* count is 1, not this ceiling: see
    `final_workers`."""
    if not limit:
        return requested
    return max(1, min(requested, limit // (450 * 1024 * 1024)))


# 官方判分配置（交付包里的 `tests/playwright.config.ts`）是 `workers: 1` +
# `fullyParallel: false`：全卷一条一条串着跑，还是一个服务端、一份持久化存储。
# 我们的全卷测量默认照抄这个数 —— 卷子共用那个存储，并发跑的就是**另一个世界**，
# 在自己的世界里量出来的绿，不必是判分方看到的绿；凡是喂给净胜判定、对账、
# 封箱交付的全卷测量，口径一致比跑得快重要。
GRADER_WORKERS = 1


def final_workers(limit: int | None) -> int:
    """全卷测量的并发数（默认与官方判分一致，=1）。

    `OCTOS_ARC_FINAL_WORKERS` 可以提速（跑得快的代价是量与判分不再是同一件事，
    只该在明确知道要这个代价时用）；内存上限照旧由 `workers_for_final` 压。
    """
    return workers_for_final(limit, int(os.environ.get("OCTOS_ARC_FINAL_WORKERS",
                                                       str(GRADER_WORKERS))))


REAP_COMMANDS = ("node", "npm", "npx", "sh", "bash")


def should_reap(comm: str, cwd: str | None, root: Path) -> bool:
    """A leftover server/build process from a tool turn: a node/npm process whose
    cwd is inside the app (frontend/ or backend/), never the kernel or the harness."""
    if not cwd or Path(comm).name not in REAP_COMMANDS:
        return False
    try:
        rel = Path(cwd).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return bool(rel.parts) and rel.parts[0] in ("frontend", "backend")


def process_cwd(pid: int) -> str | None:
    if sys.platform == "win32":
        # Windows 既没有 /proc/<pid>/cwd 也没有 lsof；工作目录要读 PEB 才拿得到，
        # 代价太大。归属改由 `win_owns_process` 的三个替代信号判断。这里老实返回
        # None —— 调用方一律当「读不到 cwd」，不会因此误杀任何进程。
        return None
    proc = Path(f"/proc/{pid}/cwd")
    try:
        return os.readlink(proc)
    except OSError:
        pass
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def _win_reap_workspace_processes(root: Path, log: Callable[[str], None]) -> int:
    """本地版的同一件事。判据从「工作目录在 frontend/ 或 backend/ 下」换成
    `win_owns_process` 那三个替代信号 —— Windows 拿不到工作目录。

    这条判据比 Linux 那条宽：Linux 只认 cwd 的前两段是 frontend/backend，
    这里认「从工作目录里起来的 node/npm/npx/sh/bash」。宽在这里是安全的 ——
    一个从 output_dir 里起来的 node 族进程，就是这次跑留下的，留着只会占 CPU
    把验收测试拖慢。外来进程仍然一个不碰。
    """
    table = win_process_table()
    if not table:
        return 0
    me = os.getpid()
    killed = 0
    for pid, _, name, _, _ in table:
        if pid == me or Path(name).stem.lower() not in REAP_COMMANDS:
            continue
        if win_owns_process(pid, root, table) and win_kill_tree(pid):
            killed += 1
    if killed:
        log(f"[reap] killed {killed} leftover process(es) from the workspace")
    return killed


def reap_workspace_processes(root: Path, log: Callable[[str], None]) -> int:
    """Kill node/npm processes left running inside the app directories (cloud
    29c840566f36: 346 strays at postflight; on a 1-CPU grader they starve the
    acceptance runs). Returns how many were killed."""
    if sys.platform == "win32":
        return _win_reap_workspace_processes(root, log)
    try:
        out = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0
    killed = 0
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, comm = int(parts[0]), parts[1].strip()
        if pid == os.getpid() or Path(comm).name not in REAP_COMMANDS:
            continue
        if should_reap(comm, process_cwd(pid), root):
            try:
                os.kill(pid, _SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
    if killed:
        log(f"[reap] killed {killed} leftover process(es) inside frontend/ or backend/")
    return killed


def workspace_contains(cwd: str | None, root: Path) -> bool:
    if not cwd or not Path(cwd).is_absolute():
        return False
    try:
        return Path(cwd).resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def kill_pid_tree(pid: int) -> None:
    """杀一个进程（Windows 上连它的子树一起）。"""
    if sys.platform == "win32":
        win_kill_tree(pid)
        return
    try:
        os.kill(pid, _SIGKILL)
    except (ProcessLookupError, PermissionError, ValueError):
        pass


def free_owned_ports(ports: list[int], root: Path) -> None:
    """Kill listeners on `ports` that were started from inside `root` (our own
    leftovers), leaving foreign processes alone. Works on macOS, Linux and
    Windows (see `owned_by_workspace` for how ownership is decided there)."""
    for port in ports:
        for pid in listening_pids(port):
            if owned_by_workspace(pid, root):
                kill_pid_tree(pid)


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def listening_ports(root: Path) -> list[int]:
    """Ports held by processes started from inside the app tree.

    A backend that ignores PORT and binds its own is a common way to miss the
    port the harness and the grader wait on. Naming the port it did take turns a
    bare timeout into a one-line fix. Foreign listeners are left out, the way
    `free_owned_ports` leaves them alone."""
    try:
        out = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    ports: set[int] = set()
    owned: dict[str, bool] = {}
    pid = ""
    for line in out.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("n") and pid:
            port = line.rsplit(":", 1)[-1]
            if not port.isdigit():
                continue
            if pid not in owned:
                try:
                    owned[pid] = workspace_contains(process_cwd(int(pid)), root)
                except ValueError:
                    owned[pid] = False
            if owned[pid]:
                ports.add(int(port))
    return sorted(ports)


def tree_digest(root: Path) -> dict[str, str]:
    """rel path -> sha256 for every regular file under root (node_modules skipped)."""
    import hashlib
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and "node_modules" not in path.parts:
            out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def restore_tree(live: Path, snapshot: Path, expected: dict[str, str]) -> list[str]:
    """Make `live` match `snapshot` again: rewrite changed/deleted files, remove
    added ones. Returns the relative paths that had to be fixed."""
    fixed: list[str] = []
    current = tree_digest(live)
    for rel, digest in expected.items():
        if current.get(rel) != digest:
            dest = live / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snapshot / rel, dest)
            fixed.append(rel)
    for rel in current:
        if rel not in expected:
            try:
                (live / rel).unlink()
            except OSError:
                pass
            fixed.append(rel)
    return fixed


def snapshot_worktree(git_run: Callable[[list[str]], object]) -> None:
    """Stage everything so `restore_worktree` can undo what a test run mutates
    (persisted JSON stores, uploaded files) without losing the model's edits."""
    git_run(["add", "-A"])


def mutated_by_tests(git_run: Callable[[list[str]], object],
                     parts: tuple[str, ...] = ("frontend", "backend")) -> list[str]:
    """Files the test run changed, against the snapshot `snapshot_worktree` staged.

    An app that keeps its state in files carries one session's actions into the
    next, which is how a spec that passes on its own fails in the suite. Naming
    the files turns "shared state" into somewhere to look.
    """
    result = git_run(["diff", "--name-only", "--", *parts])
    out = getattr(result, "stdout", "") or ""
    return sorted({line.strip() for line in out.splitlines() if line.strip()})[:12]


def restore_worktree(git_run: Callable[[list[str]], object], parts: tuple[str, ...] = ("frontend", "backend")) -> None:
    """Return tracked files to the staged snapshot and drop files a test run
    created; ignored build outputs (node_modules, dist) are left alone."""
    git_run(["checkout", "--", "."])
    git_run(["clean", "-fdq", "-e", "node_modules", "-e", "dist", "--", *parts])


def robustness_probe(port: int, proc: subprocess.Popen | None = None, timeout: float = 5.0) -> str | None:
    """Hit paths a browser or grader will request; the server must answer
    (any status) and stay alive. Cloud f9f0026819f1: an unhandled ENOENT on
    GET /favicon.ico killed the backend and 8 of 10 tests saw ECONNREFUSED."""
    import http.client
    for index, path in enumerate(("/favicon.ico", "/this-path-does-not-exist", "/api/this-route-does-not-exist")):
        deadline = time.monotonic() + timeout
        for attempt in range(2):
            conn = None
            error = None
            try:
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=max(0.001, deadline - time.monotonic()))
                conn.request("GET", path)
                resp = conn.getresponse()
                resp.read()
            except Exception as exc:  # noqa: BLE001
                error = exc
            finally:
                if conn is not None:
                    conn.close()
            if error is None:
                break
            alive = proc is None or proc.poll() is None
            # Only startup transport failures get one retry, within the original budget.
            transient = isinstance(error, (ConnectionError, TimeoutError, http.client.RemoteDisconnected))
            if index == 0 and attempt == 0 and alive and transient and deadline - time.monotonic() > 0.2:
                time.sleep(0.2)
                if proc is None or proc.poll() is None:
                    continue
                alive = False
            return (f"GET {path} got no HTTP response ({error.__class__.__name__}); "
                    f"backend {'still running' if alive else 'CRASHED (process exited)'} — unknown paths must "
                    f"return 404, never throw")
        time.sleep(0.2)
        if proc is not None and proc.poll() is not None:
            return f"backend process exited (rc={proc.returncode}) right after GET {path} — an unhandled exception " \
                   f"in the static/API handler; missing files must return 404 and the process must never die"
    return None


class AppServer:
    """Build the frontend once and run the backend on the smoke port."""

    def __init__(self, project: Path, port: int, log: Callable[[str], None], env_extra: dict | None = None,
                 grader_like: bool = False, extra_ports: list[int] | None = None):
        """`grader_like=True` starts the backend exactly as the platform does:
        only PORT is set, so any extra spec ports (e.g. 3301) get bound too.
        Per-node runs pass False (ARC_EXTRA_PORTS=0) to stay off shared ports."""
        self.project = project
        self.port = port
        self.log = log
        self.env_extra = env_extra or {}
        self.grader_like = grader_like
        self.extra_ports = extra_ports or []
        self.proc: subprocess.Popen | None = None
        self.log_file: Path | None = None

    def _run(self, cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str]:
        try:
            r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                               env=dict(os.environ, **npm_script_shell_env(), **self.env_extra))
        except subprocess.TimeoutExpired:
            return 124, f"timeout after {timeout}s"
        except OSError as exc:
            return 127, str(exc)
        return r.returncode, clip_ends((r.stdout or "") + "\n" + (r.stderr or ""), 1500)

    def build(self) -> str | None:
        frontend, backend = self.project / "frontend", self.project / "backend"
        if not (frontend / "package.json").is_file() or not (backend / "package.json").is_file():
            return "frontend/package.json or backend/package.json missing"
        for part in (frontend, backend):
            deps = {}
            try:
                deps = json.loads((part / "package.json").read_text(encoding="utf-8", errors="replace")).get("dependencies") or {}
            except Exception:  # noqa: BLE001
                pass
            if deps and not (part / "node_modules").is_dir():
                rc, out = self._run([npm_exe(), "install", "--no-audit", "--no-fund"], part, 600)
                if rc != 0:
                    return f"{part.name} `npm install` failed:\n{out}"
        # Cloud c17bc1b44d26: a one-line copy build failed because dist/ did not
        # exist yet. The grader builds from a fresh checkout too, so make the
        # target directory exist before every build (harmless when it does).
        try:
            (frontend / "dist").mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        rc, out = self._run([npm_exe(), "run", "build"], frontend, 600)
        if rc != 0:
            if windows_copy_build(frontend):
                self.log("[acceptance] Windows: `npm run build` 的 shell 引号问题，已用等价拷贝代替"
                         "（判分机走 sh，不经过这里）")
                return None
            return f"frontend `npm run build` failed:\n{out}"
        return None

    def start(self, wait_seconds: int = 45) -> str | None:
        free_port(self.port)
        if self.grader_like:
            free_owned_ports(self.extra_ports, self.project)
        self.log_file = Path(tempfile.mkstemp(prefix="octos-app-", suffix=".log")[1])
        env = dict(os.environ, PORT=str(self.port), **npm_script_shell_env(), **self.env_extra)
        env.pop("ARC_EXTRA_PORTS", None)
        if not self.grader_like:
            env["ARC_EXTRA_PORTS"] = "0"
        try:
            fh = open(self.log_file, "w")
            self.proc = subprocess.Popen([npm_exe(), "start"], cwd=self.project / "backend", env=env,
                                         stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                                         start_new_session=True)
        except OSError as exc:
            return f"backend `npm start` could not launch: {exc}"
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if self.proc.poll() is not None:
                return (f"backend `npm start` exited early (rc={self.proc.returncode}):\n"
                        f"{self.log_file.read_text(errors='replace')[-1500:]}")
            if port_open(self.port):
                err = robustness_probe(self.port, self.proc)
                if not err and self.grader_like and self.extra_ports:
                    # Cloud 3f0124e82113: the specs default to :3301, the grader sets only PORT,
                    # the backend bound PORT alone -> 10x ERR_CONNECTION_REFUSED. Same handler on both.
                    err = self.extra_ports_bound()
                if err:
                    tail = self.tail(800)
                    self.stop()
                    return f"{err}\nserver log tail:\n{tail}"
                return None
            time.sleep(0.5)
        # Ask before stopping: the process still holds whatever it did bind.
        elsewhere = [p for p in listening_ports(self.project) if p != self.port]
        self.stop()
        where = (f" It is listening on {', '.join(str(p) for p in elsewhere)} instead."
                 if elsewhere else " Nothing started from the app tree is listening on any port.")
        return f"backend did not bind port {self.port} within {wait_seconds}s.{where}\n" + \
            (self.log_file.read_text(errors="replace")[-1500:] if self.log_file else "")

    def extra_ports_bound(self, wait_seconds: float = 5.0) -> str | None:
        """Grader-like start: every port the specs default to must answer too."""
        deadline = time.time() + wait_seconds
        missing = list(self.extra_ports)
        while missing and time.time() < deadline:
            missing = [p for p in missing if not port_open(p)]
            if missing:
                time.sleep(0.25)
        if not missing:
            return None
        ports = ", ".join(map(str, missing))
        return (f"PORT CONTRACT violated: the acceptance specs default to http://127.0.0.1:{ports} and the grader "
                f"starts the backend with only PORT={self.port}; the backend bound PORT but not port(s) {ports} "
                f"(every test would fail with ERR_CONNECTION_REFUSED). Serve the same handler on each of these "
                f"ports with a separate http.createServer(handler).listen(port) unless process.env.ARC_EXTRA_PORTS === '0'.")

    def tail(self, n: int = 1500) -> str:
        try:
            return self.log_file.read_text(errors="replace")[-n:] if self.log_file else ""
        except OSError:
            return ""

    def stop(self) -> None:
        if self.proc is not None:
            kill_process_group(self.proc)
            # Collect the owned child's exit status instead of leaving it for PID 1
            # in containers whose init process does not reap orphaned children.
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self.proc = None
        free_port(self.port)
        if self.grader_like:
            free_owned_ports(self.extra_ports, self.project)
        if self.log_file:
            try:
                self.log_file.unlink()
            except OSError:
                pass
            self.log_file = None


class AcceptanceRunner:
    """Run selected spec files from `tests_dir` with the Playwright install at `root`."""

    def __init__(self, root: Path, tests_dir: Path, work_dir: Path, log: Callable[[str], None],
                 timeout_ms: int = 10000, workers: int = 2, env_extra: dict | None = None):
        self.root = root
        self.env_extra = env_extra or {}
        self.tests_dir = tests_dir
        self.work_dir = work_dir
        self.log = log
        self.timeout_ms = timeout_ms
        self.workers = workers

    def _prepare(self, workers: int | None = None) -> Path:
        # Specs `import '@playwright/test'`; Node resolves that upward from the
        # spec file, so the copied tree must sit under the Playwright install
        # (NODE_PATH is set as well for the case where it cannot).
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir)
        shutil.copytree(self.tests_dir, self.work_dir / "tests",
                        ignore=shutil.ignore_patterns("node_modules", "test-results", "playwright-report"))
        shutil.copyfile(Path(__file__).with_name("page_errors.ts"), self.work_dir / "page_errors.ts")
        for spec in (self.work_dir / "tests").rglob("*.spec.ts"):
            source = spec.read_text(encoding="utf-8")
            alias = "__octosObservePageErrors"
            while alias in source:
                alias += "_"
            observer = Path(os.path.relpath(self.work_dir / "page_errors", spec.parent)).as_posix()
            if not observer.startswith("."):
                observer = "./" + observer
            spec.write_text(source + f"\nimport {{ register as {alias} }} from {json.dumps(observer)};\n{alias}();\n", encoding="utf-8")
        shutil.copyfile(Path(__file__).with_name("action_errors.cjs"), self.work_dir / "action_errors.cjs")
        (self.work_dir / "playwright.config.ts").write_text(
            "import { defineConfig } from '@playwright/test';\n"
            # outputDir otherwise resolves against the Playwright install, where
            # failure artifacts (error-context.md) pile up across runs instead of
            # being cleared with the work dir.
            f"export default defineConfig({{ testDir: './tests', outputDir: './test-results', "
            f"timeout: {self.timeout_ms}, retries: 0, "
            f"fullyParallel: {'true' if os.environ.get('OCTOS_ARC_FULLY_PARALLEL') == '1' else 'false'}, "
            f"workers: {workers or self.workers}, reporter: [['list'], ['json', {{ outputFile: 'report.json' }}], ['./action_errors.cjs', {{ output: 'action-errors.json' }}]], "
            # The grader's own config gives expect the full test timeout and sets
            # no action or navigation timeout. Anything stricter here fails tests
            # that grading would pass and spends repair rounds on them. The
            # tighter values used to buy a named locator in the error; the page
            # snapshot and the action trace now carry that whichever timeout
            # fires.
            f"expect: {{ timeout: {self.timeout_ms} }}, "
            f"use: {{ headless: true, baseURL: process.env.E2E_BASE_URL }} }});\n")
        return self.work_dir / "playwright.config.ts"

    def _attach_rendered_pages(self, summary: RunSummary) -> None:
        """Fill each failure's `rendered_page` from the error context Playwright
        wrote for it. 每轮都会清掉上一轮的现场（`_prepare` 清整个工作目录；
        按文件跑时的 `reprepare=False` 路径清 `test-results`），所以这些文件
        只会描述本轮；目录外的路径一律忽略。"""
        work = self.work_dir.resolve()
        for index, result in enumerate(summary.results):
            if result.ok or not result.context_path:
                continue
            path = Path(result.context_path)
            try:
                if not path.resolve().is_relative_to(work) or path.stat().st_size > 1_000_000:
                    continue
                context = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue  # Diagnostics are optional; never fail a run over them.
            summary.results[index] = replace(result, rendered_page=page_snapshot(context))

    def run(self, spec_rel_paths: list[str], base_url: str, wall_timeout: int = 900,
            workers: int | None = None, reprepare: bool = True,
            fail_fast: bool = False) -> RunSummary:
        """`fail_fast` 判红即停（`--max-failures=1`，2026-10-08 提速批）—— 只给
        日常体检（对账）用：那里要的是「哪份卷子红了」，不是每份里红了几条。
        报告照常写出（Playwright 收工前总写 reporter 输出），只是**截断**：
        后面的用例没跑，`total` 比全量小 —— 拿它记账的调用方必须知道这点
        （见 `orchestrator._run_reconcile` 的记账守卫）。"""
        if reprepare:
            config = self._prepare(workers)
        else:
            # 文件隔离逐份跑时卷子工作目录整批只准备一次（见 `run_specs_isolated`）：
            # `_prepare` 的清目录+重拷全卷+逐份改写是纯重复劳动，一份文件做一遍
            # 只买到「上一份的报告不会冒充这一份」—— 所以这里自己清报告，效果
            # 一样。`test-results` 一并清掉：失败快照按文件落盘，留着会让这一份
            # 读到上一份留下的同名残影。
            config = self.work_dir / "playwright.config.ts"
            if not config.exists():          # 防御：没准备过就老实补一次
                config = self._prepare(workers)
            for stale in ("report.json", "action-errors.json"):
                try:
                    (self.work_dir / stale).unlink()
                except OSError:
                    pass
            shutil.rmtree(self.work_dir / "test-results", ignore_errors=True)
        report_path = self.work_dir / "report.json"
        # 走 local_bin 而不是直接拼路径：Linux 上 which 找到的就是这个无后缀文件，
        # 返回的字符串和以前一模一样；Windows 上它才会换成 .cmd —— 无后缀那个是
        # 给 sh 用的，交给 subprocess 就是 [WinError 193]。
        cmd = [local_bin("playwright", self.root / "node_modules" / ".bin"), "test", "-c", str(config)]
        if fail_fast:
            # 判红即停只给体检挂（见 run 的 docstring）：踩到第一条红就收工。
            cmd += ["--max-failures=1"]
        # 判定机上一直传绝对路径（/workspace/...）且跑了 13 轮没问题，保持原样。
        # Windows 上不行：实测绝对路径（反斜杠和正斜杠都试过）收到 0 条用例，
        # 相对 work_dir 的写法才认 —— 而 cmd 就是以 work_dir 为 cwd 起的。
        # 所以只在 Windows 上换成相对写法，Linux 分支逐字不变。
        if sys.platform == "win32":
            cmd += [(Path("tests") / p).as_posix() for p in spec_rel_paths]
        else:
            cmd += [str(self.work_dir / "tests" / p) for p in spec_rel_paths]
        env = dict(os.environ, E2E_BASE_URL=base_url, CI="1",
                   NODE_PATH=str(self.root / "node_modules"), **self.env_extra)
        env.pop("FORCE_COLOR", None)
        t0 = time.time()
        try:
            r = subprocess.run(cmd, cwd=self.work_dir, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=wall_timeout)
            tail = ((r.stdout or "") + (r.stderr or ""))[-2000:]
        except subprocess.TimeoutExpired:
            return RunSummary(error=f"playwright run exceeded {wall_timeout}s")
        except OSError as exc:
            return RunSummary(error=f"playwright could not start: {exc}")
        if not report_path.exists():
            killed = r.returncode < 0 or "Killed" in tail
            return RunSummary(error=(f"playwright was killed (rc={r.returncode}); likely out of memory — "
                                     f"not an application failure" if killed else
                                     f"playwright produced no report (rc={r.returncode}): {_ANSI.sub('', tail)[-600:]}"),
                              killed=killed)
        try:
            report = json.loads(report_path.read_text(encoding="utf-8", errors="replace"))
            try:
                actions = json.loads((self.work_dir / "action-errors.json").read_text(encoding="utf-8", errors="replace"))
                if isinstance(actions, dict):
                    report["action_errors"] = actions
            except (OSError, json.JSONDecodeError):
                pass  # Optional diagnostics never change acceptance outcomes.
            summary = summarize_report(report)
        except (OSError, json.JSONDecodeError) as exc:
            return RunSummary(error=f"unreadable playwright report: {exc}")
        summary.stdout_tail = _ANSI.sub("", tail)
        self._attach_rendered_pages(summary)
        if summary.total == 0:
            # Cloud run a6ccc437539f: the model had edited /workspace/tests, the
            # copied spec no longer loaded, and "0/0" looked like a verdict.
            detail = "; ".join(summary.load_errors) or summary.stdout_tail[-600:] or f"rc={r.returncode}"
            self.log(f"[acceptance] 0 tests collected from {', '.join(spec_rel_paths)}: {detail[:300]}")
            return RunSummary(error=f"Playwright collected 0 tests from {', '.join(spec_rel_paths)} "
                                    f"(spec files unreadable or broken): {detail}", load_errors=summary.load_errors)
        self.log(f"[acceptance] {summary.passed}/{summary.total} passed in {time.time()-t0:.0f}s "
                 f"({', '.join(spec_rel_paths)})")
        return summary

    def collect(self, spec_rel_paths: list[str], wall_timeout: int = 300) -> tuple[int, str]:
        """只收集、不执行：`playwright test --list`。返回 (用例数, 错误文本)。

        这是自产卷子的第一道体检，而且**只在自产卷子上需要**：官方卷子的文件名和内容
        都是既定的、跑了十几轮没变过；自产卷子是刚写出来的 TypeScript，没人编译过它。

        为什么值得单独跑一趟（几秒钟，不开浏览器）：
          · 语法错、import 错、用了不存在的 API —— 会让**整个文件一条用例都收集不到**。
          · 收集失败在 `_accept` 里的下场是 `summary.error` → 「spec run failed」：
            不修复、不回滚、节点直接判负，而且**同一个错误会在每个节点上重演一遍**
            （每个节点都会跑自己那几份卷子）。
          · 这些错误不需要浏览器就能发现，没有理由等到第一个节点验收时才发现 ——
            那时候它已经吃掉了一个节点和它的一轮修复。

        退出码为 0 但没有解析出总数时返回 (0, "")，由调用方决定这算不算问题：本函数
        只负责**观察**，判据留在 `main.py`。
        """
        if not spec_rel_paths:
            return 0, ""
        try:
            config = self._prepare()
        except OSError as exc:
            return 0, f"could not stage the specs for collection: {exc}"
        cmd = [local_bin("playwright", self.root / "node_modules" / ".bin"), "test", "-c", str(config), "--list"]
        if sys.platform == "win32":
            cmd += [(Path("tests") / p).as_posix() for p in spec_rel_paths]
        else:
            cmd += [str(self.work_dir / "tests" / p) for p in spec_rel_paths]
        env = dict(os.environ, CI="1", NODE_PATH=str(self.root / "node_modules"), **self.env_extra)
        env.pop("FORCE_COLOR", None)
        try:
            r = subprocess.run(cmd, cwd=self.work_dir, env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=wall_timeout)
        except subprocess.TimeoutExpired:
            return 0, f"`playwright test --list` exceeded {wall_timeout}s"
        except OSError as exc:
            return 0, f"playwright could not start: {exc}"
        out = (r.stdout or "") + (r.stderr or "")
        plain = _ANSI.sub("", out)
        if r.returncode != 0:
            return 0, plain[-1200:]
        m = re.search(r"Total:\s+(\d+)\s+test", plain)
        if m:
            return int(m.group(1)), ""
        # 没有 Total 行（reporter 换了措辞）：数 `›` 的行，它是 list reporter 的前缀。
        n = sum(1 for line in plain.splitlines() if "›" in line)
        return n, ""
