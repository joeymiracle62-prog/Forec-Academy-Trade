"""
Forex Toolkit - a Telegram bot that needs ONLY a Telegram bot token.

Calculators (position size, pip value, profit/loss, risk:reward) run fully offline.
The currency converter uses the free Frankfurter API (ECB daily reference rates),
which needs no API key.

Setup:
    pip install -r requirements.txt
    export BOT_TOKEN="123456:ABC..."      (Windows: set BOT_TOKEN=123456:ABC...)
    python main.py
"""
import asyncio
import json
import math
import os
import re
import urllib.parse
import urllib.request

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.environ["BOT_TOKEN"]
LOT_UNITS = 100_000  # 1 standard lot

DISCLAIMER = "⚠️ Educational tool only, not financial advice. Forex trading is high risk."

WELCOME = (
    "👋 Forex Toolkit\n\n"
    "Quick calculators for forex traders:\n"
    "• Position size from your risk\n"
    "• Pip value\n"
    "• Profit / loss\n"
    "• Risk:reward ratio\n"
    "• Currency converter\n\n"
    f"{DISCLAIMER}\n\n"
    "What do you need?"
)

MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📏 Position Size (lot size)", callback_data="kind:lot")],
    [InlineKeyboardButton("💲 Pip Value", callback_data="kind:pip")],
    [InlineKeyboardButton("📈 Profit / Loss", callback_data="kind:pnl")],
    [InlineKeyboardButton("⚖️ Risk : Reward", callback_data="kind:rr")],
    [InlineKeyboardButton("💱 Currency Converter", callback_data="kind:conv")],
])


def result_menu(kind):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Calculate again", callback_data=f"kind:{kind}")],
        [InlineKeyboardButton("🏠 Main menu", callback_data="home")],
    ])


# ---------------------------------------------------------------- flows
def usd_base(ud):
    return ud["pair"].startswith("USD")


PAIR = dict(key="pair", kind="pair",
            prompt="Which pair? It must include USD (e.g. EURUSD, GBPUSD, USDJPY).")
PRICE = dict(key="price", kind="pos", when=usd_base,
             prompt=lambda ud: f"What's the current price of {ud['pair']}?")
LOTS = dict(key="lots", kind="pos",
            prompt="Lot size? (1 = standard, 0.1 = mini, 0.01 = micro)")

FLOWS = {
    "lot": [
        PAIR,
        dict(key="balance", kind="pos", prompt="Account balance in USD?"),
        dict(key="risk", kind="pct", prompt="Risk per trade in % (e.g. 1)?"),
        dict(key="sl", kind="pos", prompt="Stop loss in pips?"),
        PRICE,
    ],
    "pip": [PAIR, LOTS, PRICE],
    "pnl": [
        PAIR,
        dict(key="side", kind="choice", choices=["Buy", "Sell"], prompt="Buy or sell?"),
        dict(key="entry", kind="pos", prompt="Entry price?"),
        dict(key="exit", kind="pos", prompt="Exit price?"),
        LOTS,
    ],
    "rr": [
        PAIR,
        dict(key="entry", kind="pos", prompt="Entry price?"),
        dict(key="stop", kind="pos", prompt="Stop loss price?"),
        dict(key="target", kind="pos", prompt="Take profit price?"),
    ],
    "conv": [
        dict(key="amount", kind="pos", prompt="Amount to convert?"),
        dict(key="from", kind="cur", prompt="From which currency? (3-letter code, e.g. USD)"),
        dict(key="to", kind="cur", prompt="To which currency? (e.g. EUR)"),
    ],
}

ERRORS = {
    "pair": "Please send a 6-letter pair that includes USD, like EURUSD or USDJPY.",
    "pos": "Please send a number greater than 0.",
    "pct": "Please send a percentage between 0 and 100 (e.g. 1 or 0.5).",
    "cur": "Please send a 3-letter currency code, like USD or EUR.",
}


def next_step(ud):
    for step in FLOWS[ud["kind"]]:
        if step["key"] not in ud and step.get("when", lambda u: True)(ud):
            return step
    return None


def parse(kind, text):
    if kind == "pair":
        p = re.sub(r"[^A-Za-z]", "", text).upper()
        if len(p) != 6 or "USD" not in (p[:3], p[3:]) or p[:3] == p[3:]:
            raise ValueError
        return p
    if kind == "cur":
        c = text.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", c):
            raise ValueError
        return c
    value = float(text.replace(",", "").strip())
    if not math.isfinite(value) or value <= 0:
        raise ValueError
    if kind == "pct" and value > 100:
        raise ValueError
    return value


# ---------------------------------------------------------------- calculations
def pip_info(pair, price=None):
    """Return (pip_size, pip value in USD for 1 standard lot)."""
    pip_size = 0.01 if "JPY" in pair else 0.0001
    per_lot_quote = LOT_UNITS * pip_size
    if pair.endswith("USD"):
        return pip_size, per_lot_quote
    return pip_size, per_lot_quote / price


def calc_lot(ud):
    pair = ud["pair"]
    _, pv = pip_info(pair, ud.get("price"))
    risk_amt = ud["balance"] * ud["risk"] / 100
    lots = risk_amt / (ud["sl"] * pv)
    lots_down = math.floor(lots * 100 + 1e-9) / 100
    text = (
        f"📏 POSITION SIZE: {pair}\n\n"
        f"Balance: ${ud['balance']:,.2f}\n"
        f"Risk: {ud['risk']:g}% = ${risk_amt:,.2f}\n"
        f"Stop loss: {ud['sl']:g} pips\n"
        f"Pip value (1 lot): ${pv:,.2f}\n\n"
        f"✅ Lot size: {lots_down:.2f}  (exact: {lots:.4f})\n"
        f"= {lots_down * LOT_UNITS:,.0f} units"
    )
    if lots_down < 0.01:
        text += "\n\n⚠️ Below the 0.01 micro-lot minimum. Lower the risk or use a smaller stop loss."
    return text


def calc_pip(ud):
    pair = ud["pair"]
    _, pv = pip_info(pair, ud.get("price"))
    total = pv * ud["lots"]
    return (
        f"💲 PIP VALUE: {pair}\n\n"
        f"Lot size: {ud['lots']:g}\n"
        f"1 pip = ${total:,.2f}\n"
        f"10 pips = ${total * 10:,.2f}\n"
        f"50 pips = ${total * 50:,.2f}"
    )


def calc_pnl(ud):
    pair = ud["pair"]
    pip_size, pv = pip_info(pair, ud["exit"] if usd_base(ud) else None)
    direction = 1 if ud["side"] == "Buy" else -1
    pips = (ud["exit"] - ud["entry"]) / pip_size * direction
    pnl = pips * pv * ud["lots"]
    icon = "🟢" if pnl >= 0 else "🔴"
    return (
        f"📈 PROFIT / LOSS: {pair}\n\n"
        f"{ud['side']} {ud['lots']:g} lots\n"
        f"Entry: {ud['entry']:g}   Exit: {ud['exit']:g}\n\n"
        f"{icon} {pips:+,.1f} pips\n"
        f"{icon} {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f}"
    )


def calc_rr(ud):
    pair = ud["pair"]
    entry, stop, target = ud["entry"], ud["stop"], ud["target"]
    if stop < entry < target:
        side = "Buy"
    elif target < entry < stop:
        side = "Sell"
    else:
        return (
            "⚠️ Those prices don't form a valid trade.\n\n"
            "For a buy: stop loss < entry < take profit.\n"
            "For a sell: take profit < entry < stop loss."
        )
    pip_size = 0.01 if "JPY" in pair else 0.0001
    risk_pips = abs(entry - stop) / pip_size
    reward_pips = abs(target - entry) / pip_size
    ratio = reward_pips / risk_pips
    breakeven = 100 / (1 + ratio)
    return (
        f"⚖️ RISK : REWARD: {pair} ({side})\n\n"
        f"Risk: {risk_pips:,.1f} pips\n"
        f"Reward: {reward_pips:,.1f} pips\n\n"
        f"✅ Ratio: 1 : {ratio:.2f}\n"
        f"Break-even win rate: {breakeven:.0f}%"
    )


def fetch_rate(frm, to):
    url = "https://api.frankfurter.dev/v1/latest?" + urllib.parse.urlencode(
        {"base": frm, "symbols": to}
    )
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.load(resp)


async def calc_conv(ud):
    frm, to, amount = ud["from"], ud["to"], ud["amount"]
    if frm == to:
        return f"💱 {amount:,.2f} {frm} = {amount:,.2f} {to}"
    try:
        data = await asyncio.to_thread(fetch_rate, frm, to)
        rate = data["rates"][to]
    except Exception:
        return (
            "⚠️ Couldn't get that rate. Check the currency codes, or try again in a moment.\n"
            "The converter covers about 30 major currencies, so a less common one may be unavailable."
        )
    return (
        f"💱 CONVERTER\n\n"
        f"{amount:,.2f} {frm} = {amount * rate:,.2f} {to}\n"
        f"Rate: 1 {frm} = {rate:,.5f} {to}\n"
        f"Date: {data.get('date', 'n/a')}\n\n"
        "ℹ️ Daily reference rate from the European Central Bank, not a live trading price."
    )


CALCS = {"lot": calc_lot, "pip": calc_pip, "pnl": calc_pnl, "rr": calc_rr}


# ---------------------------------------------------------------- conversation engine
async def ask(message, ud):
    step = next_step(ud)
    if step is None:
        kind = ud["kind"]
        if kind == "conv":
            text = await calc_conv(ud)
        else:
            text = CALCS[kind](ud)
        ud.clear()
        await message.reply_text(text, reply_markup=result_menu(kind))
        return
    prompt = step["prompt"](ud) if callable(step["prompt"]) else step["prompt"]
    markup = None
    if step["kind"] == "choice":
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(c, callback_data=f"pick:{c}") for c in step["choices"]
        ]])
    await message.reply_text(prompt, reply_markup=markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(WELCOME, reply_markup=MAIN_MENU)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    ud = context.user_data

    if data == "home":
        ud.clear()
        await q.message.reply_text(WELCOME, reply_markup=MAIN_MENU)

    elif data.startswith("kind:"):
        ud.clear()
        ud["kind"] = data.split(":", 1)[1]
        await ask(q.message, ud)

    elif data.startswith("pick:"):
        step = next_step(ud) if "kind" in ud else None
        if step is None or step["kind"] != "choice":
            await q.message.reply_text("Let's start over:", reply_markup=MAIN_MENU)
            return
        ud[step["key"]] = data.split(":", 1)[1]
        await ask(q.message, ud)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ud = context.user_data
    step = next_step(ud) if "kind" in ud else None
    if step is None:
        await update.message.reply_text("What do you need?", reply_markup=MAIN_MENU)
        return
    if step["kind"] == "choice":
        await update.message.reply_text("Please tap one of the buttons above.")
        return
    try:
        ud[step["key"]] = parse(step["kind"], update.message.text)
    except ValueError:
        await update.message.reply_text(ERRORS[step["kind"]])
        return
    await ask(update.message, ud)


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    print("Bot is running... press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
