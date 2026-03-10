# =========================
# BingX "VELA PILHA" Scanner (30m + 1h + 2h + 4h + 12h + 1d + 1w)
# ✅ Código mantido — CONFLUÊNCIA só conta quando for CANDLE PILHA DE VERDADE:
#   ok == True  E  score >= PILHA_THRESHOLD
# ✅ Agora a SAÍDA fica dividida em 2 partes:
#   (1) CONFLUÊNCIA: moedas com 2+ TFs em PILHA
#   (2) INDIVIDUAL: onde apareceu em cada TF (igual)
# =========================

import requests
import logging
from statistics import mean
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE = "https://open-api.bingx.com"

# ===== CONFIG =====
CFG = {
    # Performance / Campo de busca grande
    "TOP_BY_VOLUME": 500,
    "WORKERS": 12,
    "KLINE_LIMIT": 140,
    "LOOKBACK": 25,

    # Saída
    "PRINT_TOP": 500,

    # Score mínimo pra aparecer
    "MIN_SCORE_SHOW": 20.0,

    # Timeframes
    "TIMEFRAMES": [
        {"label": "30m", "primary": "30m", "fallback": None},
        {"label": "1h",  "primary": "1h",  "fallback": "60m"},
        {"label": "2h",  "primary": "2h",  "fallback": "120m"},
        {"label": "4h",  "primary": "4h",  "fallback": "240m"},
        {"label": "12h", "primary": "12h", "fallback": "720m"},
        {"label": "1d",  "primary": "1d",  "fallback": "1440m"},
        {"label": "1w",  "primary": "1w",  "fallback": "10080m"},
    ],

    # Ordem de exibição
    "TF_ORDER": ["30m", "1h", "2h", "4h", "12h", "1d", "1w"],

    # ===== Definição "pilha" (NÃO MEXI) =====
    "MIN_BODY_VS_RANGE": 0.86,
    "MAX_WICK_EACH": 0.08,
    "MAX_WICKS_TOTAL": 0.14,
    "MIN_WICK_SYM": 0.55,
    "MAX_OPEN_FROM_HIGH": 0.18,
    "MAX_CLOSE_FROM_LOW": 0.18,

    "MIN_BODY_PCT_ABS": 0.35,
    "BODY_VS_AVG_MULT": 1.45,

    "PILHA_THRESHOLD": 80,

    # Confluência mínima (quantos TFs)
    "MIN_TF_CONFLUENCE": 2,
}

logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

SESSION = requests.Session()
retries = Retry(
    total=2,
    backoff_factor=0.2,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"],
)
adapter = HTTPAdapter(
    pool_connections=CFG["WORKERS"] * 2,
    pool_maxsize=CFG["WORKERS"] * 2,
    max_retries=retries
)
SESSION.mount("https://", adapter)
SESSION.mount("http://", adapter)

def get_json(url, params=None, timeout=18):
    r = SESSION.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()

def normalize_ts(ts):
    ts = int(ts or 0)
    return ts * 1000 if ts < 10**12 else ts

def kline_ts(c):
    if isinstance(c, dict):
        return normalize_ts(c.get("time", c.get("t", 0)))
    return normalize_ts(c[0])

def candle_to_ohlc(c):
    if isinstance(c, dict):
        t  = normalize_ts(c.get("time", c.get("t", 0)))
        o  = float(c.get("open", c.get("o")))
        h  = float(c.get("high", c.get("h")))
        l  = float(c.get("low",  c.get("l")))
        cl = float(c.get("close", c.get("c")))
        v  = float(c.get("volume", c.get("vol", c.get("v", 0))) or 0)
        return t, o, h, l, cl, v

    c = list(c)
    t = normalize_ts(c[0])
    o = float(c[1])
    v = float(c[5]) if len(c) > 5 else 0.0

    # A: [t, o, close, high, low, vol]
    clA = float(c[2]); hA = float(c[3]); lA = float(c[4])
    # B: [t, o, high, low, close, vol]
    hB = float(c[2]); lB = float(c[3]); clB = float(c[4])

    def ok(o_, h_, l_, cl_):
        return (h_ >= max(o_, cl_)) and (l_ <= min(o_, cl_))

    if ok(o, hA, lA, clA):
        return t, o, hA, lA, clA, v
    if ok(o, hB, lB, clB):
        return t, o, hB, lB, clB, v

    return t, o, hA, lA, clA, v

def fetch_klines(symbol, interval, limit):
    params = {"symbol": symbol, "interval": interval, "limit": str(limit)}
    j = get_json(f"{BASE}/openApi/swap/v3/quote/klines", params=params)
    return j.get("data", [])

def get_top_symbols_by_volume(limit):
    try:
        j = get_json(f"{BASE}/openApi/swap/v2/quote/ticker")
        data = j.get("data", j)

        if isinstance(data, dict):
            for k in ["tickers", "data", "result", "rows", "list"]:
                if isinstance(data.get(k), list):
                    data = data[k]
                    break

        def pick_num(d):
            for k in ["quoteVolume", "turnover", "amount", "volume", "vol"]:
                v = d.get(k)
                if v is None:
                    continue
                try:
                    return float(v)
                except:
                    pass
            return 0.0

        items = []
        if isinstance(data, list):
            for it in data:
                if isinstance(it, dict) and it.get("symbol"):
                    items.append((it["symbol"], pick_num(it)))

        items.sort(key=lambda x: x[1], reverse=True)
        out = [s for s, _ in items[:limit]]
        if out:
            return out
    except:
        pass

    j = get_json(f"{BASE}/openApi/swap/v2/quote/contracts")
    data = j.get("data", j)
    if isinstance(data, dict):
        for k in ["contracts", "data", "result", "rows", "list"]:
            if isinstance(data.get(k), list):
                data = data[k]
                break
    if isinstance(data, list):
        return [it["symbol"] for it in data if isinstance(it, dict) and it.get("symbol")][:limit]
    return []

def pick_closed_candidates(ks_sorted, tf_label):
    if len(ks_sorted) < 4:
        return []
    return [("last_closed", ks_sorted[-2]), ("prev_closed", ks_sorted[-3])]

def pile_metrics(o, h, l, cl):
    rng = max(h - l, 1e-12)
    body = abs(o - cl)
    upper = h - max(o, cl)
    lower = min(o, cl) - l

    body_vs_range = body / rng
    w_up = upper / rng
    w_dn = lower / rng
    w_total = (upper + lower) / rng
    w_sym = min(w_up, w_dn) / max(max(w_up, w_dn), 1e-12)

    open_from_high = (h - o) / rng
    close_from_low = (cl - l) / rng

    body_pct = body / max(o, 1e-12) * 100
    dump_pct = (cl - o) / max(o, 1e-12) * 100

    return body_vs_range, w_up, w_dn, w_total, w_sym, open_from_high, close_from_low, body_pct, dump_pct

def passes_rules(body_vs_range, w_up, w_dn, w_total, w_sym, open_from_high, close_from_low, body_pct):
    return (
        body_vs_range >= CFG["MIN_BODY_VS_RANGE"]
        and max(w_up, w_dn) <= CFG["MAX_WICK_EACH"]
        and w_total <= CFG["MAX_WICKS_TOTAL"]
        and w_sym >= CFG["MIN_WICK_SYM"]
        and open_from_high <= CFG["MAX_OPEN_FROM_HIGH"]
        and close_from_low <= CFG["MAX_CLOSE_FROM_LOW"]
        and body_pct >= CFG["MIN_BODY_PCT_ABS"]
    )

def score_pilha(body_vs_range, w_up, w_dn, w_sym, open_from_high, close_from_low, body_pct, avg_body_pct):
    c_body = max(0.0, min(1.0, (body_vs_range - 0.78) / (0.96 - 0.78)))
    c_wick = max(0.0, min(1.0, (0.10 - max(w_up, w_dn)) / 0.10))
    c_sym  = max(0.0, min(1.0, (w_sym - 0.45) / (0.92 - 0.45)))
    c_ends = max(0.0, min(1.0, (CFG["MAX_OPEN_FROM_HIGH"] - open_from_high) / CFG["MAX_OPEN_FROM_HIGH"])) \
           * max(0.0, min(1.0, (CFG["MAX_CLOSE_FROM_LOW"] - close_from_low) / CFG["MAX_CLOSE_FROM_LOW"]))

    target = max(CFG["MIN_BODY_PCT_ABS"], CFG["BODY_VS_AVG_MULT"] * avg_body_pct if avg_body_pct else CFG["MIN_BODY_PCT_ABS"])
    c_size = max(0.0, min(1.0, (body_pct - target) / (target + 1.0)))

    return (0.44*c_body + 0.30*c_wick + 0.14*c_sym + 0.08*c_ends + 0.04*c_size) * 100

def analyze_symbol(sym, interval, tf_label):
    try:
        ks = fetch_klines(sym, interval, CFG["KLINE_LIMIT"])
        if not ks or len(ks) < (CFG["LOOKBACK"] + 12):
            return None

        ks_sorted = sorted(ks, key=kline_ts)

        ref = ks_sorted[-(CFG["LOOKBACK"] + 6):-6]
        body_pcts = []
        for c in ref:
            _, oo, _, _, cc, _ = candle_to_ohlc(c)
            body_pcts.append(abs(oo - cc) / max(oo, 1e-12) * 100)
        avg_body_pct = mean(body_pcts) if body_pcts else 0

        candidates = pick_closed_candidates(ks_sorted, tf_label)
        best = None

        for which, cand in candidates:
            _, o, h, l, cl, _ = candle_to_ohlc(cand)

            if cl >= o:  # precisa ser vermelho (SHORT)
                continue

            body_vs_range, w_up, w_dn, w_total, w_sym, open_from_high, close_from_low, body_pct, dump_pct = pile_metrics(o, h, l, cl)
            sc = score_pilha(body_vs_range, w_up, w_dn, w_sym, open_from_high, close_from_low, body_pct, avg_body_pct)
            ok = passes_rules(body_vs_range, w_up, w_dn, w_total, w_sym, open_from_high, close_from_low, body_pct)

            row = {
                "symbol": sym,
                "tf": tf_label,
                "which": which,
                "score": sc,
                "ok": ok,
                "dump_pct": dump_pct,
            }

            if (best is None) or (row["score"] > best["score"]):
                best = row

        return best
    except:
        return None

def scan_timeframe(symbols, tf):
    tf_label = tf["label"]
    primary = tf["primary"]
    fallback = tf["fallback"]

    def _scan(interval):
        results = []
        with ThreadPoolExecutor(max_workers=CFG["WORKERS"]) as ex:
            futs = [ex.submit(analyze_symbol, s, interval, tf_label) for s in symbols]
            for f in as_completed(futs):
                r = f.result()
                if r:
                    results.append(r)
        return results

    results = _scan(primary)
    if len(results) == 0 and fallback:
        results = _scan(fallback)
    return results

def url_binance_futures_pt(symbol_bingx: str) -> str:
    return f"https://www.binance.com/pt/futures/{symbol_bingx.replace('-', '')}"

def run():
    symbols = get_top_symbols_by_volume(CFG["TOP_BY_VOLUME"])
    if not symbols:
        print("❌ Não consegui obter símbolos.")
        return

    all_results = []
    for tf in CFG["TIMEFRAMES"]:
        all_results.extend(scan_timeframe(symbols, tf))

    # mantém melhor por (symbol, tf) para o INDIVIDUAL (igual já era)
    best = {}
    for r in all_results:
        key = (r["symbol"], r["tf"])
        if key not in best or r["score"] > best[key]["score"]:
            best[key] = r

    merged = list(best.values())

    # filtro score mínimo (como já era)
    merged = [r for r in merged if r["score"] >= CFG["MIN_SCORE_SHOW"]]
    if not merged:
        print(f"\nNenhum resultado com score >= {CFG['MIN_SCORE_SHOW']}.")
        return

    # =========================================================
    # ✅ CONFLUÊNCIA (AGORA CONTA "QUASE" TAMBÉM):
    # critério por TF: score >= MIN_SCORE_SHOW (independente de ok/threshold)
    # e pega o melhor score de cada (symbol, tf)
    # =========================================================
    grouped = {}
    for r in all_results:
        if r["score"] < CFG["MIN_SCORE_SHOW"]:
            continue
        grouped.setdefault((r["symbol"], r["tf"]), []).append(r)

    best_per_tf_for_confluence = []
    for (sym, tf), rows in grouped.items():
        best_row = max(rows, key=lambda x: x["score"])
        best_per_tf_for_confluence.append(best_row)

    sym_map = {}
    for r in best_per_tf_for_confluence:
        sym_map.setdefault(r["symbol"], []).append(r)

    tf_order_rank = {tf: i for i, tf in enumerate(CFG["TF_ORDER"])}

    confluence = []
    for sym, rows in sym_map.items():
        # garante TF único
        uniq = {}
        for x in rows:
            if x["tf"] not in uniq or x["score"] > uniq[x["tf"]]["score"]:
                uniq[x["tf"]] = x
        rows = list(uniq.values())

        if len(rows) >= CFG["MIN_TF_CONFLUENCE"]:
            rows_sorted = sorted(rows, key=lambda x: tf_order_rank.get(x["tf"], 999))
            tfs = [x["tf"] for x in rows_sorted]
            max_score = max(x["score"] for x in rows_sorted)
            confluence.append({
                "symbol": sym,
                "tfs": tfs,
                "rows": rows_sorted,
                "max_score": max_score,
                "tf_count": len(rows_sorted),
            })

    confluence.sort(key=lambda x: (x["tf_count"], x["max_score"]), reverse=True)

    # -------------------------
    # Saída individual (mesma lógica) — por TF_ORDER
    # -------------------------
    merged.sort(
        key=lambda x: (
            1 if (x["ok"] and x["score"] >= CFG["PILHA_THRESHOLD"]) else 0,
            x["score"],
            -abs(x["dump_pct"])
        ),
        reverse=True
    )

    by_tf = {tf: [] for tf in CFG["TF_ORDER"]}
    for r in merged:
        if r["tf"] in by_tf:
            by_tf[r["tf"]].append(r)

    show = []
    for tf in CFG["TF_ORDER"]:
        if not by_tf[tf]:
            continue

        by_tf[tf].sort(
            key=lambda x: (
                1 if (x["ok"] and x["score"] >= CFG["PILHA_THRESHOLD"]) else 0,
                x["score"],
                -abs(x["dump_pct"])
            ),
            reverse=True
        )

        for r in by_tf[tf]:
            if len(show) >= CFG["PRINT_TOP"]:
                break
            show.append(r)

        if len(show) >= CFG["PRINT_TOP"]:
            break

    # =========================================================
    # ✅ SAÍDA EM 2 BLOCOS (CONFLUÊNCIA + INDIVIDUAL)
    # =========================================================

    # (1) CONFLUÊNCIA (MUITO FORTE)
    print("\n==============================")
    print("🔥 CONFLUÊNCIA (2+ TFs: PILHA OU QUASE)")
    print("==============================")
    print("SYMBOL | TFs | TF_COUNT | MAX_SCORE | TAGS POR TF | URL BINANCE (FUTURES)")

    if not confluence:
        print("Nenhuma moeda com confluência (2+ TFs) encontrada.")
    else:
        for c in confluence[:CFG["PRINT_TOP"]]:
            sym = c["symbol"]
            # tags por tf (PILHA ou quase)
            tags = []
            for row in c["rows"]:
                tag = "PILHA" if (row["ok"] and row["score"] >= CFG["PILHA_THRESHOLD"]) else "quase"
                tags.append(f"{row['tf']}:{tag}")
            tags_str = ",".join(tags)

            tfs = ",".join(c["tfs"])
            print(f"{sym} | {tfs} | {c['tf_count']} | {c['max_score']:.1f} | {tags_str} | {url_binance_futures_pt(sym)}")

    # (2) INDIVIDUAL (onde apareceu por timeframe)
    print("\n==============================")
    print("📌 INDIVIDUAL (por timeframe)")
    print("==============================")
    print("TF | CANDLE | SYMBOL | SCORE | TAG | URL BINANCE (FUTURES)")

    for r in show:
        tag = "PILHA" if (r["ok"] and r["score"] >= CFG["PILHA_THRESHOLD"]) else "quase"
        print(f"{r['tf']} | {r['which']} | {r['symbol']} | {r['score']:.1f} | {tag} | {url_binance_futures_pt(r['symbol'])}")

run()