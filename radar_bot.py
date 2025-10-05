# pip install flask requests python-dotenv
import os, time, json, requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

# ---------- load secrets from .env ----------
load_dotenv()
HELIUS_API_KEY   = os.getenv("HELIUS_API_KEY", "")
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
EXPECTED_AUTH = os.getenv("WEBHOOK_AUTH_HEADER", "")


# ---------- thresholds (tune later) ----------
MIN_LIQ_USD     = 10000
MIN_VOL24_USD   = 2000
MAX_TOP10_PCT   = 50.0
ALERT_SCORE_MIN = 25
MINUTES_BETWEEN_ALERTS = 120

# ---------- endpoints ----------
SOLSCAN_META_URL    = "https://public-api.solscan.io/token/meta?tokenAddress="
SOLSCAN_HOLDERS_URL = "https://public-api.solscan.io/token/holders?tokenAddress="
DEXSCREENER_URL     = "https://api.dexscreener.com/latest/dex/tokens/"

app = Flask(__name__)
last_alert_time = {}
seen_mints = set()

# ---------- helpers ----------
def tg_send(text):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        r = requests.post(url, data=payload, timeout=10)
        return r.ok, r.text
    except Exception as e:
        return False, str(e)

def fetch_solscan_meta(mint):
    try:
        r = requests.get(SOLSCAN_META_URL + mint, timeout=10)
        if r.status_code == 200:
            return r.json()
    except: pass
    return {}

def fetch_solscan_holders(mint, limit=20):
    try:
        r = requests.get(f"{SOLSCAN_HOLDERS_URL}{mint}&limit={limit}", timeout=10)
        if r.status_code == 200:
            return r.json()
    except: pass
    return []

def fetch_dexscreener(mint):
    try:
        r = requests.get(DEXSCREENER_URL + mint, timeout=10)
        if r.status_code == 200:
            return r.json()
    except: pass
    return {}

def parse_liq_vol(dex_json):
    pairs = dex_json.get("pairs", []) or []
    if not pairs:
        return 0.0, 0.0, None
    best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
    liq = float(best.get("liquidity", {}).get("usd", 0) or 0)
    vol24 = float(best.get("volume", {}).get("h24", 0) or 0)
    return liq, vol24, best.get("url")

def extract_total_supply(meta):
    s = meta.get("supply")
    try:
        return float(s) if s is not None else None
    except:
        return None

def pct_top10(holders, total_supply):
    if not holders or not total_supply or total_supply == 0:
        return None
    total = 0.0
    for h in holders[:10]:
        try:
            total += float(h.get("amount", 0))
        except:
            pass
    return (total / float(total_supply)) * 100.0

def is_mint_revoked(meta):
    if "mintAuthority" not in meta:
        return None
    return meta.get("mintAuthority") in (None, "", 0)

# ---------- scoring ----------
def score(snapshot):
    greens, reds = [], []
    liq = snapshot["liquidity_usd"]
    vol24 = snapshot["vol24_usd"]
    t10 = snapshot["top10_pct"]
    revoked = snapshot["mint_revoked"]
    meta = snapshot["meta"]

    risk = 50; reward = 50

    if liq >= MIN_LIQ_USD:
        reward += 15; greens.append(f"Liquidity OK (${int(liq):,})")
    else:
        risk += 25; reds.append("Low liquidity")

    if vol24 >= MIN_VOL24_USD:
        reward += 10; greens.append(f"24h Vol OK (${int(vol24):,})")
    else:
        risk += 10; reds.append("Low 24h volume")

    if meta.get("name") and meta.get("symbol"):
        reward += 5; greens.append("Metadata exists")
    else:
        risk += 10; reds.append("No/weak metadata")

    if t10 is None:
        risk += 10; reds.append("No holder data")
    elif t10 <= MAX_TOP10_PCT:
        reward += 10; greens.append(f"Top10 holders {t10:.1f}%")
    else:
        risk += 20; reds.append(f"High holder concentration ({t10:.1f}%)")

    if revoked is True:
        reward += 10; greens.append("Mint authority revoked")
    elif revoked is False:
        risk += 15; reds.append("Mint authority present")
    else:
        reds.append("Mint authority unknown")

    risk = max(0, min(100, risk))
    reward = max(0, min(100, reward))
    flip = reward - risk
    return {"risk":risk, "reward":reward, "flipscore":flip, "greens":greens, "reds":reds}

# ---------- analyzer ----------
def analyze_mint(mint):
    meta = fetch_solscan_meta(mint) or {}
    holders = fetch_solscan_holders(mint, limit=20) or []
    dex = fetch_dexscreener(mint) or {}

    liq, vol24, pair_url = parse_liq_vol(dex)
    total_supply = extract_total_supply(meta)
    t10 = pct_top10(holders, total_supply) if total_supply else None
    revoked = is_mint_revoked(meta)

    name = meta.get("name") or (dex.get("pairs", [{}])[0].get("baseToken", {}) or {}).get("name") or "Unknown"
    symbol = meta.get("symbol") or (dex.get("pairs", [{}])[0].get("baseToken", {}) or {}).get("symbol") or "?"

    snapshot = {
        "mint": mint,
        "name": name,
        "symbol": symbol,
        "meta": meta,
        "holders": holders,
        "liquidity_usd": liq,
        "vol24_usd": vol24,
        "top10_pct": t10,
        "mint_revoked": revoked,
        "pair_url": pair_url
    }
    return snapshot, score(snapshot)

def should_alert(mint, flipscore):
    now = time.time()
    last = last_alert_time.get(mint, 0)
    if now - last < MINUTES_BETWEEN_ALERTS * 60:
        return False
    if flipscore >= ALERT_SCORE_MIN:
        last_alert_time[mint] = now
        return True
    return False

def format_msg(snapshot, score):
    greens = "\n".join([f"✅ {g}" for g in score["greens"]]) or "—"
    reds   = "\n".join([f"⚠️ {r}" for r in score["reds"]]) or "—"
    t10txt = "unknown" if snapshot["top10_pct"] is None else f"{snapshot['top10_pct']:.1f}%"
    liq, vol = int(snapshot["liquidity_usd"]), int(snapshot["vol24_usd"])
    pair = snapshot.get("pair_url") or "N/A"

    return (
        f"🚨 *New Token Scan*\n"
        f"*Name:* {snapshot['name']} ({snapshot['symbol']})\n"
        f"*Mint:* `{snapshot['mint']}`\n\n"
        f"{greens}\n{reds}\n\n"
        f"*Liquidity:* ${liq:,} | *24h Vol:* ${vol:,}\n"
        f"*Top10 Holders:* {t10txt}\n"
        f"*Mint authority revoked:* {snapshot['mint_revoked']}\n"
        f"[View on DexScreener]({pair})\n\n"
        f"*Risk:* {score['risk']}/100 | *Reward:* {score['reward']}/100 | "
        f"*FlipScore:* *{score['flipscore']}* → {'GREEN ALERT' if score['flipscore']>=ALERT_SCORE_MIN else 'PASS'}"
    )

# ---------- Helius webhook endpoint ----------
@app.route("/webhook/helius", methods=["POST"])
def helius_webhook():
    incoming = request.headers.get("Authorization", "") or request.headers.get("authorization") or ""
    
    # Force log to Render output
    import sys
    print(">>> Incoming header:", repr(incoming), file=sys.stdout, flush=True)
    print(">>> Expected header:", repr(EXPECTED_AUTH), file=sys.stdout, flush=True)

    if incoming.strip() != EXPECTED_AUTH.strip():
        print("❌ Unauthorized - mismatch", file=sys.stdout, flush=True)
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    
    data = request.json or {}
    print("✅ Webhook payload:", data, file=sys.stdout, flush=True)
    return jsonify({"ok": True, "results": data}), 200



    try:
        j = request.get_json(force=True, silent=True) or {}
        mints = j.get("mints", [])
        results = []

        for mint in mints:
            # Create the message
            msg = f"🚨 New mint detected: {mint}\n✅ Ready to flip on Phantom!"
            
            # Send to Telegram
            ok, resp = send(msg)   # this uses your existing send() function
            results.append({"mint": mint, "sent": ok, "resp": resp})

        return jsonify({"ok": True, "results": results}), 200

    except Exception as e:
        print("Webhook error:", e)
        return jsonify({"ok": False, "error": str(e)}), 500


    try:
        # Parse the webhook safely
        j = request.get_json(force=True, silent=True) or {}
        mints = j.get("mints", [])
        results = []

        for mint in mints:
            # For now: just acknowledge receipt (no crashes)
            results.append({"mint": mint, "status": "received ✅"})

        return jsonify({"ok": True, "results": results}), 200

    except Exception as e:
        print("Webhook error:", e)
        return jsonify({"ok": False, "error": str(e)}), 500



# ---------- manual test ----------
@app.route("/test/<mint>", methods=["GET"])
def test_mint(mint):
    snapshot, sc = analyze_mint(mint)
    msg = format_msg(snapshot, sc)
    ok, resp = tg_send(msg)
    return jsonify({"ok": ok, "score": sc, "resp": resp}), 200
# ------------- test endpoint -------------
@app.route("/test", methods=["GET"])
def test():
    tg_send("✅ Test alert: Your Solana Radar bot is working!")
    return {"status": "ok", "message": "Test alert sent to Telegram"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
