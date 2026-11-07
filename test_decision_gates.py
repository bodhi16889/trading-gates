#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""決策帳本 ＋ 兩個 fail-closed 缺口的離線測試（負向測試優先）。

跑法（需要 yfinance；用 ai-agents 的 venv）：
  /Users/adany/ai-agents/venv/bin/python ~/work/trading-gates/test_decision_gates.py
"""
import contextlib
import datetime
import importlib
import io
import json
import os
import sys
import tempfile
import unittest

GATES = os.path.dirname(os.path.abspath(__file__))
for p in (GATES, os.path.expanduser("~/crypto-risk"), os.path.expanduser("~/ai-agents")):
    if p not in sys.path:
        sys.path.insert(0, p)

import decision_ledger as L  # noqa: E402


class TestLedger(unittest.TestCase):
    """帳本 I/O 與回驗分類。"""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.p = os.path.join(self.d.name, "l.jsonl")

    def tearDown(self):
        self.d.cleanup()

    def test_record_appends_and_reads(self):
        self.assertTrue(L.record({"kind": "alert", "scope": "BTCUSDT"}, self.p))
        self.assertTrue(L.record({"kind": "gate", "scope": "BTCUSDT"}, self.p))
        rows = L.read_all(self.p)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.get("ts") for r in rows), "ts 應自動補上")

    def test_record_failure_returns_false_not_raise(self):
        self.assertFalse(L.record({"kind": "x"}, "/dev/null/impossible.jsonl"))

    def test_read_all_tolerates_bad_lines(self):
        with open(self.p, "w", encoding="utf-8") as f:
            f.write('{"kind":"alert","scope":"BTCUSDT","price":100,"ts":"2026-09-01T00:00:00+08:00"}\n')
            f.write("這行不是 JSON\n\n")
        self.assertEqual(len(L.read_all(self.p)), 1)

    def test_read_all_missing_file_is_empty(self):
        self.assertEqual(L.read_all(os.path.join(self.d.name, "nope.jsonl")), [])

    def test_classify(self):
        self.assertEqual(L.classify(-3, 2), "correct")          # 真的該跑
        self.assertEqual(L.classify(+3, 2), "premature")        # 賣早
        self.assertEqual(L.classify(+1, 2), "flat")
        self.assertEqual(L.classify(None, 2), "unknown")        # 取不到價 ≠ 判對
        self.assertEqual(L.classify(+2.5, 2, mode="abs"), "correct")   # 區間失效風險兌現
        self.assertEqual(L.classify(+0.5, 2, mode="abs"), "flat")

    def test_verify_entries(self):
        now = datetime.datetime.fromisoformat("2026-09-25T12:00:00+08:00")
        rows = [
            {"ts": "2026-09-20T09:00:00+08:00", "kind": "alert", "scope": "2330.TW",
             "decision": "push_exit", "price": 1000.0, "verify_mode": "down", "reason": "到停利"},
            {"ts": "2026-09-24T09:00:00+08:00", "kind": "alert", "scope": "2330.TW",
             "decision": "push_exit", "price": 1000.0, "verify_mode": "down"},
            {"ts": "2026-09-20T09:00:00+08:00", "kind": "data_fail", "scope": "2330.TW",
             "decision": "blocked_data"},
        ]
        res = L.verify_entries(rows, lambda sym, when: 950.0, horizon_days=5, band_pct=2.0, now=now)
        outs = [v["outcome"] for v in res["verdicts"]]
        self.assertEqual(len(res["verdicts"]), 2, "data_fail 不列入回驗")
        self.assertIn("correct", outs, "滿 5 天且下跌 5% → 判對")
        self.assertIn("pending", outs, "未滿 5 天 → pending")
        self.assertEqual(res["counts"]["correct"], 1)

    def test_verify_unknown_when_price_missing(self):
        rows = [{"ts": "2026-09-01T09:00:00+08:00", "kind": "alert", "scope": "X",
                 "decision": "push_exit", "price": 100.0}]
        res = L.verify_entries(rows, lambda s, w: None, horizon_days=5)
        self.assertEqual(res["verdicts"][0]["outcome"], "unknown")

    def test_summarize(self):
        s = L.summarize([{"source": "a", "kind": "alert", "decision": "push_exit", "reason": "x"},
                         {"source": "a", "kind": "alert", "decision": "push_exit", "reason": "x"}])
        self.assertEqual(s["entries"], 2)
        self.assertEqual(list(s["top_reasons"].values())[0], 2)


class TestRiskDailyFailClosed(unittest.TestCase):
    """risk_daily：無法評估時不得以 LOW 結案（fail-closed）。"""

    def setUp(self):
        sys.modules.pop("risk_daily", None)
        self.m = importlib.import_module("risk_daily")

    def test_grid_unset_is_not_low(self):
        r, _, _ = self.m.grid_part("BTC", 90000, None, None)
        self.assertEqual(r, "ATTENTION", "未設定 ≠ 安全")

    def test_grid_cfg_read_failure_is_high(self):
        r, _, note = self.m.grid_part("BTC", 90000, None, None, cfg_ok=False)
        self.assertEqual(r, "HIGH")
        self.assertIn("讀取失敗", note)

    def test_grid_normal_range_still_low(self):
        r, buf, _ = self.m.grid_part("BTC", 90000, 74000, 110000)   # 距上下緣皆 >10% → LOW
        self.assertEqual(r, "LOW")
        self.assertTrue(buf)

    def test_grid_near_boundary_is_attention(self):
        r, _, _ = self.m.grid_part("BTC", 90000, 74000, 97000)      # 距上限僅 7.8% → ATTENTION
        self.assertEqual(r, "ATTENTION")

    def test_load_cfg_reports_error_instead_of_empty(self):
        self.m.CFG = os.path.join(tempfile.gettempdir(), "no-such-cfg-xyz.json")
        cfg, err = self.m.load_cfg()
        self.assertEqual(cfg, {})
        self.assertIsNotNone(err, "設定讀不到必須回傳錯誤，不可靜默當成空設定")

    def test_oi_short_history_raises_instead_of_zero(self):
        self.m.get = lambda url: [{"sumOpenInterest": "1"}]     # 只有 1 筆
        with self.assertRaises(ValueError):
            self.m.oi_24h("BTCUSDT")

    def test_main_degrades_when_spot_fails(self):
        self.m.load_cfg = lambda: ({}, "設定壞了")
        self.m.spot = lambda sym: (_ for _ in ()).throw(RuntimeError("boom"))
        os.environ["TRADING_LEDGER"] = os.path.join(tempfile.mkdtemp(), "l.jsonl")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.m.main()
        out = json.loads(buf.getvalue())
        self.assertEqual(out["action_decision"], "HOLD_ALL")
        self.assertEqual(out["risk_level"], "HIGH", "未知 ≠ 安全")
        self.assertTrue(out["data_gaps"])
        self.assertIsNone(out["overall_health_score"], "不可給一個看起來正常的健康分數")


class TestExitAlertFailClosed(unittest.TestCase):
    """exit_alert：資料失敗／過期不得靜默。"""

    def setUp(self):
        sys.modules.pop("exit_alert", None)
        self.m = importlib.import_module("exit_alert")

    def test_bar_age_days(self):
        today = datetime.date(2026, 9, 25)
        self.assertEqual(self.m.bar_age_days("2026-09-25", today), 0)
        self.assertEqual(self.m.bar_age_days("2026-09-10", today), 15)
        self.assertIsNone(self.m.bar_age_days("not-a-date", today))

    def test_stale_data_produces_no_status(self):
        old_day = (datetime.date.today() - datetime.timedelta(days=self.m.MAX_STALE_DAYS + 3)).strftime("%Y-%m-%d")
        self.m.trend = lambda tk, period="60d": {"date": old_day, "price": 100.0, "entry": 90.0}
        e = self.m.evaluate({"ticker": "ZZZZ", "name": "t", "cost": 90, "stop_pct": 10})
        self.assertTrue(e.get("stale"))
        self.assertNotIn("status", e, "過期資料不得產出到價狀態")

    def test_same_problem_warns_once_per_day(self):
        """同日同一個問題只警告一次：首次必講，之後不洗版（但也不會假裝正常）。"""
        import contextlib, io
        d = tempfile.mkdtemp()
        os.environ["TRADING_LEDGER"] = os.path.join(d, "l.jsonl")
        cfgp = os.path.join(d, "cfg.json")
        with open(cfgp, "w", encoding="utf-8") as f:
            json.dump({"positions": [{"name": "壞代號", "ticker": "ZZZZ-NOPE"}]}, f)
        self.m.CFG, self.m.STATE = cfgp, os.path.join(d, "s.json")
        self.m.PLAN, self.m.PLAN_STATE = os.path.join(d, "p.json"), os.path.join(d, "ps.json")
        got = []
        for _ in range(2):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.m.main()
            got.append(buf.getvalue())
        self.assertIn("⚠️", got[0])
        self.assertNotIn("SILENT", got[0])          # 首次：一定要講
        self.assertIn("SILENT", got[1])             # 同日重複：不洗版

    def test_selftest_passes(self):
        self.assertEqual(self.m.selftest(), 0)


class TestCryptoGates(unittest.TestCase):
    """加密專屬三閘門：穩定幣脫鉤／小幣流動性／再平衡偏離（負向測試優先）。"""

    def setUp(self):
        sys.modules.pop("risk_daily", None)
        self.m = importlib.import_module("risk_daily")

    def _hold(self, obj):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "hodlbot_holdings.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.environ["CRYPTO_HODLBOT_HOLDINGS"] = p
        return p

    # --- 閘門 1：穩定幣脫鉤 ---
    def test_depeg_high_when_peg_breaks(self):
        self.m.stable_px = lambda s: 0.985            # 偏離 1.5%
        r = self.m.gate_depeg([])
        self.assertEqual(r["risk"], "HIGH")
        self.assertIn("暫停", r["note"])

    def test_depeg_attention_between_bands(self):
        self.m.stable_px = lambda s: 0.994            # 偏離 0.6%
        self.assertEqual(self.m.gate_depeg([])["risk"], "ATTENTION")

    def test_depeg_low_when_peg_holds(self):
        self.m.stable_px = lambda s: 1.0001
        self.assertEqual(self.m.gate_depeg([])["risk"], "LOW")

    def test_depeg_all_probes_fail_is_not_low(self):
        self.m.stable_px = lambda s: (_ for _ in ()).throw(RuntimeError("no net"))
        gaps = []
        r = self.m.gate_depeg(gaps)
        self.assertEqual(r["risk"], "HIGH", "取不到穩定幣價 ≠ 掛鉤正常")
        self.assertIsNone(r["worst_px"])
        self.assertTrue(gaps, "取不到必須留資料缺口")

    def test_depeg_zero_price_is_treated_as_dead_market(self):
        """Binance 有『代號存在但報價 0』的死交易對（如 DAIUSDT）→ 不可當成 0% 偏離。"""
        self.m.get = lambda url: {"price": "0.00000000"}
        with self.assertRaises(ValueError):
            self.m.stable_px("DAIUSDT")

    # --- 閘門 2：小幣流動性 ---
    def test_liquidity_flags_thin_volume(self):
        self.m.swap_liq = lambda sym: (2_000_000.0, 0.02)
        r = self.m.gate_liquidity(["TAO"], [])
        self.assertEqual(r["risk"], "ATTENTION")
        self.assertIn("不進場", r["note"])

    def test_liquidity_flags_wide_spread(self):
        self.m.swap_liq = lambda sym: (100_000_000.0, 0.9)
        self.assertEqual(self.m.gate_liquidity(["ONDO"], [])["risk"], "ATTENTION")

    def test_liquidity_ok_for_deep_market(self):
        self.m.swap_liq = lambda sym: (80_000_000.0, 0.02)
        self.assertEqual(self.m.gate_liquidity(["TAO", "BTC"], [])["risk"], "LOW")

    def test_liquidity_fetch_failure_is_not_low(self):
        self.m.swap_liq = lambda sym: (_ for _ in ()).throw(RuntimeError("boom"))
        gaps = []
        r = self.m.gate_liquidity(["TAO"], gaps)
        self.assertEqual(r["risk"], "ATTENTION", "取不到 ≠ 流動性充足")
        self.assertTrue(gaps)

    # --- 閘門 3：再平衡偏離 ---
    def test_rebalance_unmeasured_is_not_low(self):
        os.environ["CRYPTO_HODLBOT_HOLDINGS"] = "/nonexistent/holdings.json"
        gaps = []
        r = self.m.gate_rebalance({"coins": {"BTC": 35, "USDT": 65}}, {}, gaps)
        self.assertFalse(r["measured"])
        self.assertEqual(r["risk"], "ATTENTION", "沒有持倉資料 ≠ 沒有偏離")
        self.assertTrue(gaps)

    def test_rebalance_high_when_bot_stalled(self):
        self._hold({"value_usdt": {"BTC": 700, "USDT": 100}})      # 目標 35/65 → 實 87.5/12.5
        r = self.m.gate_rebalance({"coins": {"BTC": 35, "USDT": 65}, "rebalance_threshold_pct": 5}, {}, [])
        self.assertTrue(r["measured"])
        self.assertEqual(r["risk"], "HIGH")
        self.assertGreater(r["max_drift"], 20)

    def test_rebalance_within_threshold_is_low(self):
        self._hold({"value_usdt": {"BTC": 3500, "USDT": 6500}})
        r = self.m.gate_rebalance({"coins": {"BTC": 35, "USDT": 65}, "rebalance_threshold_pct": 5}, {}, [])
        self.assertEqual(r["risk"], "LOW")

    def test_rebalance_incomplete_holdings_is_not_low(self):
        self._hold({"value_usdt": {"BTC": 350, "USDT": 650}})      # TAO 缺
        gaps = []
        r = self.m.gate_rebalance({"coins": {"BTC": 35, "TAO": 10, "USDT": 55}}, {}, gaps)
        self.assertNotEqual(r["risk"], "LOW", "權重不完整不得宣稱正常")
        self.assertTrue(gaps)

    def test_rebalance_garbage_input_does_not_crash(self):
        self._hold({"value_usdt": {"BTC": "abc", "USDT": 100}})
        gaps = []
        r = self.m.gate_rebalance({"coins": {"BTC": 35, "USDT": 65}, "rebalance_threshold_pct": "x"}, {}, gaps)
        self.assertNotEqual(r["risk"], "LOW", "看不懂的輸入 ≠ 正常")
        self.assertTrue(r["measured"])

    # --- 帳本：逐筆門檻覆蓋（穩定幣用 0.5%，不是預設 2%）---
    def test_ledger_per_row_band_overrides_default(self):
        ts = "2026-09-01T09:00:00+08:00"
        rows = [
            {"ts": ts, "kind": "gate", "scope": "USDCUSDT", "decision": "HOLD_ALL", "price": 0.996,
             "verify_mode": "abs", "band_pct": 0.5},
            {"ts": ts, "kind": "gate", "scope": "USDCUSDT", "decision": "HOLD_ALL", "price": 0.996,
             "verify_mode": "abs"},
        ]
        res = L.verify_entries(rows, lambda s, w: 0.99, horizon_days=5, band_pct=2.0)
        self.assertEqual(res["verdicts"][0]["outcome"], "correct", "行內 0.5% 應覆蓋預設 2%")
        self.assertEqual(res["verdicts"][1]["outcome"], "flat", "無行內門檻時用預設 2%")
        self.assertEqual(res["verdicts"][0]["band_pct"], 0.5)

    # --- 閘門 4：網格經濟性（每格淨利扣掉來回手續費）---
    def _econ(self, **over):
        g = {"btc_lower": 74000, "btc_upper": 97000, "btc_grids": 35, "btc_type": "arithmetic"}
        g.update(over)
        return self.m.gate_grid_economics(g)

    def test_grid_econ_live_btc_is_ok(self):
        r = self._econ()                      # 實跑設定：毛 0.697% − 0.1% = 淨 0.597%
        self.assertEqual(r["risk"], "LOW")
        self.assertAlmostEqual(r["worst_net"], 0.597, places=3)
        self.assertEqual(r["worst"], "BTC")

    def test_grid_econ_dense_grid_is_attention(self):
        self.assertEqual(self._econ(btc_grids=60)["risk"], "ATTENTION")     # 淨 0.302%

    def test_grid_econ_extreme_density_is_high(self):
        self.assertEqual(self._econ(btc_grids=120)["risk"], "HIGH")         # 淨 0.099%

    def test_grid_econ_thin_grid_without_fee_still_flagged(self):
        self.assertEqual(self._econ(btc_grids=70, fee_round_trip_pct=0)["risk"], "ATTENTION")

    def test_grid_econ_fee_from_config_is_honoured(self):
        self.assertEqual(self._econ(fee_round_trip_pct=0.3)["risk"], "ATTENTION")   # 淨 0.397%

    def test_grid_econ_geometric_uses_ratio(self):
        r = self.m.gate_grid_economics({"eth_lower": 2000, "eth_upper": 4000, "eth_grids": 10,
                                        "eth_type": "geometric"})
        self.assertEqual(r["worst"], "ETH")
        self.assertAlmostEqual(r["worst_net"], (2 ** (1 / 9) - 1) * 100 - 0.1, places=3)

    def test_grid_econ_missing_grid_not_counted_twice(self):
        r = self._econ()                      # ETH 未設定 → 本閘門不評估（由 grid_boundary 承接）
        self.assertFalse(r["rows"]["ETH"]["evaluated"])
        self.assertEqual(r["risk"], "LOW", "未設定的網格不應在本閘門重複扣分")

    def test_grid_econ_garbage_params_do_not_crash(self):
        r = self.m.gate_grid_economics({"btc_lower": "x", "btc_upper": None, "btc_grids": 1})
        self.assertEqual(r["risk"], "LOW")
        self.assertIsNone(r["worst_net"])
        self.assertFalse(r["rows"]["BTC"]["evaluated"])

    def test_grid_econ_worst_of_two_grids_wins(self):
        # 等差 2000~4000/200 格 → 最薄一格毛 0.25% − 0.1% = 淨 0.15% → HIGH（比 BTC 的 0.597% 更差 → 取 ETH）
        r = self.m.gate_grid_economics({"btc_lower": 74000, "btc_upper": 97000, "btc_grids": 35,
                                        "eth_lower": 2000, "eth_upper": 4000, "eth_grids": 200})
        self.assertEqual(r["worst"], "ETH")
        self.assertEqual(r["risk"], "HIGH")
        self.assertAlmostEqual(r["worst_net"], 0.151, places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
