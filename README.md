# Polymarket BTC 5-Minute Edge Trading Bot

A fully automated Python trading bot that discovers and exploits pricing inefficiencies in Polymarket's Bitcoin 5-minute up/down prediction markets.

## Table of Contents

- [How It Works](#how-it-works)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Market Structure](#market-structure)
- [Probability Model](#probability-model)
- [Trading Strategy](#trading-strategy)
- [Architecture](#architecture)
- [Dashboard](#dashboard)
- [Troubleshooting](#troubleshooting)
- [Safety & Risk Management](#safety--risk-management)

## How It Works

Every 5 minutes, Polymarket creates a new binary market: **"Will BTC be above $X at time T?"**

The bot:
1. **Streams live BTC prices** from Binance WebSocket
2. **Builds an independent probability estimate** using a 4-signal ML model
3. **Fetches live order book prices** from Polymarket's CLOB API
4. **Compares model vs market** to identify the edge (pricing inefficiency)
5. **Automatically trades** when edge exceeds threshold, exits when edge collapses

### Key Innovation: Volatility-Normalized Probability Model

Instead of simple price momentum, the bot uses:
- **Distance**: How far current price is from target (40% weight)
- **Short momentum**: 4-second trend (25% weight)  
- **Medium momentum**: 14-second trend (20% weight)
- **Acceleration**: Change in momentum (15% weight)

All signals are normalized by rolling volatility to adapt to market regime.

---

## Quick Start

### Prerequisites

- **Python**: 3.8+
- **Wallet**: Polygon wallet (testnet Mumbai or mainnet)
- **USDC**: Some USDC on your chosen network
- **Private key**: For signing orders

### Installation

#### 1. Clone Repository

```bash
git clone https://github.com/abhijeet-maker/polymarket-btc-bot.git
cd polymarket-btc-bot
