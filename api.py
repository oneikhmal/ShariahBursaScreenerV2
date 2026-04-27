"""
api.py — Bursa Shariah Screener Backend
Run: uvicorn api:app --reload --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import yfinance as yf
import pandas as pd
import numpy as np
import time
import logging
from functools import lru_cache
import json

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="Bursa Shariah Screener API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Universe ──────────────────────────────────────────────────────────────────
BURSA_TICKERS = {
    "Maybank":           "1155.KL",
    "CIMB Group":        "1023.KL",
    "Public Bank":       "1295.KL",
    "RHB Bank":          "1066.KL",
    "Hong Leong Bank":   "5819.KL",
    "IOI Corporation":   "1961.KL",
    "KL Kepong":         "2445.KL",
    "Sime Darby Plant.": "5285.KL",
    "Boustead Plant.":   "5254.KL",
    "My EG Services":    "0138.KL",
    "Inari Amertron":    "0166.KL",
    "Frontken Corp":     "0128.KL",
    "IHH Healthcare":    "5225.KL",
    "KPJ Healthcare":    "5878.KL",
    "Pharmaniaga":       "7081.KL",
    "Nestle Malaysia":   "4707.KL",
    "Dutch Lady":        "3026.KL",
    "QL Resources":      "7084.KL",
    "Padini Holdings":   "7052.KL",
    "Tenaga Nasional":   "5347.KL",
    "YTL Power":         "6742.KL",
    "Petronas Gas":      "5183.KL",
    "Maxis":             "6012.KL",
    "Axiata Group":      "6888.KL",
    "Gamuda":            "5398.KL",
    "IJM Corporation":   "3336.KL",
    "Sunway Constr.":    "5263.KL",
    "Genting":           "3182.KL",
    "Genting Malaysia":  "4715.KL",
    "KLCC Prop. REIT":   "5235SS.KL",
    "Pavilion REIT":     "5212.KL",
    "Dialog Group":      "7277.KL",
    "Hartalega":         "5168.KL",
}

EXCLUDED_SECTORS = {
    "financial services","banks","insurance","diversified financial services",
    "consumer finance","capital markets","credit services",
    "beverages—brewers","distillers & vintners","beverages - brewers",
    "gambling","casinos & gaming","tobacco","adult entertainment","defense",
}
EXCLUDED_KEYWORDS = [
    "bank","banking","insurance","gaming","casino","genting","alcohol",
    "tobacco","brewery","distill","wine","beer","spirits","betting","lottery",
    "weapon","defense","armament"
]

DEBT_THRESHOLD     = 0.33
CASH_THRESHOLD     = 0.33
INT_HARD_THRESHOLD = 0.33
INT_PURIFY_THRESHOLD = 0.05


def safe(v, default=None):
    if v is None: return default
    if isinstance(v, float) and np.isnan(v): return default
    return v


def fetch_one(name, ticker):
    try:
        t = yf.Ticker(ticker)
        info = t.info
        row = {
            "ticker": ticker, "name": info.get("longName") or name,
            "sector": info.get("sector","Unknown"),
            "industry": info.get("industry","Unknown"),
            "price": safe(info.get("currentPrice") or info.get("regularMarketPrice")),
            "market_cap": safe(info.get("marketCap")),
            "description": (info.get("longBusinessSummary") or "").lower(),
            "currency": info.get("currency","MYR"),
            "total_debt": None, "cash_and_equivalents": None,
            "receivables": None, "total_assets": None,
            "total_revenue": None, "interest_income_expense": 0.0,
        }
        bs = t.balance_sheet
        if bs is not None and not bs.empty:
            col = bs.iloc[:,0]
            for key in ["Total Debt","Long Term Debt","Short Long Term Debt"]:
                v = safe(col.get(key))
                if v: row["total_debt"] = max(row["total_debt"] or 0, float(v))
            for key in ["Cash And Cash Equivalents","Cash Cash Equivalents And Short Term Investments"]:
                v = safe(col.get(key))
                if v: row["cash_and_equivalents"] = float(v); break
            for key in ["Net Receivables","Accounts Receivable"]:
                v = safe(col.get(key))
                if v: row["receivables"] = float(v); break
            v = safe(col.get("Total Assets"))
            if v: row["total_assets"] = float(v)
        inc = t.financials
        if inc is not None and not inc.empty:
            col = inc.iloc[:,0]
            v = safe(col.get("Total Revenue"))
            if v: row["total_revenue"] = float(v)
            v = safe(col.get("Interest Expense"))
            if v: row["interest_income_expense"] = abs(float(v))
        return row
    except Exception as e:
        log.warning(f"  ✗ {name}: {e}")
        return {"ticker":ticker,"name":name,"sector":"Unknown","industry":"Unknown",
                "price":None,"market_cap":None,"description":"","currency":"MYR",
                "total_debt":None,"cash_and_equivalents":None,"receivables":None,
                "total_assets":None,"total_revenue":None,"interest_income_expense":0.0}


def screen_one(row):
    mc = row.get("market_cap")
    desc = row.get("description","")
    sector = row.get("sector","").lower()
    industry = row.get("industry","").lower()
    name_lower = row.get("name","").lower()

    # 1. Sector
    sector_fail  = sector in EXCLUDED_SECTORS or industry in EXCLUDED_SECTORS
    keyword_fail = any(k in desc or k in name_lower for k in EXCLUDED_KEYWORDS)
    pass_sector  = not (sector_fail or keyword_fail)

    # 2. Debt ratio
    debt = row.get("total_debt")
    debt_ratio = None; pass_debt = None
    if debt is not None and mc and mc > 0:
        debt_ratio = debt / mc
        pass_debt  = debt_ratio < DEBT_THRESHOLD

    # 3. Cash ratio
    cash = row.get("cash_and_equivalents") or 0
    recv = row.get("receivables") or 0
    cash_ratio = None; pass_cash = None
    if mc and mc > 0 and (cash > 0 or recv > 0):
        cash_ratio = (cash + recv) / mc
        pass_cash  = cash_ratio < CASH_THRESHOLD

    # 4. Interest / revenue
    rev  = row.get("total_revenue")
    iexp = row.get("interest_income_expense") or 0
    int_ratio = None; pass_interest = None; purify_pct = None
    if rev and rev > 0:
        int_ratio  = iexp / rev
        purify_pct = int_ratio * 100
        if int_ratio >= INT_HARD_THRESHOLD: pass_interest = False
        else: pass_interest = True

    # Verdict
    fail_reasons = []
    if not pass_sector:
        fail_reasons.append(f"Excluded sector/activity: {row.get('sector')}")
    if pass_debt is False:
        fail_reasons.append(f"Debt ratio {debt_ratio:.1%} exceeds 33%")
    if pass_cash is False:
        fail_reasons.append(f"Cash ratio {cash_ratio:.1%} exceeds 33%")
    if pass_interest is False:
        fail_reasons.append(f"Interest income {int_ratio:.1%} exceeds 33% hard limit")

    has_data = any(x is not None for x in [pass_debt, pass_cash, pass_interest])

    if not pass_sector:                                   verdict = "NOT_COMPLIANT"
    elif not has_data:                                    verdict = "NO_DATA"
    elif not pass_debt or not pass_cash or not pass_interest: verdict = "NOT_COMPLIANT"
    elif int_ratio is not None and int_ratio >= INT_PURIFY_THRESHOLD: verdict = "PURIFY"
    else:                                                 verdict = "HALAL"

    def fmt(v): return round(v*100, 2) if v is not None else None

    return {
        **row,
        "description": None,  # strip for JSON size
        "debt_ratio":            fmt(debt_ratio),
        "cash_ratio":            fmt(cash_ratio),
        "interest_revenue_ratio": fmt(int_ratio),
        "purification_pct":      round(purify_pct, 3) if purify_pct else None,
        "pass_sector":   pass_sector,
        "pass_debt":     pass_debt,
        "pass_cash":     pass_cash,
        "pass_interest": pass_interest,
        "fail_reasons":  fail_reasons,
        "verdict":       verdict,
        "market_cap_fmt": fmt_myr(row.get("market_cap")),
    }


def fmt_myr(v):
    if not v: return "—"
    if v >= 1e9: return f"MYR {v/1e9:.2f}B"
    if v >= 1e6: return f"MYR {v/1e6:.1f}M"
    return f"MYR {v:,.0f}"


_cache = {"data": None, "ts": 0}
CACHE_TTL = 3600  # 1 hour


@app.get("/api/screen")
def screen_all():
    now = time.time()
    if _cache["data"] and (now - _cache["ts"]) < CACHE_TTL:
        return JSONResponse(_cache["data"])

    log.info("Fetching fresh data...")
    results = []
    for name, ticker in BURSA_TICKERS.items():
        raw = fetch_one(name, ticker)
        results.append(screen_one(raw))
        time.sleep(0.3)

    counts = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    payload = {
        "stocks": results,
        "summary": {
            "total":   len(results),
            "halal":   counts.get("HALAL", 0),
            "purify":  counts.get("PURIFY", 0),
            "fail":    counts.get("NOT_COMPLIANT", 0),
            "nodata":  counts.get("NO_DATA", 0),
        },
        "cached_at": int(now),
    }
    _cache["data"] = payload
    _cache["ts"]   = now
    return JSONResponse(payload)


@app.get("/api/health")
def health():
    return {"status": "ok"}
