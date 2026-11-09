#!/usr/bin/env python3
"""S.V.P. APA MT5 bot — closed-candle scanner with demo-first execution."""
from __future__ import annotations
import json, logging, math, os, threading, time
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
    "symbols": ["XAUUSD", "XAGUSD", "Volatility 75 Index", "Volatility 50 Index", "Step Index"],
    "scan_seconds": 15, "risk_percent": 1.0, "max_portfolio_risk_percent": 3.0,
    "max_positions": 3, "magic": 611090, "deviation_points": 30,
    "auto_execute": False, "demo_only": True, "allow_live_trading": False,
    "api_host": "127.0.0.1", "api_port": 8765, "api_token": "CHANGE_THIS_LOCAL_TOKEN",
    "max_spread_points": 0, "cooldown_minutes": 30
}
cfg = {**DEFAULTS}
if CFG_PATH.exists():
    cfg.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.StreamHandler(), logging.FileHandler(ROOT / "svp_bot.log", encoding="utf-8")])
log = logging.getLogger("svp-apa")
state = {"running": False, "connected": False, "account": None, "mode": "SAFE / SCAN ONLY",
         "last_scan": None, "signals": [], "positions": [], "rejections": [], "errors": [], "symbols": []}
lock = threading.RLock()
last_traded = {}

def rates(symbol, timeframe, count=160):
    data = mt5.copy_rates_from_pos(symbol, timeframe, 1, count)  # start at 1: closed candles only
    if data is None or len(data) < 35:
        return []
    return [{"time": int(x["time"]), "open": float(x["open"]), "high": float(x["high"]),
             "low": float(x["low"]), "close": float(x["close"]), "tick_volume": int(x["tick_volume"])} for x in data]

def pivots(bars, left=2, right=2):
    out = []
    for i in range(left, len(bars)-right):
        h, l = bars[i]["high"], bars[i]["low"]
        if all(h > bars[j]["high"] for j in range(i-left, i+right+1) if j != i):
            out.append(("H", i, h))
        if all(l < bars[j]["low"] for j in range(i-left, i+right+1) if j != i):
            out.append(("L", i, l))
    return out

def ema(values, period):
    if len(values) < period: return None
    k = 2.0/(period+1); value = sum(values[:period])/period
    for v in values[period:]: value = v*k + value*(1-k)
    return value

def h4_bias(bars):
    ps = pivots(bars)
    highs = [p[2] for p in ps if p[0] == "H"][-2:]
    lows = [p[2] for p in ps if p[0] == "L"][-2:]
    e = ema([b["close"] for b in bars], 50)
    last = bars[-1]["close"]
    if len(highs)==2 and len(lows)==2 and highs[-1]>highs[-2] and lows[-1]>lows[-2] and e and last>e:
        return "BUY"
    if len(highs)==2 and len(lows)==2 and highs[-1]<highs[-2] and lows[-1]<lows[-2] and e and last<e:
        return "SELL"
    return "NEUTRAL"

def h1_context(bars, side):
    ps = pivots(bars)
    highs = [p[2] for p in ps if p[0] == "H"]
    lows = [p[2] for p in ps if p[0] == "L"]
    last = bars[-1]
    if not highs or not lows: return None, "H1 structure not clear"
    # Sweep is supportive, never mandatory: close back inside the prior swing.
    sweep = (last["low"] < lows[-1] and last["close"] > lows[-1]) if side=="BUY" else (last["high"] > highs[-1] and last["close"] < highs[-1])
    level = lows[-1] if side=="BUY" else highs[-1]
    # H1 location: near the latest relevant swing, or a confirmed sweep.
    span = max(last["high"]-last["low"], abs(last["close"])*0.001)
    near = abs(last["close"]-level) <= max(span*4, abs(last["close"])*0.003)
    if not (near or sweep): return None, "Price is not at a useful H1 structure/liquidity area"
    return {"level": level, "sweep": sweep, "opposing_target": highs[-1] if side=="BUY" else lows[-1]}, None

def m15_confirmation(bars, side):
    recent = bars[-13:-1]
    last = bars[-1]
    if len(recent)<8: return False, "Not enough M15 candles"
    if side=="BUY":
        level=max(b["high"] for b in recent[-10:])
        ok=last["close"]>level and last["close"]>last["open"]
    else:
        level=min(b["low"] for b in recent[-10:])
        ok=last["close"]<level and last["close"]<last["open"]
    return ok, ("M15 BOS confirmed on candle close" if ok else "Waiting for a confirmed M15 BOS / break-and-retest")

def m5_trigger(bars, side):
    recent=bars[-8:-1]; last=bars[-1]
    if len(recent)<5: return False, None, "Not enough M5 candles"
    if side=="BUY":
        protected=min(b["low"] for b in bars[-8:])
        ok=last["close"]>max(b["high"] for b in recent[-5:]) and last["close"]>last["open"]
    else:
        protected=max(b["high"] for b in bars[-8:])
        ok=last["close"]<min(b["low"] for b in recent[-5:]) and last["close"]<last["open"]
    return ok, protected, ("M5 precision shift confirmed" if ok else "Waiting for M5 trigger; do not chase")

def volume_for_risk(symbol, side, entry, stop, risk_money):
    info=mt5.symbol_info(symbol)
    if not info: return 0.0
    order_type=mt5.ORDER_TYPE_BUY if side=="BUY" else mt5.ORDER_TYPE_SELL
    loss=mt5.order_calc_profit(order_type, symbol, 1.0, entry, stop)
    if loss is None or abs(loss)<=0: return 0.0
    raw=risk_money/abs(loss)
    step=float(info.volume_step or 0.01); minimum=float(info.volume_min or step); maximum=float(info.volume_max or raw)
    vol=math.floor(min(raw, maximum)/step)*step
    vol=round(vol, 8)
    return vol if vol>=minimum else 0.0

def current_risk_percent():
    account=mt5.account_info()
    if not account or account.equity<=0: return 0.0
    total=0.0
    for p in (mt5.positions_get() or []):
        if p.magic != int(cfg["magic"]): continue
        total += abs(float(p.profit)) if p.profit < 0 else 0.0
    return total/account.equity*100.0

def send_order(symbol, side, entry, stop, target, volume):
    if not cfg.get("auto_execute"): return "SCAN ONLY — automatic execution is disabled"
    account=mt5.account_info()
    if not account: return "Rejected: MT5 account unavailable"
    if cfg.get("demo_only", True) and account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        return "Rejected: demo-only mode; account is not a demo account"
    if account.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO and not cfg.get("allow_live_trading", False):
        return "Rejected: live trading has not been explicitly enabled in config"
    positions=mt5.positions_get() or []
    if len([p for p in positions if p.magic==int(cfg["magic"])]) >= int(cfg["max_positions"]):
        return "Rejected: maximum concurrent bot positions reached"
    if current_risk_percent()+float(cfg["risk_percent"]) > float(cfg["max_portfolio_risk_percent"]):
        return "Rejected: aggregate bot risk cap would be exceeded"
    info=mt5.symbol_info(symbol)
    tick=mt5.symbol_info_tick(symbol)
    if not info or not tick: return "Rejected: symbol/tick unavailable"
    price=float(tick.ask if side=="BUY" else tick.bid)
    if cfg.get("max_spread_points",0)>0 and (tick.ask-tick.bid)/info.point>cfg["max_spread_points"]:
        return "Rejected: spread exceeds configured limit"
    if volume<=0: return "Rejected: calculated volume is below broker minimum"
    request={"action":mt5.TRADE_ACTION_DEAL,"symbol":symbol,"volume":volume,
             "type":mt5.ORDER_TYPE_BUY if side=="BUY" else mt5.ORDER_TYPE_SELL,
             "price":price,"sl":stop,"tp":target,"deviation":int(cfg["deviation_points"]),
             "magic":int(cfg["magic"]),"comment":"SVP APA","type_time":mt5.ORDER_TIME_GTC,
             "type_filling":info.filling_mode}
    result=mt5.order_send(request)
    if result is None: return f"Rejected: order_send returned no result ({mt5.last_error()})"
    if result.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
        return f"Rejected by broker: retcode={result.retcode}, {result.comment}"
    return f"ORDER SENT: {side} {symbol} {volume} lots (ticket {result.order})"

def evaluate(symbol):
    if not mt5.symbol_select(symbol, True): return None, "Broker does not offer this exact symbol name"
    h4=rates(symbol,mt5.TIMEFRAME_H4); h1=rates(symbol,mt5.TIMEFRAME_H1)
    m15=rates(symbol,mt5.TIMEFRAME_M15); m5=rates(symbol,mt5.TIMEFRAME_M5)
    if min(len(h4),len(h1),len(m15),len(m5))<35: return None, "Insufficient closed-candle history"
    side=h4_bias(h4)
    if side=="NEUTRAL": return None, "H4 direction is neutral / structure unclear"
    context,reason=h1_context(h1,side)
    if reason: return None, reason
    ok,reason=m15_confirmation(m15,side)
    if not ok: return None, reason
    ok,protected,reason=m5_trigger(m5,side)
    if not ok: return None, reason
    tick=mt5.symbol_info_tick(symbol); info=mt5.symbol_info(symbol)
    if not tick or not info: return None, "No current tick"
    entry=float(tick.ask if side=="BUY" else tick.bid)
    stop=float(protected)
    target=float(context["opposing_target"])
    if side=="BUY" and not (stop<entry<target): return None, "BUY invalidation/target geometry is not valid"
    if side=="SELL" and not (target<entry<stop): return None, "SELL invalidation/target geometry is not valid"
    risk_money=float(mt5.account_info().equity)*float(cfg["risk_percent"])/100
    volume=volume_for_risk(symbol,side,entry,stop,risk_money)
    if volume<=0: return None, "Risk-based lot size is below broker minimum; trade skipped"
    return {"symbol":symbol,"side":side,"entry":entry,"stop":stop,"target":target,"volume":volume,
            "risk_percent":cfg["risk_percent"],"h1_sweep":context["sweep"],
            "reason":"H4 bias + H1 location + M15 BOS close + M5 trigger",
            "time":datetime.now(timezone.utc).isoformat()}, None

def scan_once():
    found=[]; rejected=[]
    for symbol in cfg["symbols"]:
        try:
            signal,reason=evaluate(symbol)
            if signal:
                key=(symbol,signal["side"])
                prior=last_traded.get(key,0)
                if time.time()-prior < int(cfg["cooldown_minutes"])*60:
                    rejected.append({"symbol":symbol,"reason":"Cooldown active after previous signal/order"})
                    continue
                if any(s["symbol"]==symbol and s["side"]==signal["side"] for s in state["signals"][-20:]):
                    # Avoid repeating an identical setup on every scan; new closed M5 candle resets via time.
                    if state["signals"][-1].get("trigger_bar") == rates(symbol,mt5.TIMEFRAME_M5,2)[-1]["time"]:
                        continue
                bars=rates(symbol,mt5.TIMEFRAME_M5,2)
                signal["trigger_bar"]=bars[-1]["time"] if bars else 0
                if cfg.get("auto_execute"):
                    signal["execution"]=send_order(symbol,signal["side"],signal["entry"],signal["stop"],signal["target"],signal["volume"])
                    if signal["execution"].startswith("ORDER SENT"): last_traded[key]=time.time()
                found.append(signal)
            elif reason:
                rejected.append({"symbol":symbol,"reason":reason})
        except Exception as exc:
            log.exception("Scan failed for %s",symbol); rejected.append({"symbol":symbol,"reason":str(exc)})
    with lock:
        state["signals"]=(found+state["signals"])[:100]
        state["rejections"]=rejected[:50]
        state["last_scan"]=datetime.now(timezone.utc).isoformat()
        state["positions"]=position_snapshot()
        state["symbols"]=list(cfg["symbols"])
    return found

def position_snapshot():
    if not mt5 or not mt5.terminal_info(): return []
    out=[]
    for p in (mt5.positions_get() or []):
        if p.magic==int(cfg["magic"]):
            out.append({"ticket":p.ticket,"symbol":p.symbol,"type":"BUY" if p.type==0 else "SELL",
                        "volume":p.volume,"price_open":p.price_open,"sl":p.sl,"tp":p.tp,"profit":p.profit})
    return out

def connect():
    if mt5 is None: raise RuntimeError("MetaTrader5 package missing. Install with Python 3.14 on Windows.")
    if not mt5.initialize(): raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    account=mt5.account_info()
    if not account: raise RuntimeError("MT5 terminal connected but no account is logged in.")
    if cfg.get("demo_only",True) and account.trade_mode!=mt5.ACCOUNT_TRADE_MODE_DEMO:
        mt5.shutdown(); raise RuntimeError("Demo-only safety is ON, but the logged-in MT5 account is not a demo account.")
    with lock:
        state["connected"]=True
        state["account"]={"login":account.login,"server":account.server,"currency":account.currency,
                          "balance":account.balance,"equity":account.equity,"trade_mode":int(account.trade_mode)}
        state["mode"]="DEMO AUTO" if cfg.get("auto_execute") else "SCAN ONLY"
    log.info("Connected to MT5 account %s on %s",account.login,account.server)

def bot_loop():
    try: connect()
    except Exception as exc:
        with lock: state["errors"].append(str(exc)); state["connected"]=False
        log.error("%s",exc); return
    while True:
        with lock: running=state["running"]
        if running:
            try: scan_once()
            except Exception as exc:
                log.exception("Scan loop error")
                with lock: state["errors"]=[str(exc)]+state["errors"][:19]
        time.sleep(max(5,int(cfg["scan_seconds"])))

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): return
    def headers(self):
        self.send_header("Content-Type","application/json")
        self.send_header("Access-Control-Allow-Origin","*")
        self.send_header("Access-Control-Allow-Headers","Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods","GET, POST, OPTIONS")
        self.send_header("Cache-Control","no-store")
    def reply(self,code,data):
        raw=json.dumps(data,default=str).encode()
        self.send_response(code); self.send_headers(); self.end_headers(); self.wfile.write(raw)
    def do_OPTIONS(self): self.send_response(204); self.send_headers(); self.end_headers()
    def do_GET(self):
        if self.path!="/status": return self.reply(404,{"error":"not found"})
        with lock:
            snap={**state,"positions":position_snapshot()}
        return self.reply(200,snap)
    def do_POST(self):
        if self.headers and self.headers: pass
        token=self.headers_value("Authorization")
        if token!="Bearer "+str(cfg["api_token"]): return self.reply(401,{"error":"unauthorized"})
        length=int(self.headers.get("Content-Length") or 0)
        body=json.loads(self.rfile.read(length) or b"{}")
        action=body.get("action")
        if action=="start":
            with lock: state["running"]=True
        elif action=="pause":
            with lock: state["running"]=False
        elif action=="scan":
            if not state["connected"]: return self.reply(503,{"error":"MT5 not connected"})
            scan_once()
        else: return self.reply(400,{"error":"action must be start, pause, or scan"})
        return self.reply(200,{"ok":True,"running":state["running"]})
    def headers_value(self,name): return self.headers_map.get(name) if hasattr(self,"headers_map") else self.headers.get(name)

def serve_api():
    server=ThreadingHTTPServer((cfg["api_host"],int(cfg["api_port"])),Handler)
    log.info("Local dashboard API listening at http://%s:%s/status",cfg["api_host"],cfg["api_port"])
    server.serve_forever()

if __name__=="__main__":
    threading.Thread(target=serve_api,daemon=True).start()
    bot_loop()
