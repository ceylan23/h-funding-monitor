"""
H 做多信号监控 (GitHub Actions 版)
Gate(主) + Bitget/OKX(辅助) + RSI + 止损止盈
每10分钟检查, 有信号推微信
"""
import requests
import json
import time
import os
from datetime import datetime, timezone, timedelta

WX_TOKEN = os.environ.get("WX_TOKEN", "AT_wxNW2KgGrI3o7XJJfYp9Dtx6RQN6JMwB")
WX_TOPIC = os.environ.get("WX_TOPIC", "42661")

SIGNALS = {
    # 信号0: 费率深跌后回升趋势 (核心信号, 朋友方案)
    # 费率从-2%回升到-1.3% = 空头在平仓 = 做多
    "trend_recovery": {
        "deep_threshold": -0.005,   # 费率曾低于 -0.5% 才算深跌
        "recovery_pct": 0.003,      # 回升幅度 > 0.3% (比如-2%→-1.7%)
        "cooldown": 1800,           # 30分钟冷却
    },
    "recovery": {"entry": -0.001, "recovery": -0.0003, "cooldown": 3600},
    "extreme_stable": {"rate_threshold": -0.003, "cooldown": 1800},
    "flip": {"min_negative": 5, "cooldown": 3600},
}

STATE_FILE = os.path.join(os.environ.get("TEMP", os.environ.get("TMP", "/tmp")), "funding_state.json")
sess = requests.Session()
sess.headers["User-Agent"] = "Mozilla/5.0"

def gate():
    try:
        r = sess.get("https://api.gateio.ws/api/v4/futures/usdt/contracts/H_USDT", timeout=15)
        d = r.json()
        return {"rate": float(d["funding_rate"]), "price": float(d["mark_price"])}
    except Exception as e:
        print(f"Gate error: {e}")
    return None

def gate_klines(limit=50):
    try:
        r = sess.get("https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
                     params={"contract": "H_USDT", "interval": "1h", "limit": str(limit)}, timeout=15)
        d = r.json()
        if isinstance(d, list):
            return [{"close": float(x["c"]), "high": float(x["h"]), "low": float(x["l"]),
                     "time": int(x["t"])} for x in d]
    except: pass
    return []

def bitget():
    try:
        r = sess.get("https://api.bitget.com/api/v2/mix/market/current-fund-rate",
                     params={"symbol": "HUSDT", "productType": "USDT-FUTURES"}, timeout=15)
        d = r.json()
        if d.get("code") == "00000" and d.get("data"):
            return float(d["data"][0]["fundingRate"])
    except: pass
    return None

def okx():
    try:
        r = sess.get("https://www.okx.com/api/v5/public/funding-rate",
                     params={"instId": "H-USDT-SWAP"}, timeout=15)
        d = r.json()
        if d.get("code") == "0" and d.get("data"):
            return float(d["data"][0]["fundingRate"])
    except: pass
    return None

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains = []; losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0)); losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0: return 100
    return 100 - (100 / (1 + avg_gain / avg_loss))

def analyze_price(candles, price):
    if not candles: return {}
    recent = candles[-24:] if len(candles) >= 24 else candles
    closes = [c["close"] for c in candles]
    rsi = calc_rsi(closes)
    lows = sorted([c["low"] for c in recent])
    highs = sorted([c["high"] for c in recent], reverse=True)
    support = (lows[0] + lows[1]) / 2 if len(lows) >= 2 else None
    resistance = (highs[0] + highs[1]) / 2 if len(highs) >= 2 else None
    h24 = max(c["high"] for c in recent); l24 = min(c["low"] for c in recent)
    rng = h24 - l24 if h24 != l24 else 1
    return {"rsi": rsi, "support": support, "resistance": resistance,
            "high24": h24, "low24": l24, "position": (price - l24) / rng}

def calc_sl(entry, support, pct=0.05):
    sl_s = support * 0.98 if support and support < entry else None
    sl_f = entry * (1 - pct)
    return max(sl_s, sl_f) if sl_s else sl_f

def calc_tp(entry, resistance, sl, rr=2.0):
    tp_r = resistance if resistance and resistance > entry else None
    tp_rr = entry + (entry - sl) * rr
    return min(tp_r, tp_rr) if tp_r else tp_rr

def send_wx(title, content):
    try:
        r = requests.post("https://wxpusher.zjiecode.com/api/send/message", json={
            "appToken": WX_TOKEN, "content": content, "summary": title[:100],
            "contentType": 2, "topicIds": [int(WX_TOPIC)]}, timeout=10)
        ok = r.json().get("code") == 1000
        print(f"Push: {'OK' if ok else 'FAIL'}")
        return ok
    except Exception as e:
        print(f"Push error: {e}")
        return False

def load_state():
    try:
        with open(STATE_FILE, "r") as f:
            s = json.load(f)
            for k in ["min_rate", "was_deep_negative", "consecutive_negative"]:
                if isinstance(s.get(k), dict): s[k] = 0 if k != "was_deep_negative" else False
            for k in ["prev_rates", "prev_prices"]:
                if not isinstance(s.get(k), list): s[k] = []
            return s
    except:
        return {"min_rate": 0, "was_deep_negative": False, "consecutive_negative": 0,
                "prev_rates": [], "prev_prices": [], "last_alerts": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f)

def check_signal(state, gt_rate, gt_price, bg_rate, ox_rate, pctx):
    now = time.time()
    last = state.get("last_alerts", {})
    mn = state.get("min_rate", 0)
    wd = state.get("was_deep_negative", False)
    cn = state.get("consecutive_negative", 0)
    pr = state.get("prev_rates", [])
    pp = state.get("prev_prices", [])
    if isinstance(cn, dict): cn = 0
    if isinstance(mn, dict): mn = 0

    if gt_rate < mn: mn = gt_rate
    if gt_rate < SIGNALS["recovery"]["entry"]: wd = True
    if gt_rate < 0: cn += 1
    else: cn = 0
    pr.insert(0, gt_rate); pr = pr[:100]
    pp.insert(0, gt_price); pp = pp[:100]

    price_up = len(pp) >= 3 and pp[0] > pp[2] * 1.005
    price_stable = False
    if len(pp) >= 20:
        h = max(pp[:20]); l = min(pp[:20])
        price_stable = (h - l) / h < 0.02

    rsi = pctx.get("rsi")
    rsi_ok = rsi is not None and rsi < 55
    signal = None

    # ===== 信号0: 费率深跌后回升趋势 (核心! 朋友方案) =====
    # 费率从-2%→-1.3% = 空头在平仓 = 做多信号
    # 不需要回到0, 只要趋势在回升就触发
    r = SIGNALS["trend_recovery"]
    if mn < r["deep_threshold"] and len(pr) >= 3:
        # 从最低点回升了多少
        recovery_from_low = gt_rate - mn
        # 最近3个tick的趋势: 费率在持续回升
        recent_trend = len(pr) >= 3 and pr[0] > pr[2]  # 最新 > 3轮前

        if recovery_from_low > r["recovery_pct"] and recent_trend:
            if now - last.get("trend_recovery", 0) > r["cooldown"]:
                # 朋友方案: 不设止损止盈, 只提醒入场, 出场自己判断
                signal = {
                    "tag": "FRIEND_STRATEGY",
                    "name": "🔥 朋友方案: 费率回升趋势(重点信号)",
                    "detail": f"Gate费率 {mn*100:.2f}% → {gt_rate*100:.2f}% (回升 {recovery_from_low*100:.2f}%)",
                    "reason": f"费率从深负回升 = 空头在买入平仓 = 真实买盘涌入",
                    "strength": "强" if recovery_from_low > 0.005 else "中",
                    "action": "做多",
                    "platform": "Gate",
                    "entry": gt_price,
                    "earn": f"每8小时收{abs(gt_rate)*100:.2f}%",
                    "recovery_pct": recovery_from_low * 100,
                    "note": "不设止损止盈, 拿住不动, 让利润跑",
                }
                last["trend_recovery"] = now

    # ===== 信号1: 费率回到接近0 (确认信号) =====
    r = SIGNALS["recovery"]
    if wd and gt_rate >= r["recovery"] and mn < r["entry"]:
        if now - last.get("recovery", 0) > r["cooldown"]:
            if price_up:
                confirm = []
                if bg_rate is not None and bg_rate >= r["recovery"]: confirm.append("Bitget")
                if ox_rate is not None and ox_rate >= r["recovery"]: confirm.append("OKX")
                strength = "强" if confirm and price_up and rsi_ok else "中"
                sl = calc_sl(gt_price, pctx.get("support"))
                tp = calc_tp(gt_price, pctx.get("resistance"), sl)
                signal = {"name": "🟢 做多: 空头平仓+价格涨",
                          "detail": f"Gate费率 {mn*100:.4f}%→{gt_rate*100:.4f}%",
                          "reason": f"空头平仓+价格涨" + (f"+RSI={rsi:.0f}" if rsi else ""),
                          "strength": strength, "action": "做多", "platform": "Gate",
                          "entry": gt_price, "sl": sl, "tp": tp,
                          "rr": (tp-gt_price)/(gt_price-sl) if gt_price>sl else 0,
                          "earn": f"每8小时收{abs(gt_rate)*100:.2f}%"}
                last["recovery"] = now; wd = False

    r = SIGNALS["extreme_stable"]
    if gt_rate < r["rate_threshold"] and price_stable:
        if now - last.get("extreme_stable", 0) > r["cooldown"]:
            if bg_rate and bg_rate < r["rate_threshold"] and ox_rate and ox_rate < r["rate_threshold"]:
                daily = abs(gt_rate) * 100 * 3
                sl = calc_sl(gt_price, pctx.get("support"), 0.08)
                tp = calc_tp(gt_price, pctx.get("resistance"), sl)
                signal = {"name": "💰 吃费率: 三家极端负+价格稳",
                          "detail": f"Gate:{gt_rate*100:+.3f}% Bitget:{bg_rate*100:+.3f}% OKX:{ox_rate*100:+.3f}%",
                          "reason": f"三家极端负+价格企稳" + (f"+RSI={rsi:.0f}" if rsi else ""),
                          "strength": "中", "action": "做多吃费率", "platform": "Gate(费率最高)",
                          "entry": gt_price, "sl": sl, "tp": tp,
                          "rr": (tp-gt_price)/(gt_price-sl) if gt_price>sl else 0,
                          "earn": f"每天约{daily:.1f}%"}
                last["extreme_stable"] = now

    r = SIGNALS["flip"]
    if cn >= r["min_negative"] and gt_rate > 0:
        if now - last.get("flip", 0) > r["cooldown"]:
            if price_up:
                confirm = []
                if bg_rate and bg_rate > 0: confirm.append("Bitget")
                if ox_rate and ox_rate > 0: confirm.append("OKX")
                sl = calc_sl(gt_price, pctx.get("support"))
                tp = calc_tp(gt_price, pctx.get("resistance"), sl)
                signal = {"name": "🔄 做多: 趋势反转",
                          "detail": f"Gate连续{cn}周期负费率后转正({gt_rate*100:+.4f}%)",
                          "reason": f"空头撤退+价格涨" + (f"+RSI={rsi:.0f}" if rsi else ""),
                          "strength": "强" if confirm else "中", "action": "做多", "platform": "Gate",
                          "entry": gt_price, "sl": sl, "tp": tp,
                          "rr": (tp-gt_price)/(gt_price-sl) if gt_price>sl else 0}
                last["flip"] = now

    state.update({"min_rate": mn, "was_deep_negative": wd, "consecutive_negative": cn,
                  "prev_rates": pr, "prev_prices": pp, "last_alerts": last})
    return signal

def main():
    tz8 = timezone(timedelta(hours=8))
    print(f"[{datetime.now(tz8).strftime('%Y-%m-%d %H:%M:%S')}] 检查中...")

    gt = gate()
    if not gt: print("Gate API 失败"); return

    bg = bitget(); ox = okx()
    candles = gate_klines(50)
    pctx = analyze_price(candles, gt["price"])

    gt_rate = gt["rate"]; gt_price = gt["price"]
    rsi = pctx.get("rsi")

    print(f"Gate:   {gt_rate*100:+.4f}% ${gt_price:,.4f}")
    if bg: print(f"Bitget: {bg*100:+.4f}%")
    if ox: print(f"OKX:    {ox*100:+.4f}%")
    if rsi: print(f"RSI:    {rsi:.1f}")
    if pctx.get("support"): print(f"支撑:   ${pctx['support']:,.4f}")
    if pctx.get("resistance"): print(f"阻力:   ${pctx['resistance']:,.4f}")

    state = load_state()
    signal = check_signal(state, gt_rate, gt_price, bg, ox, pctx)

    if signal:
        is_friend = signal.get("tag") == "FRIEND_STRATEGY"

        if is_friend:
            # 朋友方案: 重点标出
            print(f"\n{'*'*60}")
            print(f"***  {signal['name']}  ***")
            print(f"***  {signal['detail']}  ***")
            print(f"***  {signal['reason']}  ***")
            print(f"***  强度:{signal['strength']} | 平台:{signal['platform']} | 建议:{signal['action']}  ***")
            if signal.get("entry"): print(f"***  入场:${signal['entry']:,.4f}  ***")
            if signal.get("earn"): print(f"***  {signal['earn']}  ***")
            print(f"***  策略: 不设止损止盈, 拿住不动  ***")
            print(f"{'*'*60}")

            earn_h = f"<p>💰 {signal['earn']}</p>" if signal.get("earn") else ""
            html = (
                f"<h1 style='color:red'>🔥 {signal['name']}</h1>"
                f"<h2>Gate费率 {signal.get('recovery_pct', 0):.2f}% 回升中</h2>"
                f"<p>📊 <b>{signal['detail']}</b></p>"
                f"<p>💡 <b>{signal['reason']}</b></p>"
                f"<p>🎯 强度:<b>{signal['strength']}</b> 平台:<b>{signal['platform']}</b></p>"
                f"<h3>📈 入场:<b>${signal.get('entry', gt_price):,.4f}</b></h3>"
                f"{earn_h}"
                f"<p>⚠️ <b>不设止损止盈, 拿住不动, 让利润跑</b></p>"
                f"<p>📉 Gate:{gt_rate*100:+.3f}% | Bitget:{bg*100:+.3f}% | OKX:{ox*100:+.3f}%</p>"
                f"<p>⏰ {datetime.now(tz8).strftime('%Y-%m-%d %H:%M:%S')}</p>"
            )
            send_wx(f"🔥 朋友方案触发! Gate费率回升{signal.get('recovery_pct',0):.2f}%", html)
        else:
            # 其他信号: 普通显示
            print(f"\n{'='*50}")
            print(f"信号: {signal['name']}")
            print(f"详情: {signal['detail']}")
            print(f"原因: {signal['reason']}")
            print(f"强度: {signal['strength']} | 平台: {signal['platform']} | 建议: {signal['action']}")
            if signal.get("entry"): print(f"入场: ${signal['entry']:,.4f}")
            if signal.get("sl"): print(f"止损: ${signal['sl']:,.4f}")
            if signal.get("tp"): print(f"止盈: ${signal['tp']:,.4f}")
            if signal.get("rr"): print(f"盈亏比: {signal['rr']:.1f}:1")
            if signal.get("earn"): print(f"费率收益: {signal['earn']}")
            print(f"{'='*50}")

            sl_h = f"<p>🛑 止损:<b>${signal['sl']:,.4f}</b></p>" if signal.get("sl") else ""
            tp_h = f"<p>🎯 止盈:<b>${signal['tp']:,.4f}</b></p>" if signal.get("tp") else ""
            rr_h = f"<p>📊 盈亏比:<b>{signal['rr']:.1f}:1</b></p>" if signal.get("rr") else ""
            earn_h = f"<p>💰 {signal['earn']}</p>" if signal.get("earn") else ""
            html = (
                f"<h2>{signal['name']}</h2>"
                f"<p>📊 <b>{signal['detail']}</b></p>"
                f"<p>💡 {signal['reason']}</p>"
                f"<p>🎯 强度:<b>{signal['strength']}</b> 平台:<b>{signal['platform']}</b> 建议:<b>{signal['action']}</b></p>"
                f"<p>📈 入场:<b>${signal.get('entry', gt_price):,.4f}</b></p>"
                f"{sl_h}{tp_h}{rr_h}{earn_h}"
                f"<p>📉 Gate:{gt_rate*100:+.3f}% | Bitget:{bg*100:+.3f}% | OKX:{ox*100:+.3f}%</p>"
                f"<p>⏰ {datetime.now(tz8).strftime('%Y-%m-%d %H:%M:%S')}</p>"
            )
            send_wx(f"🟢 H做多信号: {signal['action']}", html)
    else:
        print("\n无做多信号")
        mn = state.get("min_rate", 0)
        if isinstance(mn, (int, float)) and mn < -0.0005:
            print(f"历史最低费率: {mn*100:+.4f}%")
        print("条件: 费率从<-0.1%回升到>-0.03% + 价格涨 + RSI<55")

    save_state(state)
    print("完成")

if __name__ == "__main__":
    main()
