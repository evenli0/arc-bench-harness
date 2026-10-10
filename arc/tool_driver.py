"""工具档驱动 —— 纯 Python 的 OpenAI 原生 tool_calls 循环。

v2 用 LocalDriver 顶替内核时，工具档整个消失了：`main.py` 里凡是「切到工具档」
的分支都落进 `no tool mode` 的死胡同，节点零次尝试判失败。这个模块把那个档位
补回来，而且**云端跑的就是这份代码**——平台执行提交的 main.py，不再需要那个
59MB 的 Linux 二进制。

于是本地/云端的差异面，从「我的驱动 vs 内核」缩小成「**我的驱动在 Windows vs
Linux**」。这份文件里的每一条平台分支都是按这个标准写的：宁可两边一样严，
不可本地松。

接口与 OctosDriver / LocalDriver 对齐：run / without_tools / end_scope / close，
外加属性 hooks / mode / session_scope / monitor / tools_disabled。
**故意不继承 LocalDriver** —— main.py 的 tool_tier_available()（`not isinstance(...)`）
靠这个判据决定要不要恢复那 6 处补偿补丁；继承过来等于白写。

跑 `python tool_driver.py --selftest` 可离线自检（不联网、不调模型）。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from acceptance import npm_script_shell_env
from text_repair import _find_span, ensure_charset, repair_flattened_js
from llm_proxy import open_upstream

# deepseek-v4 会把默认的 4096 全花在思考上然后返回空内容。llm_proxy 的
# ensure_max_tokens 会把更小的值抬上来，这里只是「没有代理时的兜底」。
DEFAULT_MAX_TOKENS = int(os.environ.get("OCTOS_ARC_MAX_TOKENS", "32768"))

# 上游抖动时的重试：429 / 5xx / 连接被掐。认证类错误不重试。
_TRANSIENT_CODES = {408, 425, 429, 500, 502, 503, 504}
_ATTEMPTS = 3

# llm_proxy 会按名字剥掉这些工具。给出其中一个 = 换来一个什么都做不了的空回合。
_PROXY_DROP_TOOLS = {"spawn", "ask_user_question", "check", "tool_search", "update_plan", "exec_command"}

# 写类工具的兜底名单。正常情况下从 main.py 注入的 hook 里读
# `tool_filter`（protected_hooks() 已经列全了），这里只是 hook 缺失时的退路。
_WRITE_TOOL_FALLBACK = {"write_file", "edit_file", "diff_edit", "apply_patch", "create_file", "append_file"}

# 目录遍历时永远跳过的重目录。
_SKIP_DIRS = {".git", "node_modules", "dist", ".venv", "venv", "__pycache__", ".arc", "playwright-report", "test-results"}

# 工具结果的默认上限（字符）。
CAP_READ = 50000
CAP_GREP = 30000
CAP_LIST = 50000
CAP_SHELL = 50000
CAP_WRITE = 2000

# grep 单行截断，防止一个 minified 文件把整条结果吃掉。
_GREP_LINE_CAP = 500

# Windows 既没有 os.killpg 也没有 SIGKILL（acceptance.py:56 有同款写法）。
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _log(msg: str) -> None:
    print(msg, flush=True)


class ToolDenied(Exception):
    """路径被策略拒绝。调用方把它变成一条工具错误，不是异常。"""


# ====================================================================== 工具表


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[dict], tuple[bool, str]]
    cap: int = CAP_READ
    shell_family: bool = False


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def _p_str(desc: str) -> dict:
    return {"type": "string", "description": desc}


def _p_int(desc: str, default: int | None = None) -> dict:
    out: dict = {"type": "integer", "description": desc}
    if default is not None:
        out["default"] = default
    return out


def _p_bool(desc: str, default: bool = False) -> dict:
    return {"type": "boolean", "description": desc, "default": default}


def truncate_head_tail(text: str, cap: int, head_ratio: float = 0.7) -> str:
    """头 70% / 尾 30%。尾比头重要：错误信息和退出码都在尾部。"""
    if cap <= 0 or len(text) <= cap:
        return text
    head = int(cap * head_ratio)
    tail = cap - head
    return (text[:head] + f"\n... {len(text) - cap} characters elided ...\n" + text[-tail:])


# ====================================================================== 驱动


class ToolDriver:
    """带工具的回合驱动。

    每个 run() 是一个独立的工具循环；会话不跨回合 —— 每回合的 prompt 已经带全了
    它需要的状态（和 OctosDriver 的 session_scope="turn" 同一个理由）。
    """

    def __init__(self, cwd: Path, env: dict, data_dir: Path | None = None,
                 max_iterations: int | None = None, events_log: Path | None = None) -> None:
        self.cwd = Path(cwd)
        self.env = env
        self.data_dir = data_dir
        self.max_iterations = max_iterations
        self.events_log = events_log
        self.mode = "tool"
        # 无状态驱动：回合之间没有上下文可留。
        self.session_scope = "turn"
        self.monitor = None
        self.hooks: list = []
        self.tools_disabled = False
        self.turns = 0
        # 本回合起过的后台进程（close() 时收拾）。Windows 上没有 lsof/ps 兜底，
        # 这里是唯一的收尸人。
        self._children: list[subprocess.Popen] = []
        # 正在飞的那个 HTTP 响应，交给看门狗在截止时间掐断。
        #
        # 为什么非掐不可：urllib 的 `timeout=` 是**每次 recv** 的超时，不是墙钟总时
        # 间。上游只要保持连接、慢慢吐字节（代理转发 SSE 时就是这样），那个超时永
        # 远不会响。实测过一次：main.py 给了一个节点 144 秒，驱动里单个请求跑了
        # 293.9 秒才回来，把节点预算打成 -126 秒，那一轮的修复一次都没跑。
        self._inflight = None
        self._inflight_lock = threading.Lock()
        self._aborted = False
        self._registry: dict[str, Tool] = self._build_registry()
        self._sh_logged = False

    # ------------------------------------------------------------ 配置

    def _config(self) -> tuple[str, str, str]:
        """每回合现读 —— start_llm_proxy() 会在构造之后改写 OPENAI_BASE_URL，
        缓存下来会打到错误的地址（local_driver.py 记着这个坑）。"""
        env = self.env
        base = (env.get("_ARC_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
        model = env.get("_ARC_MODEL") or os.environ.get("OCTOS_MODEL") or os.environ.get("MODEL") or ""
        key_env = env.get("_ARC_KEY_ENV") or "OPENAI_API_KEY"
        key = env.get(key_env) or env.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        return base, model, key

    def _iteration_cap(self) -> int:
        """一次 run() 里最多几个「问模型」的来回。

        代理那边的 turn_budget 在非 minimal 树里是 0（不限），所以这个数才是
        真正拦住死循环的东西。默认与 OCTOS_MAX_ITERATIONS 取小，再压到 60。
        """
        explicit = os.environ.get("OCTOS_ARC_TOOL_ITERATIONS")
        if explicit:
            try:
                return max(1, int(explicit))
            except ValueError:
                pass
        return max(1, min(int(self.max_iterations or 60), 60))

    # ------------------------------------------------------------ 接口

    @contextmanager
    def without_tools(self):
        """codegen 回合：一个请求、一个回复，退化回 LocalDriver 的行为。

        代理那边也会剥（main.py 设 proxy.no_tools），这里是同一件事的两道保险。
        """
        previous = self.tools_disabled
        self.tools_disabled = True
        try:
            yield
        finally:
            self.tools_disabled = previous

    def end_scope(self, scope: str) -> None:
        if scope == self.session_scope or self.session_scope == "turn":
            self.close()

    def close(self) -> None:
        """把本回合起的后台进程整组收掉。

        这里是第一道收尸人，也是唯一知道「这个回合到底起了哪些进程」的地方：
        回合一结束就收，残留根本活不到验收轮。背后的 _port_watchdog /
        reap_workspace_processes 是兜底 —— 它们只按端口和归属猜，猜不到
        「哪个回合留下的」。两道都在，是因为只靠这里不够：被硬掐断的回合
        （见 _run_with_heartbeat）会在请求线程还在解开时就走到这儿，那时模型
        可能刚刚才把 npm start 递出去，Popen 拿在手上了、进程还没起来。
        """
        for proc in list(self._children):
            try:
                self._kill_tree(proc)
            except Exception:  # noqa: BLE001 - 收尸失败不该把流程带崩
                pass
        self._children.clear()

    @staticmethod
    def _kill_tree(proc: subprocess.Popen) -> None:
        """连子树一起杀。proc.kill() 只杀 npm 那一层，它拉起的 node server.js
        会留下变成孤儿占着端口（acceptance.kill_process_group / win_kill_tree
        是同款实现）。"""
        if proc.poll() is not None:
            return
        killpg = getattr(os, "killpg", None)
        if killpg is not None:
            try:
                killpg(os.getpgid(proc.pid), _SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        else:
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=15)
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            proc.kill()
        except OSError:
            pass

    # ------------------------------------------------------------ 回合

    def run(self, prompt: str, timeout: int, monitor=None) -> tuple[bool, str]:
        self.monitor = monitor
        self.turns += 1
        self._aborted = False
        limit = max(1.0, float(timeout))
        deadline = time.monotonic() + limit
        started = time.time()
        try:
            ok, text = self._run_with_heartbeat(lambda: self._session(prompt, deadline),
                                                deadline, limit)
        except Exception as exc:  # noqa: BLE001 - 驱动绝不能把流程带崩
            ok, text = False, f"tool turn raised: {exc}"[:1000]
        finally:
            self.monitor = None
            if self.session_scope == "turn":
                self.close()
        elapsed = time.time() - started
        # 守时是硬契约。main.py 按 implement_timeout 扣节点预算，超一秒就从后面
        # 的修复轮里扣一秒。超时了必须留下痕迹 —— 那条线上曾经有一个限时 144 秒
        # 的回合跑了 294 秒、把节点预算打成负数，而日志里一个字都没有。
        if elapsed > limit + 1.0:
            _log(f"[tool] turn overran its {limit:.0f}s limit: {elapsed:.0f}s "
                 f"(+{elapsed - limit:.0f}s)")
        if monitor is not None:
            try:
                monitor.finish(text)
            except Exception:  # noqa: BLE001
                pass
        return ok, text

    # 看门狗轮询间隔。心跳仍然每 30 秒一行（平台把长时间无输出的进程当成卡死
    # 杀掉），但**守时的检查**要密得多：30 秒一跳意味着最多迟到 30 秒才动手。
    _POLL_SECS = 2.0
    _HEARTBEAT_SECS = 30.0
    # 掐断生效时线程毫秒级就收干净，这个宽限期一秒也花不掉；掐不断时它纯属
    # 白等，所以给短一点。回合超时的上界 = _POLL_SECS + _ABORT_GRACE_SECS。
    _ABORT_GRACE_SECS = 1.0

    def _run_with_heartbeat(self, fn, deadline: float | None = None,
                            limit: float | None = None) -> tuple[bool, str]:
        """在子线程里跑回合：每 30 秒一行心跳，到点就掐断正在飞的请求。

        两件事必须在同一个循环里做，因为它是唯一知道现在几点的地方。心跳是给平台
        看的（它在进程长时间无输出时会当成卡死）；掐断是给 main.py 的预算看的。
        """
        box: dict = {}

        def target() -> None:
            try:
                box["r"] = fn()
            except Exception as exc:  # noqa: BLE001
                box["r"] = (False, f"tool turn raised: {exc}"[:1000])

        th = threading.Thread(target=target, daemon=True)
        th.start()
        t0 = time.time()
        next_beat = self._HEARTBEAT_SECS
        aborted = False
        while True:
            th.join(self._POLL_SECS)
            if not th.is_alive():
                break
            elapsed = time.time() - t0
            if elapsed >= next_beat:
                next_beat += self._HEARTBEAT_SECS
                _log(f"[flow] turn still running ({int(elapsed)}s elapsed)")
            if deadline is not None and not aborted and time.monotonic() >= deadline:
                aborted = True
                _log(f"[tool] turn hit its {limit:.0f}s limit at {int(elapsed)}s; "
                     f"aborting the request in flight")
                self._abort_inflight()
                th.join(self._ABORT_GRACE_SECS)
                if th.is_alive():
                    # 请求线程还没收干净也不再等了 —— 继续等下去正是要修的那个毛病。
                    # 它是守护线程，进程退出时会被带走；client 侧断开之后代理那边
                    # 那次上游调用也已经没有意义了。
                    _log(f"[tool] turn abandoned at {int(time.time() - t0)}s "
                         f"(the request thread is still unwinding)")
                    return False, f"tool turn timed out after {limit:.0f}s"
                break
        return box.get("r", (False, "turn thread ended without result"))

    def _abort_inflight(self) -> None:
        """尽力叫醒正在飞的那个请求 —— 叫不醒也没关系，回合照样准时返回。

        **能力边界要说清楚**，免得下次又以为它一定能掐断：

        - 卡在 `resp.read()` 上（响应头已经收到、正文没来）时**能**掐断：
          对底下的 socket 做 shutdown 会让阻塞中的 recv 立刻返回。
        - 卡在 `open_upstream()` 里（上游连状态行都不发）时**掐不断** ——
          那一刻还没有响应对象可关。`urllib` 没留任何接缝能在
          `getresponse()` 之前拿到连接，要拿到就得把 `do_open` 整个重写，
          而 `open_upstream` 里那段「回环必须绕开系统代理」的注释是踩过坑
          写下的，不值得为这个去动它。

        两种情况下 `_run_with_heartbeat` 都会在宽限期后放弃这个线程，
        所以**回合的墙钟是有保证的，被放弃的上游请求不是**。
        """
        self._aborted = True          # 让 _chat 把那句异常报成「超时」，别触发重试
        with self._inflight_lock:
            resp = self._inflight
        if resp is None:
            return
        # 只踹 socket，**绝不 resp.close()**：close() 走的是 BufferedReader，那把
        # 锁正被阻塞在 read() 里的线程拿着 —— 看门狗会被堵死在那儿，宽限期和
        # 放弃都轮不到执行（实测这一堵就是 26 秒，正好是要修的那个毛病）。
        #
        # 另外，Windows 11 / Python 3.10 上实测过：shutdown(SHUT_RDWR)、
        # shutdown(SHUT_RD)、close() 三个都**叫不醒**阻塞在另一个线程里的 recv，
        # 全都要等 socket 自己的超时。Linux 上 shutdown 是会唤醒的，云机是
        # Linux，所以这一段值得留。两边都别指望它 —— 真正保证回合准时返回的
        # 是上面那个放弃逻辑。
        try:
            sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ 主循环

    def _session(self, prompt: str, deadline: float) -> tuple[bool, str]:
        # deadline 由 run() 算好传进来 —— 看门狗和这个循环必须共用同一条线，
        # 各算各的就会一个掐断、另一个还在等。
        messages: list[dict] = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": prompt},
        ]
        tools = self._offered_tools()
        if tools:
            _log(f"[tool] offering {len(tools)} tool(s): {', '.join(t['function']['name'] for t in tools)}")
        cap = self._iteration_cap()
        last_text = ""

        for iteration in range(1, cap + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, ("tool turn timed out" + (f"; last reply: {last_text[-300:]!r}" if last_text else ""))

            # 重试只包住**一个 HTTP 请求**，绝不包住整个回合：工具回合不是幂等的，
            # 重放会把 write_file / shell 再执行一遍。（LocalDriver 能重试整回合，
            # 是因为它的回合是无状态的。）
            ok, msg, err = self._chat_with_retries(messages, tools, remaining, deadline)
            if not ok:
                return False, err

            text = self._message_text(msg)
            if text:
                last_text = text

            calls = msg.get("tool_calls") or []
            if not calls:
                return True, last_text

            messages.append(self._assistant_message(msg, calls))
            for index, call in enumerate(calls):
                name, args, call_id, parse_err = self._parse_call(call, iteration, index)
                self._log_event("tool/started", {
                    "tool_call_id": call_id, "tool_name": name,
                    "arguments": args if parse_err is None else {},
                })
                t0 = time.time()
                if parse_err is not None:
                    ok_t, out = False, parse_err
                else:
                    ok_t, out = self._dispatch(name, args)
                self._log_event("tool/completed", {
                    "tool_call_id": call_id, "tool_name": name, "success": ok_t,
                    "output_preview": out[:300], "output_chars": len(out),
                    "duration_ms": int((time.time() - t0) * 1000),
                })
                messages.append({"role": "tool", "tool_call_id": call_id, "name": name,
                                 "content": truncate_head_tail(out, self._cap_for(name))})
            messages = self._fit(messages)
            tools = self._offered_tools()   # verify_text 每节点可能改 extra_drop_tools

        _log(f"[tool] iteration cap {cap} reached; handing the turn back with what was written")
        return True, last_text or f"(iteration cap {cap} reached)"

    # ------------------------------------------------------------ 请求

    def _chat_with_retries(self, messages: list[dict], tools: list[dict],
                           remaining: float, deadline: float) -> tuple[bool, dict, str]:
        last = (False, {}, "no attempt")
        for attempt in range(1, _ATTEMPTS + 1):
            budget = min(remaining, deadline - time.monotonic())
            if budget <= 0:
                return False, {}, "tool turn timed out"
            ok, msg, err = self._chat(messages, tools, budget)
            if ok:
                return True, msg, ""
            last = (False, {}, err)
            if not self._transient(err) or attempt == _ATTEMPTS:
                break
            wait = min(5 * attempt, 20)
            if wait >= deadline - time.monotonic():
                break
            _log(f"[tool] transient failure (attempt {attempt}/{_ATTEMPTS}): {err[-200:]!r}")
            time.sleep(wait)
        return last

    # 超过这个秒数的单个请求单独打一行日志。
    _SLOW_REQUEST_SECS = 20.0

    def _chat(self, messages: list[dict], tools: list[dict],
              timeout: float) -> tuple[bool, dict, str]:
        base, model, key = self._config()
        if not base:
            return False, {}, "no OPENAI_BASE_URL configured"
        if not model:
            return False, {}, "no model configured (set MODEL or OCTOS_MODEL)"

        payload: dict = {"model": model, "messages": messages, "stream": False,
                         "max_tokens": DEFAULT_MAX_TOKENS}
        # tools_disabled 时不带 tools 字段：代理那边已经会剥，再发就是协议违规。
        if tools and not self.tools_disabled:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if key:
            headers["Authorization"] = "Bearer " + key

        req = urllib.request.Request(base + "/chat/completions", data=body, headers=headers, method="POST")
        sock_timeout = max(30, int(timeout))
        t0 = time.time()
        try:
            # open_upstream：回环地址必须绕过系统代理。带系统代理时同一个请求体
            # 会被代理吞掉（llm_proxy.py 里记着这个坑）。
            with open_upstream(req, sock_timeout, base) as resp:
                # 登记给看门狗：到点它会从另一个线程关掉这个响应，把下面这句
                # read() 叫醒。sock_timeout 只挡得住「上游彻底不说话」。
                with self._inflight_lock:
                    self._inflight = resp
                try:
                    raw, status = resp.read(), resp.status
                finally:
                    with self._inflight_lock:
                        self._inflight = None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            return False, {}, f"HTTP {exc.code} from model endpoint: {detail}"
        except Exception as exc:  # noqa: BLE001
            if self._aborted:
                # 是我们自己掐的。措辞要和 _transient() 认得的那句一致，
                # 否则会白白重试一次刚被掐断的请求。
                return False, {}, "tool turn timed out"
            return False, {}, f"{type(exc).__name__}: {exc}"[:800]

        spent = time.time() - t0
        if spent >= self._SLOW_REQUEST_SECS:
            # 慢请求单独记一笔。上游卡住和「模型想得久」在别处长得一模一样，
            # 这条日志是唯一能区分的：完成 token 少而耗时长，就是上游卡住。
            _log(f"[tool] request took {spent:.0f}s (socket timeout {sock_timeout}s, "
                 f"prompt {len(body)} bytes)")

        if status != 200:
            return False, {}, f"HTTP {status}: {raw.decode('utf-8', 'replace')[:800]}"

        msg = self._extract_message(raw)
        if msg is None:
            return False, {}, f"unparseable reply: {raw.decode('utf-8', 'replace')[:800]}"
        if msg.get("finish_reason") == "length":
            # main.py 靠 "truncated" 这个字触发「一次一个文件」的重试。
            return False, {}, ("response truncated by the output limit; nothing was saved. "
                               "truncated")[:800]
        return True, msg, ""

    @staticmethod
    def _transient(text: str) -> bool:
        lowered = text.lower()
        if "tool turn timed out" in lowered:
            return False  # 自己的墙钟上限，不是上游抖动
        codes = {int(c) for c in re.findall(r"\bhttp(?:/\d(?:\.\d)?)?\s+(\d{3})\b", lowered)}
        if codes:
            return bool(codes & _TRANSIENT_CODES)
        return any(k in lowered for k in ("timed out", "connection reset", "connection aborted",
                                          "remote end closed", "temporarily unavailable"))

    @staticmethod
    def _extract_message(raw: bytes) -> dict | None:
        """取 choices[0].message，**连 reasoning_content 一起**。

        LocalDriver 丢掉 reasoning_content 是对的（那是思考过程，不是产物），
        但在工具循环里必须原样带回去：DeepSeek 要求带 tool_calls 的助手消息
        把它回传，否则下一个请求 400。缺了就是「第一轮能跑、第二轮全挂」。
        """
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") or {}
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        if isinstance(content, list):
            # 少数网关把 content 拆成 [{type: text, text: ...}]
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        out: dict = {"content": content if isinstance(content, str) else ("" if content is None else str(content)),
                     "tool_calls": message.get("tool_calls") or [],
                     "finish_reason": first.get("finish_reason")}
        for key in ("reasoning_content", "reasoning"):
            value = message.get(key)
            if isinstance(value, str) and value:
                out[key] = value
        return out

    @staticmethod
    def _message_text(msg: dict) -> str:
        text = msg.get("content") or ""
        return text if isinstance(text, str) else str(text)

    @staticmethod
    def _assistant_message(msg: dict, calls: list) -> dict:
        """回传给上游的助手行。只带协议要求的字段，外加（存在时的）reasoning_content。"""
        out: dict = {"role": "assistant", "content": msg.get("content") or ""}
        for key in ("reasoning_content", "reasoning"):
            if msg.get(key):
                out[key] = msg[key]
        out["tool_calls"] = [
            {
                "id": str(c.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str((c.get("function") or {}).get("name") or ""),
                    "arguments": (c.get("function") or {}).get("arguments") or "{}",
                },
            }
            for c in calls if isinstance(c, dict)
        ]
        return out

    @staticmethod
    def _parse_call(call: dict, iteration: int, index: int) -> tuple[str, dict, str, str | None]:
        fn = (call.get("function") or {}) if isinstance(call, dict) else {}
        name = str(fn.get("name") or "")
        call_id = str(call.get("id") or f"call_{iteration}_{index}")
        raw = fn.get("arguments")
        if isinstance(raw, dict):
            return name, raw, call_id, None
        text = raw if isinstance(raw, str) else "{}"
        try:
            args = json.loads(text or "{}")
        except ValueError:
            # 一次修复尝试：有些模型在 JSON 外面裹一层代码围栏。
            stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
            try:
                args = json.loads(stripped)
            except ValueError:
                return name, {}, call_id, f"could not parse arguments as JSON: {text[:300]!r}"
        if not isinstance(args, dict):
            return name, {}, call_id, f"arguments must be a JSON object, got {type(args).__name__}"
        return name, args, call_id, None

    # ------------------------------------------------------------ 上下文

    def _context_chars(self) -> int:
        try:
            return max(8000, int(os.environ.get("OCTOS_ARC_TOOL_CONTEXT_CHARS", "400000")))
        except ValueError:
            return 400000

    def _fit(self, messages: list[dict]) -> list[dict]:
        """把整段对话压进一个字符预算里，按「从旧到新」的顺序丢内容。

        两条硬约束：
          1. 带 tool_calls 的助手行，和它每个 tool_call_id 对应的工具行，
             **必须成对存在** —— 拆开任何一个，上游直接 400。
             所以这里只清空 content，从不删消息对象。
          2. system / 第一条 user（harness 的提示词，带 spec）/ 最后一条 user /
             最近 6 条，永不清理。
        """
        budget = self._context_chars()
        total = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        if total <= budget:
            return messages

        protected = {0, 1, len(messages) - 1}
        protected |= set(range(max(0, len(messages) - 6), len(messages)))
        elided = 0
        for i, msg in enumerate(messages):
            if total <= budget:
                break
            if i in protected or msg.get("role") == "system":
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content:
                continue
            size = len(json.dumps(msg, ensure_ascii=False))
            replacement = f"<elided: {len(content)} chars of {msg.get('role')} content; re-read if needed>"
            msg["content"] = replacement
            total -= size - len(json.dumps(msg, ensure_ascii=False))
            elided += 1
        if elided:
            _log(f"[tool] context over {budget} chars: elided the content of {elided} message(s)")
        return messages

    # ------------------------------------------------------------ 事件

    def _log_event(self, method: str, params: dict) -> None:
        """与 main.py:854 的 _log_event 同构：喂给 guard，再落一行 JSONL。

        guard.TurnMonitor 整层在 v2 的本地运行里是死的 —— 它完全靠
        tool/started 与 tool/completed 驱动，而 codegen 档没有工具事件。
        这一档恢复之后它才真正开始工作。
        """
        if self.monitor is not None and method in ("tool/started", "tool/completed"):
            try:
                self.monitor.observe(method, params)
            except Exception:  # noqa: BLE001 - 守卫绝不能把一回合弄挂
                pass
        if self.events_log is None:
            return
        try:
            self.events_log.parent.mkdir(parents=True, exist_ok=True)
            with self.events_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"method": method, "params": params}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------ 工具注册

    def _build_registry(self) -> dict[str, Tool]:
        return {t.name: t for t in (
            Tool("read_file", "Read a text file, optionally a line range. Prefer a range over a whole large file.",
                 _schema({"path": _p_str("File path, relative to the application directory."),
                          "start_line": _p_int("First line to return (1-based)."),
                          "end_line": _p_int("Last line to return (inclusive)."),
                          "limit": _p_int("Maximum number of lines to return.")},
                         ["path"]),
                 self._t_read_file, CAP_READ),

            Tool("list_dir", "List the entries of one directory (names only, directories marked with a trailing slash).",
                 _schema({"path": _p_str("Directory path. Defaults to the application directory.")},
                         ["path"]),
                 self._t_list_dir, CAP_LIST),

            Tool("glob", "Find files by path pattern. `**` matches any number of directories, e.g. `frontend/**/*.js`.",
                 _schema({"pattern": _p_str("Glob pattern, POSIX separators."),
                          "limit": _p_int("Maximum number of paths to return.", 100)},
                         ["pattern"]),
                 self._t_glob, CAP_LIST),

            Tool("grep", "Search file contents with a regular expression. Returns `path:line: text`.",
                 _schema({"pattern": _p_str("Regular expression to search for."),
                          "path": _p_str("File or directory to search. Defaults to the application directory."),
                          "file_pattern": _p_str("Only search files whose name matches this glob, e.g. `*.js`."),
                          "limit": _p_int("Maximum number of matches to return.", 50),
                          "context": _p_int("Lines of context to show around each match.", 0),
                          "ignore_case": _p_bool("Case-insensitive match.")},
                         ["pattern"]),
                 self._t_grep, CAP_GREP),

            Tool("write_file", "Write a complete file, creating parent directories. Overwrites the file if it exists. "
                               "Always send the whole file, never a fragment.",
                 _schema({"path": _p_str("File path, relative to the application directory."),
                          "content": _p_str("The complete file contents.")},
                         ["path", "content"]),
                 self._t_write_file, CAP_WRITE),

            Tool("edit_file", "Replace one exact string in a file. `old_string` must appear exactly once; "
                              "include enough surrounding lines to make it unique.",
                 _schema({"path": _p_str("File path, relative to the application directory."),
                          "old_string": _p_str("The exact text to replace."),
                          "new_string": _p_str("The replacement text.")},
                         ["path", "old_string", "new_string"]),
                 self._t_edit_file, CAP_WRITE),

            Tool("shell", "Run a POSIX shell command in the application directory and return its combined "
                          "stdout/stderr and exit code. Backslashes, quotes and expansions are the shell's, not the "
                          "tool's. Set `workdir` instead of prefixing `cd`. Never bind the grading port; the harness "
                          "starts its own server after your turn.",
                 _schema({"command": _p_str("The shell command to run."),
                          "workdir": _p_str("Working directory. Defaults to the application directory."),
                          "timeout_secs": _p_int("Kill the command after this many seconds (1-600).", 120),
                          "background": _p_bool("Start it and return the PID without waiting.")},
                         ["command"]),
                 self._t_shell, CAP_SHELL, shell_family=True),
        )}

    def _offered_tools(self) -> list[dict]:
        """本回合真正递给模型的 schema。

        给出的名字与代理会剥的名字必须不相交 —— 递过去又被剥掉，等于换来一个
        什么都做不了的空回合。代理那边除了 DROP_TOOLS 还会按 extra_drop_tools
        剥 shell（minimal 档），那一条不用在这里重复：剥掉之后模型根本看不见，
        行为是一致的，只是多花了几百字节的请求体。
        """
        if self.tools_disabled:
            return []
        return [
            {"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": t.parameters}}
            for t in self._registry.values() if t.name not in _PROXY_DROP_TOOLS
        ]

    def _cap_for(self, name: str) -> int:
        tool = self._registry.get(name)
        return tool.cap if tool else CAP_READ

    def _dispatch(self, name: str, args: dict) -> tuple[bool, str]:
        tool = self._registry.get(name)
        if tool is None:
            known = ", ".join(sorted(self._registry))
            return False, f"unknown tool {name!r}; available tools: {known}"
        if self.tools_disabled and tool.shell_family:
            return False, f"{name} is not available in this turn"
        try:
            return tool.run(args)
        except ToolDenied as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 - 一个工具炸了不该终结整轮
            return False, f"{name} failed: {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------ 路径

    def _protected_roots(self) -> list[str]:
        """受保护目录从 main.py 注入的 hook 里读，不另开一条通道。

        protected_hooks() 的形状是 [{"event","command","timeout_ms","tool_filter"}]，
        目录就在 command[2:]（前两项是 python 解释器和 deny_protected.py）。
        """
        roots: list[str] = []
        for hook in self.hooks or []:
            if not isinstance(hook, dict):
                continue
            command = hook.get("command") or []
            if len(command) > 2 and str(command[1]).endswith("deny_protected.py"):
                roots.extend(str(p) for p in command[2:] if p)
        for extra in (getattr(self, "protected_dirs", None) or []):
            roots.append(str(extra))
        return [os.path.realpath(p) for p in roots if p]

    @staticmethod
    def _norm(path: str) -> str:
        """Windows 上大小写不敏感，deny_protected.py 的字符串前缀比较会被
        `...\\TESTS\\x` 这种写法绕过。normcase 在 Linux 上是恒等，所以这里
        两边同码，只把 Windows 的洞补上。"""
        return os.path.normcase(os.path.realpath(path))

    def _deny(self, target: str) -> str | None:
        real = self._norm(target)
        for root in self._protected_roots():
            norm_root = os.path.normcase(root)
            if real == norm_root or real.startswith(norm_root + os.sep):
                return (f"Denied: {target} is inside a protected directory (official tests / requirements). "
                        f"Those files are graded as-is and must never change. Write only under frontend/ and backend/.")
        return None

    _BAD_NAME = re.compile(r'[<>:"|?*\x00-\x1f]')
    _RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)}

    def _resolve(self, raw: str, *, for_write: bool) -> Path:
        """把模型给的路径变成一个可用的绝对路径。

        写入侧刻意比内核严一格：拒绝绝对路径 / 盘符 / UNC / `..`。理由是两个平台
        的失败方式**不一样**（Windows 上 `C:\\x` 静默写到别处，Linux 上写出一个
        名叫 `C:\\x` 的文件），本地多严一点至少不会掩盖云端的失败。
        读取侧不设限 —— 官方 spec 和需求文档就在应用目录之外，设计回合要读它们。
        """
        text = str(raw or "").strip().strip("`'\"")
        if not text:
            raise ToolDenied("Denied: empty path")
        # 本地提示词自己教了反斜杠（inline_sources 在 Windows 上打的是
        # `--- frontend\\src\\booking.html ---`），所以模型很可能照着写。
        # 两个平台都归一化，接受的输入集合才一样。
        text = text.replace("\\", "/")

        if for_write:
            if text.startswith("/") or re.match(r"^[A-Za-z]:", text) or text.startswith("//"):
                raise ToolDenied(f"Denied: {raw!r} must be a path relative to the application directory")
            parts = [p for p in text.split("/") if p not in ("", ".")]
            if ".." in parts:
                raise ToolDenied(f"Denied: {raw!r} must not contain `..`")
            for part in parts:
                stem = part.split(".")[0].lower()
                if self._BAD_NAME.search(part) or stem in self._RESERVED or part != part.rstrip(" ."):
                    raise ToolDenied(f"Denied: {raw!r} contains a name this platform cannot create portably")
            target = self.cwd / Path(*parts) if parts else self.cwd
        else:
            target = Path(text) if (text.startswith("/") or re.match(r"^[A-Za-z]:", text)) else self.cwd / text

        absolute = os.path.realpath(str(target))
        if for_write:
            deny = self._deny(absolute)
            if deny:
                raise ToolDenied(deny)
        return Path(absolute)

    # ------------------------------------------------------------ 遍历

    def _iter_files(self, root: Path) -> Iterator[Path]:
        if root.is_file():
            yield root
            return
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                yield Path(dirpath) / name

    @staticmethod
    def _match_glob(pattern: str, rel_posix: str) -> bool:
        """自己的 glob 匹配器，**不用 Path.glob / fnmatch.fnmatch**。

        两者都会在 Windows 上折大小写（fnmatch 走 os.path.normcase），于是
        `*.HTML` 在本地命中、在云端不命中 —— 正是「本地比云上松」。
        fnmatchcase 不折大小写，`**` 由这里自己实现（fnmatch 没有 `**` 语义）。
        """
        pat_parts = [p for p in pattern.replace("\\", "/").split("/") if p not in ("", ".")]
        path_parts = [p for p in rel_posix.split("/") if p not in ("", ".")]

        def walk(pi: int, si: int) -> bool:
            while pi < len(pat_parts):
                if pat_parts[pi] == "**":
                    if pi == len(pat_parts) - 1:
                        return True
                    return any(walk(pi + 1, k) for k in range(si, len(path_parts) + 1))
                if si >= len(path_parts) or not fnmatch.fnmatchcase(path_parts[si], pat_parts[pi]):
                    return False
                pi, si = pi + 1, si + 1
            return si == len(path_parts)

        return walk(0, 0)

    def _rel(self, path: Path) -> str:
        """输出里一律用 POSIX 分隔符。云端打的是 `/`；本地打 `\\` 会让模型
        学错分隔符，然后把它写进下一次的工具参数里。"""
        try:
            return path.relative_to(self.cwd).as_posix()
        except ValueError:
            return path.as_posix()

    # ------------------------------------------------------------ 各工具实现

    def _t_read_file(self, args: dict) -> tuple[bool, str]:
        path = self._resolve(args.get("path", ""), for_write=False)
        if not path.is_file():
            return False, f"no such file: {self._rel(path)}"
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
        start = max(1, int(args.get("start_line") or 1))
        end = int(args.get("end_line") or 0)
        limit = int(args.get("limit") or 0)
        if end <= 0:
            end = start + limit - 1 if limit > 0 else len(lines)
        window = lines[start - 1:end]
        body = "\n".join(f"{start + i}\t{line}" for i, line in enumerate(window))
        header = f"{self._rel(path)} lines {start}-{start + len(window) - 1} of {len(lines)}\n"
        return True, header + body

    def _t_list_dir(self, args: dict) -> tuple[bool, str]:
        path = self._resolve(args.get("path") or ".", for_write=False)
        if not path.is_dir():
            return False, f"not a directory: {self._rel(path)}"
        entries = []
        for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            entries.append(f"{entry.name}/" if entry.is_dir() else entry.name)
        return True, f"{self._rel(path)}\n" + "\n".join(entries)

    def _t_glob(self, args: dict) -> tuple[bool, str]:
        pattern = str(args.get("pattern") or "").strip()
        if not pattern:
            return False, "pattern is required"
        limit = max(1, int(args.get("limit") or 100))
        hits: list[str] = []
        for path in self._iter_files(self.cwd):
            if self._match_glob(pattern, self._rel(path)):
                hits.append(self._rel(path))
                if len(hits) >= limit:
                    break
        return True, "\n".join(hits) if hits else f"no files match {pattern!r}"

    def _t_grep(self, args: dict) -> tuple[bool, str]:
        pattern = str(args.get("pattern") or "")
        if not pattern:
            return False, "pattern is required"
        flags = re.IGNORECASE if args.get("ignore_case") else 0
        try:
            rx = re.compile(pattern, flags)
        except re.error as exc:
            return False, f"invalid regular expression: {exc}"
        root = self._resolve(args.get("path") or ".", for_write=False)
        file_pattern = str(args.get("file_pattern") or "")
        limit = max(1, int(args.get("limit") or 50))
        context = max(0, int(args.get("context") or 0))

        hits: list[str] = []
        for path in self._iter_files(root):
            if file_pattern and not fnmatch.fnmatchcase(path.name, file_pattern):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
            except OSError:
                continue
            rel = self._rel(path)
            for i, line in enumerate(lines):
                if not rx.search(line):
                    continue
                for c in range(max(0, i - context), min(len(lines), i + context + 1)):
                    prefix = ":" if c == i else "-"
                    hits.append(f"{rel}{prefix}{c + 1}{prefix} {lines[c][:_GREP_LINE_CAP]}")
                if len(hits) >= limit:
                    return True, "\n".join(hits[:limit]) + f"\n(match limit {limit} reached)"
        return True, "\n".join(hits) if hits else f"no matches for {pattern!r}"

    def _t_write_file(self, args: dict) -> tuple[bool, str]:
        raw = args.get("path", "")
        content = args.get("content")
        if content is None:
            return False, "content is required"
        path = self._resolve(raw, for_write=True)
        body = str(content)
        if path.suffix.lower() in (".html", ".htm"):
            # 没有 charset 声明的页面会被 Chromium 按 Latin-1 解，中文全变 mojibake
            # （local s5/s10: 0/6）。
            body = ensure_charset(body)
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n"：默认的 newline=None 会把 \n 翻成 os.linesep，于是同一份
        # 模型输出在 Windows 上落成 CRLF、在 Linux 上落成 LF。
        path.write_text(body, encoding="utf-8", newline="\n")
        if path.suffix.lower() in (".js", ".cjs", ".mjs"):
            repair_flattened_js(path)
        return True, f"wrote {self._rel(path)} ({len(body)} chars, {body.count(chr(10)) + 1} lines)"

    def _t_edit_file(self, args: dict) -> tuple[bool, str]:
        raw = args.get("path", "")
        old = args.get("old_string")
        new = args.get("new_string")
        if old is None or new is None:
            return False, "old_string and new_string are required"
        old, new = str(old), str(new)
        path = self._resolve(raw, for_write=True)
        if not path.is_file():
            return False, f"no such file: {self._rel(path)}"
        text = path.read_text(encoding="utf-8", errors="replace")
        if old == new:
            return False, "old_string and new_string are identical; nothing to change"
        if text.count(old) > 1:
            return False, (f"old_string appears {text.count(old)} times in {self._rel(path)}; "
                           f"include more surrounding lines to make it unique")
        span = _find_span(text, old)
        if span is None:
            return False, (f"old_string not found in {self._rel(path)}. Read the file again and quote it exactly, "
                           f"or use write_file to replace the whole file.")
        updated = text[:span[0]] + new + text[span[1]:]
        if path.suffix.lower() in (".html", ".htm"):
            updated = ensure_charset(updated)
        path.write_text(updated, encoding="utf-8", newline="\n")
        if path.suffix.lower() in (".js", ".cjs", ".mjs"):
            repair_flattened_js(path)
        return True, f"edited {self._rel(path)} (-{len(old)} +{len(new)} chars)"

    # ------------------------------------------------------------ shell

    def _sh_path(self) -> str | None:
        """POSIX shell 的位置。**绝不回退到 cmd.exe。**

        判分机是 Linux，harness 自己的提示词就是 POSIX 写法
        （`( ... ) < /dev/null > smoke-server.log 2>&1 &`、`echo $!`、`&&`）。
        交给 cmd.exe 的话，本地每一个 shell 回合都会失败、云端却成功 —— 本地
        就不再是云端的预测量，而且变差的原因不在模型身上。

        发现逻辑复用 acceptance.npm_script_shell_env()：v2 已经在那里把 npm 的
        script-shell 指到 Git 的 sh，同一个来源，不会再漂移。
        """
        explicit = os.environ.get("OCTOS_SH")
        if explicit:
            return explicit if Path(explicit).is_file() else None
        if sys.platform == "win32":
            return npm_script_shell_env().get("npm_config_script_shell")
        for candidate in ("/bin/sh", shutil.which("sh"), shutil.which("bash")):
            if candidate and Path(candidate).is_file():
                return candidate
        return None

    def _shell_env(self) -> dict:
        env = dict(os.environ)
        if sys.platform == "win32":
            # MSYS 会把「看起来像路径」的 argv 悄悄改写；Linux 从不这么做。
            env["MSYS_NO_PATHCONV"] = "1"
            env["MSYS2_ARG_CONV_EXCL"] = "*"
        return env

    def _t_shell(self, args: dict) -> tuple[bool, str]:
        command = str(args.get("command") or "")
        if not command.strip():
            return False, "command is required"
        workdir = args.get("workdir")
        cwd = self._resolve(workdir, for_write=True) if workdir else Path(os.path.realpath(str(self.cwd)))
        if not cwd.is_dir():
            return False, f"workdir is not a directory: {self._rel(cwd)}"

        sh = self._sh_path()
        if sh is None:
            # 优雅失败：整轮还能继续（模型可以只写文件），而不是抛异常。
            return False, ("no POSIX shell available on this host (looked for sh/bash, and Git's sh on Windows); "
                           "use the file tools instead")

        try:
            timeout = int(args.get("timeout_secs") or 120)
        except (TypeError, ValueError):
            return False, "timeout_secs must be an integer"
        timeout = max(1, min(600, timeout))

        argv = [sh, "-c", command]
        popen_kw: dict = {"cwd": str(cwd), "env": self._shell_env(), "stdin": subprocess.DEVNULL}
        if sys.platform == "win32":
            popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_kw["start_new_session"] = True   # 整组杀得掉

        if args.get("background"):
            log_path = cwd / "shell-background.log"
            with log_path.open("ab") as fh:
                proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, **popen_kw)
            self._children.append(proc)
            return True, (f"started in background, pid={proc.pid}, output -> {self._rel(log_path)}. "
                          f"It is killed when this turn ends; do not rely on it outliving the turn.")

        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **popen_kw)
        except OSError as exc:
            return False, f"could not start {sh}: {exc}"
        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            try:
                out, _ = proc.communicate(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                out = b""
            text = out.decode("utf-8", "replace")
            return False, (f"command exceeded its {timeout}s limit and was killed (exit 124)\n"
                           + truncate_head_tail(text, 4000))
        text = out.decode("utf-8", "replace")
        code = proc.returncode
        tail = f"\n(exit code {code})"
        if code != 0:
            return False, truncate_head_tail(text, CAP_SHELL - len(tail)) + tail
        return True, (truncate_head_tail(text, CAP_SHELL - len(tail)) if text else "(no output)") + tail

    # ------------------------------------------------------------ 系统提示

    @staticmethod
    def _system_prompt() -> str:
        """刻意短且静态（前缀缓存按它对齐）。

        标题里绝不能出现 llm_proxy.DROP_SECTIONS 里那些 `##` 段名
        （`## Background Tasks`、`## Queue Behavior` 之类）—— trim_system_prompt
        会把它们从系统提示里整段删掉。
        """
        return (
            "You are a coding agent working inside a generated web application.\n"
            "Every change to the application must go through a tool call: use read_file, list_dir, glob and grep to "
            "inspect, and write_file or edit_file to change files. Never claim to have changed a file you did not "
            "change with a tool.\n"
            "Prefer editing an existing file over rewriting it. Send write_file the complete file, never a fragment.\n"
            "The application directory is the working directory; paths are relative to it and use forward slashes.\n"
            "The official tests and requirements directories are read-only: reading them is expected, writing to them "
            "is denied.\n"
            "Verify with the shell tool before you finish: build the frontend, start the backend on the smoke port, "
            "curl the endpoints you changed, then stop the server. Never bind the grading port.\n"
            "When the work is done, reply with a short summary and no tool calls."
        )


# ====================================================================== 自检


def _selftest() -> int:
    """离线自检：不联网、不调模型，逐个工具走一遍并断言行为。

    两个平台各跑一次、把输出 diff 一遍 —— 这是唯一能便宜地抓住
    「Windows 接受、Linux 拒绝」那类静默分歧的机制。
    """
    import tempfile

    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {label}{'' if cond else '  <- ' + detail}")
        if not cond:
            failures.append(label)

    root = Path(tempfile.mkdtemp(prefix="tool-driver-selftest-"))
    protected = root / "official-tests"
    protected.mkdir(parents=True, exist_ok=True)
    (protected / "REQ-1.1-x.spec.ts").write_text("// official\n", encoding="utf-8")

    app = root / "app"
    (app / "frontend" / "src").mkdir(parents=True, exist_ok=True)
    (app / "backend").mkdir(parents=True, exist_ok=True)
    (app / "backend" / "server.js").write_text("const x = 1;\nconst y = 2;\n", encoding="utf-8")
    (app / "frontend" / "src" / "index.html").write_text(
        "<html><body>你好</body></html>\n", encoding="utf-8")
    (app / "frontend" / "src" / "UP.HTML").write_text("<html>upper</html>\n", encoding="utf-8")
    (app / "node_modules" / "junk").mkdir(parents=True, exist_ok=True)
    (app / "node_modules" / "junk" / "index.js").write_text("nope\n", encoding="utf-8")

    print("environment")
    print(f"  platform={sys.platform} linesep={os.linesep!r} default_encoding={sys.getdefaultencoding()}")
    print(f"  python={sys.executable}")
    print(f"  node={shutil.which('node')}")
    driver = ToolDriver(app, dict(os.environ), None, 60, None)
    sh = driver._sh_path()
    print(f"  shell={sh}")
    if sh is None:
        print("  WARNING: no POSIX shell — the shell tool will refuse every command on this host")

    # hooks 的注入通道（main.py:3775 的形状）
    driver.hooks = [{"event": "before_tool_call",
                     "command": [sys.executable, str(Path(__file__).parent / "hooks" / "deny_protected.py"),
                                 str(protected)],
                     "timeout_ms": 4000,
                     "tool_filter": sorted(_WRITE_TOOL_FALLBACK)}]

    print("tools")
    names = [t["function"]["name"] for t in driver._offered_tools()]
    print(f"  offered={names}")
    check("schema names disjoint from proxy DROP_TOOLS",
          not (set(names) & _PROXY_DROP_TOOLS), str(set(names) & _PROXY_DROP_TOOLS))
    check("every advertised tool has an implementation",
          all(n in driver._registry for n in names))
    check("every implementation is advertised", sorted(names) == sorted(driver._registry))

    ok, out = driver._t_read_file({"path": "backend/server.js", "start_line": 2})
    check("read_file range", ok and "const y" in out and "const x" not in out, out[:120])
    ok, out = driver._t_list_dir({"path": "frontend"})
    check("list_dir marks directories", ok and "src/" in out, out[:120])
    ok, out = driver._t_glob({"pattern": "backend/**/*.js"})
    check("glob ** nesting", ok and "backend/server.js" in out, out[:160])
    ok, out = driver._t_glob({"pattern": "**/*.HTML"})
    check("glob is case-sensitive (no Windows folding)",
          ok and "UP.HTML" in out and "index.html" not in out, out[:160])
    ok, out = driver._t_glob({"pattern": "**/*"})
    check("walk skips node_modules", ok and "node_modules" not in out, out[:200])
    ok, out = driver._t_grep({"pattern": "const\\s+y"})
    check("grep finds a match with path:line", ok and "backend/server.js:" in out, out[:160])
    ok, out = driver._t_grep({"pattern": "("})
    check("grep rejects a bad regex without raising", not ok, out[:120])

    ok, out = driver._t_write_file({"path": "frontend/src/new.html", "content": "<html>你好</html>\n"})
    written = app / "frontend" / "src" / "new.html"
    check("write_file creates the page", ok and written.is_file(), out[:160])
    check("write_file injects charset", written.is_file() and "charset" in written.read_text(encoding="utf-8").lower())
    check("write_file uses LF on every platform", written.is_file() and b"\r\n" not in written.read_bytes())
    check("write_file reports a relative POSIX path", ok and "frontend/src/new.html" in out, out[:160])

    ok, out = driver._t_write_file({
        "path": "backend/server.js",
        "content": "const x = 1;\nconst y = 2;\nconst z = 3;\n"})
    check("write_file overwrites", ok and "const z" in (app / "backend" / "server.js").read_text(encoding="utf-8"))
    ok, out = driver._t_edit_file({"path": "backend/server.js", "old_string": "const z = 3;", "new_string": "const z = 4;"})
    check("edit_file exact match", ok and "const z = 4;" in (app / "backend" / "server.js").read_text(encoding="utf-8"), out[:160])
    ok, out = driver._t_edit_file({"path": "backend/server.js", "old_string": "const x = 1;", "new_string": "const x = 1;"})
    check("edit_file rejects a no-op edit", not ok, out[:160])
    ok, out = driver._t_edit_file({"path": "backend/server.js", "old_string": "const nope;", "new_string": "x"})
    check("edit_file reports a miss", not ok, out[:160])

    print("path policy (identical accept set on both platforms)")
    for label, bad in [("backslash normalisation is accepted", None),
                       ("absolute path refused", "/etc/passwd"),
                       ("drive letter refused", "C:/Windows/x.txt"),
                       ("UNC refused", "//server/share/x.txt"),
                       ("dotdot refused", "../../escape.txt"),
                       ("illegal name refused", "backend/a<b>.js"),
                       ("trailing dot refused", "backend/weird.")]:
        if bad is None:
            ok, out = driver._t_write_file({"path": "frontend\\src\\bs.html", "content": "<html>x</html>\n"})
            check(label, ok and (app / "frontend" / "src" / "bs.html").is_file(), out[:160])
            continue
        try:
            driver._resolve(bad, for_write=True)
            check(label, False, "was accepted")
        except ToolDenied:
            check(label, True)
        except Exception as exc:  # noqa: BLE001
            check(label, False, f"{type(exc).__name__}: {exc}")

    print("protected paths")
    # 走 _dispatch —— 模型的一次 write_file 实际经过的就是这条路。
    # 直接调 _t_write_file 会绕过它，报出来的失败原因也就不是模型会看到的那个。
    protected_file = protected / "REQ-1.1-x.spec.ts"
    before = protected_file.read_text(encoding="utf-8")
    ok, out = driver._dispatch("write_file", {"path": str(protected_file), "content": "tampered"})
    check("absolute write into protected is refused",
          not ok and protected_file.read_text(encoding="utf-8") == before, out[:160])
    ok, out = driver._dispatch("edit_file", {"path": str(protected_file),
                                             "old_string": "// official", "new_string": "// tampered"})
    check("edit into protected is refused",
          not ok and protected_file.read_text(encoding="utf-8") == before, out[:160])
    check("protected dirs come from the injected hook", driver._protected_roots() != [],
          str(driver._protected_roots()))

    # 真正要靠受保护检查兜住的那一种：目录就在应用里面，路径是**相对**的，
    # 所以「拒绝绝对路径」那条帮不上忙，只有 _deny 拦得住。
    inner = app / "specs"
    inner.mkdir(parents=True, exist_ok=True)
    (inner / "REQ-9.9-y.spec.ts").write_text("// official inner\n", encoding="utf-8")
    driver.hooks.append({"event": "before_tool_call",
                         "command": [sys.executable, str(Path(__file__).parent / "hooks" / "deny_protected.py"),
                                     str(inner)],
                         "timeout_ms": 4000,
                         "tool_filter": sorted(_WRITE_TOOL_FALLBACK)})
    ok, out = driver._dispatch("write_file", {"path": "specs/REQ-9.9-y.spec.ts", "content": "tampered"})
    check("relative write into an in-app protected dir is refused by _deny",
          not ok and "protected" in out.lower()
          and (inner / "REQ-9.9-y.spec.ts").read_text(encoding="utf-8") == "// official inner\n", out[:200])
    ok, out = driver._dispatch("shell", {"command": "echo x", "workdir": "specs"})
    check("shell workdir inside protected is refused", not ok and "protected" in out.lower(), out[:200])

    ok, out = driver._dispatch("read_file", {"path": str(protected_file)})
    check("reading protected is allowed (design turns need it)", ok and "official" in out, out[:160])

    print("shell")
    if sh is not None:
        ok, out = driver._t_shell({"command": "echo hello && exit 3"})
        check("shell reports a non-zero exit", not ok and "exit code 3" in out and "hello" in out, out[:200])
        ok, out = driver._t_shell({"command": "pwd", "workdir": "backend"})
        # 只看第一行：末尾还有一行 `(exit code 0)`。
        check("shell honours workdir", ok and out.splitlines()[0].strip().endswith("backend"), out[:200])
        ok, out = driver._t_shell({"command": "sleep 5", "timeout_secs": 1})
        check("shell enforces its timeout", not ok and "1s limit" in out, out[:200])
    else:
        ok, out = driver._t_shell({"command": "echo hi"})
        check("shell degrades gracefully with no POSIX shell", not ok and "no POSIX shell" in out, out[:200])

    print("events")
    events = root / "events.jsonl"
    driver.events_log = events

    class _Spy:
        def __init__(self) -> None:
            self.seen: list[str] = []
            self.final = ""

        def observe(self, method: str, params: dict) -> None:
            self.seen.append(method)

        def finish(self, text: str) -> None:
            self.final = text

    spy = _Spy()
    driver.monitor = spy
    driver._log_event("tool/started", {"tool_call_id": "c1", "tool_name": "write_file", "arguments": {}})
    driver._log_event("tool/completed", {"tool_call_id": "c1", "success": True, "output_preview": "ok"})
    driver.monitor = None
    check("monitor sees both tool events", spy.seen == ["tool/started", "tool/completed"], str(spy.seen))
    check("events land in the JSONL log",
          events.is_file() and len(events.read_text(encoding="utf-8").strip().split("\n")) == 2)

    print("context fitting")
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    msgs.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
    msgs.append({"role": "tool", "tool_call_id": "a", "content": "x" * 5000})
    for i in range(8):
        msgs.append({"role": "assistant", "content": f"a{i}"})
        msgs.append({"role": "user", "content": f"u{i}"})
    os.environ["OCTOS_ARC_TOOL_CONTEXT_CHARS"] = "9000"
    fitted = driver._fit(msgs)
    check("fitting keeps the system message", fitted[0]["content"] == "s")
    check("fitting keeps the first user message", fitted[1]["content"] == "u")
    check("fitting keeps the tool/assistant pair intact",
          any(m.get("tool_call_id") == "a" for m in fitted)
          and any(m.get("tool_calls") for m in fitted))
    check("fitting elides content to get under budget",
          sum(len(json.dumps(m, ensure_ascii=False)) for m in fitted) <= 9000)
    os.environ.pop("OCTOS_ARC_TOOL_CONTEXT_CHARS", None)

    print()
    if failures:
        print(f"FAILED ({len(failures)}): {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("tool_driver: nothing to do. Run `python tool_driver.py --selftest`.")
