from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI
import requests, pandas as pd, numpy as np
import warnings
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter

warnings.filterwarnings("ignore")

app = FastAPI(title="Racker Ultra API", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # depois você pode restringir
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ================= CONFIG ================= #
@dataclass
class Config:
    EMA_FAST: int = 9
    EMA_MID: int  = 21
    EMA_SLOW: int = 50
    MA_TREND: int = 200
    SCAN_TFS: tuple = ("5m",)   # 🔒 CONFIRMADO
    TOP_BY_VOLUME: int = 250
    WORKERS: int = 60
    SUPORTE_BUFFER: float = 1.0

CFG = Config()
BASE_URL = "https://open-api.bingx.com"

SESSION = requests.Session()
adapter = HTTPAdapter(
    pool_connections=CFG.WORKERS,
    pool_maxsize=CFG.WORKERS
)
SESSION.mount("https://", adapter)

# ================= CORE ================= #
def get_json(url, params=None):
    try:
        r = SESSION.get(url, params=params, timeout=5)
        r.raise_for_status()
        return r.json()
    except:
        return {}

def fetch_klines(symbol, interval):
    params = {"symbol": symbol, "interval": interval, "limit": "210"}
    data = get_json(f"{BASE_URL}/openApi/swap/v3/quote/klines", params)
    return data.get("data", [])

def analyze_logic(ks, tf_name):
    if not ks or len(ks) < CFG.MA_TREND:
        return None

    df = pd.DataFrame(
        ks,
        columns=["time", "open", "high", "low", "close", "vol"]
    )
    df[["open","high","low","close"]] = df[["open","high","low","close"]].apply(pd.to_numeric)
    df = df.iloc[::-1].reset_index(drop=True)

    close = df["close"]
    e9  = close.ewm(span=CFG.EMA_FAST, adjust=False).mean()
    e21 = close.ewm(span=CFG.EMA_MID, adjust=False).mean()
    e50 = close.ewm(span=CFG.EMA_SLOW, adjust=False).mean()
    ma200 = close.rolling(CFG.MA_TREND).mean()

    idx = len(df) - 1
    c_price = close.iloc[idx]

    if c_price > ma200.iloc[idx]:
        return None

    efeito = None
    if df["high"].iloc[idx] >= e21.iloc[idx] and c_price < e9.iloc[idx]:
        efeito = f"BEIJO DA MORTE ({tf_name})"
    elif e9.iloc[idx] < e21.iloc[idx] < e50.iloc[idx]:
        efeito = f"CACHOEIRA ({tf_name})"
    elif abs(e9.iloc[idx] - e21.iloc[idx]) < abs(e9.iloc[idx-1] - e21.iloc[idx-1]):
        efeito = f"ANTECIPAÇÃO ({tf_name})"

    if not efeito:
        return None

    # 🔋 PILHA
    p = df.iloc[idx-1]
    body = abs(p["open"] - p["close"])
    rng = max(p["high"] - p["low"], 1e-9)
    pilha = f"🔋 PILHA ({tf_name})" if body / rng >= 0.85 and p["close"] < p["open"] else "Não Encontrado"

    # ⚠️ SUPORTE
    min_recent = df["low"].iloc[-30:].min()
    dist_sup = ((c_price - min_recent) / min_recent) * 100
    perigo = "⚠️ EXISTE PERIGO" if dist_sup <= CFG.SUPORTE_BUFFER else "NÃO EXISTE"

    return {
        "efeito": efeito,
        "pilha": pilha,
        "perigo": perigo
    }

def analyze_symbol(sym, c_map):
    for tf in CFG.SCAN_TFS:
        res = analyze_logic(fetch_klines(sym, tf), tf)
        if res:
            return {
                "par": sym.replace("-USDT", ""),
                "efeito": res["efeito"],
                "tf": tf,
                "perigo": res["perigo"],
                "pilha": res["pilha"],
                "variacao": f"{float(c_map.get(sym, 0)):.2f}%"
            }
    return None

# ================= ENDPOINT ================= #
@app.get("/scan")
def scan():
    ticker_data = get_json(f"{BASE_URL}/openApi/swap/v2/quote/ticker").get("data", [])
    if not ticker_data:
        return {"total": 0, "resultados": []}

    c_map = {x["symbol"]: x.get("priceChangePercent", 0) for x in ticker_data}
    syms = [x["symbol"] for x in ticker_data if x["symbol"].endswith("-USDT")][:CFG.TOP_BY_VOLUME]

    results = []

    with ThreadPoolExecutor(max_workers=CFG.WORKERS) as executor:
        futures = [executor.submit(analyze_symbol, s, c_map) for s in syms]
        for f in as_completed(futures):
            r = f.result()
            if r:
                results.append(r)

    return {
        "total": len(results),
        "resultados": results
    }
