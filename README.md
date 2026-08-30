# Ink Chain NFT Copy Bot (Telegram)

Python bot that watches wallet(s) on **Ink** (Optimism Superchain, chain id `57073`). When a watched wallet mints an NFT, the bot copies **free public mints** from your wallets and sends live status to Telegram.

This is the same copy-mint engine as the Robinhood bot, pointed at Ink. **Use a new BotFather token** so it does not conflict with Robinhood or Ethereum bots.

## What it copies

- Free SeaDrop `mintPublic` while the public window is open
- Other free public `mint` / `claim` calls when calldata can be rewritten to your wallet

It **skips** signed mints, allowlists, paid ETH/ERC20 mints (`FREE_MINTS_ONLY=true`), closed windows, and sold-out drops.

## Network

| Field | Value |
|---|---|
| Chain ID | `57073` |
| Public RPC | `https://rpc-gel.inkonchain.com` |
| Backup public RPC | `https://rpc-qnd.inkonchain.com` |
| Explorer | https://explorer.inkonchain.com |
| Gas | ETH on Ink |

Prefer a paid Ink RPC (Alchemy / Chainstack / QuickNode) for production.

## Quick start (VPS)

```bash
git clone -b cursor/ink-nft-copy-bot-1e4f https://github.com/Adebanjo79/Telegrambot.git /opt/nft-copy-bot
cd /opt/nft-copy-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env
```

Fill in a **new** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `TARGET_WALLETS`, and `PRIVATE_KEYS`. Minting wallets need Ink ETH for gas.

```bash
# dry run first
.venv/bin/python main.py
```

Then systemd:

```bash
cp deploy/nft-copy-bot.service /etc/systemd/system/nft-copy-bot.service
systemctl daemon-reload
systemctl enable --now nft-copy-bot
systemctl status nft-copy-bot --no-pager
```

Telegram `/status` should show `Network: Ink (57073)` and `Build: ink-copy-2026-08-23`.

## Telegram

`/status` `/targets` `/wallets` `/addwallet` `/removewallet` `/pause` `/resume` `/balance` `/help`

Owner-only. Never reuse this bot token on another running bot.

## Limits

- Cannot copy `mintSigned` or allowlist proofs
- Short public windows can still close mid-blast
- A bad primary RPC fails over to the backup; the bot cannot fix a broken node
- Free SeaDrop copies cap gas at 200k so small wallets (~$0.10) can mint when Ink gas is cheap
