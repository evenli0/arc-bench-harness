"""纯 Python 驱动 —— 顶替 Rust 内核对 codegen / tiny 档位的全部职责。

为什么能顶替：在这两个档位里，内核剩下的工作只有一件事 ——
**把一段文本发给模型，把回的那段文本交回来。**
工具被摘掉了，会话是一次性的，沙箱不参与。所以这里用 urllib 直接顶上，
把 59MB 的 Linux 二进制依赖从提交包里整个去掉。

接口与 OctosDriver 对齐：run / without_tools / end_scope / close。
main.py 的其余部分（分档、提示词、验收、修复、预算）一行都不用动。

请求发往 llm_proxy（start_llm_proxy 已把 OPENAI_BASE_URL 改写到本地），
所以推理档位、提示词裁剪、工具剥离、token 预算这些策略仍然生效。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from llm_proxy import open_upstream

# 输出上限。deepseek-v4 会把默认的 4096 全花在思考上然后返回空内容；
# 一个完整的注册页 HTML 也远不止 4096。llm_proxy 的 ensure_max_tokens
# 会把更小的值抬到这里，所以这个数字只是「没有代理时的兜底」。
DEFAULT_MAX_TOKENS = int(os.environ.get("OCTOS_ARC_MAX_TOKENS", "32768"))

# 上游抖动时的重试：429 / 5xx / 连接被掐。认证类错误不重试。
_TRANSIENT_CODES = {408, 425, 429, 500, 502, 503, 504}
_ATTEMPTS = 3


def _log(msg: str) -> None:
    print(msg, flush=True)


class LocalDriver:
    """一次请求一个回合的无状态驱动。

    每回合都从 self.env 现读 base_url / key / model —— 因为 start_llm_proxy()
    会在构造之后改写 OPENAI_BASE_URL，缓存下来会打到错误的地址。
    """

    def __init__(self, cwd: Path, env: dict, data_dir: Path | None = None,
                 max_iterations: int | None = None, events_log: Path | None = None) -> None:
        self.cwd = cwd
        self.env = env
        self.data_dir = data_dir
        self.max_iterations = max_iterations
        self.events_log = events_log
        self.mode = "local"
        # 无状态 → 回合之间没有上下文可留，「每回合一个新会话」正是现状。
        self.session_scope = "turn"
        self.monitor = None
        self.hooks: list = []
        self.tools_disabled = False
        self.turns = 0

    # ------------------------------------------------------------ 配置

    def _config(self) -> tuple[str, str, str]:
        env = self.env
        base = (env.get("_ARC_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
        model = env.get("_ARC_MODEL") or os.environ.get("OCTOS_MODEL") or os.environ.get("MODEL") or ""
        key_env = env.get("_ARC_KEY_ENV") or "OPENAI_API_KEY"
        key = env.get(key_env) or env.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        return base, model, key

    # ------------------------------------------------------------ 接口

    @contextmanager
    def without_tools(self):
        """本驱动没有工具可摘 —— 它本来就不发工具，所以这里只是把开关翻一下。

        留这个方法是因为三个驱动（本驱动 / ToolDriver / OctosDriver）共用同一份
        接口：调用点不该因为「这回用的是哪个驱动」而改。真正的工具剥离发生在
        llm_proxy：proxy.no_tools 会让代理把请求体里的 tools / tool_choice 删掉。
        """
        previous = self.tools_disabled
        self.tools_disabled = True
        try:
            yield
        finally:
            self.tools_disabled = previous

    def end_scope(self, scope: str) -> None:
        """无状态，没有会话要关。"""
        return

    def close(self) -> None:
        return

    # ------------------------------------------------------------ 回合

    def run(self, prompt: str, timeout: int, monitor=None) -> tuple[bool, str]:
        self.monitor = monitor
        self.turns += 1
        try:
            return self._run_with_heartbeat(lambda: self._run_with_retries(prompt, timeout))
        finally:
            self.monitor = None

    def _run_with_heartbeat(self, fn) -> tuple[bool, str]:
        """在子线程里跑回合，每 30 秒打一行心跳。

        平台会在进程长时间无输出时把它当成卡死杀掉 —— 一句话都不说的
        十分钟请求和真的挂掉，在它眼里没有区别。
        """
        box: dict = {}

        def target() -> None:
            try:
                box["r"] = fn()
            except Exception as exc:  # noqa: BLE001 - 驱动绝不能把流程带崩
                box["r"] = (False, f"turn raised: {exc}"[:1000])

        th = threading.Thread(target=target, daemon=True)
        th.start()
        t0 = time.time()
        while True:
            th.join(30)
            if not th.is_alive():
                break
            _log(f"[flow] turn still running ({int(time.time() - t0)}s elapsed)")
        return box.get("r", (False, "turn thread ended without result"))

    def _run_with_retries(self, prompt: str, timeout: int) -> tuple[bool, str]:
        last = (False, "no attempt")
        deadline = time.monotonic() + timeout
        for attempt in range(1, _ATTEMPTS + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False, "local turn timed out"
            ok, text = self._one_request(prompt, remaining)
            if ok:
                return True, text
            last = (ok, text)
            if not self._transient(text) or attempt == _ATTEMPTS:
                break
            _log(f"[driver] transient failure (attempt {attempt}/{_ATTEMPTS}): {text[-200:]!r}")
            time.sleep(min(5 * attempt, 20))
        return last

    @staticmethod
    def _transient(text: str) -> bool:
        lowered = text.lower()
        if "local turn timed out" in lowered:
            return False  # 自己的墙钟上限，不是上游抖动
        codes = {int(c) for c in __import__("re").findall(r"\bhttp(?:/\d(?:\.\d)?)?\s+(\d{3})\b", lowered)}
        if codes:
            return bool(codes & _TRANSIENT_CODES)
        return any(k in lowered for k in ("timed out", "connection reset", "connection aborted",
                                          "remote end closed", "temporarily unavailable"))

    def _one_request(self, prompt: str, timeout: float) -> tuple[bool, str]:
        base, model, key = self._config()
        if not base:
            return False, "no OPENAI_BASE_URL configured"
        if not model:
            return False, "no model configured (set MODEL or OCTOS_MODEL)"

        body = json.dumps({
            "model": model,
            "messages": [
                # 占位 system：codegen 回合里 llm_proxy 会用 system_override
                # （tiny-system.md 那一行）整条替换掉它。
                {"role": "system", "content": "You are a coding agent."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "max_tokens": DEFAULT_MAX_TOKENS,
        }, ensure_ascii=False).encode("utf-8")

        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        if key:
            headers["Authorization"] = "Bearer " + key

        req = urllib.request.Request(base + "/chat/completions", data=body, headers=headers, method="POST")
        try:
            # open_upstream：回环地址必须绕过系统代理。带系统代理时同一个
            # 请求体会被代理吞掉（llm_proxy.py 里记着这个坑）。
            with open_upstream(req, max(30, int(timeout)), base) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:800]
            return False, f"HTTP {exc.code} from model endpoint: {detail}"
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"[:800]

        if status != 200:
            return False, f"HTTP {status}: {raw.decode('utf-8', 'replace')[:800]}"

        text = self._extract(raw)
        if text is None:
            return False, f"unparseable reply: {raw.decode('utf-8', 'replace')[:800]}"
        return True, text

    @staticmethod
    def _extract(raw: bytes) -> str | None:
        """从标准 chat completion 里取 content。

        reasoning_content 是思考过程，不是产物，丢掉。
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
        content = message.get("content")
        if content is None:
            return ""
        if isinstance(content, list):
            # 少数网关把 content 拆成 [{type: text, text: ...}]
            parts = [p.get("text", "") for p in content if isinstance(p, dict)]
            return "".join(parts)
        return str(content)
