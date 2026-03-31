#!/usr/bin/env python3
"""
Polymarket BTC 5-Minute Edge Trading Bot
Exploits pricing inefficiencies in BTC up/down prediction markets
"""

import json
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from math import exp, log
from typing import Dict, Optional, Tuple

import requests
import websocket
from py_clob_client.client import ClobClient
from py_clob_client.order_args import OrderArgs, OrderType

# ============================================================================
# CONFIGURATION
# ============================================================================

DRY_RUN = True  # Set to False for live trading

BANKROLL = 50.0
EDGE_THRESHOLD = 0.18
HOLD_THRESHOLD = 0.10
ROUND_SECONDS = 300
EXIT_BUFFER_S = 15
MAX_BET_FRACTION = 0.12
PRICE_HISTORY = 60
CLOB_POLL_S = 2

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

PRIVATE_KEY = os.environ.get("PRIVATE_KEY", "")
if not PRIVATE_KEY and not DRY_RUN:
    raise ValueError("PRIVATE_KEY env var required for live trading")

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class Market:
    """Active market state"""
    slug: str = ""
    condition_id: str = ""
    tokens: Dict[str, str] = field(default_factory=dict)  # {"yes": id, "no": id}
    target_price: float = 0.0
    yes_mid: float = 0.0
    no_mid: float = 0.0
    round_end: int = 0


@dataclass
class Position:
    """Current position"""
    direction: str = ""  # "YES" or "NO"
    entry_price: float = 0.0
    size: float = 0.0
    order_id: Optional[str] = None
    entry_time: float = 0.0


# ============================================================================
# GLOBAL STATE
# ============================================================================

btc_prices = deque(maxlen=PRICE_HISTORY)
market_lock = threading.Lock()
active_market = Market()
current_position = Position()
last_clob_poll = 0
balance = BANKROLL


# ============================================================================
# BTC PROBABILITY MODEL
# ============================================================================

def compute_probability(target_price: float) -> float:
    """
    Estimate P(BTC > target at round end) using multi-signal model with
    volatility normalization and confidence adjustment.
    
    Signals:
    1. Distance: (current_price - target) / vol (weight: 0.40)
    2. Short momentum: (price[-1] - price[-5]) / vol (weight: 0.25)
    3. Medium momentum: (price[-1] - price[-15]) / vol (weight: 0.20)
    4. Acceleration: short_mom - (price[-5] - price[-10]) / vol (weight: 0.15)
    
    Squashed through sigmoid, confidence-adjusted, clamped to [0.05, 0.95].
    """
    if len(btc_prices) < 20:
        return 0.5

    prices = list(btc_prices)
    current = prices[-1]

    # 1. Compute volatility (std dev of recent returns)
    returns = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    mean_return = sum(returns) / len(returns)
    variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
    vol = max(0.5, variance ** 0.5)

    # 2. Compute signals (normalized by volatility)
    distance = (current - target_price) / vol
    
    short_mom = (prices[-1] - prices[-5]) / vol if len(prices) >= 5 else 0
    medium_mom = (prices[-1] - prices[-15]) / vol if len(prices) >= 15 else 0
    
    accel = 0
    if len(prices) >= 10:
        accel = short_mom - (prices[-5] - prices[-10]) / vol
    else:
        accel = short_mom

    # 3. Weighted combination
    score = (
        0.40 * distance +
        0.25 * short_mom +
        0.20 * medium_mom +
        0.15 * accel
    )

    # 4. Sigmoid squash
    prob = 1.0 / (1.0 + exp(-score * 0.5))

    # 5. Confidence shrinkage in high volatility
    confidence = min(1.0, 1.0 / (1.0 + vol / 10.0))
    prob = 0.5 + (prob - 0.5) * confidence

    # 6. Clamp to [0.05, 0.95]
    prob = max(0.05, min(0.95, prob))

    return prob


# ============================================================================
# MARKET DISCOVERY
# ============================================================================

def get_round_boundaries() -> Tuple[int, int, int]:
    """
    Get the boundaries for current/next/prev 300s round aligned to UTC epoch.
    Returns (base, next_boundary, prev_boundary)
    """
    now = int(time.time())
    base = now - (now % 300)
    return base, base + 300, base - 300


def find_active_market() -> Optional[Market]:
    """
    Discover active BTC 5-minute market by checking candidate slugs against Gamma API.
    Returns Market object if found, else None.
    
    Tries: next_boundary, base, prev_boundary (in order)
    """
    base, next_boundary, prev_boundary = get_round_boundaries()
    candidates = [next_boundary, base, prev_boundary]

    for boundary in candidates:
        slug = f"btc-updown-5m-{boundary}"
        try:
            resp = requests.get(
                f"{GAMMA_HOST}/markets/slug/{slug}",
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                if "id" in data and "question" in data:
                    condition_id = data["id"]
                    target = extract_btc_target(
                        data.get("question", ""),
                        data.get("description", "")
                    )
                    if target:
                        market = fetch_market_tokens(
                            condition_id,
                            slug,
                            target,
                            boundary
                        )
                        if market:
                            return market
        except Exception as e:
            pass

    return None


def extract_btc_target(question: str, description: str) -> Optional[float]:
    """
    Parse target BTC price from market question/description.
    
    Target is between $10,000 and $500,000.
    Return first matching number (not timestamps/durations).
    """
    import re
    text = f"{question} {description}"
    numbers = re.findall(r'\d+(?:\.\d+)?', text)
    
    for num_str in numbers:
        try:
            num = float(num_str)
            if 10000 <= num <= 500000:
                return num
        except ValueError:
            pass
    
    return None


def fetch_market_tokens(
    condition_id: str,
    slug: str,
    target: float,
    round_end: int
) -> Optional[Market]:
    """
    Fetch YES/NO token IDs for a condition_id from CLOB API.
    """
    try:
        resp = requests.get(
            f"{CLOB_HOST}/markets/{condition_id}",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            tokens = {}
            
            for token_info in data.get("tokens", []):
                outcome = token_info.get("outcome", "").upper()
                if outcome in ["UP", "YES", "HIGHER"]:
                    tokens["yes"] = token_info.get("token_id", "")
                elif outcome in ["DOWN", "NO", "LOWER"]:
                    tokens["no"] = token_info.get("token_id", "")

            if "yes" in tokens and "no" in tokens:
                market = Market(
                    slug=slug,
                    condition_id=condition_id,
                    tokens=tokens,
                    target_price=target,
                    round_end=round_end,
                )
                return market
    except Exception as e:
        pass

    return None


# ============================================================================
# PRICE FETCHING
# ============================================================================

def fetch_order_book(token_id: str) -> Tuple[float, float]:
    """
    Fetch order book for token and return mid-price.
    
    Uses mid-price: (best_bid + best_ask) / 2
    NOT the inverse (1 - yes_price for NO).
    
    Returns (mid, mid) on success, (0.0, 0.0) on error.
    """
    try:
        resp = requests.get(
            f"{CLOB_HOST}/book?token_id={token_id}",
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            
            if bids and asks:
                best_bid = float(bids[0].get("price", 0.0))
                best_ask = float(asks[0].get("price", 0.0))
                mid = (best_bid + best_ask) / 2.0
                return mid, mid
    except Exception as e:
        pass
    
    return 0.0, 0.0


# ============================================================================
# CLOB MARKET POLLER (BACKGROUND THREAD)
# ============================================================================

def market_poller_thread():
    """
    Poll CLOB every 2 seconds for active market and live prices.
    Updates shared active_market under lock.
    """
    global active_market, last_clob_poll

    while True:
        try:
            current_time = int(time.time())
            if current_time - last_clob_poll >= CLOB_POLL_S:
                last_clob_poll = current_time

                new_market = find_active_market()
                if new_market:
                    yes_mid, _ = fetch_order_book(new_market.tokens["yes"])
                    no_mid, _ = fetch_order_book(new_market.tokens["no"])

                    with market_lock:
                        new_market.yes_mid = yes_mid
                        new_market.no_mid = no_mid
                        active_market = new_market

            time.sleep(0.5)
        except Exception as e:
            print(f"\n[ERROR] Market poller: {e}")
            time.sleep(1)


# ============================================================================
# ORDER PLACEMENT
# ============================================================================

def clob_place_order(
    token_id: str,
    price: float,
    size: float,
    side: str
) -> str:
    """
    Place a limit order on CLOB. Returns order_id on success, or "SIM_ORDER" in dry run.
    
    Args:
        token_id: Token to trade
        price: Mid-price
        size: Amount in USDC
        side: "BUY" or "SELL"
    
    Returns:
        order_id (string) or "SIM_ORDER" if DRY_RUN
    """
    if DRY_RUN:
        print(f"\n[SIM] {side} {size:.2f} USDC @ {price:.3f}")
        return "SIM_ORDER"

    try:
        client = ClobClient(
            host=CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)

        # Cross the spread slightly to improve fill probability
        if side == "BUY":
            order_price = price - 0.005
        else:
            order_price = price + 0.005

        order_args = OrderArgs(
            token_id=token_id,
            price=order_price,
            size=size,
            side=side,
            order_type=OrderType.GTC,
        )
        result = client.create_order(order_args)
        return result.get("order_id", "")
    except Exception as e:
        print(f"\n[ERROR] Order placement: {e}")
        return ""


def clob_cancel_order(order_id: str):
    """Cancel an order on CLOB."""
    if DRY_RUN:
        print(f"\n[SIM] CANCEL {order_id}")
        return

    try:
        client = ClobClient(
            host=CLOB_HOST,
            key=PRIVATE_KEY,
            chain_id=CHAIN_ID
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        client.cancel_order(order_id)
    except Exception as e:
        print(f"\n[ERROR] Cancel: {e}")


# ============================================================================
# TRADING LOGIC
# ============================================================================

def compute_edges(
    our_prob: float,
    yes_mid: float,
    no_mid: float
) -> Tuple[float, str]:
    """
    Compute edge for YES and NO, return (best_edge, direction).
    
    yes_edge = our_prob - yes_mid
    no_edge = (1 - our_prob) - no_mid
    
    Returns whichever is higher.
    """
    yes_edge = our_prob - yes_mid
    no_edge = (1.0 - our_prob) - no_mid
    
    if yes_edge >= no_edge:
        return yes_edge, "YES"
    else:
        return no_edge, "NO"


def position_size(edge: float, bankroll: float) -> float:
    """
    Half-Kelly criterion with max bet fraction.
    
    size = (edge / 2) * bankroll, capped at MAX_BET_FRACTION * bankroll
    """
    kelly_size = (edge / 2.0) * bankroll
    max_size = MAX_BET_FRACTION * bankroll
    return min(kelly_size, max_size)


def execute_trading_logic(current_time: int):
    """
    Main trading loop: entry/exit decisions, order placement.
    Called on every BTC price tick.
    """
    global current_position, balance

    with market_lock:
        market = active_market
        if not market.slug:
            return

    if len(btc_prices) < 20:
        return

    our_prob = compute_probability(market.target_price)
    best_edge, direction = compute_edges(our_prob, market.yes_mid, market.no_mid)
    time_left = market.round_end - current_time

    # === ENTRY ===
    if not current_position.direction:
        if best_edge >= EDGE_THRESHOLD and time_left > EXIT_BUFFER_S + 30:
            size = position_size(best_edge, balance)
            if size > 0:
                token_id = market.tokens[direction.lower()]
                entry_price = market.yes_mid if direction == "YES" else market.no_mid
                order_id = clob_place_order(
                    token_id,
                    entry_price,
                    size,
                    "BUY"
                )
                
                if order_id or DRY_RUN:
                    current_position = Position(
                        direction=direction,
                        entry_price=entry_price,
                        size=size,
                        order_id=order_id,
                        entry_time=current_time,
                    )
                    print(
                        f"\n[ENTRY] {direction} {size:.2f} @ "
                        f"{current_position.entry_price:.3f}, "
                        f"edge +{best_edge:.3f}, {time_left}s left"
                    )

    # === EXIT ===
    elif current_position.direction:
        pos_edge = (
            our_prob - market.yes_mid
            if current_position.direction == "YES"
            else (1 - our_prob) - market.no_mid
        )
        time_held = current_time - current_position.entry_time
        
        should_exit = (
            pos_edge < HOLD_THRESHOLD or
            time_left < EXIT_BUFFER_S or
            time_held > 240
        )

        if should_exit:
            reason = ""
            if pos_edge < HOLD_THRESHOLD:
                reason = f"edge collapsed ({pos_edge:.3f})"
            elif time_left < EXIT_BUFFER_S:
                reason = f"round ending ({time_left}s)"
            else:
                reason = f"max hold time (240s)"

            if current_position.order_id and current_position.order_id != "SIM_ORDER":
                clob_cancel_order(current_position.order_id)

            token_id = market.tokens[current_position.direction.lower()]
            exit_price = (
                market.yes_mid
                if current_position.direction == "YES"
                else market.no_mid
            )
            clob_place_order(
                token_id,
                exit_price,
                current_position.size,
                "SELL"
            )

            print(
                f"\n[EXIT] {current_position.direction} "
                f"{current_position.size:.2f} @ {exit_price:.3f}, "
                f"reason: {reason}"
            )
            current_position = Position()


# ============================================================================
# BINANCE WEBSOCKET
# ============================================================================

def on_binance_message(ws, message):
    """Handle incoming Binance price tick."""
    try:
        data = json.loads(message)
        price = float(data["p"])
        btc_prices.append(price)

        current_time = int(time.time())
        execute_trading_logic(current_time)

        # Dashboard
        with market_lock:
            market = active_market
            time_left = (
                market.round_end - current_time
                if market.slug else 0
            )
            status = (
                f"{current_position.direction} {current_position.size:.2f}"
                if current_position.direction
                else "[flat]"
            )

        our_prob = (
            compute_probability(market.target_price)
            if market.slug else 0
        )
        best_edge, direction = (
            compute_edges(our_prob, market.yes_mid, market.no_mid)
            if market.slug
            else (0, "")
        )

        print(
            f"\rBTC {price:>10,.0f} | Target {market.target_price:>10,.2f} | "
            f"YES {market.yes_mid:.3f} NO {market.no_mid:.3f} | "
            f"Model {our_prob:.3f} | BestEdge +{best_edge:.3f} {direction} | "
            f"{time_left:>3}s | Bal {balance:.2f} | {status}",
            end="",
            flush=True,
        )
    except Exception as e:
        print(f"\n[ERROR] Binance message: {e}")


def on_binance_error(ws, error):
    """Handle WebSocket errors."""
    print(f"\n[ERROR] Binance WS: {error}")


def on_binance_close(ws, close_status_code, close_msg):
    """Handle WebSocket close."""
    print(f"\n[INFO] Binance connection closed: {close_msg}")


def on_binance_open(ws):
    """Handle WebSocket open."""
    print("\n[INFO] Binance WebSocket connected")


def run_binance_websocket():
    """Main thread: Binance WebSocket feed."""
    ws_url = "wss://stream.binance.com:9443/ws/btcusdt@aggTrade"
    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_binance_message,
        on_error=on_binance_error,
        on_close=on_binance_close,
        on_open=on_binance_open,
    )
    ws.run_forever()


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 100)
    print("Polymarket BTC 5-Minute Edge Trading Bot")
    print(f"DRY_RUN: {DRY_RUN}")
    print(f"Bankroll: ${BANKROLL}")
    print(f"Edge Threshold: {EDGE_THRESHOLD}")
    print(f"Hold Threshold: {HOLD_THRESHOLD}")
    print(f"Max Bet Fraction: {MAX_BET_FRACTION * 100:.1f}%")
    print("=" * 100)

    # Start background market poller
    poller = threading.Thread(target=market_poller_thread, daemon=True)
    poller.start()
    print("[INFO] Market poller started")

    # Start Binance WebSocket (blocks)
    try:
        run_binance_websocket()
    except KeyboardInterrupt:
        print("\n[INFO] Bot stopped by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
