# Robinhood Chain NFT Copy Bot (Telegram)

Python bot that watches wallet(s) on **Robinhood Chain** (Ethereum L2, chain id `4663`). When a watched wallet mints an NFT, the bot detects it, optionally copies the same calldata from **your** wallet, and pushes live status to Telegram.

## What it does

1. Polls Robinhood Chain for transactions from `TARGET_WALLETS`
2. Detects mint-like activity (common `mint` / `claim` selectors, or ERC-721/1155 `Transfer` from `0x0`)
3. Simulates the same call from your wallet, then broadcasts (unless `DRY_RUN=true`)
4. Sends detection / success / failure alerts to your Telegram account
5. Lets you `/pause`, `/resume`, check `/status` and `/balance` from Telegram

## Quick start

### Option A — clone from GitHub (recommended on Windows)

In **PowerShell**:

```powershell
cd $HOME\Downloads
git clone -b cursor/robinhood-nft-copy-bot-1e4f https://github.com/Adebanjo79/Telegrambot.git robinhood-nft-copy-bot
cd robinhood-nft-copy-bot
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
python main.py
```

If `Activate.ps1` is blocked, run once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

### Option B — if you downloaded a Cursor zip

1. In File Explorer, open `Downloads` and find the unzipped folder (it may be named like `telegram-bot-cursor-...` or end with `(1)`).
2. Confirm you see `main.py` and `requirements.txt` inside it.
3. In PowerShell, discover the real path instead of guessing:

```powershell
Get-ChildItem $HOME\Downloads -Directory | Where-Object { $_.Name -like "*robinhood*" -or $_.Name -like "*telegram-bot*" }
```

Then `cd` into the folder that contains `main.py`:

```powershell
cd "C:\Users\USER\Downloads\<exact-folder-name-here>"
# if the zip nested another folder:
cd .\telegram-bot-cursor-robinhood-nft-copy-bot-*
dir   # you must see main.py here
```

Do **not** paste a path that wraps across two lines. If PowerShell says `Cannot find path`, the folder name is wrong — use `Get-ChildItem` above.

### Linux / macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your Telegram token, owner id, target wallets, and private key
python main.py
```

## Create the Telegram bot

1. In Telegram, open [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and copy the token → `TELEGRAM_BOT_TOKEN`
3. Message your new bot once (so it can DM you)
4. Get your numeric user id from [@userinfobot](https://t.me/userinfobot) → `TELEGRAM_OWNER_ID`

## `.env` settings

| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | From BotFather |
| `TELEGRAM_OWNER_ID` | Only this Telegram user can control the bot |
| `RPC_URL` | Default `https://rpc.mainnet.chain.robinhood.com` |
| `CHAIN_ID` | `4663` (Robinhood Chain mainnet) |
| `EXPLORER_URL` | Default `https://robinhoodchain.blockscout.com` |
| `TARGET_WALLETS` | Comma-separated wallets to copy |
| `PRIVATE_KEY` | **Your** wallet key (needs ETH for gas) |
| `FREE_MINTS_ONLY` | `true` = skip paid mints |
| `DRY_RUN` | `true` = notify + simulate only (recommended first) |
| `POLL_INTERVAL_SEC` | Block poll interval |
| `GAS_LIMIT` | Gas limit for copy txs |

## Telegram commands

- `/start` `/help` — help
- `/status` — watcher state, mode, last block
- `/targets` — wallets being watched
- `/pause` / `/resume` — stop / start copying
- `/balance` — your ETH on Robinhood Chain

## Network details (Robinhood Chain)

- Chain ID: `4663`
- RPC: `https://rpc.mainnet.chain.robinhood.com`
- Explorer: [robinhoodchain.blockscout.com](https://robinhoodchain.blockscout.com)
- Native gas token: ETH

For production, prefer Alchemy / QuickNode RPC URLs from the [Robinhood Chain docs](https://docs.robinhood.com/chain/connecting/).

## Recommended first run

1. Leave `DRY_RUN=true`
2. Set one known active `TARGET_WALLETS` address
3. Start the bot and confirm Telegram `/status` works
4. When a mint is detected you should get a simulation result without spending gas
5. Only then set `DRY_RUN=false` and fund your wallet with a little ETH on Robinhood Chain

## Important limits

- Best for **public / free** mints where calldata is not tied to the target wallet
- Will **fail safely after simulation** if the mint needs a whitelist, Merkle proof, signature, or allowlist for the other address
- Needs a small ETH balance on Robinhood Chain for gas even when mint price is `0`
- Never commit `.env` or share your private key
- Automation for public on-chain calls only — use at your own risk

## Project layout

```
main.py                 # entrypoint
requirements.txt
.env.example
bot/
  config.py             # env settings
  models.py             # MintCandidate
  watcher.py            # polls target wallets
  mint_copy.py          # simulate + broadcast
  telegram_bot.py       # commands + alerts
```
