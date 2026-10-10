"""配额打爆时「停手等」：等的是不稀缺的墙钟，省的是稀缺的修复轮次。

2026-10-01 正赛日志量出来的事实：18 点档平台配额耗尽（"Free allocated quota
exceeded"），github 28 个 / sheet 24 个回合在 3 秒内速死（tools=0），REQ-1 整支
的修复轮次被烧光。这些契约钉住修复后的行为：

  · 配额类错误识别得住，别的失败和数字巧合不误伤；
  · 等待之后把同一个回合原样重跑，成功了就算这一个回合正常完成；
  · 三重上限（单次、整跑累计、收尾底线）一个都不许越；
  · 环境开关能整体关掉，关了就和从前一样。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main as M  # noqa: E402

QUOTA = "bridge_error: API Error: 429 Free allocated quota exceeded."


class _Driver:
    """不 sleep、不心跳的最小驱动：按脚本逐次返回。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def run(self, prompt, timeout, monitor=None):
        item = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        return item


class _QuotaFlow:
    """跑得动 `hold_for_quota` / `turn` 的最小 Flow。"""

    def __init__(self, driver, remaining):
        self.driver = driver
        self._remaining = float(remaining)
        self.tail_seconds = 1800       # 和 `Flow.__init__` 的默认值同源
        self.node_timeout = 2400
        self.quota_waited_total = 0.0
        # turn() 需要的几件
        self.output_dir = Path(".")
        self.probe_summaries = {}
        self.turn_count = 0
        self.guard_enabled = True
        self.pending_corrections = []

    def remaining(self):
        return self._remaining

    def wound_down(self):
        return False

    def protected_prefixes(self):
        return []

    def restore_protected(self):
        return []

    hold_for_quota = M.Flow.hold_for_quota
    turn = M.Flow.turn


def _patch_sleep(monkeypatch, flow):
    """把 `sleep_logging` 换成一个记录器，等待照常扣墙钟。"""
    slept = []

    def fake_sleep(seconds):
        slept.append(float(seconds))
        flow._remaining -= float(seconds)

    monkeypatch.setattr(M, "sleep_logging", fake_sleep)
    return slept


# ---------------------------------------------------------------- 识别

def test_the_platforms_quota_text_is_recognized_and_other_failures_are_not():
    assert M.quota_exhausted(QUOTA)
    assert M.quota_exhausted("402 quota will reset when the window rolls over")
    assert M.quota_exhausted("HTTP 429 Too Many Requests")
    assert not M.quota_exhausted("octos turn timed out")
    assert not M.quota_exhausted("connection reset by peer")
    # 数字巧合不误伤：一个用例名里出现 429 不是配额事件
    assert not M.quota_exhausted("expected 429 rows, got 17")


# ---------------------------------------------------------------- 等待循环

def test_a_quota_killed_turn_holds_then_completes(monkeypatch):
    monkeypatch.setenv("OCTOS_ARC_QUOTA_PROBE_SECONDS", "5")
    driver = _Driver([(False, QUOTA), (True, "done")])
    flow = _QuotaFlow(driver, remaining=30000)
    slept = _patch_sleep(monkeypatch, flow)

    ok, text, waited = flow.hold_for_quota("prompt", 600, "REQ-1 implement", None, QUOTA)

    assert ok and text == "done", "接口恢复后同一个回合直接完成"
    assert driver.calls == 2, "第一次等完重跑仍撞配额，再等一次才恢复"
    assert waited == 10 and flow.quota_waited_total == 10
    assert slept == [5, 5]


def test_the_hold_never_eats_into_the_closing_reserve(monkeypatch):
    """收尾底线 = tail(1800) + node_timeout(2400)；低于它一秒都不等。"""
    driver = _Driver([(False, QUOTA)])
    flow = _QuotaFlow(driver, remaining=4000)
    _patch_sleep(monkeypatch, flow)

    ok, _, waited = flow.hold_for_quota("prompt", 600, "label", None, QUOTA)

    assert not ok and waited == 0.0
    assert driver.calls == 0, "收尾底线之下不许开等，一次重跑都不发"


def test_the_hold_is_capped_and_gives_up(monkeypatch):
    monkeypatch.setenv("OCTOS_ARC_QUOTA_PROBE_SECONDS", "5")
    monkeypatch.setenv("OCTOS_ARC_QUOTA_WAIT_SECONDS", "10")
    monkeypatch.setenv("OCTOS_ARC_QUOTA_TOTAL_WAIT_SECONDS", "10")
    driver = _Driver([(False, QUOTA)])          # 永远配额
    flow = _QuotaFlow(driver, remaining=100000)
    slept = _patch_sleep(monkeypatch, flow)

    ok, _, waited = flow.hold_for_quota("prompt", 600, "label", None, QUOTA)

    assert not ok
    assert waited == 10, "单次上限 10 秒，等到就放弃"
    assert slept == [5, 5], f"等两觉各重跑一次：5s → 重跑 → 5s → 放弃，实际 {slept}"
    assert driver.calls == 2
    assert flow.quota_waited_total == 10


def test_the_total_budget_spans_the_whole_run(monkeypatch):
    """整跑累计上限跨回合：上一个回合等掉的份额，这一个回合不许再等。"""
    monkeypatch.setenv("OCTOS_ARC_QUOTA_PROBE_SECONDS", "5")
    monkeypatch.setenv("OCTOS_ARC_QUOTA_TOTAL_WAIT_SECONDS", "7")
    driver = _Driver([(False, QUOTA)])
    flow = _QuotaFlow(driver, remaining=100000)
    flow.quota_waited_total = 5.0               # 之前的回合已经等掉 5s
    _patch_sleep(monkeypatch, flow)

    ok, _, waited = flow.hold_for_quota("p", 600, "label", None, QUOTA)

    assert not ok and waited == 2, "只剩 2s 的累计额度，就只能等 2s"
    assert flow.quota_waited_total == 7
    assert driver.calls == 1


def test_the_hold_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("OCTOS_ARC_QUOTA_HOLD", "0")
    driver = _Driver([(False, QUOTA)])
    flow = _QuotaFlow(driver, remaining=100000)
    _patch_sleep(monkeypatch, flow)

    ok, _, waited = flow.hold_for_quota("p", 600, "label", None, QUOTA)

    assert not ok and waited == 0.0 and driver.calls == 0


# ---------------------------------------------------------------- 与 turn() 的接线

def test_a_quota_failed_turn_holds_inside_the_real_turn_wrapper(monkeypatch):
    """`Flow.turn` 认得出配额类失败并走等待 —— 不接线的话上面全是死代码。"""
    monkeypatch.setenv("OCTOS_ARC_QUOTA_PROBE_SECONDS", "5")
    driver = _Driver([(False, QUOTA), (True, "done")])
    flow = _QuotaFlow(driver, remaining=30000)
    _patch_sleep(monkeypatch, flow)

    ok, text = flow.turn("prompt", 600, "REQ-1 implement")

    assert ok and text == "done"
    assert driver.calls == 2
    assert flow.quota_waited_total == 5


def test_a_non_quota_failure_does_not_trigger_the_hold(monkeypatch):
    driver = _Driver([(False, "octos turn timed out")])
    flow = _QuotaFlow(driver, remaining=30000)
    _patch_sleep(monkeypatch, flow)

    ok, text = flow.turn("prompt", 600, "REQ-1 implement")

    assert not ok
    assert driver.calls == 1, "普通失败一行都不许多等"
    assert flow.quota_waited_total == 0.0
