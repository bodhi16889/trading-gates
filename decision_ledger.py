#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""決策帳本（被拒訊號帳本）＋ 事後回驗。

用途：回答「這道風控閘門是保護，還是亂擋？」
  1. record()  —— 主流程把「有決策的時刻」寫進 append-only JSONL（推播、被擋、資料異常、閘門判定）。
  2. verify_entries() —— N 天後回驗每一筆可回價的推播：判對（真的該跑）／賣早（不該跑）／未定。
  3. summarize() —— 不連網統計各閘門觸發次數，用來找「幾乎沒在擋」與「一直在擋」的閘門。

呼叫端約定（見 skill finance/trading-decision-gates 的 fail-closed 紀律）：
  * 帳本寫入失敗**不得**讓主流程崩潰；record() 回傳 False，由呼叫方在輸出裡加一行告警。
  * 只記「有決策」的事件，不記每日「正常」狀態（帳本要能一眼掃完）。
  * scope 用可回價的代號（台股如 2377.TW；加密如 BTCUSDT），meta 放顯示名稱。
  * 取不到事後價格 → 標 unknown，**不可**當成「判對」（fail-closed）。

CLI:
  python3 decision_ledger.py report [--path P] [--days N]
  python3 decision_ledger.py verify [--path P] [--horizon 5] [--band 2.0] [--market crypto|stock]
"""
import datetime
import json
import os
import sys
import urllib.request

DEFAULT_PATH = "~/work/trading-gates/decisions.jsonl"
UA = {"User-Agent": "Mozilla/5.0"}


def ledger_path(path=None):
    return os.path.expanduser(path or os.environ.get("TRADING_LEDGER") or DEFAULT_PATH)


def now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def record(entry, path=None):
    """附加一筆決策。回傳 True/False；失敗不拋例外（呼叫方負責告警）。"""
    e = dict(entry)
    e.setdefault("ts", now_iso())
    try:
        p = ledger_path(path)
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        return True
    except Exception:
        return False


def _parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(str(s))
    except Exception:
        return None


def read_all(path=None, days=None):
    """讀整本帳本；壞行跳過（寧可少一筆，不要整份讀不出來）。"""
    try:
        with open(ledger_path(path), encoding="utf-8") as f:
            raw = f.read().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    if days:
        cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(days=int(days))
        out = [e for e in out if _parse_ts(e.get("ts")) and _parse_ts(e["ts"]) >= cutoff]
    return out


def summarize(entries):
    """不連網統計：各來源／種類／決定的次數、最常見原因、時間範圍。"""
    by, reasons = {}, {}
    for e in entries:
        k = f"{e.get('source')}/{e.get('kind')}/{e.get('decision')}"
        by[k] = by.get(k, 0) + 1
        r = (e.get("reason") or "")[:40]
        if r:
            reasons[r] = reasons.get(r, 0) + 1
    return {
        "entries": len(entries),
        "by_source_kind_decision": dict(sorted(by.items(), key=lambda kv: -kv[1])),
        "top_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:10]),
        "range": [entries[0].get("ts"), entries[-1].get("ts")] if entries else [],
    }


VERIFIABLE_KINDS = ("alert", "tier", "gate")


def classify(change_pct, band_pct, mode="down"):
    """down：跌超過 band → 判對（該跑）；漲超過 band → 賣早。abs：任一方向超過 band → 風險兌現。"""
    if change_pct is None:
        return "unknown"
    if mode == "abs":
        return "correct" if abs(change_pct) >= band_pct else "flat"
    if change_pct <= -band_pct:
        return "correct"
    if change_pct >= band_pct:
        return "premature"
    return "flat"


def verify_entries(entries, price_lookup, horizon_days=5, band_pct=2.0, now=None):
    """事後回驗。price_lookup(symbol, when_dt) -> float|None。"""
    now = now or datetime.datetime.now().astimezone()
    verdicts = []
    for e in entries:
        if e.get("kind") not in VERIFIABLE_KINDS:
            continue
        ts, sym, px = _parse_ts(e.get("ts")), e.get("scope"), e.get("price")
        if not ts or not sym or px in (None, 0):
            continue
        if (now - ts).days < horizon_days:
            verdicts.append({"ts": e.get("ts"), "scope": sym, "decision": e.get("decision"),
                             "outcome": "pending", "reason": f"未滿 {horizon_days} 天"})
            continue
        later = price_lookup(sym, ts + datetime.timedelta(days=horizon_days))
        change = None if later is None else (later / float(px) - 1) * 100.0
        verdicts.append({
            "ts": e.get("ts"), "scope": sym, "decision": e.get("decision"),
            "price": px, "price_later": later,
            "change_pct": None if change is None else round(change, 2),
            "outcome": classify(change, e.get("band_pct") or band_pct, e.get("verify_mode") or "down"),
            "band_pct": float(e.get("band_pct") or band_pct),
            "reason": (e.get("reason") or "")[:60],
        })
    counts, by_dec = {}, {}
    for v in verdicts:
        counts[v["outcome"]] = counts.get(v["outcome"], 0) + 1
        d = by_dec.setdefault(v.get("decision") or "?",
                              {"correct": 0, "premature": 0, "flat": 0, "unknown": 0, "pending": 0})
        d[v["outcome"]] = d.get(v["outcome"], 0) + 1
    return {"horizon_days": horizon_days, "band_pct": band_pct,
            "counts": counts, "by_decision": by_dec, "verdicts": verdicts}


def price_at_binance(symbol, when):
    st = int(when.timestamp() * 1000)
    url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}"
           f"&interval=1d&startTime={st}&limit=1")
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            k = json.loads(r.read().decode())
        return float(k[0][4]) if k else None
    except Exception:
        return None


def price_at_stock(ticker, when):
    """需 yfinance（~/ai-agents venv 內）。取不到回 None → 記為 unknown，不當成判對。"""
    try:
        import yfinance as yf
        start = when.strftime("%Y-%m-%d")
        end = (when + datetime.timedelta(days=7)).strftime("%Y-%m-%d")
        df = yf.Ticker(ticker).history(start=start, end=end, interval="1d").dropna()
        return float(df["Close"].iloc[0]) if not df.empty else None
    except Exception:
        return None


def main(argv):
    cmd, path, days, market, horizon, band = None, None, None, "crypto", 5, 2.0
    args = list(argv)
    if args and not args[0].startswith("-"):
        cmd = args.pop(0)
    while args:
        a = args.pop(0)
        if a == "--path":
            path = args.pop(0)
        elif a == "--days":
            days = int(args.pop(0))
        elif a == "--horizon":
            horizon = int(args.pop(0))
        elif a == "--band":
            band = float(args.pop(0))
        elif a == "--market":
            market = args.pop(0)
        else:
            print(f"未知參數：{a}", file=sys.stderr)
            return 2
    entries = read_all(path, days=days)
    if cmd == "report":
        print(json.dumps(summarize(entries), ensure_ascii=False, indent=2))
    elif cmd == "verify":
        lookup = price_at_binance if market == "crypto" else price_at_stock
        print(json.dumps(verify_entries(entries, lookup, horizon_days=horizon, band_pct=band),
                         ensure_ascii=False, indent=2))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
