# Telegram NFT Copy-Mint Bot (Robinhood Chain)

Visual Studio C# bot that watches a target wallet on **Robinhood Chain** (Ethereum L2, chain id `4663`). When that wallet mints a **free NFT**, the bot tries to mint the same contract call from **your** wallet and notifies you in Telegram.

## What it does

1. Polls Robinhood Chain for transactions from `TARGET_WALLET`
2. Detects mint-like activity (common `mint`/`claim` selectors, or ERC-721 `Transfer` from `0x0`)
3. Replays the same calldata from your wallet (simulation first, then broadcast)
4. Sends status / success / failure messages to your Telegram account

## Open in Visual Studio

1. Install **Visual Studio 2022** with the **.NET desktop development** workload (.NET 8 SDK)
2. Open `TelegramNftCopyBot.sln`
3. Copy `TelegramNftCopyBot/.env.example` → `TelegramNftCopyBot/.env`
4. Fill in the values (see below)
5. Press **F5** (or `dotnet run --project TelegramNftCopyBot`)

## Create the Telegram bot

1. In Telegram, open [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and copy the token → `TELEGRAM_BOT_TOKEN`
3. Get your numeric user id from [@userinfobot](https://t.me/userinfobot) → `TELEGRAM_OWNER_ID`

## `.env` settings

| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `TELEGRAM_OWNER_ID` | Only this Telegram user can control the bot |
| `RPC_URL` | Default `https://rpc.mainnet.chain.robinhood.com` |
| `CHAIN_ID` | `4663` (Robinhood Chain mainnet) |
| `TARGET_WALLET` | Wallet you want to copy |
| `PRIVATE_KEY` | **Your** wallet key (needs ETH for gas) |
| `FREE_MINTS_ONLY` | `true` = skip paid mints |
| `POLL_INTERVAL_MS` | Block poll interval |
| `GAS_LIMIT` | Gas limit for copy txs |

## Telegram commands

- `/start` `/help` — help
- `/status` — watcher state, wallets, last block
- `/pause` / `/resume` — stop / start copying
- `/balance` — your ETH on Robinhood Chain

## Network details (Robinhood Chain)

- Chain ID: `4663`
- RPC: `https://rpc.mainnet.chain.robinhood.com`
- Explorer: [robinhoodchain.blockscout.com](https://robinhoodchain.blockscout.com)
- Native gas token: ETH

## Important limits

- Works for **public / free** mints where calldata is not tied to the target wallet
- Will **fail** (safely, after simulation) if the mint needs a whitelist, Merkle proof, signature, or allowlist for the other address
- Needs a small ETH balance on Robinhood Chain for gas even when mint price is `0`
- Never commit `.env` or share your private key
- This is automation for public on-chain calls only — use at your own risk

## Project layout

```
TelegramNftCopyBot.sln
TelegramNftCopyBot/
  Program.cs
  .env.example
  Config/BotSettings.cs
  Models/MintCandidate.cs
  Services/
    WalletWatcherService.cs   # watches target wallet
    MintCopyService.cs        # sends your mint tx
    TelegramBotService.cs     # Telegram commands
    TelegramNotifier.cs       # push alerts
```
