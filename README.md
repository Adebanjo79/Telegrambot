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
| `RPC_URL` | Default public RPC (rate-limited). Prefer Chainstack/Alchemy/QuickNode for stability |
| `RPC_URLS` | Optional comma-separated endpoints; the bot fails over automatically when a node is full |
| `RPC_LOAD_BALANCE` | `true` = share traffic across all endpoints; `false` = extras are backups only |
| `CHAIN_ID` | `4663` (Robinhood Chain mainnet) |
| `EXPLORER_URL` | Default `https://robinhoodchain.blockscout.com` |
| `TARGET_WALLETS` | Comma-separated wallets to copy |
| `PRIVATE_KEY` | **Your** wallet key (needs ETH for gas) |
| `FREE_MINTS_ONLY` | `true` = skip paid mints |
| `DRY_RUN` | `true` = notify + simulate only (recommended first) |
| `POLL_INTERVAL_SEC` | Block poll interval |
| `GAS_LIMIT` | Max gas units (default `200000`). Free SeaDrop copies cap at 200k so small wallets can mint |
| `RPC_RATE_LIMIT` | Your plan's requests/second cap (default `25`) |
| `RPC_WARN_PERCENT` | Warn once usage passes this share of the cap (default `80`) |
| `RPC_SLOW_MS` | Warn and fail over to backup when average response exceeds this (default `1500`) |
| `PENDING_DETECTION` | `true` = watch the mempool before block confirmation (advanced; default `false`) |
| `PENDING_WS_URL` | WebSocket RPC (`wss://...`) with full pending transactions |
| `PENDING_SUBSCRIPTION` | `auto`, `alchemy`, or `full`; default `auto` |
| `WALLET_STATE_REFRESH_SEC` | Refresh cached balances/nonces every cycle (default `5`) |
| `WALLET_STATE_TTL_SEC` | Fall back to live state after this age (default `15`) |

### Faster pending detection (advanced)

Pending detection can see a target mint before it reaches a block. For an
Alchemy Robinhood endpoint:

```env
PENDING_DETECTION=true
PENDING_WS_URL=wss://robinhood-mainnet.g.alchemy.com/v2/YOUR_KEY
PENDING_SUBSCRIPTION=auto
```

Alchemy mode filters by `TARGET_WALLETS` on the server, so the bot does not
download every pending transaction. Other providers must support full
transactions from `eth_subscribe("newPendingTransactions", true)`.

**Risk:** pending transactions are not final. A target transaction can be
replaced, dropped, or revert after the bot has already broadcast, costing gas.
Keep `PENDING_DETECTION=false` if avoiding that risk matters more than speed.

The wallet-state cache is enabled automatically. It refreshes balances and
pending nonces before a mint appears, removing those RPC calls from the
critical free-mint blast path.

## RPC alerts

The bot watches its own request rate and latency, and messages you on Telegram:

| Message | Meaning |
|---|---|
| `⚠️ RPC is getting close to its limit` | Above `RPC_WARN_PERCENT` of the cap, or responses are slow |
| `🚨 RPC FULL / rate-limited` | The provider is already rejecting requests |
| `🔁 Switched RPC node` | Failed over to the next entry in `RPC_URLS` |
| `✅ RPC back to normal` / `✅ RPC recovered` | Load or errors cleared |

Warnings repeat at most every 3 minutes while a problem lasts.

### Using two providers together

Alchemy supports Robinhood Chain (`https://robinhood-mainnet.g.alchemy.com/v2/KEY`),
so it pairs well with Chainstack:

```dotenv
RPC_URLS=https://robinhood-mainnet.core.chainstack.com/KEY,https://robinhood-mainnet.g.alchemy.com/v2/KEY
RPC_LOAD_BALANCE=true
```

With balancing on, each node handles about half the requests, so the warning
threshold scales with the number of nodes. If one provider is rate-limited or
goes down, the request is retried on the other one immediately.

## Telegram commands

- `/start` `/help` — help
- `/status` — watcher state, mode, last block, RPC health
- `/targets` — wallets being watched
- `/pause` / `/resume` — stop / start copying
- `/balance` — your ETH on Robinhood Chain

## Network details (Robinhood Chain)

- Chain ID: `4663`
- RPC: `https://rpc.mainnet.chain.robinhood.com`
- Explorer: [robinhoodchain.blockscout.com](https://robinhoodchain.blockscout.com)
- Native gas token: ETH

For production, prefer Alchemy / QuickNode RPC URLs from the [Robinhood Chain docs](https://docs.robinhood.com/chain/connecting/).

If Telegram shows `Remote end closed connection` / watcher RPC blips, the public RPC is dropping you. Fix:

1. Create a free Alchemy app on Robinhood Chain
2. Set `RPC_URL=https://robinhood-mainnet.g.alchemy.com/v2/YOUR_KEY` in `.env`
3. Optionally set `POLL_INTERVAL_SEC=1.5`
4. Restart `python main.py`

## Recommended first run

1. Leave `DRY_RUN=true`
2. Set one known active `TARGET_WALLETS` address
3. Start the bot and confirm Telegram `/status` works
4. When a mint is detected you should get a simulation result without spending gas
5. Only then set `DRY_RUN=false` and fund your wallet with a little ETH on Robinhood Chain

## Run 24/7 on a VPS (laptop can be off)

The bot only runs where `python main.py` is executing. Put it on your VPS so it keeps minting while your laptop is asleep/off.

### 1. SSH into the VPS

```bash
ssh ubuntu@YOUR_VPS_IP
```

(Use your real username/IP.)

### 2. Install Python + git

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

### 3. Get the bot code

```bash
cd ~
git clone -b cursor/robinhood-nft-copy-bot-1e4f https://github.com/Adebanjo79/Telegrambot.git robinhood-nft-copy-bot
cd robinhood-nft-copy-bot
```

Or upload the folder from your laptop with `scp` / SFTP.

### 4. Create `.env` on the VPS

```bash
cp .env.example .env
nano .env
```

Paste the same values from your laptop `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID`, `TARGET_WALLETS`, `PRIVATE_KEY`, Alchemy `RPC_URL`, `DRY_RUN=false`).

### 5. Install deps

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 6. Test once

```bash
python main.py
```

Confirm Telegram gets the online message, then press `Ctrl+C`.

### 7. Run forever with systemd

Edit `deploy/nft-copy-bot.service` if your username/path differ, then:

```bash
sudo cp deploy/nft-copy-bot.service /etc/systemd/system/nft-copy-bot.service
sudo nano /etc/systemd/system/nft-copy-bot.service   # fix User= and paths if needed
sudo systemctl daemon-reload
sudo systemctl enable --now nft-copy-bot
sudo systemctl status nft-copy-bot
```

Useful commands:

```bash
sudo systemctl restart nft-copy-bot
sudo systemctl stop nft-copy-bot
journalctl -u nft-copy-bot -f
```

After this, you can turn the laptop off. Control the bot from Telegram (`/status`, `/pause`, `/resume`).

**Important:** stop the bot on your laptop first so you don’t run two copies with the same wallet at once.


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
