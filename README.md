# EVM NFT Copy Bot (Telegram) — Robinhood Chain + Ethereum

Python bot that watches wallet(s) on an EVM chain (Robinhood Chain `4663` or **Ethereum Mainnet** `1`). When a watched wallet mints an NFT, the bot detects it, optionally copies the call from **your** wallet, and pushes live status to Telegram.

## What it does

1. Polls the configured chain for transactions from `TARGET_WALLETS`
2. Detects mint-like activity (common `mint` / `claim` selectors, or ERC-721/1155 `Transfer` from `0x0`)
3. Adapts SeaDrop `mintPublic` calldata to your wallet, simulates, then broadcasts (unless `DRY_RUN=true`)
4. Sends detection / success / failure alerts to your Telegram account
5. Lets you `/pause`, `/resume`, check `/status` and `/balance` from Telegram

## Ethereum Mainnet setup

Same bot code — point `.env` at Ethereum:

```bash
cp .env.ethereum.example .env
# fill TELEGRAM_*, TARGET_WALLETS, PRIVATE_KEY
# set RPC_URL to Alchemy/Infura/QuickNode Ethereum URL when possible
python main.py
```

Key Ethereum values:

| Variable | Value |
|---|---|
| `CHAIN_ID` | `1` |
| `NETWORK_NAME` | `Ethereum Mainnet` |
| `RPC_URL` | `https://ethereum.publicnode.com` (free) or Alchemy/Infura |
| `EXPLORER_URL` | `https://etherscan.io` |
| `DRY_RUN` | keep `true` first — Ethereum gas is expensive |
| `POLL_INTERVAL_SEC` | `2.0` recommended on public RPC |

### Run Ethereum bot on VPS (alongside Robinhood bot)

```bash
cd ~
git clone -b cursor/ethereum-nft-copy-bot-1e4f https://github.com/Adebanjo79/Telegrambot.git ethereum-nft-copy-bot
cd ethereum-nft-copy-bot
cp .env.ethereum.example .env
nano .env   # fill values, CHAIN_ID=1, Ethereum RPC
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python main.py   # test, then Ctrl+C
sudo cp deploy/eth-nft-copy-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now eth-nft-copy-bot
sudo systemctl status eth-nft-copy-bot
```

Use a **different Telegram bot token** than the Robinhood bot if you run both, or the same token only if you use different owner chats carefully (same token + same owner is OK for two bots but alerts will mix).

## Robinhood Chain quick start

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
| `RPC_URL` | Default public RPC (rate-limited). Prefer Alchemy/QuickNode for stability |
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
