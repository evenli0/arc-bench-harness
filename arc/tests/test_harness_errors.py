"""`octos/agent/harness_errors.py` 的对位测试。

12 条移植自 `crates/octos-agent/src/harness_errors.rs` 的 `#[cfg(test)]` 模块
（Rust 原名保留在每条 docstring 或注释里）。`#27b` 那对钉子是重点：
**配额耗尽是「供应商降级」不是「致命」**，必须给 `switch_provider`，
好让 ProviderChain 推进到备用泳道；而**真 401 在每条泳道上都会同样失败**，
必须留在 `fail_fast` —— 这条红线不许松。

另外三条是新增的：变体表的不变量（Rust 靠 `match` 穷尽性保证，Python 没有
那个检查，所以改成可断言的数据）、`LlmErrorKind` 全族覆盖、以及刚抓到的
`to_ascii_lowercase` 陷阱。
"""

from __future__ import annotations

import pytest

from octos.agent.harness_errors import (
    HARNESS_ERROR_SCHEMA_VERSION,
    HARNESS_EVENT_SCHEMA_V1,
    MAX_HARNESS_ERROR_MESSAGE_BYTES,
    OCTOS_LOOP_ERROR_TOTAL,
    RecoveryHint,
    _VARIANTS,
    HarnessError,
    HarnessErrorEvent,
)
from octos.agent.hooks import HookDeniedError
from octos.llm.errors import LlmError, LlmErrorKind, _ascii_lower, _take_chars


def _b(s: str) -> int:
    """字节长度 —— Rust 的 `String::len()` 是字节数，不是字符数。"""
    return len(s.encode("utf-8"))


# ── #27b 的钉子 ───────────────────────────────────────────────────────────

def test_quota_switches_provider_but_401_stays_fail_fast():
    """Rust: `quota_switches_provider_but_401_stays_fail_fast`

    #27b 钉住的那对契约：配额（HTTP 402 / 带计费标记的 429）是**供应商降级**，
    必须提示 `SwitchProvider`，好让 ProviderChain 推进到健康的备用泳道
    （实况证据：2026-08-26/27 那次 k3 七天窗口烧穿，配置好的备用槽位始终没被
    触发，就因为提示是 FailFast）；而**真** 401 鉴权失败在每条泳道上都一模一样
    地失败，必须留在 `FailFast` —— 这条契约不许松。
    """
    quota = HarnessError.quota(
        "API error (moonshot-coding@api/k3): 402 quota will reset when "
        "the current 7-day window ends")
    assert quota.recovery_hint() == RecoveryHint.SWITCH_PROVIDER, (
        "配额耗尽必须给 switch_provider，链才会推进到备用槽位"
    )

    auth = HarnessError.authentication("API error: 401 invalid api key")
    assert auth.recovery_hint() == RecoveryHint.FAIL_FAST, (
        "真 401 在每条泳道上都失败 —— FailFast 是 #27b 的红线"
    )


def test_quota_403_classifies_as_quota_not_internal():
    """Rust: `quota_403_classifies_as_quota_not_internal`

    dspfac（mini1）症状的回归测试：403 带 Wisemodel 的
    `no_active_wisemodel_package` 体，曾经报成 `variant=internal recovery=bug`，
    因为 `eyre::bail!()` 出来的 report 降级不到 `LlmError`。走新的
    `from_status_with_label` 之后，provider 把 HTTP 错误导流到 `LlmError`，
    harness 就能挑出 `Quota` 变体并给出可行动的消息。
    """
    body = ('{"error":{"code":"no_active_wisemodel_package",'
            '"message":"没有有效的 Wisemodel 资源包，请购买后再使用",'
            '"type":"insufficient_quota"}}')
    llm = LlmError.from_status_with_label(403, body, "MiniMax-M2.5-highspeed")
    err = HarnessError.from_llm_error(llm)

    assert err.variant_name() == "quota"
    assert err.recovery_hint() == RecoveryHint.SWITCH_PROVIDER, (
        "#27b —— 配额是供应商降级：给 switch_provider，让链推进到备用泳道"
        "（曾经是 FailFast，把配好的备用槽位困死了）"
    )
    assert "MiniMax-M2.5-highspeed" in err.message
    assert "top up or switch provider" in err.message


# ── 变体名与 hint 的联动 ──────────────────────────────────────────────────

def test_variant_name_covers_every_variant():
    """Rust: `variant_name_covers_every_variant`

    加了变体就往这里加一条。这条测试让 `variant_name()` / `recovery_hint()`
    与变体集合保持同步。
    """
    samples = [
        HarnessError.rate_limited("x"),
        HarnessError.context_overflow("x"),
        HarnessError.authentication("x"),
        HarnessError.quota("x"),
        HarnessError.invalid_request("x", detail="x"),
        HarnessError.content_filtered("x"),
        HarnessError.provider_unavailable("x"),
        HarnessError.network("x"),
        HarnessError.timeout("x"),
        HarnessError.tool_execution("x", tool_name="shell"),
        HarnessError.plugin_spawn("x", plugin_name="demo"),
        HarnessError.plugin_timeout("x", plugin_name="demo", timeout_secs=30),
        HarnessError.plugin_protocol("x", plugin_name="demo"),
        HarnessError.delegate_depth_exceeded("x", depth=3, limit=2),
        HarnessError.policy_deny("x"),
        HarnessError.internal("x"),
    ]
    for err in samples:
        assert err.variant_name()
        assert err.recovery_hint()
        ev = err.to_event("s", "t")
        assert ev["schema"] == HARNESS_EVENT_SCHEMA_V1


# ── classify_report ──────────────────────────────────────────────────────

def test_classify_report_with_tool_context_uses_tool_execution():
    """Rust: `classify_report_with_tool_context_uses_tool_execution`"""
    err = HarnessError.classify_report(
        RuntimeError("shell exited with status 1"), tool_name="shell")
    assert err.kind == "tool_execution"
    assert err.variant_name() == "tool_execution"
    assert err.tool_name == "shell"


def test_classify_report_without_tool_context_is_internal():
    """Rust: `classify_report_without_tool_context_is_internal`"""
    err = HarnessError.classify_report(RuntimeError("unexpected state"))
    assert err.kind == "internal"
    assert err.recovery_hint() == RecoveryHint.BUG


def test_classify_report_downcasts_llm_error():
    """Rust: `classify_report_downcasts_llm_error`"""
    llm = LlmError.from_status(429, "Too Many Requests")
    err = HarnessError.classify_report(llm, tool_name="shell")
    assert err.variant_name() == "rate_limited"


def test_classify_report_downcasts_hook_deny_through_eyre():
    """Rust: `classify_report_downcasts_hook_deny_through_eyre`

    #2249 —— `before_llm_call` 拒绝会抛类型化的 `HookDeniedError`；循环边界
    必须把它分类成 `policy` / `expected`，绝不能是 `internal` / `bug`
    （策略拒绝不该把人叫醒）。
    """
    classified = HarnessError.classify_report(
        HookDeniedError("no network calls today"))
    assert classified.variant_name() == "policy"
    assert classified.recovery_hint() == RecoveryHint.EXPECTED
    assert "no network calls today" in classified.message


def test_classify_report_downcasts_quota_through_eyre():
    """Rust: `classify_report_downcasts_quota_through_eyre`

    端到端：provider 会 `raise LlmError.from_status_with_label(...)`，
    循环边界的 `classify_report` 必须把 Quota 提取出来。
    """
    llm = LlmError.from_status_with_label(403, '{"type":"insufficient_quota"}', "lane-x")
    classified = HarnessError.classify_report(llm)
    assert classified.variant_name() == "quota"
    assert classified.recovery_hint() != RecoveryHint.BUG


def test_classify_report_downcasts_auth_403_through_eyre():
    """Rust: `classify_report_downcasts_auth_403_through_eyre`

    没有配额标记的 403 留在 Authentication。
    """
    llm = LlmError.from_status_with_label(403, "Forbidden", "openai")
    classified = HarnessError.classify_report(llm)
    assert classified.variant_name() == "authentication"
    assert classified.recovery_hint() == RecoveryHint.FAIL_FAST
    assert "openai" in classified.message
    assert "check your API key" in classified.message


def test_classify_report_downcasts_invalid_request_through_eyre():
    """Rust: `classify_report_downcasts_invalid_request_through_eyre`"""
    body = ('{"error":{"type":"invalid_request_error",'
            '"message":"unknown parameter"}}')
    llm = LlmError.from_status_with_label(400, body, "anthropic")
    classified = HarnessError.classify_report(llm)
    assert classified.variant_name() == "invalid_request"
    assert "Provider rejected the request" in classified.message
    assert "anthropic" in classified.message


# ── 事件载荷 ─────────────────────────────────────────────────────────────

def test_from_payload_conversion_emits_error_kind():
    """Rust: `from_payload_conversion_emits_error_kind`"""
    err = HarnessError.authentication("bad key")
    body = err.to_event_body("s", "t")
    assert body.variant == "authentication"
    assert body.recovery == "fail_fast"


def test_message_truncation_bounded_at_1_kib():
    """Rust: `message_truncation_bounded_at_1_kib`

    Rust 断的是 `String::len()` —— **字节数**，所以这里也按字节断。
    """
    huge = "x" * (MAX_HARNESS_ERROR_MESSAGE_BYTES + 200)
    err = HarnessError.internal(huge)
    body = err.to_event_body("s", "t")
    assert _b(body.message) <= MAX_HARNESS_ERROR_MESSAGE_BYTES + _b("…")


# ── 新增：Rust 侧没有的不变量 ─────────────────────────────────────────────

def test_every_variant_has_exactly_one_valid_recovery_hint():
    """「每个变体恰好一个 RecoveryHint」在 Rust 里靠 `match` 穷尽性保证。

    Python 没有那个检查 —— 少写一个分支不会编译失败，只会静默走进 `else`。
    所以这条不变量在这里必须**从代码结构变成可断言的数据**：

    1. 表里每个变体都有 hint，且 hint 是 `RecoveryHint` 的合法取值；
    2. 每个变体的 `required` 字段在 `details()` 里必然出现；
    3. `LlmErrorKind` 的**每一个** kind 都能被 `from_llm_error` 处理掉
       （少一个分支就是运行时 `ValueError`，不是编译错误）。
    """
    for variant, spec in _VARIANTS.items():
        assert RecoveryHint.is_valid(spec.hint), (
            f"{variant!r} 的 hint {spec.hint!r} 不是合法的 RecoveryHint"
        )
        assert not (set(spec.required) & set(spec.optional)), (
            f"{variant!r} 的 required 与 optional 有重叠："
            f"{set(spec.required) & set(spec.optional)}"
        )

    # LlmErrorKind 全族覆盖：逐个喂给 from_llm_error，不许有漏的
    probe = {
        "authentication": LlmErrorKind("authentication"),
        "quota": LlmErrorKind("quota"),
        "rate_limited": LlmErrorKind.rate_limited(),
        "context_overflow": LlmErrorKind.context_overflow(),
        "model_not_found": LlmErrorKind.model_not_found("m"),
        "server_error": LlmErrorKind.server_error(503),
        "network": LlmErrorKind("network"),
        "timeout": LlmErrorKind("timeout"),
        "invalid_request": LlmErrorKind.invalid_request("d"),
        "content_filtered": LlmErrorKind("content_filtered"),
        "stream_error": LlmErrorKind("stream_error"),
        "provider": LlmErrorKind.provider(),
    }
    assert set(probe) == set(LlmErrorKind.VALID_KINDS), (
        "LlmErrorKind 加了新 kind，但这条测试没跟上 —— "
        "from_llm_error 会漏掉它并在运行期抛 ValueError"
    )
    for name, kind in probe.items():
        err = HarnessError.from_llm_error(
            LlmError(kind=kind, message="m", provider="lane"))
        assert err.variant_name() in _VARIANTS, name


def test_tool_scoped_is_exactly_the_in_batch_variants():
    """`variant_is_tool_scoped` 必须只认批内那四个。

    判错了的后果是不对称的：把任务级错误当成批内的，会让观察者**不报**真正的
    任务终止；把批内的当成任务级的，会把每次工具报错都升级成任务失败。
    """
    scoped = {v for v, spec in _VARIANTS.items() if spec.tool_scoped}
    assert scoped == {"tool_execution", "plugin_spawn",
                      "plugin_timeout", "plugin_protocol"}
    for variant in scoped:
        assert HarnessError.variant_is_tool_scoped(variant)
    assert not HarnessError.variant_is_tool_scoped("internal")
    assert not HarnessError.variant_is_tool_scoped("policy")
    assert not HarnessError.variant_is_tool_scoped("no_such_variant")


def test_ascii_lower_is_not_unicode_lower():
    """`body_signals_quota` 走的是 `_ascii_lower`，不是 `str.lower()`。

    Rust 的 `to_ascii_lowercase()` 只折 A-Z；Python 的 `str.lower()` 是 Unicode
    感知的（`'İ'.lower()` 会**展开成两个码点**）。响应体是 JSON，正常全 ASCII，
    但错误体里带非 ASCII 是常事 —— 本项目自己的 Wisemodel 403 体就是中文。

    这条测试的价值不在今天，在于有人"顺手"把 `_ascii_lower` 换成 `.lower()`
    时它不会静默通过。
    """
    assert _ascii_lower("ABC") == "abc"
    # 非 ASCII 大写字母：Rust 不动，Python 的 .lower() 会动
    assert _ascii_lower("İ") == "İ", "Rust 的 to_ascii_lowercase 不碰非 ASCII"
    assert "İ".lower() != "İ", "前提：Python 的 lower() 会动它"
    # 码点展开：.lower() 长度会变，这是个真实的形状差异
    assert len("İ".lower()) == 2
    assert len(_ascii_lower("İ")) == 1
    # 中文不受影响（没有大小写），所以那条 403 用例两条路都对 —— 这正说明
    # 「现有测试全绿」证明不了这个选择
    assert _ascii_lower("没有有效的资源包") == "没有有效的资源包"

    # ---- 下面这段是给**调用点**加牙的 ----
    # 上面都在测辅助函数本身。如果有人把 from_status_with_label 里的
    # `_ascii_lower(body)` 换成 `body.lower()`，那些断言**照样全绿**。
    # 需要一个能观测到分岔的输入。
    #
    # U+212A（KELVIN SIGN）的 Unicode 简单小写映射是 ASCII 的 'k'，而
    # `to_ascii_lowercase` 不碰它。关键字里只有 `package` 含 'k'，所以把
    # KELVIN 塞在关键字中间就能造出分歧：
    kelvin_body = '{"code":"pacKage"}'
    assert "package" not in _ascii_lower(kelvin_body), "Rust 路径：不命中"
    assert "package" in kelvin_body.lower(), "Python 路径：会误命中"

    # 真按内核走 → 没有配额标记 → 403 是鉴权问题
    assert LlmError.from_status_with_label(
        403, kelvin_body, "lane").kind.kind == "authentication", (
        "KELVIN SIGN 不能被折成 'k' —— 折了就会把一条鉴权 403 误判成配额耗尽，"
        "让 ProviderChain 白白切走一条泳道"
    )
    # 对照：真 ASCII 的 PACKAGE 才该判成配额
    assert LlmError.from_status_with_label(
        403, '{"code":"PACKAGE"}', "lane").kind.kind == "quota"


def test_take_chars_counts_characters_not_bytes():
    """`_take_chars` 对应 `chars().take(n)` —— 按字符，不是按字节。

    按字节切会切碎多字节字符；那条 Wisemodel 体的中文 message 正好会踩到。
    """
    s = "中" * 300          # 900 字节、300 字符
    assert len(_take_chars(s, 200)) == 200
    assert _b(_take_chars(s, 200)) == 600, "按字节切会得到 200 字节 / 不足 200 字符"


def test_schema_constants_match_the_kernel():
    """常量是别人按值读的契约（Prometheus 标签、事件信封），别被顺手改掉。"""
    assert HARNESS_ERROR_SCHEMA_VERSION == 1
    assert HARNESS_EVENT_SCHEMA_V1 == "octos.harness.event.v1"
    assert OCTOS_LOOP_ERROR_TOTAL == "octos_loop_error_total"
    assert MAX_HARNESS_ERROR_MESSAGE_BYTES == 1024


def test_policy_deny_variant_name_is_policy_not_policy_deny():
    """`PolicyDeny` 的 `variant_name()` 是 `"policy"` —— 容易写错，钉住。

    Prometheus 标签和持久化事件都按这个值读，写成长名字会让仪表盘上的分片
    静默改变。
    """
    assert HarnessError.policy_deny("x").variant_name() == "policy"
    assert "policy_deny" not in _VARIANTS


def test_event_body_round_trips():
    """`HarnessErrorEvent` 的线格式往返（可选的 workflow/phase/details 跳过规则）。"""
    err = HarnessError.plugin_timeout("boom", plugin_name="demo", timeout_secs=30)
    body = err.to_event_body("sess", "task", workflow="wf", phase="act")
    wire = body.to_wire()
    assert wire["details"] == {"plugin_name": "demo", "timeout_secs": 30}
    assert wire["workflow"] == "wf" and wire["phase"] == "act"
    assert HarnessErrorEvent.from_wire(wire) == body

    minimal = HarnessError.internal("x").to_event_body("s", "t")
    assert "workflow" not in minimal.to_wire()
    assert "phase" not in minimal.to_wire()
    assert "details" not in minimal.to_wire()


def test_required_fields_are_enforced_at_construction():
    """缺必填字段的变体构造不出来 —— 而不是等到序列化时才崩。"""
    with pytest.raises(ValueError, match="缺少必需字段"):
        HarnessError("tool_execution", "x", {})
    with pytest.raises(ValueError, match="unknown HarnessError variant"):
        HarnessError("nope", "x")
