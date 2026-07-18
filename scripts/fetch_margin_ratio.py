#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
台股個股「融資維持率」估算與篩選工具
==========================================

背景與重要說明
--------------
台灣證交所 / 櫃買中心只公布「大盤（全市場）融資維持率」，並未公布「個股」融資維持率，
因為維持率本質上是「整戶」（單一投資人帳戶）的概念，且需要知道每一筆融資的實際
「融資成本」（買進成本），這個資料只有券商知道。

因此本程式採用市場上常見的「加權平均成本法」來 **估算** 個股融資維持率，
方法與 XQ 全球贏家等看盤軟體公開說明的邏輯相同：

    融資成本(t) = 融資成本(t-1) × (今日餘額(t) - 今日買進(t)) / 今日餘額(t)
                  + 收盤價(t) × 今日買進(t) / 今日餘額(t)

    個股融資維持率(估) = 現在股價 / (融資成本 × 融資成數) × 100%

限制（請務必閱讀，輸出網頁也會顯示）：
 1. 「融資成本」是用最近 N 個交易日（預設 60 日）的資料回推的加權平均值，
    若融資部位是在回推區間「之前」就已經存在，則成本的起始值只能用區間第一天
    的收盤價當作估計種子，可能與實際成本有落差。
 2. 融資成數預設為 60%（一般上市櫃普通股），ETF 另外設為 90%；實際成數會因
    個股是否being列為「注意股 / 處置股 / 全額交割股」等而調整，本程式未逐一
    比對這些名單，僅在 Note 欄位有值時於表格中標示提醒。
 3. 這是 **估算值**，僅供篩選觀察使用，不是券商實際計算的整戶維持率，
    不構成投資建議，請勿作為單一交易依據。

資料來源
--------
 - FinMind API（https://finmindtrade.com/）
     * TaiwanStockInfo                    → 全市場股票清單
     * TaiwanStockMarginPurchaseShortSale → 個股每日融資融券資料
     * TaiwanStockPrice                   → 個股每日收盤價
 - 台灣證券交易所 OpenAPI（https://openapi.twse.com.tw/）（僅用於加速：
   批次取得上市股票「今日收盤價」與「今日融資餘額」，用來預先篩掉目前沒有
   融資餘額 / 不在使用者指定股價區間內的股票，減少呼叫 FinMind 的次數）
 - 證券櫃檯買賣中心（TPEx，上櫃）網站前端 JSON API（非正式 OpenAPI 文件端點，
   參考開源專案 chunkai1312/node-twstock 的實作方式），功能同上、用於上櫃股票。
   若此輔助來源連線失敗或格式改變，程式會自動退回「全部改用 FinMind 逐檔查詢」，
   不影響正確性，只是速度變慢。

環境變數（皆為選填，未設定則使用預設值）
--------
 FINMIND_TOKEN            FinMind API token（強烈建議設定，可提高速率限制到 600 次/小時）
 MARGIN_RATIO_THRESHOLD   篩選門檻，預設 130（表示 < 130%）
 LOOKBACK_DAYS            回推估算融資成本用的交易日數，預設 60
 PRICE_MIN / PRICE_MAX    只篩選股價介於此區間的股票（可只設一個），預設不限制
 MARKETS                  要掃描的市場，逗號分隔，預設 "twse,tpex"
 STOCK_LIMIT              測試用：只處理前 N 檔股票（0 或不設 = 不限制）
 REQUESTS_PER_HOUR        FinMind 呼叫節流速度，預設：有 token 580，沒 token 280
 OUTPUT_DIR               輸出資料夾，預設 "docs"
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------

FINMIND_BASE_URL = "https://api.finmindtrade.com/api/v4/data"
TWSE_STOCK_DAY_ALL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_MI_MARGN = "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"

TAIPEI_TZ = timezone(timedelta(hours=8))


def env_float(name: str, default: Optional[float]) -> Optional[float]:
    v = os.environ.get(name, "").strip()
    if not v:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except ValueError:
        return default


FINMIND_TOKEN = os.environ.get("FINMIND_TOKEN", "").strip()
MARGIN_RATIO_THRESHOLD = env_float("MARGIN_RATIO_THRESHOLD", 130.0)
LOOKBACK_DAYS = env_int("LOOKBACK_DAYS", 60)
PRICE_MIN = env_float("PRICE_MIN", None)
PRICE_MAX = env_float("PRICE_MAX", None)
MARKETS = [m.strip().lower() for m in os.environ.get("MARKETS", "twse,tpex").split(",") if m.strip()]
STOCK_LIMIT = env_int("STOCK_LIMIT", 0)
REQUESTS_PER_HOUR = env_int("REQUESTS_PER_HOUR", 580 if FINMIND_TOKEN else 280)
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "docs")

DEFAULT_FINANCING_RATIO = 0.6   # 一般上市櫃普通股融資成數（自備四成）
ETF_FINANCING_RATIO = 0.9       # ETF 融資成數（多數為九成，僅為概略假設）

MIN_SLEEP_SEC = 3600.0 / max(REQUESTS_PER_HOUR, 1)


# --------------------------------------------------------------------------
# 節流 + 重試的 FinMind 呼叫
# --------------------------------------------------------------------------

class RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last_call = 0.0

    def wait(self):
        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_call = time.monotonic()


_limiter = RateLimiter(MIN_SLEEP_SEC)
_session = requests.Session()


def finmind_get(dataset: str, data_id: str = "", start_date: str = "",
                 end_date: str = "", extra: Optional[dict] = None,
                 max_retries: int = 5) -> list:
    """呼叫 FinMind API，內建節流與速率限制的重試/等待機制。"""
    params = {"dataset": dataset}
    if data_id:
        params["data_id"] = data_id
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN
    if extra:
        params.update(extra)

    for attempt in range(1, max_retries + 1):
        _limiter.wait()
        try:
            resp = _session.get(FINMIND_BASE_URL, params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"  [warn] request error ({dataset} {data_id}): {exc}, retry {attempt}/{max_retries}")
            time.sleep(min(30 * attempt, 180))
            continue

        if resp.status_code == 200:
            try:
                payload = resp.json()
            except ValueError:
                print(f"  [warn] non-JSON response for {dataset} {data_id}, retry")
                time.sleep(10)
                continue
            msg = str(payload.get("msg", ""))
            if "reach api request limit" in msg.lower() or payload.get("status") == 402:
                wait_s = _seconds_to_next_hour() + 30
                print(f"  [rate-limit] hit FinMind hourly limit, sleeping {wait_s:.0f}s until next window...")
                time.sleep(wait_s)
                continue
            return payload.get("data", [])
        elif resp.status_code in (429, 402):
            wait_s = _seconds_to_next_hour() + 30
            print(f"  [rate-limit] HTTP {resp.status_code}, sleeping {wait_s:.0f}s...")
            time.sleep(wait_s)
            continue
        else:
            print(f"  [warn] HTTP {resp.status_code} for {dataset} {data_id}, retry {attempt}/{max_retries}")
            time.sleep(min(15 * attempt, 120))
            continue

    print(f"  [error] giving up on {dataset} {data_id} after {max_retries} retries")
    return []


def _seconds_to_next_hour() -> float:
    now = datetime.now(timezone.utc)
    nxt = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    return max((nxt - now).total_seconds(), 60.0)


# --------------------------------------------------------------------------
# 輔助：批次抓取 TWSE 上市股票今日收盤價 + 融資餘額（用來預先篩選，加速整體流程）
# --------------------------------------------------------------------------

def fetch_twse_bulk_snapshot() -> dict:
    """回傳 {stock_id: {"close": float, "balance": int}}；失敗則回傳空 dict（自動退回逐檔模式）。"""
    snapshot: dict = {}
    try:
        r = _session.get(TWSE_STOCK_DAY_ALL, timeout=30)
        r.raise_for_status()
        rows = r.json()
        for row in rows:
            code = row.get("Code") or row.get("證券代號")
            close = row.get("ClosingPrice") or row.get("收盤價")
            if code and close not in (None, "", "--"):
                try:
                    snapshot[code] = {"close": float(str(close).replace(",", "")), "balance": None}
                except ValueError:
                    pass
    except Exception as exc:
        print(f"[info] TWSE STOCK_DAY_ALL bulk fetch failed ({exc}); will skip TWSE price pre-filter.")
        return {}

    try:
        r = _session.get(TWSE_MI_MARGN, timeout=30)
        r.raise_for_status()
        rows = r.json()
        for row in rows:
            code = row.get("股票代號") or row.get("Code")
            bal = row.get("融資今日餘額") or row.get("MarginTodayBalance")
            if code in snapshot and bal not in (None, "", "--"):
                try:
                    snapshot[code]["balance"] = int(str(bal).replace(",", ""))
                except ValueError:
                    pass
    except Exception as exc:
        print(f"[info] TWSE MI_MARGN bulk fetch failed ({exc}); will skip zero-balance pre-filter for TWSE.")

    return snapshot


# --------------------------------------------------------------------------
# 輔助：批次抓取 TPEx 上櫃股票今日收盤價 + 融資餘額（同上，用來預先篩選）
# --------------------------------------------------------------------------

TPEX_DAILY_QUOTES = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
TPEX_MARGIN_BALANCE = "https://www.tpex.org.tw/www/zh-tw/margin/balance"


def _tpex_num(v) -> Optional[float]:
    if v in (None, "", "---", "--"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def _fetch_tpex_table(url: str, date_str: str) -> Optional[list]:
    r = _session.get(url, params={"date": date_str, "response": "json"}, timeout=30)
    r.raise_for_status()
    payload = r.json()
    tables = payload.get("tables") or []
    if not tables or not tables[0].get("totalCount"):
        return None
    return tables[0].get("data") or []


def fetch_tpex_bulk_snapshot() -> dict:
    """回傳 {stock_id: {"close": float, "balance": float}}；失敗則回傳空 dict（自動退回逐檔模式）。

    資料來源為櫃買中心網站前端使用的 JSON API（afterTrading/dailyQuotes 與
    margin/balance），非官方 OpenAPI 文件正式列出的端點，格式參考自開源專案
    chunkai1312/node-twstock 的實作。若櫃買中心未來調整此格式，這個函式會
    fail-soft（回傳空 dict），程式會自動退回「上櫃股票逐檔查詢 FinMind」，
    不影響正確性，只是速度變慢。
    """
    snapshot: dict = {}

    # 往回找最近一個有資料的交易日（假日/尚未收盤時會是空的）
    quotes_rows = None
    used_date = None
    for back in range(0, 6):
        d = datetime.now(TAIPEI_TZ) - timedelta(days=back)
        date_str = d.strftime("%Y/%m/%d")
        try:
            rows = _fetch_tpex_table(TPEX_DAILY_QUOTES, date_str)
        except Exception as exc:
            print(f"[info] TPEx dailyQuotes 查詢 {date_str} 失敗 ({exc})；將略過上櫃預先篩選。")
            return {}
        if rows:
            quotes_rows = rows
            used_date = date_str
            break

    if not quotes_rows:
        print("[info] TPEx dailyQuotes 找不到近期交易日資料；將略過上櫃預先篩選。")
        return {}

    for row in quotes_rows:
        if not row or len(row) < 3:
            continue
        symbol, name, *values = row
        close = _tpex_num(values[0]) if len(values) > 0 else None
        if symbol and close is not None:
            snapshot[symbol.strip()] = {"close": close, "balance": None}

    try:
        margin_rows = _fetch_tpex_table(TPEX_MARGIN_BALANCE, used_date) or []
        for row in margin_rows:
            if not row or len(row) < 5:
                continue
            symbol, name, *values = row
            symbol = symbol.strip()
            balance = _tpex_num(values[4]) if len(values) > 4 else None  # marginBalance
            if symbol in snapshot and balance is not None:
                snapshot[symbol]["balance"] = balance
    except Exception as exc:
        print(f"[info] TPEx margin/balance 查詢失敗 ({exc})；將略過上櫃零融資餘額預先篩選"
              f"（收盤價預先篩選仍會生效）。")

    print(f"      TPEx 快照使用交易日: {used_date}")
    return snapshot


# --------------------------------------------------------------------------
# 主要資料結構
# --------------------------------------------------------------------------

@dataclass
class StockResult:
    stock_id: str
    stock_name: str
    market: str
    industry: str
    close_price: float
    margin_balance_lots: float
    est_cost: float
    financing_ratio: float
    ratio_pct: float
    note: str = ""


def get_stock_universe() -> list:
    """取得全市場股票清單（單次呼叫 TaiwanStockInfo，不需要 data_id）。"""
    print("[1/4] 取得全市場股票清單 (TaiwanStockInfo) ...")
    data = finmind_get("TaiwanStockInfo")
    if not data:
        print("[error] 無法取得股票清單，程式終止。")
        sys.exit(1)

    # 用 stock_id 去重，保留最新一筆
    latest = {}
    for row in data:
        sid = row.get("stock_id", "")
        if not sid or not sid.isdigit():
            continue
        latest[sid] = row

    universe = []
    for sid, row in latest.items():
        typ = str(row.get("type", "")).lower()
        if MARKETS and typ not in MARKETS:
            continue
        universe.append({
            "stock_id": sid,
            "stock_name": row.get("stock_name", ""),
            "industry": row.get("industry_category", ""),
            "market": typ,
        })
    universe.sort(key=lambda x: x["stock_id"])
    print(f"      符合市場條件（{','.join(MARKETS)}）的股票共 {len(universe)} 檔")
    return universe


def compute_est_cost(margin_rows: list, price_by_date: dict) -> tuple:
    """回傳 (最新融資餘額張數, 估計融資成本, 最新收盤價, note)。若無法估計回傳 (0, None, None, '')。"""
    if not margin_rows:
        return 0, None, None, ""

    margin_rows = sorted(margin_rows, key=lambda r: r.get("date", ""))
    cost_prev = None
    last_balance = 0.0
    last_close = None
    note = ""

    for i, row in enumerate(margin_rows):
        d = row.get("date", "")
        close = price_by_date.get(d)
        if close is None:
            continue  # 當日沒有價格資料（例如假日或資料缺漏），跳過
        try:
            balance_today = float(row.get("MarginPurchaseTodayBalance", 0) or 0)
            buy_today = float(row.get("MarginPurchaseBuy", 0) or 0)
        except (TypeError, ValueError):
            continue

        if row.get("Note"):
            note = str(row.get("Note"))

        if balance_today <= 0:
            cost_prev = None
            last_balance = 0.0
            last_close = close
            continue

        if cost_prev is None or i == 0:
            cost_today = close  # 種子值：假設區間第一天的部位是用當天收盤價買的
        elif buy_today >= balance_today:
            cost_today = close
        else:
            carried = balance_today - buy_today
            cost_today = (cost_prev * carried + close * buy_today) / balance_today

        cost_prev = cost_today
        last_balance = balance_today
        last_close = close

    return last_balance, cost_prev, last_close, note


def process_stock(info: dict, lookback_days: int) -> Optional[StockResult]:
    sid = info["stock_id"]
    end_date = datetime.now(TAIPEI_TZ).strftime("%Y-%m-%d")
    start_date = (datetime.now(TAIPEI_TZ) - timedelta(days=int(lookback_days * 1.6) + 10)).strftime("%Y-%m-%d")

    margin_rows = finmind_get("TaiwanStockMarginPurchaseShortSale", data_id=sid,
                               start_date=start_date, end_date=end_date)
    if not margin_rows:
        return None

    price_rows = finmind_get("TaiwanStockPrice", data_id=sid,
                              start_date=start_date, end_date=end_date)
    price_by_date = {r.get("date"): r.get("close") for r in price_rows if r.get("close") is not None}
    if not price_by_date:
        return None

    balance, cost, close, note = compute_est_cost(margin_rows, price_by_date)
    if not balance or balance <= 0 or not cost or cost <= 0 or not close:
        return None

    financing_ratio = ETF_FINANCING_RATIO if info.get("industry") == "ETF" else DEFAULT_FINANCING_RATIO
    ratio_pct = close / (cost * financing_ratio) * 100.0

    return StockResult(
        stock_id=sid,
        stock_name=info.get("stock_name", ""),
        market=info.get("market", ""),
        industry=info.get("industry", ""),
        close_price=close,
        margin_balance_lots=balance,
        est_cost=round(cost, 2),
        financing_ratio=financing_ratio,
        ratio_pct=round(ratio_pct, 2),
        note=note,
    )


def main():
    started = datetime.now(TAIPEI_TZ)
    print(f"===== 台股個股融資維持率估算開始 {started.isoformat()} =====")
    print(f"門檻: < {MARGIN_RATIO_THRESHOLD}%　回推天數: {LOOKBACK_DAYS}　市場: {MARKETS}")
    print(f"股價區間篩選: PRICE_MIN={PRICE_MIN} PRICE_MAX={PRICE_MAX}")
    print(f"FinMind token: {'已設定' if FINMIND_TOKEN else '未設定（匿名，速率較低）'}　"
          f"節流速度約 {REQUESTS_PER_HOUR} 次/小時")

    universe = get_stock_universe()

    print("[2/4] 批次預先取得上市／上櫃股票今日收盤價 / 融資餘額（加速篩選）...")
    bulk_snapshot = {}

    if "twse" in MARKETS:
        twse_snapshot = fetch_twse_bulk_snapshot()
        if twse_snapshot:
            print(f"      取得 {len(twse_snapshot)} 檔上市股票快照")
        else:
            print("      上市快照略過（將對所有上市股票逐檔向 FinMind 查詢）")
        bulk_snapshot.update(twse_snapshot)

    if "tpex" in MARKETS:
        tpex_snapshot = fetch_tpex_bulk_snapshot()
        if tpex_snapshot:
            print(f"      取得 {len(tpex_snapshot)} 檔上櫃股票快照")
        else:
            print("      上櫃快照略過（將對所有上櫃股票逐檔向 FinMind 查詢）")
        bulk_snapshot.update(tpex_snapshot)

    candidates = []
    skipped_prefilter = 0
    for info in universe:
        sid = info["stock_id"]
        snap = bulk_snapshot.get(sid)
        if snap is not None:
            price = snap.get("close")
            balance = snap.get("balance")
            if balance is not None and balance <= 0:
                skipped_prefilter += 1
                continue
            if price is not None:
                if PRICE_MIN is not None and price < PRICE_MIN:
                    skipped_prefilter += 1
                    continue
                if PRICE_MAX is not None and price > PRICE_MAX:
                    skipped_prefilter += 1
                    continue
        candidates.append(info)

    if STOCK_LIMIT and STOCK_LIMIT > 0:
        candidates = candidates[:STOCK_LIMIT]

    print(f"      預先篩選跳過 {skipped_prefilter} 檔（無融資餘額 / 不在股價區間）")
    print(f"[3/4] 需向 FinMind 查詢的股票共 {len(candidates)} 檔"
          f"（預估約 {len(candidates) * 2 / max(REQUESTS_PER_HOUR, 1):.1f} 小時，"
          f"視實際回應狀況可能更久）")

    results: list = []
    for idx, info in enumerate(candidates, 1):
        try:
            res = process_stock(info, LOOKBACK_DAYS)
        except Exception:
            print(f"  [warn] {info['stock_id']} 處理失敗：\n{traceback.format_exc()}")
            res = None

        if res is not None:
            if PRICE_MIN is not None and res.close_price < PRICE_MIN:
                res = None
            elif PRICE_MAX is not None and res.close_price > PRICE_MAX:
                res = None

        if res is not None and res.ratio_pct < MARGIN_RATIO_THRESHOLD:
            results.append(res)
            print(f"  [{idx}/{len(candidates)}] {info['stock_id']} {info['stock_name']} "
                  f"→ 估算維持率 {res.ratio_pct:.1f}%  ← 符合門檻")
        elif idx % 50 == 0 or idx == len(candidates):
            print(f"  [{idx}/{len(candidates)}] 進度中...（目前累積 {len(results)} 檔符合）")

    results.sort(key=lambda r: r.ratio_pct)

    print(f"[4/4] 完成，共 {len(results)} 檔股票估算融資維持率 < {MARGIN_RATIO_THRESHOLD}%")
    write_outputs(results, started)


def write_outputs(results: list, started: datetime):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1) JSON（給下次比對 / 其他程式使用）
    json_path = os.path.join(OUTPUT_DIR, "margin_ratio_alert.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": started.isoformat(),
            "threshold": MARGIN_RATIO_THRESHOLD,
            "lookback_days": LOOKBACK_DAYS,
            "count": len(results),
            "stocks": [r.__dict__ for r in results],
        }, f, ensure_ascii=False, indent=2)

    # 2) CSV
    csv_path = os.path.join(OUTPUT_DIR, "margin_ratio_alert.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["股票代號", "股票名稱", "市場", "產業別", "收盤價", "融資餘額(張)",
                          "估計融資成本", "融資成數", "估算融資維持率(%)", "備註"])
        for r in results:
            writer.writerow([r.stock_id, r.stock_name, r.market, r.industry, r.close_price,
                              r.margin_balance_lots, r.est_cost, r.financing_ratio,
                              r.ratio_pct, r.note])

    # 3) HTML 網頁
    html_path = os.path.join(OUTPUT_DIR, "index.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(results, started))

    print(f"輸出完成: {html_path}, {csv_path}, {json_path}")


def render_html(results: list, started: datetime) -> str:
    rows_html = []
    for r in results:
        note_html = f'<span class="note">{escape(r.note)}</span>' if r.note else ""
        risk_class = "risk-high" if r.ratio_pct < 120 else ("risk-mid" if r.ratio_pct < 130 else "")
        rows_html.append(f"""
        <tr class="{risk_class}">
          <td>{escape(r.stock_id)}</td>
          <td>{escape(r.stock_name)}</td>
          <td>{escape(r.market.upper())}</td>
          <td>{escape(r.industry)}</td>
          <td class="num">{r.close_price:,.2f}</td>
          <td class="num">{r.margin_balance_lots:,.0f}</td>
          <td class="num">{r.est_cost:,.2f}</td>
          <td class="num">{r.financing_ratio*100:.0f}%</td>
          <td class="num ratio">{r.ratio_pct:.1f}%</td>
          <td>{note_html}</td>
        </tr>""")

    updated_str = started.strftime("%Y-%m-%d %H:%M (台北時間)")

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>台股個股融資維持率篩選（估算值） &lt; {MARGIN_RATIO_THRESHOLD:.0f}%</title>
<style>
  :root {{
    --bg: #0f1420; --panel:#161d2e; --border:#2a3450; --text:#e7ecf7; --muted:#93a0bd;
    --accent:#4f8cff; --high:#ff5470; --mid:#ffb454; --good:#3ddc97;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin:0; font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", Segoe UI, sans-serif;
    background: var(--bg); color: var(--text); padding: 24px 16px 60px;
  }}
  .wrap {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
  .meta {{ color: var(--muted); font-size: 0.9rem; margin-bottom: 18px; }}
  .disclaimer {{
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 16px; font-size: 0.85rem; color: var(--muted); line-height:1.6; margin-bottom: 22px;
  }}
  .disclaimer b {{ color: var(--mid); }}
  .stat-row {{ display:flex; gap:12px; margin-bottom: 18px; flex-wrap: wrap; }}
  .stat {{
    background: var(--panel); border:1px solid var(--border); border-radius: 10px;
    padding: 12px 18px; min-width: 140px;
  }}
  .stat .n {{ font-size: 1.6rem; font-weight: 700; }}
  .stat .l {{ font-size: 0.78rem; color: var(--muted); }}
  table {{ width:100%; border-collapse: collapse; background: var(--panel); border-radius: 10px; overflow:hidden; }}
  th, td {{ padding: 9px 10px; border-bottom: 1px solid var(--border); font-size: 0.86rem; text-align:left; }}
  th {{ background:#1c2540; color: var(--muted); font-weight:600; position: sticky; top:0; }}
  td.num {{ text-align:right; font-variant-numeric: tabular-nums; }}
  td.ratio {{ font-weight:700; }}
  tr.risk-high td.ratio {{ color: var(--high); }}
  tr.risk-mid td.ratio {{ color: var(--mid); }}
  tr:hover {{ background: rgba(79,140,255,0.08); }}
  .note {{ font-size: 0.78rem; color: var(--mid); }}
  .empty {{ padding: 40px; text-align:center; color: var(--muted); }}
  footer {{ margin-top: 24px; color: var(--muted); font-size: 0.78rem; }}
  a {{ color: var(--accent); }}
  .table-wrap {{ overflow-x:auto; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>台股個股融資維持率篩選（估算值）</h1>
  <div class="meta">更新時間：{updated_str}　｜　篩選門檻：估算維持率 &lt; {MARGIN_RATIO_THRESHOLD:.0f}%　｜
    資料來源：<a href="https://finmindtrade.com/" target="_blank" rel="noopener">FinMind API</a></div>

  <div class="disclaimer">
    <b>請注意：</b>台灣證交所／櫃買中心只公布「大盤」融資維持率，並未公布個股數值。
    本頁的「個股融資維持率」是用近期融資買進與收盤價，以<b>加權平均成本法</b>回推估算
    （公式：現價 ÷ (估計融資成本 × 融資成數)），<b>非券商實際計算的整戶維持率</b>，
    僅供觀察篩選參考，不構成投資建議，請自行查證並謹慎判斷。
  </div>

  <div class="stat-row">
    <div class="stat"><div class="n">{len(results)}</div><div class="l">符合篩選檔數</div></div>
    <div class="stat"><div class="n">{MARGIN_RATIO_THRESHOLD:.0f}%</div><div class="l">篩選門檻</div></div>
    <div class="stat"><div class="n">{started.strftime('%m/%d')}</div><div class="l">資料日期</div></div>
  </div>

  <div class="table-wrap">
  {"" if results else '<div class="empty">目前沒有符合條件（估算維持率 < ' + f'{MARGIN_RATIO_THRESHOLD:.0f}' + '%）的股票。</div>'}
  {"" if not results else '''
  <table>
    <thead><tr>
      <th>代號</th><th>名稱</th><th>市場</th><th>產業別</th>
      <th>收盤價</th><th>融資餘額(張)</th><th>估計成本</th><th>融資成數</th>
      <th>估算維持率</th><th>備註</th>
    </tr></thead>
    <tbody>''' + "".join(rows_html) + '''</tbody>
  </table>
  '''}
  </div>

  <footer>
    本頁由 GitHub Actions 排程自動產生／更新，程式碼與試算方法請見
    <a href="https://github.com" target="_blank" rel="noopener">GitHub repo</a> 的 README。
  </footer>
</div>
</body>
</html>
"""


def escape(s) -> str:
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


if __name__ == "__main__":
    main()
