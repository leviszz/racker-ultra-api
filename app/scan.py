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
from statistics import mean

# ================= DB ================= #

async def get_async_db():
    async with AsyncSessionLocal() as session:
        yield session


warnings.filterwarnings("ignore", category=FutureWarning)

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
        "5m","15m","30m","1h","4h"
    ])

    # ── Candle Pilha ──────────────────────────────────────────
    # Tamanho da janela de referência para avg_body_pct
    # (igual ao scanner do chefe: 25 velas, excluindo as 6 mais recentes)
    LOOKBACK: int = 25
    LOOKBACK_SKIP: int = 6

    # Thresholds de pilha (idênticos ao scanner do chefe)
    MIN_BODY_VS_RANGE: float = 0.86
    MAX_WICK_EACH:     float = 0.08
    MAX_WICKS_TOTAL:   float = 0.14
    MIN_WICK_SYM:      float = 0.55
    MAX_OPEN_FROM_HIGH: float = 0.18
    MAX_CLOSE_FROM_LOW: float = 0.18
    MIN_BODY_PCT_ABS:  float = 0.35
    BODY_VS_AVG_MULT:  float = 1.45
    PILHA_THRESHOLD:   float = 80.0

CFG = Config()

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
    monthly["time"] = monthly["time"].astype(int)//10**6
    return monthly.values.tolist()

def build_yearly_from_weekly(symbol):
    data = fetch_klines(symbol,"1w",1000)
    if not data:
        return []
    df = pd.DataFrame(data,columns=["time","open","high","low","close","vol"]).apply(pd.to_numeric)
    df["time"] = pd.to_datetime(df["time"],unit="ms")
    df = df.set_index("time")
    yearly = df.resample("YE").agg({"open":"first","high":"max","low":"min","close":"last","vol":"sum"}).dropna()
    yearly = yearly.reset_index()
    yearly["time"] = yearly["time"].astype(int)//10**6
    return yearly.values.tolist()

# ================= CANDLE PILHA ================= #

def pile_metrics(o, h, l, cl):
    rng = max(h - l, 1e-12)
    body = abs(o - cl)
    upper = h - max(o, cl)
    lower = min(o, cl) - l

    body_vs_range   = body / rng
    w_up            = upper / rng
    w_dn            = lower / rng
    w_total         = (upper + lower) / rng
    w_sym           = min(w_up, w_dn) / max(max(w_up, w_dn), 1e-12)
    open_from_high  = (h - o) / rng
    close_from_low  = (cl - l) / rng
    body_pct        = body / max(o, 1e-12) * 100
    dump_pct        = (cl - o) / max(o, 1e-12) * 100

    return body_vs_range, w_up, w_dn, w_total, w_sym, open_from_high, close_from_low, body_pct, dump_pct


def passes_rules(body_vs_range, w_up, w_dn, w_total, w_sym, open_from_high, close_from_low, body_pct):
    
    # Se não há sombras, simetria não se aplica — candle é perfeito
    sym_ok = (w_total < 1e-9) or (w_sym >= CFG.MIN_WICK_SYM)

    return (
        body_vs_range  >= CFG.MIN_BODY_VS_RANGE
        and max(w_up, w_dn) <= CFG.MAX_WICK_EACH
        and w_total        <= CFG.MAX_WICKS_TOTAL
        and sym_ok                                  # ← substituiu w_sym >= MIN_WICK_SYM
        and open_from_high <= CFG.MAX_OPEN_FROM_HIGH
        and close_from_low <= CFG.MAX_CLOSE_FROM_LOW
        and body_pct       >= CFG.MIN_BODY_PCT_ABS
    )


def score_pilha(body_vs_range, w_up, w_dn, w_sym, open_from_high, close_from_low, body_pct, avg_body_pct):
    c_body = max(0.0, min(1.0, (body_vs_range - 0.78) / (0.96 - 0.78)))
    c_wick = max(0.0, min(1.0, (0.10 - max(w_up, w_dn)) / 0.10))
    c_sym  = max(0.0, min(1.0, (w_sym - 0.45) / (0.92 - 0.45)))
    c_ends = (
        max(0.0, min(1.0, (CFG.MAX_OPEN_FROM_HIGH - open_from_high) / CFG.MAX_OPEN_FROM_HIGH))
        * max(0.0, min(1.0, (CFG.MAX_CLOSE_FROM_LOW - close_from_low) / CFG.MAX_CLOSE_FROM_LOW))
    )
    target = max(
        CFG.MIN_BODY_PCT_ABS,
        CFG.BODY_VS_AVG_MULT * avg_body_pct if avg_body_pct else CFG.MIN_BODY_PCT_ABS
    )
    c_size = max(0.0, min(1.0, (body_pct - target) / (target + 1.0)))

    return (0.44*c_body + 0.30*c_wick + 0.14*c_sym + 0.08*c_ends + 0.04*c_size) * 100


def detect_pilha(df: pd.DataFrame) -> str:
    n = len(df)
    min_rows = CFG.LOOKBACK + CFG.LOOKBACK_SKIP + 4
    if n < min_rows:
        return "---"

    ref = df.iloc[-(CFG.LOOKBACK + CFG.LOOKBACK_SKIP) : -CFG.LOOKBACK_SKIP]
    body_pcts = [
        abs(row["open"] - row["close"]) / max(row["open"], 1e-12) * 100
        for _, row in ref.iterrows()
    ]
    avg_body_pct = mean(body_pcts) if body_pcts else 0.0

    candidates = [df.iloc[-2], df.iloc[-3]]
    best_score = -1.0
    best_tag   = "---"

    for i, candle in enumerate(candidates):
        o  = float(candle["open"])
        h  = float(candle["high"])
        l  = float(candle["low"])
        cl = float(candle["close"])

        if cl >= o:
            continue

        metrics = pile_metrics(o, h, l, cl)
        (body_vs_range, w_up, w_dn, w_total,
         w_sym, open_from_high, close_from_low,
         body_pct, _) = metrics

        sc = score_pilha(
            body_vs_range, w_up, w_dn, w_sym,
            open_from_high, close_from_low,
            body_pct, avg_body_pct
        )

        # 👇 Substitui o print antigo por este
        if sc >= 70:
            print(f"  [CANDIDATO] score={sc:.1f}")
            print(f"    body_vs_range={body_vs_range:.3f} (min {CFG.MIN_BODY_VS_RANGE}) {'✅' if body_vs_range >= CFG.MIN_BODY_VS_RANGE else '❌'}")
            print(f"    max_wick     ={max(w_up,w_dn):.3f} (max {CFG.MAX_WICK_EACH})  {'✅' if max(w_up,w_dn) <= CFG.MAX_WICK_EACH else '❌'}")
            print(f"    w_total      ={w_total:.3f} (max {CFG.MAX_WICKS_TOTAL}) {'✅' if w_total <= CFG.MAX_WICKS_TOTAL else '❌'}")
            print(f"    w_sym        ={w_sym:.3f} (min {CFG.MIN_WICK_SYM})  {'✅' if w_sym >= CFG.MIN_WICK_SYM else '❌'}")
            print(f"    open_from_hi ={open_from_high:.3f} (max {CFG.MAX_OPEN_FROM_HIGH}) {'✅' if open_from_high <= CFG.MAX_OPEN_FROM_HIGH else '❌'}")
            print(f"    close_from_lo={close_from_low:.3f} (max {CFG.MAX_CLOSE_FROM_LOW}) {'✅' if close_from_low <= CFG.MAX_CLOSE_FROM_LOW else '❌'}")
            print(f"    body_pct     ={body_pct:.3f} (min {CFG.MIN_BODY_PCT_ABS}) {'✅' if body_pct >= CFG.MIN_BODY_PCT_ABS else '❌'}")

        ok = passes_rules(
            body_vs_range, w_up, w_dn, w_total,
            w_sym, open_from_high, close_from_low,
            body_pct
        )

        if ok and sc >= CFG.PILHA_THRESHOLD and sc > best_score:
            best_score = sc
            best_tag   = "🔋 PILHA"

    return best_tag

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

    e9   = close.ewm(span=CFG.EMA_FAST, adjust=False).mean()
    e21  = close.ewm(span=CFG.EMA_MID,  adjust=False).mean()
    e50  = close.ewm(span=CFG.EMA_SLOW, adjust=False).mean()
    ma200 = close.rolling(CFG.MA_TREND).mean()

    # ── Índice do último candle fechado (não o candle em formação) ──
    idx = len(df) - 2

    price = close.iloc[idx]

    if pd.isna(ma200.iloc[idx]):
        return None

    # Filtro de tendência: só SHORT (abaixo da MA200)
    if price > ma200.iloc[idx]:
        return None

    efeito = None

    # ── Cachoeira / Beijo da morte / Antecipação ──
    if idx >= 5 and e9.iloc[idx] < e21.iloc[idx] < e50.iloc[idx]:
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

    # ── Candle Pilha (lógica do chefe) ──
    pilha = detect_pilha(df)

    if not efeito and pilha == "---":
        return None

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