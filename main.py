from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import requests, pandas as pd
import warnings
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter

warnings.filterwarnings("ignore")

app = FastAPI(title="Racker Ultra PRO Turbo", version="17.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
    SCAN_TFS: dict = field(default_factory=lambda: {
        "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400
    })
    TOP_BY_VOLUME: int = 250
    WORKERS: int = 60
    SUPORTE_BUFFER: float = 1.2

CFG = Config()
BASE_URL = "https://open-api.bingx.com"

SESSION = requests.Session()
adapter = HTTPAdapter(pool_connections=CFG.WORKERS, pool_maxsize=CFG.WORKERS)
SESSION.mount("https://", adapter)

# ================= CORE ================= #
def get_json(url, params=None):
    try:
        r = SESSION.get(url, params=params, timeout=5)
        return r.json() if r.status_code == 200 else {}
    except:
        return {}

def fetch_klines(symbol, interval):
    params = {"symbol": symbol, "interval": interval, "limit": "250"}
    data = get_json(f"{BASE_URL}/openApi/swap/v3/quote/klines", params)
    return data.get("data", [])

def binance_url(symbol):
    return f"https://www.binance.com/pt/futures/{symbol.replace('-', '')}"

def analyze_logic(ks, tf):
    if not ks or len(ks) < CFG.MA_TREND:
        return None

    df = pd.DataFrame(ks, columns=["time","open","high","low","close","vol"]).apply(pd.to_numeric)
    df = df.sort_values("time").reset_index(drop=True)

    close, high, low = df["close"], df["high"], df["low"]
    e9  = close.ewm(span=CFG.EMA_FAST, adjust=False).mean()
    e21 = close.ewm(span=CFG.EMA_MID, adjust=False).mean()
    e50 = close.ewm(span=CFG.EMA_SLOW, adjust=False).mean()
    ma200 = close.rolling(CFG.MA_TREND).mean()

    idx = len(df) - 1
    price = close.iloc[idx]

    if price > ma200.iloc[idx]:
        return None

    efeito = None

    if e9.iloc[idx] < e21.iloc[idx] < e50.iloc[idx]:
        efeito = f"INÍCIO CACHOEIRA ({tf})" if not (e9.iloc[idx-5] < e21.iloc[idx-5] < e50.iloc[idx-5]) else f"FLUXO CACHOEIRA ({tf})"
    else:
        dist = abs(high.iloc[idx] - e21.iloc[idx]) / e21.iloc[idx] * 100
        if dist < 0.3 and price < e9.iloc[idx]:
            efeito = f"BEIJO DA MORTE ({tf})"
        elif abs(e9.iloc[idx] - e21.iloc[idx]) < abs(e9.iloc[idx-1] - e21.iloc[idx-1]):
            efeito = f"ANTECIPAÇÃO ({tf})"

    if not efeito:
        return None

    pilha = "---"
    o, h, l, c = df.iloc[idx][["open","high","low","close"]]
    if (o > c) and ((o-c)/(h-l) >= 0.8) and ((c-l)/(h-l) <= 0.15):
        pilha = f"🔋 PILHA ({tf})"

    min_recent = low.iloc[max(0, idx-30):idx].min()
    perigo = "SUPORTE PERIGOSO" if ((price - min_recent)/min_recent)*100 <= CFG.SUPORTE_BUFFER else "SUPORTE FRACO"

    return {"efeito": efeito, "pilha": pilha, "perigo": perigo}

def analyze_symbol(sym, c_map):
    results = []
    for tf in CFG.SCAN_TFS.keys():
        res = analyze_logic(fetch_klines(sym, tf), tf)
        if res:
            results.append({
                "par": sym.replace("-USDT",""),
                "sinal": res["efeito"],
                "tf": tf,
                "suporte": res["perigo"],
                "pilha": res["pilha"],
                "variacao": f"{float(c_map.get(sym,0)):.2f}%",
                "binance": binance_url(sym)
            })
    return results

# ================= ENDPOINT ================= #
@app.get("/scan")
def scan():
    ticker = get_json(f"{BASE_URL}/openApi/swap/v2/quote/ticker").get("data", [])
    if not ticker:
        return {"total": 0, "resultados": []}

    c_map = {x["symbol"]: x.get("priceChangePercent", 0) for x in ticker}
    syms = [x["symbol"] for x in ticker if x["symbol"].endswith("-USDT")][:CFG.TOP_BY_VOLUME]

    results = []
    with ThreadPoolExecutor(max_workers=CFG.WORKERS) as ex:
        for f in as_completed([ex.submit(analyze_symbol, s, c_map) for s in syms]):
            r = f.result()
            if r:
                results.extend(r)

    return {"total": len(results), "resultados": results}
