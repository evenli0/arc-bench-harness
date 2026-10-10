"""Minimal Octos UI Protocol v1 stdio driver (mirrors octos-tui).

Spawns `octos serve --stdio --solo` and speaks NDJSON JSON-RPC over
stdin/stdout. No handshake: send session/open, then turn/start, then collect
notifications until turn/completed or turn/error. Approvals are auto-approved
so the agent never blocks unattended.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator


class OctosProtocolError(RuntimeError):
    pass


class OctosStdioSession:
    def __init__(self, octos_bin: str, cwd: Path, env: dict, data_dir: Path,
                 on_event: Callable[[str, dict], None] | None = None,
                 extra_args: list[str] | None = None) -> None:
        self.cwd = str(cwd)
        self.data_dir = Path(data_dir)
        self.on_event = on_event or (lambda method, params: None)
        cmd = [octos_bin, "serve", "--stdio", "--solo", "--data-dir", str(data_dir)]
        cmd.extend(extra_args or [])
        if env.get("OCTOS_DANGER_FULL_ACCESS") == "1":
            cmd.append("--danger-full-access")
        # ⚠️ `encoding="utf-8"` 是**必须**的，不是顺手写的。
        #
        # 只给 `text=True` 的话，Python 用 `locale.getpreferredencoding(False)`
        # 解码子进程的输出：中文 Windows 上是 **cp936**。而内核吐的是 UTF-8
        # （JSON.stringify → process.stdout.write，node 一律 UTF-8）——
        # 于是**任何非 ASCII 都被静默解成乱码**。
        #
        # 实测：本机跑一次真回合，模型返回的正文长成 `鎴戜細鐩存摴鐢�`
        # （"我会直接…" 的 UTF-8 字节被当 GBK 解）。同一次里，桥写到 stderr 的
        # `[boot] 保护目录 = …` 也一起乱了，于是测试里按中文子串去捞那一行会
        # **捞不到** —— 症状看起来像「接线断了」，其实是编码。
        #
        # 为什么 v3 一直没事：这条路在 v3 里**从没跑过**。`_run_stdio` 需要
        # `octos_bin`，而 v3 找不到那个 Rust 二进制（没有 Linux x86_64 release、
        # 本机无 cargo），异常 → 掉到 `run_octos` 的 chat 兜底。v4 把内核换成
        # 我们自己的 JS 产物之后它第一次跑活，这个洞才露出来。
        #
        # Linux 判分机上大概率是 UTF-8 所以看不出来 —— 但那是**靠 locale 侥幸**，
        # 不是靠代码。显式写死，两边同码。
        # `start_new_session=True` 是 close() 里 killpg 的**前置条件**，不是可选项：
        # 不给它，子进程和我们同属一个进程组，`killpg(getpgid(pid))` 杀的是
        # **我们自己**。close() 里另有一道「拿错组就不杀」的兜底（见那里），
        # 但真正的保证在这一行。只在 POSIX 有意义（Windows 上 subprocess 收下就丢）。
        popen_kw: dict = {}
        if os.name != "nt":
            popen_kw["start_new_session"] = True
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=self.cwd, env=env,
            errors="replace", bufsize=1, **popen_kw,
        )
        self._pending: dict[str, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._notifications: queue.Queue = queue.Queue()
        self._stderr_lines: list[str] = []
        self._id_counter = 0
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()
        self.session_id = f"arc-bundle:{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------ plumbing

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            line = line.rstrip()
            self._stderr_lines.append(line)
            if "[arc-mod]" in line:
                # Patched-core proof marker (examples/core-mod): forward it so
                # the driver can log it into the graded stdout/stderr capture.
                self.on_event("core/marker", {"line": line})

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "method" in frame:
                self._notifications.put(frame)
            elif "id" in frame:
                with self._pending_lock:
                    q = self._pending.pop(str(frame["id"]), None)
                if q is not None:
                    q.put(frame)

    def _send(self, method: str, params: dict | None, want_response: bool,
              timeout: float = 60.0) -> dict | None:
        self._id_counter += 1
        rid = f"py-{self._id_counter}"
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        q: queue.Queue | None = None
        if want_response:
            msg["id"] = rid
            q = queue.Queue()
            with self._pending_lock:
                self._pending[rid] = q
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise OctosProtocolError(
                f"octos stdio write failed (process died?): {exc}; "
                f"stderr tail: {self.stderr_tail()}") from exc
        if q is None:
            return None
        try:
            frame = q.get(timeout=timeout)
        except queue.Empty as exc:
            raise OctosProtocolError(f"timeout waiting response to {method}") from exc
        if "error" in frame:
            raise OctosProtocolError(f"{method} failed: {frame['error']}")
        return frame.get("result")

    def stderr_tail(self, n: int = 10) -> str:
        return "\n".join(self._stderr_lines[-n:])

    # ------------------------------------------------------------ protocol

    def bootstrap_profile(self, provider: str, model: str, base_url: str | None,
                          api_key_env: str | None, timeout: float = 60.0,
                          hooks: list | None = None, tools_disabled: bool = False) -> None:
        """Create a solo profile and select its LLM (serve mode has no config-
        file default profile like `octos chat` does, so we onboard one).

        The requested id must be unique per call: the runner machine's
        profile store persists across submissions and across our own retry
        re-bootstraps, so a fixed id collides with earlier runs
        ("local profile 'arc-2' already exists with different email")."""
        import os
        unique = f"arc-{os.getpid()}-{int(time.time() * 1000) % 10_000_000}"
        res = self._send("profile/local/create", {
            "requested_id": unique,
            "name": "ARC Bundle Agent",
            # legacy fields required by older releases (<= v2.0.1)
            "username": unique,
            "email": f"{unique}@solo.local",
        }, want_response=True, timeout=timeout)
        self.profile_id = res.get("profile_id") if isinstance(res, dict) else None
        if not self.profile_id:
            raise OctosProtocolError(f"profile/local/create gave no profile_id: {res}")
        fields = {"hooks": hooks} if hooks else {}
        if tools_disabled:
            fields["tool_policy"] = {"deny": ["*"]}
        if fields:
            # The solo ProfileRuntime builds its HookExecutor from the profile's
            # own config (config_from_profile), not from the host config.json or
            # profile-defaults.json — verified with real stdio turns. Patch the
            # registry file before the LLM upsert re-reads and re-saves it.
            self._patch_profile_config(fields)
        api_type = "anthropic" if provider == "anthropic" else "openai"
        route: dict = {"api_type": api_type}
        if base_url:
            route["base_url"] = base_url
        if api_key_env:
            route["api_key_env"] = api_key_env
        self._send("profile/llm/upsert", {
            "profile_id": self.profile_id,
            "set_primary": True,
            "selection": {
                "family_id": provider,
                "model_id": model,
                "route": route,
            },
        }, want_response=True, timeout=timeout)

    def _patch_profile_config(self, fields: dict) -> None:
        import json as _json
        from pathlib import Path as _Path
        for root in (self.data_dir, self.data_dir / "profiles"):
            path = _Path(root) / "profiles" / f"{self.profile_id}.json" if root == self.data_dir \
                else _Path(root) / f"{self.profile_id}.json"
            if not path.is_file():
                continue
            try:
                data = _json.loads(path.read_text(encoding="utf-8"))
                data.setdefault("config", {}).update(fields)
                path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                return
            except (OSError, ValueError) as exc:
                raise OctosProtocolError(f"could not patch profile config at {path}: {exc}") from exc
        raise OctosProtocolError(f"profile registry file for {self.profile_id} not found under {self.data_dir}")

    def open(self, timeout: float = 120.0) -> None:
        params = {"session_id": self.session_id, "cwd": self.cwd}
        if getattr(self, "profile_id", None):
            params["profile_id"] = self.profile_id
        self._send("session/open", params, want_response=True, timeout=timeout)

    def run_turn(self, text: str, timeout: float = 1800.0) -> tuple[bool, str]:
        """Run one turn; stream events to on_event. Returns (ok, full_text)."""
        deadline = time.monotonic() + timeout
        if timeout <= 0:
            return False, "octos turn timed out"
        turn_id = str(uuid.uuid4())
        self._send("turn/start", {
            "session_id": self.session_id,
            "turn_id": turn_id,
            "input": [{"kind": "text", "text": text}],
        }, want_response=True, timeout=min(60.0, timeout))

        chunks: list[str] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "octos turn timed out"
            if self.proc.poll() is not None:
                return False, (f"octos process exited {self.proc.returncode}; "
                               f"stderr tail: {self.stderr_tail()}")
            try:
                frame = self._notifications.get(timeout=min(remaining, 5.0))
            except queue.Empty:
                continue
            method = frame.get("method", "")
            params = frame.get("params") or {}
            self.on_event(method, params)
            if method == "server/heartbeat":
                continue
            if method == "message/delta" and params.get("turn_id") == turn_id:
                chunks.append(str(params.get("text", "")))
            elif method == "approval/requested":
                # Auto-approve so unattended runs never block.
                try:
                    self._send("approval/respond", {
                        "approval_id": params.get("approval_id"),
                        "decision": "approve",
                        "approval_scope": "request",
                    }, want_response=True, timeout=30.0)
                except OctosProtocolError:
                    pass
            elif method == "turn/completed" and params.get("turn_id") == turn_id:
                return True, "".join(chunks)
            elif method == "turn/error" and params.get("turn_id") == turn_id:
                return False, (f"{params.get('code', 'error')}: "
                               f"{params.get('message', '')}"[:1000])

    def close(self) -> None:
        """结束会话 —— **连同整棵子进程树**。

        为什么不能只 `terminate()`：shim 在 Windows 上是 `octos-kernel.cmd`，
        CreateProcess 遇到 .cmd 会经 `%COMSPEC%` 解释它，于是**直接子进程是
        cmd.exe、内核 node 是孙子**。`terminate()` 只杀 cmd.exe，node 变孤儿
        活下来 —— 实测三次 open/close，node 进程数 1 → 2 → 3 → 4，每轮漏一个，
        一个回合一个。

        （Linux 的 shim 是 `#!/bin/sh` + `exec`，PID 就是 node 本身，所以那边
        `terminate()` 恰好够用。那是**靠 shim 的写法侥幸**，不是靠代码 ——
        shim 哪天改成不带 exec 的两行，同样的漏就出现在判分机上，而且症状是
        「跑了几个回合之后机器变慢」，极难联想到是收尸。）

        两个平台的前置条件不同，都写死在下面：

        * **POSIX**：`killpg` 要求子进程自成一组 ⇒ Popen 必须带
          `start_new_session=True`（在 `__init__` 里）。**没带的话
          `getpgid` 返回的是我们自己的组，`killpg` 就把 harness 自己杀了**
          —— 所以这里还有一道「拿错组就不杀」的兜底。
        * **Windows**：`taskkill /T` 靠**活着的父进程**遍历子树，所以必须在
          cmd.exe 还活着的时候调用。顺序反过来（先 terminate 再 taskkill）
          就等于 `proc.kill()`，一棵树都救不回来。
        """
        if self.proc.poll() is not None:
            return

        if os.name == "nt":
            # Windows 上没有「优雅地请一个控制台子进程退出」这回事：
            # taskkill 不带 /F 发的是 WM_CLOSE，控制台程序收不到；而
            # `terminate()` 是 TerminateProcess，一样是硬杀，却会砍断子树。
            # 所以这里没有先礼后兵 —— 一步到底。
            try:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            # 先 SIGTERM 请整组退出（node 收到就退，通常几毫秒），10 s 不退再强杀。
            self._signal_group(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
                return
            except Exception:
                pass
            self._signal_group(signal.SIGKILL)

        try:
            self.proc.wait(timeout=10)
        except Exception:
            # 整组杀都无效（权限、或 pid 已被复用）—— 至少把这一个收掉。
            try:
                self.proc.kill()
            except Exception:
                pass

    def _signal_group(self, sig: int) -> None:
        """把信号发给**整个子进程组**。

        那道 `pgid != 自己的 pgid` 的判断是刻意留的兜底：killpg 一旦拿到的是
        我们自己的组，杀的就是 harness 本身，而那个症状（判分进程中途消失）
        完全看不出跟这里有关。宁可「没杀掉」也不要「自杀」。
        """
        killpg = getattr(os, "killpg", None)
        if killpg is None:                     # Windows：没有进程组这回事
            return
        try:
            pgid = os.getpgid(self.proc.pid)
            if pgid == os.getpgid(0):          # 同组 = 拿错了，杀下去就是自杀
                return
            killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def __enter__(self) -> "OctosStdioSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
