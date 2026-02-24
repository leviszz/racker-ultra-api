from fastapi import Depends, APIRouter
from app.users import fastapi_users
from app.models import User
import requests
import pandas as pd
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
import warnings

warnings.filterwarnings("ignore")

router = APIRouter()
current_active_user = fastapi_users.current_user(active=True)

# ================= CONFIG ================= #
@dataclass
class Config:
    EMA_FAST: int = 9
    EMA_MID: int  = 21
    EMA_SLOW: int = 50
    MA_TREND: int = 200
    TOP_BY_VOLUME: int = 250
    WORKERS: int = 40
    SUPORTE_BUFFER: float = 1.2

    SCAN_TFS: list = field(default_factory=lambda: [
        "5m", "15m", "30m", "1h", "4h",
        "1d", "1w",
        "1M",  # mensal sintético
        "1Y"   # anual sintético
    ])

CFG = Config()

# ================= TRADUÇÃO VISUAL ================= #
TF_LABELS = {
    "5m": "5 Minutos",
    "15m": "15 Minutos",
    "30m": "30 Minutos",
    "1h": "1 Hora",
    "4h": "4 Horas",
    "1d": "1 Dia",
    "1w": "1 Semana",
    "1M": "1 Mês",
    "1Y": "1 Ano"
}

BASE_URL = "https://open-api.bingx.com"

SESSION = requests.Session()
adapter = HTTPAdapter(pool_connections=CFG.WORKERS, pool_maxsize=CFG.WORKERS)
SESSION.mount("https://", adapter)

# ================= CORE ================= #

def get_json(url, params=None):
    try:
        r = SESSION.get(url, params=params, timeout=6)
        return r.json() if r.status_code == 200 else {}
    except:
        return {}

def fetch_klines(symbol, interval, limit=500):
    params = {"symbol": symbol, "interval": interval, "limit": str(limit)}
    data = get_json(f"{BASE_URL}/openApi/swap/v3/quote/klines", params)
    return data.get("data", [])

def binance_url(symbol):
    return f"https://www.binance.com/pt/futures/{symbol.replace('-', '')}"

# ================= TIMEFRAMES SINTÉTICOS ================= #

def build_monthly_from_daily(symbol):
    data = fetch_klines(symbol, "1d", 1000)
    if not data:
        return []

    df = pd.DataFrame(data, columns=["time","open","high","low","close","vol"]).apply(pd.to_numeric)
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    df = df.set_index("time")

    monthly = df.resample("ME").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum"
    }).dropna()

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

    yearly = df.resample("YE").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "vol": "sum"
    }).dropna()

    yearly = yearly.reset_index()
    yearly["time"] = yearly["time"].astype(int) // 10**6
    return yearly.values.tolist()

# ================= LÓGICA ================= #

def analyze_logic(ks, tf):

    if not ks or len(ks) < 50:
        return None

    df = pd.DataFrame(ks, columns=["time","open","high","low","close","vol"]).apply(pd.to_numeric)
    df = df.sort_values("time").reset_index(drop=True)

    close, high, low = df["close"], df["high"], df["low"]

    e9  = close.ewm(span=CFG.EMA_FAST, adjust=False).mean()
    e21 = close.ewm(span=CFG.EMA_MID, adjust=False).mean()
    e50 = close.ewm(span=CFG.EMA_SLOW, adjust=False).mean()

    ma_period = min(CFG.MA_TREND, len(df))
    ma200 = close.rolling(ma_period).mean()

    idx = len(df) - 1
    price = close.iloc[idx]

    if pd.isna(ma200.iloc[idx]):
        return None

    efeito = None

    if e9.iloc[idx] < e21.iloc[idx] < e50.iloc[idx]:
        efeito = f"INÍCIO CACHOEIRA ({TF_LABELS.get(tf, tf)})"
    else:
        dist = abs(high.iloc[idx] - e21.iloc[idx]) / e21.iloc[idx] * 100
        if dist < 0.3 and price < e9.iloc[idx]:
            efeito = f"BEIJO DA MORTE ({TF_LABELS.get(tf, tf)})"

    if not efeito:
        return None

    # Candle Pilha
    pilha = "---"
    o, h, l, c = df.iloc[idx][["open","high","low","close"]]
    if (h - l) > 0 and (o > c):
        if ((o - c)/(h - l) >= 0.8) and ((c - l)/(h - l) <= 0.15):
            pilha = f"🔋 PILHA ({TF_LABELS.get(tf, tf)})"

    min_recent = low.iloc[max(0, idx-30):idx].min()
    perigo = "SUPORTE PERIGOSO" if ((price - min_recent)/min_recent)*100 <= CFG.SUPORTE_BUFFER else "SUPORTE FRACO"

    return {
        "efeito": efeito,
        "perigo": perigo,
        "pilha": pilha
    }

# ================= ANALISAR PAR ================= #

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
            "par": sym.replace("-USDT", ""),
            "sinal": res["efeito"],
            "tf": TF_LABELS.get(tf, tf),
            "suporte": res["perigo"],
            "pilha": res["pilha"],
            "variacao": f"{float(c_map.get(sym,0)):.2f}%",
            "binance": binance_url(sym)
        })
            

    return results

# ================= ENDPOINT ================= #

@router.get("/scan")
async def scan(user: User = Depends(current_active_user)):

    ticker = get_json(f"{BASE_URL}/openApi/swap/v2/quote/ticker").get("data", [])
    if not ticker:
        return {"total": 0, "resultados": []}

    c_map = {x["symbol"]: x.get("priceChangePercent", 0) for x in ticker}
    syms = [x["symbol"] for x in ticker if x["symbol"].endswith("-USDT")][:CFG.TOP_BY_VOLUME]

    results = []

    with ThreadPoolExecutor(max_workers=CFG.WORKERS) as ex:
        futures = [ex.submit(analyze_symbol, s, c_map) for s in syms]
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.extend(r)

    return {"total": len(results), "resultados": results}