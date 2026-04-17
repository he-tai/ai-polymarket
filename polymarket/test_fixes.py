"""
验证所有修复点的单元测试（无需外部依赖）。
运行方式：python polymarket/test_fixes.py
"""
from __future__ import annotations

import json
import os
import sys
import types
import tempfile
import time
from datetime import datetime, timezone, timedelta

# ── 将项目根目录加入 path ────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, PROJECT_ROOT)

# ── Mock 外部依赖（测试环境不安装 py_clob_client）────────────────────────
for _m in ["py_clob_client", "py_clob_client.client", "py_clob_client.clob_types"]:
    sys.modules.setdefault(_m, types.ModuleType(_m))
sys.modules["py_clob_client.client"].ClobClient = object
sys.modules["py_clob_client.clob_types"].OrderArgs = object
sys.modules["py_clob_client.clob_types"].PartialCreateOrderOptions = object
_cfg_mod = types.ModuleType("polymarket.config")
_cfg_mod.LiveTradingConfig = object
sys.modules["polymarket.config"] = _cfg_mod

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
errors: list[str] = []

def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name}" + (f": {detail}" if detail else ""))
        errors.append(name)


print("\n=== P0: 风控检查集成 (risk.py) ===")
from polymarket.risk import RiskLimits, RiskState, check_order, mid_price

limits = RiskLimits(
    max_notional_per_trade=25.0,
    max_position_size=200.0,
    max_daily_loss=50.0,
    min_edge_bps=10.0,
)
state = RiskState()

ok, reason = check_order(side="BUY", price=0.5, size=10.0, fair=0.55, state=state, limits=limits)
check("正常订单通过风控", ok, reason)

ok, reason = check_order(side="BUY", price=0.5, size=60.0, fair=0.55, state=state, limits=limits)
check("超额名义金额被拒绝", not ok and "notional" in reason, reason)

state2 = RiskState(realized_pnl=-51.0)
ok, reason = check_order(side="BUY", price=0.5, size=5.0, fair=0.55, state=state2, limits=limits)
check("日亏损超限被拒绝", not ok and "daily loss" in reason, reason)

ok, reason = check_order(side="BUY", price=0.55, size=5.0, fair=0.55, state=state, limits=limits)
check("Edge 不足被拒绝", not ok and "edge" in reason, reason)

fair = mid_price(0.48, 0.52)
check("mid_price 计算正确", abs(fair - 0.50) < 1e-9, str(fair))
check("mid_price 缺失返回 None", mid_price(None, 0.52) is None)


print("\n=== P0: RiskState 持久化 ===")
with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
    tmp_path = f.name

try:
    s = RiskState(position_size=10.0, realized_pnl=-15.0)
    s.save(tmp_path)
    loaded = RiskState.load(tmp_path)
    check("持久化保存后加载一致", loaded.position_size == 10.0 and abs(loaded.realized_pnl - (-15.0)) < 1e-9)

    # 模拟昨天的数据
    yesterday = (datetime.now(tz=timezone.utc) - timedelta(days=1)).date()
    with open(tmp_path, "w") as f:
        json.dump({"position_size": 50.0, "realized_pnl": -30.0, "trade_date": str(yesterday)}, f)
    loaded_new_day = RiskState.load(tmp_path)
    check("新交易日 PnL 重置为 0", abs(loaded_new_day.realized_pnl) < 1e-9)
    check("新交易日持仓保留", abs(loaded_new_day.position_size - 50.0) < 1e-9)
finally:
    os.unlink(tmp_path)


print("\n=== P1: 策略信号改进 (strategy.py) ===")
from polymarket.strategy import MeanReversionConfig, mean_reversion_signal, signal_summary

cfg = MeanReversionConfig(window=5, z_entry=1.2, min_hours_to_resolution=6.0, trend_threshold=0.15)

# 正常均值回归信号（价格在窄幅震荡，最后一个极端偏离，不触发趋势过滤）
prices_low = [0.50, 0.51, 0.50, 0.51, 0.50, 0.20]  # range=0.31>0.15 → 会触发趋势，调小 threshold
cfg_tight = MeanReversionConfig(window=5, z_entry=1.2, min_hours_to_resolution=6.0, trend_threshold=0.5)
signal = mean_reversion_signal(prices_low, cfg_tight)
check("低价格产生买入信号", signal.should_buy, f"zscore={signal.zscore:.3f}")

prices_high = [0.50, 0.51, 0.50, 0.51, 0.50, 0.80]
signal = mean_reversion_signal(prices_high, cfg_tight)
check("高价格产生卖出信号", signal.should_sell, f"zscore={signal.zscore:.3f}")

# TTR 过滤
future_3h = (datetime.now(tz=timezone.utc) + timedelta(hours=3)).isoformat()
signal_ttr = mean_reversion_signal([0.5] * 10 + [0.3], cfg, end_date_iso=future_3h)
check("临近结算 TTR 触发跳过", signal_ttr.skipped and "TTR" in signal_ttr.skip_reason, signal_ttr.skip_reason)

# TTR 充足时正常计算
future_24h = (datetime.now(tz=timezone.utc) + timedelta(hours=24)).isoformat()
signal_ok = mean_reversion_signal([0.5, 0.51, 0.49, 0.50, 0.48, 0.30], cfg, end_date_iso=future_24h)
check("充足 TTR 正常计算信号", not signal_ok.skipped)

# 趋势过滤
trend_prices = [0.1, 0.15, 0.25, 0.35, 0.45, 0.55]  # 强趋势
signal_trend = mean_reversion_signal(trend_prices, cfg)
check("趋势市场抑制买卖信号", not signal_trend.should_buy and not signal_trend.should_sell and signal_trend.in_trend)
check("趋势市场仍暴露 Z-score", signal_trend.zscore != 0.0)

# signal_summary 注入 AI prompt
summary = signal_summary(signal_trend)
check("signal_summary 包含量化信息", "Z-score" in summary and "趋势" in summary)

# 数据不足
signal_short = mean_reversion_signal([0.5, 0.6], cfg)
check("数据不足时安全跳过", signal_short.skipped)


print("\n=== P2: 订单追踪 (execution.py) ===")
from polymarket.execution import TrackedOrder

order = TrackedOrder(order_id="abc123", token_id="tok1", side="BUY", price=0.5, size=10.0)
check("新订单未过期", not order.is_expired(300.0))

old_order = TrackedOrder(order_id="xyz789", token_id="tok2", side="SELL", price=0.6, size=5.0)
old_order.submitted_at = time.time() - 400
check("超时订单检测为过期", old_order.is_expired(300.0))

old_order.status = "filled"
check("已成交订单不视为过期", not old_order.is_expired(300.0))


print()
if errors:
    print(f"\033[91m{len(errors)} 个测试失败：{errors}\033[0m")
    sys.exit(1)
else:
    total = 20
    print(f"\033[92m所有测试通过 ✓\033[0m")