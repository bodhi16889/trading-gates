#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""決策帳本週報：把帳本裡的推播／警示做 N 天後回驗 →「這道閘門是保護還是亂擋」。

只讀最近 LOOKBACK 天，避免帳本長大後每次回驗都要打上百個價格查詢。
無可報內容時輸出空字串（cron watchdog：空 stdout 不投遞）。

用法：/usr/bin/python3 ~/work/trading-gates/weekly_verify.py
環境變數：GATES_LOOKBACK_DAYS（35）／GATES_VERIFY_HORIZON（5）／GATES_VERIFY_BAND（2）
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decision_ledger as L  # noqa: E402

LOOKBACK = int(os.environ.get("GATES_LOOKBACK_DAYS", "35"))
HORIZON = int(os.environ.get("GATES_VERIFY_HORIZON", "5"))
BAND = float(os.environ.get("GATES_VERIFY_BAND", "2"))

SOURCES = {
    "exit_alert": ("台股到價提醒", L.price_at_stock),
    "crypto_risk_daily": ("加密風控日報", L.price_at_binance),
}
KIND_LABEL = {
    "alert": "推播", "tier": "分批掛單觸發", "dedup": "被擋（去重）",
    "data_fail": "被擋（資料失敗）", "stale": "被擋（資料過期）",
    "gate": "閘門觸發", "decision": "每日決策",
}
OUTCOME_LABEL = {
    "correct": "判對", "premature": "賣早", "flat": "無明顯走勢",
    "unknown": "取不到價（不列入判對）", "pending": "未滿期",
}


def build_summary():
    rows = L.read_all(days=LOOKBACK)
    if not rows:
        return ""
    lines = [f"📒 決策帳本週報｜{datetime.date.today():%Y-%m-%d}"
             f"（近 {LOOKBACK} 天／回驗 {HORIZON} 天後／門檻 ±{BAND:g}%）"]
    for src, (name, lookup) in SOURCES.items():
        sub = [r for r in rows if r.get("source") == src]
        if not sub:
            continue
        lines.append(f"【{name}】共 {len(sub)} 筆")
        kinds = {}
        for r in sub:
            kinds[r.get("kind")] = kinds.get(r.get("kind"), 0) + 1
        lines.append("・" + "、".join(
            f"{KIND_LABEL.get(k, k)} {v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])))
        res = L.verify_entries(sub, lookup, horizon_days=HORIZON, band_pct=BAND)
        if res["verdicts"]:
            lines.append("・回驗：" + "、".join(
                f"{OUTCOME_LABEL.get(k, k)} {v}"
                for k, v in sorted(res["counts"].items(), key=lambda kv: -kv[1])))
            for dec, d in sorted(res["by_decision"].items()):
                if d.get("correct") or d.get("premature"):
                    lines.append(f"   - {dec}：判對 {d.get('correct', 0)}／賣早 {d.get('premature', 0)}"
                                 f"／無走勢 {d.get('flat', 0)}")
    lines.append("（判對＝事後確實往保護方向走；賣早＝事後續漲、擋過頭；取不到價不列入判對）")
    return "\n".join(lines)


if __name__ == "__main__":
    out = build_summary()
    if out:
        print(out)
