#!/usr/bin/env python3
"""
Meme coin scanner + wallet tracker (alert-only, does NOT trade).

Each cycle it:
  1. Pulls fresh/boosted tokens from DexScreener (free, no API key)
  2. Filters by liquidity, volume, age, FDV (preset: safe / balanced / degen)
  3. Runs safety checks (GoPlus for EVM chains, RugCheck for Solana)
  4. Scores what's left (0-100), prints the top picks, sends Telegram alerts
  5. Watches wallets you choose and alerts when they pick up a new token

Settings come from environment variables (nothing secret is stored in this file):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   Telegram alerts
  SCAN_PRESET     safe | balanced | degen     (default: balanced)
  SCAN_CHAINS     e.g. "solana,bsc,base"      (default: solana,bsc,base)
  WALLETS         wallets to track, one per line or separated by ";" :
                      solana,<wallet address>,label
                      bsc,0x...,label
                  PUBLIC ADDRESSES ONLY. Never put a seed phrase or private key anywhere.
  SOLANA_RPC, BSC_RPC, BASE_RPC, ETHEREUM_RPC   optional custom RPC URLs

Run:
  python3 memecoin_scanner.py --demo           # fake data, no internet needed
  python3 memecoin_scanner.py --test-telegram  # sends a test message to your Telegram
  python3 memecoin_scanner.py --once           # one live scan, then exit
  python3 memecoin_scanner.py                  # keeps scanning until you stop it

A high score means "passes basic filters and shows momentum", NOT "will go up".
Always check the chart, holders and contract yourself before buying.
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

BASE_CONFIG = {
    "chains": ["solana", "bsc", "base"],  # also: ethereum, arbitrum, polygon
    "scan_interval_sec": 300,
    "top_n": 10,
    "log_file": "scanner_log.csv",
    "state_file": "state.json",
    "wallet_min_liquidity_usd": 5_000,
}

# "degen" catches earlier launches but with a much higher rug risk.
PRESETS = {
    "safe": {"min_liquidity_usd": 50_000, "min_volume_24h_usd": 100_000, "min_age_hours": 6,
             "max_age_hours": 72, "max_fdv_usd": 10_000_000, "min_score_for_alert": 70},
    "balanced": {"min_liquidity_usd": 20_000, "min_volume_24h_usd": 50_000, "min_age_hours": 2,
                 "max_age_hours": 72, "max_fdv_usd": 20_000_000, "min_score_for_alert": 60},
    "degen": {"min_liquidity_usd": 10_000, "min_volume_24h_usd": 20_000, "min_age_hours": 0.5,
              "max_age_hours": 48, "max_fdv_usd": 30_000_000, "min_score_for_alert": 55},
}

DEX = "https://api.dexscreener.com"
EVM_IDS = {"ethereum": "1", "bsc": "56", "base": "8453", "arbitrum": "42161", "polygon": "137"}
HEADERS = {"User-Agent": "memecoin-scanner/2.0"}

EVM_RPC = {"bsc": "https://bsc-dataseed.binance.org", "base": "https://mainnet.base.org",
           "ethereum": "https://ethereum-rpc.publicnode.com"}
EVM_MAX_BLOCK_RANGE = {"bsc": 2000, "base": 1000, "ethereum": 300}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
SOL_RPC_DEFAULT = "https://api.mainnet-beta.solana.com"
SOL_IGNORE = {"So11111111111111111111111111111111111111112",    # wrapped SOL
              "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",    # USDC
              "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}    # USDT
IGNORE_SYMBOLS = {"USDT", "USDC", "DAI", "BUSD", "FDUSD", "USD1", "WBNB", "WETH", "WSOL", "SOL",
                  "BNB", "ETH", "CBBTC", "WBTC", "BTCB"}


# ---------------------------------------------------------------- config + state
def load_config():
    name = os.getenv("SCAN_PRESET", "balanced").strip().lower()
    if name not in PRESETS:
        print(f"[warn] unknown SCAN_PRESET '{name}', using balanced")
        name = "balanced"
    cfg = {**BASE_CONFIG, **PRESETS[name], "preset": name}
    chains = os.getenv("SCAN_CHAINS", "")
    if chains.strip():
        cfg["chains"] = [c.strip().lower() for c in chains.split(",") if c.strip()]
    return cfg


def load_state(path):
    try:
        with open(path) as f:
            st = json.load(f)
        if not isinstance(st, dict):
            st = {}
    except (FileNotFoundError, ValueError):
        st = {}
    for k in ("seen", "wallet_seen", "sol_sigs", "blocks"):
        st.setdefault(k, {})
    return st


def save_state(st, path):
    cutoff = time.time() - 7 * 86400  # forget alerts older than a week
    for k in ("seen", "wallet_seen"):
        st[k] = {a: t for a, t in st[k].items() if t > cutoff}
    with open(path, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)


def load_wallets():
    raw = os.getenv("WALLETS", "")
    if not raw.strip() and os.path.exists("wallets.txt"):
        with open("wallets.txt") as f:
            raw = f.read()
    wallets = []
    for line in raw.replace(";", "\n").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 2 or not parts[1]:
            continue
        label = parts[2] if len(parts) > 2 and parts[2] else parts[1][:6]
        wallets.append((parts[0].lower(), parts[1], label))
    return wallets


# ---------------------------------------------------------------- helpers
def http_get(url, params=None, retries=3):
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                last = "rate limited"
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            last = e.__class__.__name__
            time.sleep(1 + i)
    raise RuntimeError(f"GET failed after {retries} tries ({last})")


def rpc(url, method, params):
    last = None
    for i in range(3):
        try:
            r = requests.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                              headers=HEADERS, timeout=25)
            if r.status_code == 429:
                time.sleep(2 * (i + 1))
                last = "rate limited"
                continue
            r.raise_for_status()
            j = r.json()
            if j.get("error"):
                raise RuntimeError("rpc error")
            return j.get("result")
        except Exception as e:  # noqa: BLE001
            last = e.__class__.__name__
            time.sleep(1 + i)
    raise RuntimeError(f"RPC {method} failed ({last})")  # no URL printed: it may contain a key


def age_hours(pair):
    ts = pair.get("pairCreatedAt")
    if not ts:
        return None
    return (time.time() * 1000 - ts) / 3_600_000


def num(d, *keys, default=0):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return d if isinstance(d, (int, float)) else default


# ---------------------------------------------------------------- data
def fetch_candidates(chains):
    """Collect (chain, address) from latest profiles + boosts, then fetch pair data."""
    found = {}
    for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1"):
        try:
            for item in http_get(DEX + path):
                chain, addr = item.get("chainId"), item.get("tokenAddress")
                if chain in chains and addr:
                    found[(chain, addr)] = True
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {path}: {e}")
        time.sleep(0.4)

    by_chain = {}
    for chain, addr in found:
        by_chain.setdefault(chain, []).append(addr)

    pairs = []
    for chain, addrs in by_chain.items():
        for i in range(0, len(addrs), 30):
            chunk = addrs[i:i + 30]
            try:
                data = http_get(f"{DEX}/tokens/v1/{chain}/{','.join(chunk)}")
                pairs.extend(data if isinstance(data, list) else [])
            except Exception as e:  # noqa: BLE001
                print(f"[warn] pairs {chain}: {e}")
            time.sleep(0.3)

    # keep the most liquid pair per token
    best = {}
    for p in pairs:
        key = (p.get("chainId"), (p.get("baseToken") or {}).get("address"))
        if key not in best or num(p, "liquidity", "usd") > num(best[key], "liquidity", "usd"):
            best[key] = p
    return list(best.values())


def lookup_pair(chain, token):
    try:
        data = http_get(f"{DEX}/tokens/v1/{chain}/{token}")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, list) or not data:
        return None
    return max(data, key=lambda p: num(p, "liquidity", "usd"))


# ---------------------------------------------------------------- filters + score
def passes_filters(p, cfg):
    liq = num(p, "liquidity", "usd")
    vol = num(p, "volume", "h24")
    fdv = p.get("fdv") or p.get("marketCap") or 0
    age = age_hours(p)
    if liq < cfg["min_liquidity_usd"] or vol < cfg["min_volume_24h_usd"]:
        return False
    if age is None or not (cfg["min_age_hours"] <= age <= cfg["max_age_hours"]):
        return False
    if fdv and fdv > cfg["max_fdv_usd"]:
        return False
    return True


def score_pair(p):
    liq = num(p, "liquidity", "usd")
    vol = num(p, "volume", "h24")
    fdv = p.get("fdv") or p.get("marketCap") or 0
    age = age_hours(p) or 0
    s = 0.0

    s += min(liq / 100_000, 1) * 20  # liquidity depth

    turnover = vol / liq if liq else 0  # volume vs liquidity
    if 1 <= turnover <= 6:
        s += 20
    elif 0.5 <= turnover < 1 or 6 < turnover <= 15:
        s += 10

    b, sl = num(p, "txns", "h1", "buys"), num(p, "txns", "h1", "sells")  # buy pressure
    ratio = b / max(sl, 1)
    pressure = min(ratio / 2, 1) * 20
    s += pressure if (b + sl) >= 30 else pressure / 2

    ch1, ch24 = num(p, "priceChange", "h1"), num(p, "priceChange", "h24")  # momentum, avoid chasing
    if 5 <= ch1 <= 100:
        s += 15
    elif 0 <= ch1 < 5:
        s += 8
    elif 100 < ch1 <= 300:
        s += 6
    if ch24 > 1000:
        s -= 10

    if 6 <= age <= 24:  # age sweet spot
        s += 10
    elif 2 <= age < 6 or 24 < age <= 48:
        s += 7
    else:
        s += 4

    if fdv:  # FDV headroom
        s += 15 if fdv < 1e6 else 11 if fdv < 5e6 else 7 if fdv < 20e6 else 3
    return max(0, min(100, round(s)))


# ---------------------------------------------------------------- safety checks
def check_goplus(chain, addr):
    r = http_get(f"https://api.gopluslabs.io/api/v1/token_security/{EVM_IDS[chain]}",
                 params={"contract_addresses": addr})
    d = (r.get("result") or {}).get(addr.lower())
    if not d:
        return "UNKNOWN", ["no security data"]
    fail, warn = [], []
    if d.get("is_honeypot") == "1":
        fail.append("HONEYPOT")
    if d.get("cannot_sell_all") == "1":
        fail.append("cannot sell all")
    for k in ("buy_tax", "sell_tax"):
        try:
            t = float(d.get(k) or 0)
            if t > 0.10:
                fail.append(f"{k} {t * 100:.0f}%")
            elif t > 0.05:
                warn.append(f"{k} {t * 100:.0f}%")
        except ValueError:
            pass
    for key, label in (("is_mintable", "mintable"), ("hidden_owner", "hidden owner"),
                       ("can_take_back_ownership", "owner can reclaim"),
                       ("transfer_pausable", "pausable")):
        if d.get(key) == "1":
            warn.append(label)
    if d.get("is_open_source") == "0":
        warn.append("closed source")
    return ("FAIL", fail + warn) if fail else ("WARN", warn) if warn else ("PASS", [])


def check_rugcheck(addr):
    r = http_get(f"https://api.rugcheck.xyz/v1/tokens/{addr}/report/summary")
    risks = r.get("risks") or []
    danger = [x.get("name", "?") for x in risks if x.get("level") == "danger"]
    warn = [x.get("name", "?") for x in risks if x.get("level") == "warn"]
    return ("FAIL", danger + warn) if danger else ("WARN", warn) if warn else ("PASS", [])


def check_security(chain, addr):
    try:
        if chain in EVM_IDS:
            return check_goplus(chain, addr)
        if chain == "solana":
            return check_rugcheck(addr)
    except Exception as e:  # noqa: BLE001
        return "UNKNOWN", [f"check failed ({e.__class__.__name__})"]
    return "UNKNOWN", ["no checker for this chain"]


# ---------------------------------------------------------------- telegram + log
_tg_warned = False


def send_telegram(text):
    global _tg_warned  # noqa: PLW0603
    tok, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        if not _tg_warned:
            print("[info] Telegram not set up (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)")
            _tg_warned = True
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                          timeout=15)
        if r.status_code != 200:
            print(f"[warn] telegram HTTP {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[warn] telegram failed ({e.__class__.__name__})")
        return False


def log_rows(rows, path):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_utc", "chain", "symbol", "address", "score", "security",
                        "notes", "price_usd", "liq_usd", "vol24_usd", "fdv", "url"])
        w.writerows(rows)


# ---------------------------------------------------------------- market scan
def run_scan(cfg, pairs, state, alerts=True, log=True):
    seen = state["seen"]
    picks = []
    for p in pairs:
        if not passes_filters(p, cfg):
            continue
        chain = p.get("chainId")
        tok = p.get("baseToken") or {}
        status, notes = check_security(chain, tok.get("address", ""))
        if status == "FAIL":
            continue
        sc = score_pair(p) - (10 if status == "UNKNOWN" else 0) - (5 if status == "WARN" else 0)
        picks.append((max(sc, 0), status, notes, p))
        time.sleep(0.3)

    picks.sort(key=lambda x: x[0], reverse=True)
    picks = picks[:cfg["top_n"]]

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== Scan {now} UTC | preset {cfg['preset']} | {len(pairs)} candidates | "
          f"{len(picks)} passed ===")
    if not picks:
        print("Nothing passed the filters this cycle.")
        return
    print(f"{'SCORE':<6}{'CHAIN':<8}{'SYMBOL':<12}{'LIQ $':>10}{'VOL24 $':>12}{'1h %':>8}  SECURITY")
    rows = []
    for sc, status, notes, p in picks:
        tok = p.get("baseToken") or {}
        sym = (tok.get("symbol") or "?")[:11]
        print(f"{sc:<6}{p.get('chainId', ''):<8}{sym:<12}{num(p, 'liquidity', 'usd'):>10,.0f}"
              f"{num(p, 'volume', 'h24'):>12,.0f}{num(p, 'priceChange', 'h1'):>8.1f}  "
              f"{status} {', '.join(notes)[:40]}")
        rows.append([now, p.get("chainId"), sym, tok.get("address"), sc, status, "; ".join(notes),
                     p.get("priceUsd"), num(p, "liquidity", "usd"), num(p, "volume", "h24"),
                     p.get("fdv"), p.get("url")])
        key = f"{p.get('chainId')}:{tok.get('address')}"
        if alerts and sc >= cfg["min_score_for_alert"] and key not in seen:
            seen[key] = time.time()
            send_telegram(
                f"{sym} on {p.get('chainId')} | score {sc} | security {status}\n"
                f"Liq ${num(p, 'liquidity', 'usd'):,.0f} | Vol24 ${num(p, 'volume', 'h24'):,.0f} "
                f"| 1h {num(p, 'priceChange', 'h1'):.1f}%\n"
                f"{('Flags: ' + ', '.join(notes)) if notes else ''}\n{p.get('url')}")
    if log:
        log_rows(rows, cfg["log_file"])


# ---------------------------------------------------------------- wallet tracking
def pad_topic(addr):
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def watch_solana(wallet, state, url):
    """Return mints this wallet received since the last check (first run = baseline only)."""
    _, addr, _label = wallet
    key = f"solana:{addr}"
    last = state["sol_sigs"].get(key)
    opts = {"limit": 15}
    if last:
        opts["until"] = last
    sigs = rpc(url, "getSignaturesForAddress", [addr, opts]) or []
    if not sigs:
        return []
    state["sol_sigs"][key] = sigs[0]["signature"]  # newest first
    if not last:
        return []
    mints = []
    for s in reversed(sigs):
        if s.get("err"):
            continue
        tx = rpc(url, "getTransaction", [s["signature"],
                                         {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
        meta = (tx or {}).get("meta") or {}
        if not tx or meta.get("err"):
            continue

        def balances(rows):
            return {b["mint"]: (b.get("uiTokenAmount") or {}).get("uiAmount") or 0
                    for b in rows or [] if b.get("owner") == addr}
        pre, post = balances(meta.get("preTokenBalances")), balances(meta.get("postTokenBalances"))
        for mint, amt in post.items():
            if mint not in SOL_IGNORE and amt - pre.get(mint, 0) > 0 and mint not in mints:
                mints.append(mint)
        time.sleep(0.4)
    return mints


def watch_evm(chain, wallets, state):
    """Return [(token_address, label)] for ERC-20 tokens received by any wallet since last check."""
    url = os.getenv(f"{chain.upper()}_RPC") or EVM_RPC.get(chain)
    if not url:
        print(f"[warn] no RPC for chain '{chain}' (set {chain.upper()}_RPC)")
        return []
    latest = int(rpc(url, "eth_blockNumber", []), 16)
    last = state["blocks"].get(chain)
    state["blocks"][chain] = latest
    if last is None:
        return []  # first run: baseline only
    start = max(last + 1, latest - EVM_MAX_BLOCK_RANGE.get(chain, 1000))
    if start > latest:
        return []
    labels = {pad_topic(w[1]): w[2] for w in wallets}
    logs = rpc(url, "eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(latest),
                                      "topics": [TRANSFER_TOPIC, None, list(labels)]}]) or []
    found = {}
    for lg in logs:
        topics = lg.get("topics") or []
        if len(topics) != 3:  # ERC-20 transfers have 3 topics (NFT transfers have 4)
            continue
        found[lg["address"].lower()] = labels.get(topics[2].lower(), "?")
    return list(found.items())


def run_wallet_watch(cfg, state, wallets, alerts=True):
    if not wallets:
        return
    by_chain = {}
    for w in wallets:
        by_chain.setdefault(w[0], []).append(w)

    hits = []  # (chain, token, label)
    for chain, ws in by_chain.items():
        try:
            if chain == "solana":
                url = os.getenv("SOLANA_RPC") or SOL_RPC_DEFAULT
                for w in ws:
                    hits += [(chain, m, w[2]) for m in watch_solana(w, state, url)]
            elif chain in EVM_IDS:
                hits += [(chain, t, label) for t, label in watch_evm(chain, ws, state)]
            else:
                print(f"[warn] wallet chain '{chain}' not supported")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] wallet watch {chain}: {e}")

    for chain, token, label in hits:
        key = f"{chain}:{token}:{label}"
        if key in state["wallet_seen"]:
            continue
        state["wallet_seen"][key] = time.time()
        pair = lookup_pair(chain, token)
        if not pair or num(pair, "liquidity", "usd") < cfg["wallet_min_liquidity_usd"]:
            continue  # no real market = dust or spam
        sym = ((pair.get("baseToken") or {}).get("symbol") or "?")[:11]
        if sym.upper() in IGNORE_SYMBOLS:
            continue
        status, notes = check_security(chain, token)
        if status == "FAIL":
            continue
        print(f"[wallet] a tracked wallet picked up {sym} on {chain} ({status})")  # label kept off logs
        if alerts:
            send_telegram(
                f"👀 {label} picked up {sym} ({chain})\n"
                f"Liq ${num(pair, 'liquidity', 'usd'):,.0f} | Vol24 ${num(pair, 'volume', 'h24'):,.0f}"
                f" | FDV ${(pair.get('fdv') or 0):,.0f}\n"
                f"Security: {status} {', '.join(notes)}\n{pair.get('url')}")


# ---------------------------------------------------------------- demo data
def demo_pairs():
    now_ms = time.time() * 1000

    def mk(sym, chain, liq, vol, fdv, age_h, ch1, ch24, buys, sells):
        return {"chainId": chain, "baseToken": {"address": "0x" + sym.lower() * 4, "symbol": sym},
                "priceUsd": "0.0012", "liquidity": {"usd": liq}, "volume": {"h24": vol},
                "fdv": fdv, "pairCreatedAt": now_ms - age_h * 3_600_000,
                "priceChange": {"h1": ch1, "h24": ch24},
                "txns": {"h1": {"buys": buys, "sells": sells}},
                "url": f"https://dexscreener.com/{chain}/demo-{sym.lower()}"}
    return [
        mk("GOODCOIN", "bsc", 85_000, 240_000, 900_000, 10, 22, 80, 180, 90),
        mk("MIDCOIN", "base", 40_000, 70_000, 3_000_000, 30, 3, 15, 40, 35),
        mk("PUMPED", "solana", 120_000, 900_000, 15_000_000, 5, 280, 1500, 400, 380),
        mk("TOOSMALL", "bsc", 5_000, 90_000, 200_000, 8, 40, 90, 60, 20),
        mk("TOOOLD", "base", 90_000, 300_000, 2_000_000, 200, 5, 20, 80, 60),
    ]


# ---------------------------------------------------------------- main
def main():
    global check_security  # noqa: PLW0603
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="use fake data, no internet needed")
    ap.add_argument("--once", action="store_true", help="scan once and exit")
    ap.add_argument("--test-telegram", action="store_true", help="send a test Telegram message")
    args = ap.parse_args()

    if args.test_telegram:
        ok = send_telegram("✅ Meme coin scanner is connected. You'll get alerts here.")
        print("Telegram test sent." if ok else "Telegram test FAILED. Check the token and chat ID.")
        return 0 if ok else 1

    cfg = load_config()

    if args.demo:
        check_security = lambda chain, addr: ("PASS", [])  # noqa: E731
        run_scan(cfg, demo_pairs(), {"seen": {}}, alerts=False, log=False)
        return 0

    state = load_state(cfg["state_file"])
    wallets = load_wallets()
    print(f"Preset: {cfg['preset']} | chains: {','.join(cfg['chains'])} | wallets tracked: {len(wallets)}")

    while True:
        try:
            run_scan(cfg, fetch_candidates(cfg["chains"]), state)
        except Exception as e:  # noqa: BLE001
            print(f"[error] scan failed: {e}")
        try:
            run_wallet_watch(cfg, state, wallets)
        except Exception as e:  # noqa: BLE001
            print(f"[error] wallet watch failed: {e}")
        save_state(state, cfg["state_file"])
        if args.once:
            break
        time.sleep(cfg["scan_interval_sec"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
