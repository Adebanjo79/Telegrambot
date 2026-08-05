from __future__ import annotations

import asyncio
import logging
import sys

from web3 import Web3

from bot.config import Settings
from bot.mint_copy import MintCopyService
from bot.rpc import is_transient_rpc_error
from bot.telegram_bot import TelegramTracker
from bot.watcher import WalletWatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def build_web3(rpc_url: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 45}))
    if not w3.is_connected():
        raise RuntimeError(f"Cannot connect to RPC: {rpc_url}")
    return w3


async def watch_loop(
    tracker: TelegramTracker,
    watcher: WalletWatcher,
    mint_copy: MintCopyService,
) -> None:
    settings = tracker.settings
    last_rpc_alert = 0.0
    while True:
        try:
            if watcher.enabled:
                candidates = await asyncio.to_thread(watcher.poll)
                for candidate in candidates:
                    if mint_copy.already_copied(candidate.source_tx_hash):
                        continue

                    await tracker.notify(
                        "👀 Target mint activity\n"
                        f"From: {candidate.target_wallet}\n"
                        f"Block: {candidate.block_number}\n"
                        f"Contract: {candidate.contract_address}\n"
                        f"Value: {candidate.value_eth} ETH\n"
                        f"Hint: {candidate.method_hint}\n"
                        f"Source: {tracker.explorer_tx(candidate.source_tx_hash)}\n"
                        f"Minting wallets: {len(mint_copy.my_wallets)}\n"
                        f"{'Simulating (DRY_RUN)…' if settings.dry_run else 'Copying…'}"
                    )

                    results = await asyncio.to_thread(mint_copy.try_copy_all, candidate)
                    for wallet, ok, message, copy_hash in results:
                        prefix = f"Wallet `{wallet}`\n"
                        if ok and copy_hash:
                            await tracker.notify(
                                f"✅ Copy mint sent\n"
                                f"{prefix}"
                                f"{tracker.explorer_tx(copy_hash)}\n"
                                f"{message}"
                            )
                        elif ok:
                            await tracker.notify(f"✅ {prefix}{message}")
                        else:
                            await tracker.notify(f"❌ Copy mint not sent\n{prefix}{message}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if is_transient_rpc_error(exc):
                log.warning("Transient RPC error (will retry): %s", exc)
                now = asyncio.get_running_loop().time()
                # Avoid spamming Telegram on public RPC disconnects.
                if now - last_rpc_alert > 120:
                    last_rpc_alert = now
                    try:
                        await tracker.notify(
                            "⚠️ RPC connection blip (retrying). "
                            "Public RPC is rate-limited — prefer Alchemy/QuickNode in RPC_URL."
                        )
                    except Exception:
                        pass
                await asyncio.sleep(max(settings.poll_interval_sec, 2.0))
                continue

            log.exception("Poll loop error")
            try:
                await tracker.notify(f"⚠️ Watcher error: {exc}")
            except Exception:
                pass

        await asyncio.sleep(settings.poll_interval_sec)


async def async_main() -> int:
    try:
        settings = Settings.load()
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        print("Copy .env.example to .env and fill in the values.", file=sys.stderr)
        return 1

    w3 = build_web3(settings.rpc_url)
    chain_id = w3.eth.chain_id
    if chain_id != settings.chain_id:
        log.warning(
            "RPC chain id %s does not match configured CHAIN_ID %s",
            chain_id,
            settings.chain_id,
        )

    mint_copy = MintCopyService(settings, w3)
    watcher = WalletWatcher(settings, w3)
    head = watcher.bootstrap()
    tracker = TelegramTracker(settings, watcher, mint_copy)

    mode = "DRY_RUN" if settings.dry_run else "LIVE"
    log.info("Starting NFT copy bot on Robinhood Chain (%s)", settings.chain_id)
    log.info(
        "Mode=%s targets=%s my_wallet=%s block=%s",
        mode,
        len(settings.target_wallets),
        mint_copy.my_wallet,
        head,
    )

    async with tracker.app:
        await tracker.app.start()
        await tracker.app.updater.start_polling(drop_pending_updates=True)

        await tracker.notify(
            f"🟢 NFT copy bot online ({mode})\n"
            f"Robinhood Chain id {settings.chain_id}\n"
            f"Targets: {len(settings.target_wallets)}\n"
            f"Minting wallets: {len(settings.my_wallets)}\n"
            f"Primary wallet: {mint_copy.my_wallet}\n"
            f"Free mints only: {settings.free_mints_only}\n"
            f"Starting at block {head}"
        )

        watch_task = asyncio.create_task(watch_loop(tracker, watcher, mint_copy))
        try:
            await watch_task
        except asyncio.CancelledError:
            pass
        finally:
            if not watch_task.done():
                watch_task.cancel()
                try:
                    await watch_task
                except asyncio.CancelledError:
                    pass
            await tracker.app.updater.stop()
            await tracker.app.stop()

    return 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        print("\nStopped.")
        raise SystemExit(0)


if __name__ == "__main__":
    main()
