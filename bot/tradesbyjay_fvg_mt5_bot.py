#!/usr/bin/env python3
"""TradesByJay MT5 scanner: HTF support/resistance + M5 displacement FVG.

Default mode is scan-only. Demo-account guard is always enforced unless the
operator deliberately changes both demo_only and allow_live_trading.
"""
from __future__ import annotations
import base64, json, logging, math, os, threading, time, urllib.error, urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

ROOT = Path(__file__).resolve().parent
CFG_PATH = ROOT / "config.json"
DEFAULTS = {
    "symbols": ["XAUUSD", "Volatility 75 Index", "Volatility 50 Index", "Step Index"],
    "htf_timeframe": "H1", "htf_lookback": 50, "m5_bars": 160,
    "min_fvg_size_pips": 2.0, "pip_size_overrides": {},
    "require_liquidity_sweep": False, "sweep_lookback": 5,
    "risk_percent": 1.0, "risk_reward": 2.0,
    "scan_seconds": 5, "publish_seconds": 30,
    "max_spread_points": 0, "deviation_points": 20,
    "magic": 161126, "auto_execute": True, "demo_only": True,
    "allow_live_trading": False, "max_positions": 4,
    "api_host": "127.0.0.1", "api_port": 8765,
    "api_token": "CHANGE_THIS_LOCAL_TOKEN",
    "github_repo": "lamar-progst/apa-dashboard", "github_token": "",
    "telegram_bot_token": "", "telegram_chat_id": ""
}
cfg = {**DEFAULTS}
if CFG_PATH.exists():
    cfg.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "tradesbyjay_bot.log", encoding="utf-8")])
log = logging.getLogger("tradesbyjay")
lock = threading.RLock()
state = {"online": False, "updated": None, "strategy": "TRADESBYJAY_HTF_SR_M5_FVG",
         "status": "STARTING", "markets": [], "signals": [], "positions": [],
         "performance": {}, "bot": {"running": False, "connected": False, "mode": "SCAN ONLY",
         "last_scan": None, "last_publish": None, "errors": []}}
SEEN_PATH = ROOT / "seen_fvg.json"
PENDING_PATH = ROOT / "pending_fvg.json"
try:
    seen_fvg = {(str(x[0]), str(x[1]), int(x[2])) for x in json.loads(SEEN_PATH.read_text(encoding="utf-8"))}
except Exception:
    seen_fvg = set()
try:
    pending_meta = {int(k): v for k, v in json.loads(PENDING_PATH.read_text(encoding="utf-8")).items()}
except Exception:
    pending_meta = {}
last_publish = 0.0
last_telegram = set()

def save_seen_fvg():
    try:
        SEEN_PATH.write_text(json.dumps([list(x) for x in sorted(seen_fvg)]), encoding="utf-8")
    except Exception as exc:
        log.warning("Could not persist FVG deduplication state: %s", exc)

def save_pending_meta():
    try:
        PENDING_PATH.write_text(json.dumps({str(k): v for k, v in pending_meta.items()}), encoding="utf-8")
    except Exception as exc:
        log.warning("Could not persist pending FVG metadata: %s", exc)

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def rates(symbol, timeframe, count):
    data = mt5.copy_rates_from_pos(symbol, timeframe, 1, count)
    if data is None or len(data) < 3:
        return []
    return [{"time": int(x["time"]), "open": float(x["open"]), "high": float(x["high"]),
             "low": float(x["low"]), "close": float(x["close"]),
             "tick_volume": int(x["tick_volume"])} for x in data]

def htf_constant():
    return mt5.TIMEFRAME_H1 if str(cfg["htf_timeframe"]).upper() == "H1" else mt5.TIMEFRAME_H4

def pip_size(symbol, info):
    override = cfg.get("pip_size_overrides", {}).get(symbol)
    if override:
        return float(override)
    return float(info.point) * (10 if info.digits in (3, 5) else 1)

def zone_touch(bars, zone):
    for b in reversed(bars):
        if b["low"] <= zone["high"] and b["high"] >= zone["low"]:
            return b["time"]
    return None

def sr_zones(bars):
    look = bars[-int(cfg["htf_lookback"]):]
    if len(look) < 10:
        return None, None
    lo = min(look, key=lambda b: b["low"])
    hi = max(look, key=lambda b: b["high"])
    return ({"low": lo["low"], "high": lo["high"], "time": lo["time"]},
            {"low": hi["low"], "high": hi["high"], "time": hi["time"]})

def detect_fvg(bars, symbol, info):
    if len(bars) < 3:
        return None
    a, b, c = bars[-3], bars[-2], bars[-1]
    minimum = float(cfg["min_fvg_size_pips"]) * pip_size(symbol, info)
    bull_gap = c["low"] - a["high"]
    if bull_gap >= minimum and b["close"] > b["open"]:
        return {"side": "BUY", "low": a["high"], "high": c["low"],
                "entry": (a["high"] + c["low"]) / 2,
                "origin": a["low"], "third_time": c["time"], "size": bull_gap}
    bear_gap = a["low"] - c["high"]
    if bear_gap >= minimum and b["close"] < b["open"]:
        return {"side": "SELL", "low": c["high"], "high": a["low"],
                "entry": (c["high"] + a["low"]) / 2,
                "origin": a["high"], "third_time": c["time"], "size": bear_gap}
    return None

def swept(bars, side):
    n = int(cfg["sweep_lookback"])
    if len(bars) < n + 2:
        return False
    prev, last = bars[-n-1:-1], bars[-1]
    if side == "BUY":
        level = min(x["low"] for x in prev)
        return last["low"] < level and last["close"] > level
    level = max(x["high"] for x in prev)
    return last["high"] > level and last["close"] < level

def norm_price(price, info):
    tick = float(info.trade_tick_size or info.point)
    return round(round(float(price) / tick) * tick, info.digits)

def volume_for_risk(symbol, side, entry, stop, info, account):
    risk_cash = float(account.equity) * float(cfg["risk_percent"]) / 100.0
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    loss = mt5.order_calc_profit(order_type, symbol, 1.0, entry, stop)
    if loss is None or not loss:
        return 0.0
    raw = risk_cash / abs(float(loss))
    step, minimum, maximum = float(info.volume_step), float(info.volume_min), float(info.volume_max)
    vol = math.floor(min(raw, maximum) / step) * step
    vol = round(vol, 8)
    return vol if vol >= minimum else 0.0

def telegram(message):
    token, chat = cfg.get("telegram_bot_token"), cfg.get("telegram_chat_id")
    if not token or not chat:
        return
    data = json.dumps({"chat_id": chat, "text": message}).encode()
    req = urllib.request.Request("https://api.telegram.org/bot" + token + "/sendMessage",
        data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as exc:
        log.warning("Telegram send failed: %s", exc)

def publish_github(payload):
    token = cfg.get("github_token", "").strip()
    repo = cfg.get("github_repo", "").strip()
    if not token or not repo:
        return False, "GitHub publishing is not configured"
    url = "https://api.github.com/repos/" + repo + "/contents/state.json"
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "TradesByJay-MT5"}
    try:
        get = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(get, timeout=12) as response:
            current = json.loads(response.read().decode())
        raw = json.dumps(payload, separators=(",", ":"), default=str).encode()
        body = {"message": "Update TradesByJay live MT5 market feed",
                "content": base64.b64encode(raw).decode(), "sha": current["sha"]}
        put = urllib.request.Request(url, data=json.dumps(body).encode(),
            headers={**headers, "Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(put, timeout=20) as response:
            response.read()
        return True, None
    except Exception as exc:
        return False, str(exc)

def account_snapshot():
    a = mt5.account_info()
    if not a:
        return None
    return {"login": a.login, "server": a.server, "currency": a.currency,
            "balance": round(a.balance, 2), "equity": round(a.equity, 2),
            "trade_mode": int(a.trade_mode)}

def open_positions():
    out = []
    for p in (mt5.positions_get() or []):
        if p.magic != int(cfg["magic"]):
            continue
        tick = mt5.symbol_info_tick(p.symbol)
        price = (tick.bid if p.type == mt5.POSITION_TYPE_BUY else tick.ask) if tick else None
        out.append({"ticket": p.ticket, "symbol": p.symbol,
            "side": "BUY" if p.type == mt5.POSITION_TYPE_BUY else "SELL",
            "entry": p.price_open, "stop": p.sl, "target": p.tp,
            "volume": p.volume, "floating_pl": round(p.profit, 2),
            "current_price": price, "status": "OPEN"})
    return out

def pending_orders():
    return [o for o in (mt5.orders_get() or []) if o.magic == int(cfg["magic"])]

def manage_pending(symbol, bars):
    last = bars[-1]
    active_orders = pending_orders()
    active_tickets = {int(o.ticket) for o in active_orders}
    stale = [ticket for ticket in pending_meta if ticket not in active_tickets]
    for ticket in stale:
        pending_meta.pop(ticket, None)
    if stale:
        save_pending_meta()
    for order in active_orders:
        if order.symbol != symbol:
            continue
        meta = pending_meta.get(order.ticket)
        if not meta:
            continue
        invalid = last["close"] < meta["low"] if meta["side"] == "BUY" else last["close"] > meta["high"]
        if invalid:
            result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": order.ticket,
                                     "symbol": symbol, "magic": int(cfg["magic"]),
                                     "comment": "TBJ FVG invalidated"})
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                pending_meta.pop(order.ticket, None)
                save_pending_meta()
                log.info("%s pending FVG order cancelled after invalidation", symbol)

def place_limit(symbol, fvg, info, tick, account):
    entry = norm_price(fvg["entry"], info)
    spread = float(tick.ask - tick.bid)
    if fvg["side"] == "BUY":
        order_type = mt5.ORDER_TYPE_BUY_LIMIT
        stop = norm_price(fvg["origin"] - spread, info)
        distance = entry - stop
        if entry >= tick.ask or distance <= 0:
            return "SKIPPED — midpoint is not below current ask", None
        target = norm_price(entry + distance * float(cfg["risk_reward"]), info)
    else:
        order_type = mt5.ORDER_TYPE_SELL_LIMIT
        stop = norm_price(fvg["origin"] + spread, info)
        distance = stop - entry
        if entry <= tick.bid or distance <= 0:
            return "SKIPPED — midpoint is not above current bid", None
        target = norm_price(entry - distance * float(cfg["risk_reward"]), info)
    levels = {"entry": entry, "stop": stop, "target": target, "volume": None}
    if distance < float(info.trade_stops_level) * info.point:
        return "SKIPPED — stop distance below broker minimum", levels
    if not cfg.get("auto_execute", False):
        return "ALERT ONLY — automatic execution is disabled", levels
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO and cfg.get("demo_only", True):
        return "BLOCKED — demo-only guard", levels
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO and not cfg.get("allow_live_trading", False):
        return "BLOCKED — live execution not explicitly enabled", levels
    if len([p for p in (mt5.positions_get() or []) if p.magic == int(cfg["magic"])]) >= int(cfg["max_positions"]):
        return "BLOCKED — max bot positions reached", levels
    if any(o.symbol == symbol for o in pending_orders()):
        return "BLOCKED — pending bot order already exists", levels
    if float(cfg.get("max_spread_points", 0)) > 0 and spread / info.point > float(cfg["max_spread_points"]):
        return "BLOCKED — spread too wide", levels
    volume = volume_for_risk(symbol, fvg["side"], entry, stop, info, account)
    if volume <= 0:
        return "SKIPPED — minimum lot would exceed risk budget", levels
    levels["volume"] = volume
    request = {"action": mt5.TRADE_ACTION_PENDING, "symbol": symbol, "volume": volume,
        "type": order_type, "price": entry, "sl": stop, "tp": target,
        "deviation": int(cfg["deviation_points"]), "magic": int(cfg["magic"]),
        "comment": "TBJ SR FVG M5", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN}
    result = mt5.order_send(request)
    if not result or result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
        return "ORDER REJECTED — " + str(getattr(result, "comment", mt5.last_error())), levels
    pending_meta[int(result.order)] = {"side": fvg["side"], "low": fvg["low"], "high": fvg["high"], "symbol": symbol}
    save_pending_meta()
    levels["ticket"] = int(result.order)
    return "LIMIT ORDER PLACED", levels

def scan_symbol(symbol):
    info = mt5.symbol_info(symbol)
    if not info or not mt5.symbol_select(symbol, True):
        return {"symbol": symbol, "stage": "OFFLINE", "note": "Symbol unavailable"}, None
    htf = rates(symbol, htf_constant(), int(cfg["htf_lookback"]) + 10)
    m5 = rates(symbol, mt5.TIMEFRAME_M5, int(cfg["m5_bars"]))
    tick = mt5.symbol_info_tick(symbol)
    if len(htf) < 10 or len(m5) < 3 or not tick:
        return {"symbol": symbol, "stage": "WAIT", "note": "Waiting for broker candles/tick"}, None
    support, resistance = sr_zones(htf)
    manage_pending(symbol, m5)
    touched_support = zone_touch(m5[-20:], support)
    touched_resistance = zone_touch(m5[-20:], resistance)
    fvg = detect_fvg(m5, symbol, info)
    market = {"symbol": symbol, "price": tick.bid, "stage": "WATCH", "side": "WAIT",
        "strategy": "HTF SUPPORT/RESISTANCE + M5 FVG",
        "support_zone": support, "resistance_zone": resistance,
        "market_speed": "LIVE", "data_ready": True, "timeframes": {cfg["htf_timeframe"]: True, "M5": True},
        "candles": m5[-48:], "m5_candle_time": m5[-1]["time"], "entry_timeframe": "M5", "updated_at": now_iso(),
        "note": "Waiting for price to interact with a higher-timeframe zone and a confirmed M5 FVG.",
        "conditions": {"HTF support/resistance available": True,
                       "Price interacted with HTF zone": bool(touched_support or touched_resistance),
                       "M5 displacement FVG": False, "Optional liquidity sweep": not cfg["require_liquidity_sweep"]}}
    if fvg:
        touch = touched_support if fvg["side"] == "BUY" else touched_resistance
        correct_zone = touch is not None and touch <= fvg["third_time"]
        sweep_ok = not cfg["require_liquidity_sweep"] or swept(m5, fvg["side"])
        market["side"] = fvg["side"]
        market["conditions"]["M5 displacement FVG"] = True
        market["conditions"]["Optional liquidity sweep"] = sweep_ok
        if correct_zone and sweep_ok:
            key = (symbol, fvg["side"], fvg["third_time"])
            market.update({"stage": "READY", "entry": fvg["entry"], "stop": None, "target": None,
                "fvg": {"low": fvg["low"], "high": fvg["high"], "midpoint": fvg["entry"], "size": fvg["size"]},
                "invalidation_level": fvg["low"] if fvg["side"] == "BUY" else fvg["high"],
                "next_required": "FVG midpoint retracement / limit fill",
                "note": "Confirmed " + fvg["side"] + " M5 FVG after HTF zone interaction."})
            if key not in seen_fvg:
                seen_fvg.add(key)
                save_seen_fvg()
                account = mt5.account_info()
                execution, order = place_limit(symbol, fvg, info, tick, account)
                signal = {"signal_id": "|".join(map(str, key)), "symbol": symbol, "side": fvg["side"],
                    "entry": order["entry"] if order else fvg["entry"],
                    "stop": order["stop"] if order else None, "target": order["target"] if order else None,
                    "status": "PENDING" if execution == "LIMIT ORDER PLACED" else "SETUP DETECTED",
                    "execution": execution, "volume": order["volume"] if order else None,
                    "sent_at": time.time(), "created_at": now_iso(), "fvg": market["fvg"]}
                telegram("TradesByJay setup: " + fvg["side"] + " " + symbol +
                         " | FVG midpoint " + str(round(fvg["entry"], info.digits)) +
                         " | " + execution)
                return market, signal
    return market, None

def scan_once():
    markets, fresh = [], []
    for symbol in cfg["symbols"]:
        try:
            market, signal = scan_symbol(symbol)
            markets.append(market)
            if signal:
                fresh.append(signal)
        except Exception as exc:
            log.exception("Scan failed for %s", symbol)
            markets.append({"symbol": symbol, "stage": "ERROR", "note": str(exc)})
    with lock:
        prior = state.get("signals", [])
        state["signals"] = (fresh + prior)[:100]
        state["markets"] = markets
        state["positions"] = open_positions()
        state["updated"] = now_iso()
        state["online"] = True
        state["status"] = "LIVE" if state["bot"]["connected"] else "SCANNING"
        state["bot"]["last_scan"] = state["updated"]
    return fresh

def publish_loop():
    global last_publish
    while True:
        time.sleep(1)
        if not state["bot"]["running"] or not state["bot"]["connected"] or not state.get("updated"):
            continue
        if time.time() - last_publish < int(cfg["publish_seconds"]):
            continue
        with lock:
            payload = {k: v for k, v in state.items() if k != "bot"}
            payload["online"] = bool(state.get("online")) and bool(state.get("updated"))
            payload["updated"] = state.get("updated")
        ok, error = publish_github(payload)
        last_publish = time.time()
        with lock:
            state["bot"]["last_publish"] = now_iso() if ok else state["bot"]["last_publish"]
            if error:
                state["bot"]["errors"] = [error] + state["bot"]["errors"][:9]
        if ok:
            log.info("Published live feed to GitHub Pages state.json")
        else:
            log.warning("Feed publish failed: %s", error)

def connect_mt5():
    if mt5 is None:
        raise RuntimeError("MetaTrader5 package missing. Install it in a supported Windows Python environment.")
    if not mt5.initialize():
        raise RuntimeError("MT5 initialize failed: " + str(mt5.last_error()))
    account = mt5.account_info()
    if not account:
        raise RuntimeError("MT5 terminal has no logged-in account.")
    if cfg.get("demo_only", True) and account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        mt5.shutdown()
        raise RuntimeError("Demo-only mode is enabled, but this is not a demo account.")
    with lock:
        state["bot"]["connected"] = True
        state["bot"]["account"] = account_snapshot()
        state["bot"]["mode"] = "DEMO AUTO" if cfg.get("auto_execute") else "SCAN ONLY"
    log.info("Connected to MT5 account %s / %s", account.login, account.server)

def bot_loop():
    while True:
        if not state["bot"]["connected"]:
            try:
                connect_mt5()
            except Exception as exc:
                with lock:
                    state["bot"]["errors"] = [str(exc)] + state["bot"]["errors"][:9]
                    state["bot"]["connected"] = False
                log.warning("MT5 not ready; retrying in 10 seconds: %s", exc)
                time.sleep(10)
                continue
        if state["bot"]["running"]:
            try:
                scan_once()
            except Exception as exc:
                log.exception("Scanner error")
                with lock:
                    state["bot"]["errors"] = [str(exc)] + state["bot"]["errors"][:9]
        time.sleep(max(3, int(cfg["scan_seconds"])))

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return
    def send_headers(self):
        self.send_header("Content-Type", "application/json")
        origin = self.headers.get("Origin", "")
        allowed = {"https://lamar-progst.github.io", "http://127.0.0.1", "http://localhost"}
        if origin in allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Cache-Control", "no-store")
    def reply(self, code, data):
        raw = json.dumps(data, default=str).encode()
        self.send_response(code); self.send_headers(); self.end_headers(); self.wfile.write(raw)
    def do_OPTIONS(self):
        self.send_response(204); self.send_headers(); self.end_headers()
    def do_GET(self):
        if self.path != "/status":
            return self.reply(404, {"error": "not found"})
        with lock:
            bot = dict(state["bot"])
            snap = {**state, "bot": bot, "running": bot["running"],
                    "connected": bot["connected"], "mode": bot["mode"],
                    "account": bot.get("account"), "last_scan": bot.get("last_scan"),
                    "rejections": [], "errors": bot.get("errors", []),
                    "positions": open_positions() if mt5 else []}
        return self.reply(200, snap)
    def do_POST(self):
        if cfg["api_token"] == "CHANGE_THIS_LOCAL_TOKEN":
            return self.reply(403, {"error": "Set api_token in bot/config.json first"})
        if self.headers.get("Authorization", "") != "Bearer " + str(cfg["api_token"]):
            return self.reply(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        action = body.get("action")
        if action == "start":
            with lock: state["bot"]["running"] = True
        elif action == "pause":
            with lock: state["bot"]["running"] = False
        elif action == "scan":
            if not state["bot"]["connected"]:
                return self.reply(503, {"error": "MT5 not connected"})
            scan_once()
        else:
            return self.reply(400, {"error": "action must be start, pause, or scan"})
        return self.reply(200, {"ok": True, "running": state["bot"]["running"]})

def serve_api():
    server = ThreadingHTTPServer((cfg["api_host"], int(cfg["api_port"])), Handler)
    log.info("Local dashboard API listening at http://%s:%s/status", cfg["api_host"], cfg["api_port"])
    server.serve_forever()

if __name__ == "__main__":
    if str(cfg["htf_timeframe"]).upper() not in ("H1", "H4"):
        raise SystemExit("htf_timeframe must be H1 or H4")
    if not 0 < float(cfg["risk_percent"]) <= 1:
        raise SystemExit("risk_percent must be greater than 0 and at most 1")
    if float(cfg["risk_reward"]) <= 0:
        raise SystemExit("risk_reward must be greater than zero")
    threading.Thread(target=serve_api, daemon=True).start()
    threading.Thread(target=publish_loop, daemon=True).start()
    bot_loop()
