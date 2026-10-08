"""
Pump.fun graduation scanner.

Listens to every bonding-curve graduation on pump.fun, measures how long
the token spent on the curve, and sends the slow ones to Telegram.

A token that fills its curve in three minutes was filled by bots.
One that takes six hours was filled by people. That difference is the
whole idea.

Run:
    uvicorn app:app --host 127.0.0.1 --port 8091
"""

import csv
import json
import threading
import time
from collections import defaultdict, deque
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Request

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text())

TG_TOKEN = CFG["tg_token"]
CHANNEL = CFG.get("channel", "")
MIN_AGE_H = float(CFG.get("min_age_hours", 6))
CONFIRM_MINUTES = float(CFG.get("confirmation_minutes", 5))
MIN_VOLUME_5M_USD = float(CFG.get("min_5m_volume_usd", 5000))
MIN_BUYS_5M = int(CFG.get("min_5m_buys", 20))
MIN_LIQUIDITY_USD = float(CFG.get("min_liquidity_usd", 10000))
MIN_BUY_SELL_RATIO = float(CFG.get("min_buy_sell_ratio", 1.1))
REQUIRE_SOCIAL_OR_WEBSITE = bool(CFG.get("require_social_or_website", True))
MAX_TOP_HOLDER_PERCENT = float(CFG.get("max_top_holder_percent", 20))
MIN_MOMENTUM_VOLUME_SOL = float(CFG.get("min_momentum_volume_sol", 10))
MIN_MOMENTUM_BUYS = int(CFG.get("min_momentum_buys", 20))
MOMENTUM_WINDOW_SECONDS = int(CFG.get("momentum_window_seconds", 300))
MOMENTUM_ALERT_COOLDOWN = int(CFG.get("momentum_alert_cooldown", 3600))
WEBHOOK_AUTH = CFG.get("webhook_auth", "")
RPC = f"https://mainnet.helius-rpc.com/?api-key={CFG['helius_key']}"

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_SOURCES = {"PUMP_AMM", "PUMP_FUN"}
WSOL_MINT = "So11111111111111111111111111111111111111112"

USERS = BASE / "users.json"
REJECTIONS = BASE / "rejections.jsonl"
users = json.loads(USERS.read_text()) if USERS.exists() else {}

app = FastAPI()


def webhook_authorized(req: Request):
    """Accept all traffic only when no secret is configured."""
    if not WEBHOOK_AUTH:
        return True
    supplied = req.headers.get("authorization", "")
    return supplied == WEBHOOK_AUTH


# ----------------------------------------------------------------- telegram

def api(method: str, params: dict):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    try:
        with urllib.request.urlopen(url, data, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"ok": False, "error_code": e.code}
    except Exception as e:
        print("telegram:", e)
        return {"ok": False}


def send(chat_id, text: str, buttons=None):
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": "true"}
    if buttons:
        p["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    return api("sendMessage", p)


def save_users():
    USERS.write_text(json.dumps(users, ensure_ascii=False, indent=1))


def record_rejection(mint, reason, signature=""):
    record = {
        "timestamp": int(time.time()),
        "mint": mint,
        "reason": reason,
        "signature": signature,
    }
    with REJECTIONS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def is_member(user_id) -> bool:
    """Gate access on channel membership. Empty channel in config = open to all.
    The bot must be an admin of the channel for this call to work."""
    if not CHANNEL:
        return True
    r = api("getChatMember", {"chat_id": CHANNEL, "user_id": user_id})
    if not r.get("ok"):
        return False
    return r["result"].get("status") in ("creator", "administrator", "member")


def broadcast(text: str, buttons=None):
    if CHANNEL:
        channel_result = send(CHANNEL, text, buttons)
        if not channel_result.get("ok"):
            print("channel send failed:", channel_result)
        else:
            print(f"channel alert sent: {CHANNEL}")

    now = time.time()
    sent = dropped = 0
    for uid, u in list(users.items()):
        if now - u.get("checked", 0) > 21600:
            u["member"] = is_member(uid)
            u["checked"] = now
        if not u.get("member"):
            continue
        r = send(uid, text, buttons)
        if not r.get("ok"):
            desc = (r.get("description") or "").lower()
            if r.get("error_code") == 403 or "blocked" in desc or "deactivated" in desc:
                users.pop(uid, None)
                dropped += 1
                continue
        else:
            sent += 1
        time.sleep(0.04)
    save_users()
    print(f"broadcast: sent={sent} dropped={dropped} total={len(users)}")


# ---------------------------------------------------------------------- rpc

stats = {"events": 0, "graduations": 0, "alerts": 0, "filtered": 0, "rpc": 0}
volume_stats = {"events": 0, "tokens": 0, "unparsed": 0}
volume_windows = defaultdict(deque)
volume_alerted = {}


def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    stats["rpc"] += 1
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def age_minutes(mint, grad_ts):
    """How long the token lived before graduating.

    Pages signatures backwards and stops as soon as it finds one older than
    the threshold, so old tokens cost one or two calls instead of twelve.
    """
    cutoff = grad_ts - MIN_AGE_H * 3600
    before = None
    oldest = grad_ts
    for _ in range(20):
        p = [mint, {"limit": 1000}]
        if before:
            p[1]["before"] = before
        try:
            res = rpc("getSignaturesForAddress", p).get("result") or []
        except Exception as e:
            print("age:", e)
            return None
        if not res:
            break
        bt = res[-1].get("blockTime")
        if bt:
            oldest = bt
            if bt < cutoff:
                return (grad_ts - bt) / 60
        before = res[-1]["signature"]
        if len(res) < 1000:
            break
    return (grad_ts - oldest) / 60


def token_meta(mint):
    try:
        a = rpc("getAsset", {"id": mint}).get("result") or {}
        md = (a.get("content") or {}).get("metadata") or {}
        return md.get("symbol", ""), md.get("name", "")
    except Exception:
        return "", ""


def migration_confirmed(signature):
    """Require the graduation transaction to contain a Pump.fun migration.

    CREATE_POOL alone is not proof of a bonding-curve graduation: unrelated
    pools can produce the same enhanced transaction type. The migration
    instruction is emitted in the transaction log by the Pump program.
    """
    if not signature:
        return False
    try:
        result = rpc("getTransaction", [
            signature,
            {"encoding": "jsonParsed", "commitment": "confirmed",
             "maxSupportedTransactionVersion": 0},
        ]).get("result") or {}
    except Exception as e:
        print("migration:", e)
        return False
    meta = result.get("meta") or {}
    if meta.get("err") is not None:
        return False
    logs = "\n".join(meta.get("logMessages") or []).lower()
    migration_words = ("instruction: migrate", "instruction: migrate_v2",
                       "instruction: migrate_v2")
    return any(word in logs for word in migration_words)


def holder_concentration(mint):
    """Return the largest visible holder percentage from Solana RPC."""
    try:
        supply = rpc("getTokenSupply", [mint]).get("result", {}).get("value") or {}
        total = int(supply.get("amount") or 0)
        if total <= 0:
            return None, "no token supply"
        largest = rpc("getTokenLargestAccounts", [mint]).get("result", {}).get("value") or []
        if not largest:
            return None, "no holder data"
        accounts = [row.get("address") for row in largest if row.get("address")]
        details = rpc("getMultipleAccounts", [
            accounts, {"encoding": "jsonParsed"}
        ]).get("result", {}).get("value") or []
        owners = {}
        for row, detail in zip(largest, details):
            parsed = (((detail or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            owner = parsed.get("owner")
            if not owner or owner in (PUMP_PROGRAM, PUMP_AMM):
                continue
            owners[owner] = owners.get(owner, 0) + int(row.get("amount") or 0)
        if not owners:
            return None, "no user holders found"
        top = max(owners.values())
        return top / total * 100, ""
    except Exception as e:
        print("holders:", e)
        return None, "holder lookup failed"


def parse_pump_swap(e):
    """Extract one PumpSwap trade; unknown direction/size is rejected."""
    if e.get("type") != "SWAP" or e.get("source") != "PUMP_AMM":
        return None
    payer = e.get("feePayer") or ""
    token_transfer = next((t for t in (e.get("tokenTransfers") or [])
                           if (t.get("mint") or "").endswith("pump")), None)
    if not payer or not token_transfer:
        return None
    if token_transfer.get("toUserAccount") == payer:
        side = "buy"
    elif token_transfer.get("fromUserAccount") == payer:
        side = "sell"
    else:
        return None
    sol_amounts = []
    for t in (e.get("tokenTransfers") or []):
        if t.get("mint") != WSOL_MINT:
            continue
        amount = float(t.get("tokenAmount") or 0)
        if side == "buy" and t.get("fromUserAccount") == payer:
            sol_amounts.append(amount)
        elif side == "sell" and t.get("toUserAccount") == payer:
            sol_amounts.append(amount)
    sol = sum(sol_amounts)
    if sol <= 0:
        return None
    return {"mint": token_transfer["mint"], "side": side, "sol": sol,
            "buyer": e.get("feePayer") or "", "ts": e.get("timestamp") or time.time()}


def record_momentum_trade(trade):
    now = time.time()
    window = volume_windows[trade["mint"]]
    window.append(trade)
    cutoff = now - MOMENTUM_WINDOW_SECONDS
    while window and window[0]["ts"] < cutoff:
        window.popleft()
    buys = [t for t in window if t["side"] == "buy"]
    sells = [t for t in window if t["side"] == "sell"]
    buy_sol = sum(t["sol"] for t in buys)
    sell_sol = sum(t["sol"] for t in sells)
    unique_buyers = len({t["buyer"] for t in buys if t["buyer"]})
    if (buy_sol + sell_sol >= MIN_MOMENTUM_VOLUME_SOL and
            len(buys) >= MIN_MOMENTUM_BUYS and buy_sol > sell_sol and
            unique_buyers >= MIN_MOMENTUM_BUYS and
            now - volume_alerted.get(trade["mint"], 0) >= MOMENTUM_ALERT_COOLDOWN):
        volume_alerted[trade["mint"]] = now
        return buy_sol, sell_sol, len(buys), len(sells), unique_buyers
    return None


def dex_confirmation(mint):
    """Return whether the new PumpSwap pair has enough activity/liquidity.

    DexScreener's m5 values are rolling five-minute values, so this is a
    practical confirmation signal rather than an exact post-graduation ledger.
    """
    url = f"https://api.dexscreener.com/tokens/v1/solana/{mint}"
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; QLO-Alert-Bot/1.0)",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            pairs = json.loads(r.read())
    except Exception as e:
        print("dexscreener:", e)
        return False, "DexScreener unavailable"

    if not isinstance(pairs, list) or not pairs:
        return False, "not indexed yet"

    pumpswap = [p for p in pairs if p.get("dexId") == "pumpswap"]
    pair = max(pumpswap or pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0))
    liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
    volume = float((pair.get("volume") or {}).get("m5") or 0)
    txns_5m = (pair.get("txns") or {}).get("m5") or {}
    buys = int(txns_5m.get("buys") or 0)
    sells = int(txns_5m.get("sells") or 0)
    info = pair.get("info") or {}
    websites = [x for x in (info.get("websites") or []) if x.get("url")]
    socials = [x for x in (info.get("socials") or []) if x.get("url") or x.get("handle")]
    if liquidity < MIN_LIQUIDITY_USD:
        return False, f"liquidity ${liquidity:,.0f} < ${MIN_LIQUIDITY_USD:,.0f}"
    if volume < MIN_VOLUME_5M_USD:
        return False, f"5m volume ${volume:,.0f} < ${MIN_VOLUME_5M_USD:,.0f}"
    if buys < MIN_BUYS_5M:
        return False, f"5m buys {buys} < {MIN_BUYS_5M}"
    if sells == 0:
        ratio = float("inf") if buys else 0
    else:
        ratio = buys / sells
    if buys <= sells or ratio < MIN_BUY_SELL_RATIO:
        return False, f"buy/sell ratio {buys}/{sells} ({ratio:.2f}) < {MIN_BUY_SELL_RATIO:.2f}"
    if REQUIRE_SOCIAL_OR_WEBSITE and not websites and not socials:
        return False, "no website or social link"
    top_percent, holder_reason = holder_concentration(mint)
    if top_percent is None:
        return False, holder_reason
    if top_percent > MAX_TOP_HOLDER_PERCENT:
        return False, f"top holder {top_percent:.1f}% > {MAX_TOP_HOLDER_PERCENT:.1f}%"
    return True, (
        f"liquidity ${liquidity:,.0f}, 5m volume ${volume:,.0f}, "
        f"buys/sells {buys}/{sells}, top holder {top_percent:.1f}%, "
        f"links {len(websites) + len(socials)}"
    )


# ------------------------------------------------------------------ scanner

queue = []
qlock = threading.Lock()
seen = {}


def keyboard(mint):
    return [[
        {"text": "Pump.fun", "url": f"https://pump.fun/coin/{mint}"},
        {"text": "DexScreener", "url": f"https://dexscreener.com/solana/{mint}"},
    ], [
        {"text": "Axiom", "url": f"https://axiom.trade/t/{mint}"},
        {"text": "Solscan", "url": f"https://solscan.io/token/{mint}"},
    ]]


def build(mint, symbol, name, mins):
    hours = mins / 60
    age = f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} d"
    title = f"${symbol}" + (f" \u2014 {name}" if name and name != symbol else "")
    return "\n".join([
        "\U0001F40C <b>SLOW GRADUATION</b>",
        "",
        title if symbol else "<i>unnamed</i>",
        "",
        f"<code>Time on curve   {age}</code>",
        f"<code>Median token    0.3 h</code>",
        "",
        "<b>Contract</b>",
        f"<code>{mint}</code>",
    ])


def worker():
    while True:
        time.sleep(2)
        with qlock:
            batch, queue[:] = list(queue), []
        for mint, grad_ts in batch:
            try:
                mins = age_minutes(mint, grad_ts)
                if mins is None or mins < MIN_AGE_H * 60:
                    continue
                if CONFIRM_MINUTES > 0:
                    time.sleep(CONFIRM_MINUTES * 60)
                approved, reason = dex_confirmation(mint)
                if not approved:
                    stats["filtered"] += 1
                    record_rejection(mint, reason)
                    print(f"filtered: {mint} ({reason})")
                    continue
                sym, name = token_meta(mint)
                stats["alerts"] += 1
                broadcast(build(mint, sym, name, mins), keyboard(mint))
                print(f"alert: {mint} {mins / 60:.1f}h ({reason})")
            except Exception as e:
                print("worker:", e)


threading.Thread(target=worker, daemon=True).start()


# ---------------------------------------------------------------- endpoints

@app.post("/hook")
async def hook(req: Request):
    """Helius webhook: CREATE_POOL events on the pump.fun AMM."""
    if not webhook_authorized(req):
        return {"ok": False, "error": "unauthorized"}
    body = await req.json()
    events = body if isinstance(body, list) else [body]
    now = time.time()

    for e in events:
        stats["events"] += 1
        if e.get("type") != "CREATE_POOL":
            continue
        if e.get("transactionError") not in (None, "", {}):
            continue
        if e.get("source") not in PUMP_SOURCES:
            print(f"ignored CREATE_POOL source={e.get('source')!r} sig={e.get('signature', '')}")
            continue
        accs = {a.get("account") for a in (e.get("accountData") or [])}
        if PUMP_PROGRAM not in accs or PUMP_AMM not in accs:
            continue                      # not an official PumpSwap pool event

        mint = ""
        for a in (e.get("accountData") or []):
            for c in (a.get("tokenBalanceChanges") or []):
                m = c.get("mint") or ""
                if m.endswith("pump"):
                    mint = m
                    break
            if mint:
                break
        if not mint:
            continue

        if not migration_confirmed(e.get("signature")):
            stats["filtered"] += 1
            record_rejection(mint, "no confirmed Pump.fun migration", e.get("signature", ""))
            print(f"filtered: {mint} (no confirmed Pump.fun migration)")
            continue

        if mint in seen and now - seen[mint] < 3600:
            continue
        seen[mint] = now
        for k, v in list(seen.items()):
            if now - v > 7200:
                seen.pop(k, None)

        stats["graduations"] += 1
        with qlock:
            queue.append((mint, e.get("timestamp") or now))

    return {"ok": True}


@app.post("/volume-hook")
async def volume_hook(req: Request):
    """Receive PumpSwap swaps and emit conservative momentum alerts."""
    if not webhook_authorized(req):
        return {"ok": False, "error": "unauthorized"}
    body = await req.json()
    events = body if isinstance(body, list) else [body]
    tokens = set()
    for e in events:
        volume_stats["events"] += 1
        trade = parse_pump_swap(e)
        if not trade:
            volume_stats["unparsed"] += 1
            continue
        mint = trade["mint"]
        tokens.add(mint)
        result = record_momentum_trade(trade)
        if result:
            approved, reason = dex_confirmation(mint)
            if not approved:
                stats["filtered"] += 1
                record_rejection(mint, f"momentum: {reason}", e.get("signature", ""))
                continue
            buy_sol, sell_sol, buys, sells, buyers = result
            sym, name = token_meta(mint)
            stats["alerts"] += 1
            text = "\n".join([
                "\U0001F525 <b>MOMENTUM ALERT</b>", "",
                f"${sym} — {name}" if sym else "<i>unnamed</i>", "",
                f"<code>5m buys/sells  {buy_sol:.2f}/{sell_sol:.2f} SOL</code>",
                f"<code>Trades         {buys}/{sells}</code>",
                f"<code>Unique buyers  {buyers}</code>", "",
                "<b>Contract</b>", f"<code>{mint}</code>",
            ])
            broadcast(text, keyboard(mint))
    volume_stats["tokens"] += len(tokens)
    return {"ok": True, "events": len(events), "tokens": len(tokens)}


WELCOME = (
    "\U0001F4E1 <b>Pump.fun graduation scanner</b>\n\n"
    "You will get an alert when a token takes hours to fill its bonding "
    "curve instead of minutes.\n\n"
    "A curve filled in three minutes was filled by bots. One that takes six "
    "hours was filled by people.\n\n"
    "Not financial advice. Most of these still go to zero.\n\n"
    "Send /stop to unsubscribe."
)


@app.post("/tg")
async def telegram(req: Request):
    upd = await req.json()
    msg = upd.get("message") or {}
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return {"ok": True}

    uid = str(chat.get("id"))
    text = (msg.get("text") or "").strip()

    if text.startswith("/start"):
        if not is_member(uid):
            ch = CHANNEL.lstrip("@")
            send(uid, "\U0001F512 Subscribe to the channel first, then send /start again.",
                 [[{"text": "Open channel", "url": f"https://t.me/{ch}"}]])
            return {"ok": True}
        first = uid not in users
        users[uid] = {
            "username": chat.get("username", ""),
            "joined": users.get(uid, {}).get("joined") or int(time.time()),
            "member": True,
            "checked": time.time(),
        }
        save_users()
        send(uid, WELCOME if first else "\u2705 You are back on the list.")

    elif text.startswith("/stop"):
        if users.pop(uid, None):
            save_users()
        send(uid, "Unsubscribed. Send /start to come back.")

    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True, "users": len(users), **stats,
            "volume": volume_stats, "min_age_hours": MIN_AGE_H,
            "queue": len(queue)}
