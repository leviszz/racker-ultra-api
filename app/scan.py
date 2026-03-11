from fastapi import Depends, APIRouter
from app.users import fastapi_users
from app.models import User
import requests
import pandas as pd
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
import warnings
import asyncio
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import AsyncSessionLocal
from app.models import UserClick

warnings.filterwarnings("ignore", category=FutureWarning)

# ================= DB ================= #

async def get_async_db():
    async with AsyncSessionLocal() as session:
        yield session

router = APIRouter()
current_active_user = fastapi_users.current_user(active=True)

# ================= CONFIG ================= #

@dataclass
class Config:
    EMA_FAST: int = 9
    EMA_MID: int = 21
    EMA_SLOW: int = 50
    MA_TREND: int = 200

    TOP_BY_VOLUME: int = 250
    WORKERS: int = 20
    SUPORTE_BUFFER: float = 1.2

    SCAN_TFS: list = field(default_factory=lambda: [
        "5m", "15m", "30m", "1h", "4h"
    ])

CFG = Config()

TF_LABELS = {
    "5m":  "5 Minutos",
    "15m": "15 Minutos",
    "30m": "30 Minutos",
    "1h":  "1 Hora",
    "4h":  "4 Horas",
    "1d":  "1 Dia",
    "1w":  "1 Semana",
    "1M":  "1 Mês",
    "1Y":  "1 Ano"
}

BASE_URL = "https://open-api.bingx.com"

# ================= HTTP SESSION ================= #

SESSION = requests.Session()
adapter = HTTPAdapter(
    pool_connections=CFG.WORKERS,
    pool_maxsize=CFG.WORKERS
)
SESSION.mount("https://", adapter)

# ================= CACHE GLOBAL ================= #

LATEST_RESULTS = []
LAST_UPDATE = None

# ================= REQUEST ================= #

def get_json(url, params=None):
    try:
        r = SESSION.get(url, params=params, timeout=10)
        if r.status_code == 200:
            return r.json()
        print("HTTP error:", r.status_code)
    except Exception as e:
        print("Request error:", e)
    return {}

# ================= KLINES ================= #

def fetch_klines(symbol, interval, limit=250):
    params = {
        "symbol": symbol,
        "interval": interval,
        "limit": str(limit)
    }
    data = get_json(f"{BASE_URL}/openApi/swap/v3/quote/klines", params)
    return data.get("data", [])

# ================= URL BINANCE ================= #

def binance_url(symbol):
    return f"https://www.binance.com/pt/futures/{symbol.replace('-', '')}"

# ================= MONTHLY / YEARLY ================= #

def build_monthly_from_daily(symbol):
    data = fetch_klines(symbol, "1d", 1000)
    if not data:
        return []
    df = pd.DataFrame(data, columns=["time","open","high","low","close","vol"]).apply(pd.to_numeric)
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    df = df.set_index("time")
    monthly = df.resample("ME").agg({"open":"first","high":"max","low":"min","close":"last","vol":"sum"}).dropna()
    monthly = monthly.reset_index()
    monthly["time"] = monthly["time"].astype(int) // 10**6
    return monthly.values.tolist()

def build_yearly_from_weekly(symbol):
    data = fetch_klines(symbol, "1w", 1000)
    if not data:
        return []
    df = pd.DataFrame(data, columns=["time","open","high","low","close","vol"]).apply(pd.to_numeric)
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    df = df.set_index("time")
    yearly = df.resample("YE").agg({"open":"first","high":"max","low":"min","close":"last","vol":"sum"}).dropna()
    yearly = yearly.reset_index()
    yearly["time"] = yearly["time"].astype(int) // 10**6
    return yearly.values.tolist()

# ================= LOGIC ================= #

def analyze_logic(ks, tf):
    if not ks or len(ks) < CFG.MA_TREND:
        return None

    df = pd.DataFrame(
        ks,
        columns=["time","open","high","low","close","vol"]
    ).apply(pd.to_numeric)

    df = df.sort_values("time").reset_index(drop=True)

    close = df["close"]
    high  = df["high"]
    low   = df["low"]

    e9    = close.ewm(span=CFG.EMA_FAST, adjust=False).mean()
    e21   = close.ewm(span=CFG.EMA_MID,  adjust=False).mean()
    e50   = close.ewm(span=CFG.EMA_SLOW, adjust=False).mean()
    ma200 = close.rolling(CFG.MA_TREND).mean()

    # Lógica original do chefe: último candle (idx = len - 1)
    idx   = len(df) - 1
    price = close.iloc[idx]

    if pd.isna(ma200.iloc[idx]):
        return None

    # Filtro de tendência: só SHORT (abaixo da MA200)
    if price > ma200.iloc[idx]:
        return None

    efeito = None

    # ── Cachoeira / Beijo da morte / Antecipação ──
    if e9.iloc[idx] < e21.iloc[idx] < e50.iloc[idx]:
        if not (e9.iloc[idx-5] < e21.iloc[idx-5] < e50.iloc[idx-5]):
            efeito = f"INÍCIO CACHOEIRA ({tf})"
        else:
            efeito = f"FLUXO CACHOEIRA ({tf})"
    else:
        dist = abs(high.iloc[idx] - e21.iloc[idx]) / e21.iloc[idx] * 100
        if dist < 0.3 and price < e9.iloc[idx]:
            efeito = f"BEIJO DA MORTE ({tf})"
        elif abs(e9.iloc[idx] - e21.iloc[idx]) < abs(e9.iloc[idx-1] - e21.iloc[idx-1]):
            efeito = f"ANTECIPAÇÃO ({tf})"

    if not efeito:
        return None

    # ── Candle Pilha (lógica original do chefe) ──
    pilha = "---"
    o = float(df.iloc[idx]["open"])
    h = float(df.iloc[idx]["high"])
    l = float(df.iloc[idx]["low"])
    c = float(df.iloc[idx]["close"])

    if (o > c) and ((o - c) / (h - l) >= 0.8) and ((c - l) / (h - l) <= 0.15):
        pilha = f"🔋 PILHA ({tf})"

    # ── Suporte ──
    min_recent = low.iloc[max(0, idx-30):idx].min()
    perigo = (
        "SUPORTE PERIGOSO"
        if ((price - min_recent) / min_recent) * 100 <= CFG.SUPORTE_BUFFER
        else "SUPORTE FRACO"
    )

    return {
        "efeito": efeito,
        "pilha":  pilha,
        "perigo": perigo
    }

# ================= ANALYZE SYMBOL ================= #

def analyze_symbol(sym, c_map):
    results = []

    for tf in CFG.SCAN_TFS:
        if tf == "1M":
            ks = build_monthly_from_daily(sym)
        elif tf == "1Y":
            ks = build_yearly_from_weekly(sym)
        else:
            ks = fetch_klines(sym, tf)

        res = analyze_logic(ks, tf)

        if res:
            results.append({
                "par":      sym.replace("-USDT", ""),
                "sinal":    res["efeito"],
                "tf":       TF_LABELS.get(tf, tf),
                "suporte":  res["perigo"],
                "pilha":    res["pilha"],
                "variacao": f"{float(c_map.get(sym, 0) or 0):.2f}%",
                "binance":  binance_url(sym)
            })

    return results

# ================= FULL SCAN ================= #

def run_full_scan():
    ticker = get_json(f"{BASE_URL}/openApi/swap/v2/quote/ticker").get("data", [])

    if not ticker:
        return []

    c_map = {
        x["symbol"]: x.get("priceChangePercent", 0)
        for x in ticker
    }

    syms = [
        x["symbol"]
        for x in ticker
        if x["symbol"].endswith("-USDT")
    ][:CFG.TOP_BY_VOLUME]

    results = []

    with ThreadPoolExecutor(max_workers=CFG.WORKERS) as ex:
        futures = [ex.submit(analyze_symbol, s, c_map) for s in syms]
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.extend(r)

    print("Resultados encontrados:", len(results))
    return results

# ================= SCAN LOOP ================= #

async def scan_loop():
    global LATEST_RESULTS, LAST_UPDATE

    while True:
        try:
            print("Rodando scan automático...")
            LATEST_RESULTS = await asyncio.to_thread(run_full_scan)
            LAST_UPDATE = datetime.utcnow()
            print("Scan finalizado")
        except Exception as e:
            print("Erro no scan:", e)

        await asyncio.sleep(300)

# ================= ENDPOINT ================= #

@router.get("/scan")
async def scan(
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    try:
        novo_clique = UserClick(user_id=user.id, coin="GERAL")
        db.add(novo_clique)
        await db.commit()
    except Exception as e:
        print("Erro registrar clique:", e)

    return {
        "total":              len(LATEST_RESULTS),
        "resultados":         LATEST_RESULTS,
        "ultima_atualizacao": LAST_UPDATE
    }

# ================= TRACK COIN ================= #

@router.post("/track-coin/{moeda}")
async def track_coin(
    moeda: str,
    user: User = Depends(current_active_user),
    db: AsyncSession = Depends(get_async_db)
):
    try:
        novo_clique = UserClick(user_id=user.id, coin=moeda.upper())
        db.add(novo_clique)
        await db.commit()
        return {"status": "sucesso", "moeda": moeda}
    except Exception as e:
        print("Erro registrar moeda:", e)
        return {"status": "erro"}