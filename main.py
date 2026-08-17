from __future__ import annotations

import asyncio
import logging
import subprocess
import sys

from web3 import Web3

from bot.config import Settings
from bot.mint_copy import MintCopyService
from bot.pending_watcher import PendingWalletWatcher
from bot.provider import FailoverHTTPProvider
from bot.version import BOT_VERSION
from bot.rpc import (
    is_rpc_capacity_error,
    is_transient_rpc_error,
    rpc_capacity_message,
)
from bot.telegram_bot import TelegramTracker
from bot.watcher import WalletWatcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def code_version() -> str:
    """Pinned version + git SHA so Telegram proves which code is running."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        sha = out.strip() or "unknown"
    except Exception:
        sha = "unknown"
    return f"{BOT_VERSION} ({sha})"


def build_web3(
    rpc_urls: tuple[str, ...] | list[str],
    load_balance: bool = False,
    max_rps: float = 20.0,
    failback_after_sec: float = 30.0,
) -> Web3:
    # Leave ~20% headroom under the plan cap so mint bursts don't 429.
    provider = FailoverHTTPProvider(
        list(rpc_urls),
        load_balance=load_balance,
        max_rps=max(1.0, max_rps * 0.8),
        failback_after_sec=failback_after_sec,
        request_kwargs={"timeout": 45},
    )
    w3 = Web3(provider)
    if not w3.is_connected():
        raise RuntimeError(f"Cannot connect to any RPC: {', '.join(rpc_urls)}")
    return w3


async def check_rpc_load(
    tracker: TelegramTracker,
    provider: FailoverHTTPProvider,
    now: float,
    was_warned: bool,
    last_alert: float,
) -> tuple[bool, float]:
    """
    Warn before the RPC quota is actually hit, and when it turns slow.

    Returns the updated (was_warned, last_alert) pair.
    """
    settings = tracker.settings
    stats = provider.load_stats()
    per_sec = stats["per_sec"]
    avg_ms = stats["avg_ms"]
    tracker.rpc_per_sec = per_sec
    tracker.rpc_avg_ms = avg_ms

    # Load balancing spreads requests, so the usable cap is per-node × nodes.
    nodes = len(provider.endpoints) if provider.load_balance else 1
    limit = settings.rpc_rate_limit * nodes
    threshold = limit * (settings.rpc_warn_percent / 100.0)
    used_pct = (per_sec / limit * 100.0) if limit > 0 else 0.0

    problems: list[str] = []
    if limit > 0 and per_sec >= threshold:
        problems.append(
            f"Quota almost full: {per_sec:.1f}/{limit:.0f} requests per second "
            f"({used_pct:.0f}% of your plan)"
        )
    if stats["requests"] >= 3 and avg_ms >= settings.rpc_slow_ms:
        problems.append(f"RPC is slow: {avg_ms:.0f} ms average response")

    if problems:
        tracker.rpc_health = "BUSY" if "Quota" in problems[0] else "SLOW"
        if not was_warned or now - last_alert >= 180:
            body = "\n".join(f"• {p}" for p in problems)
            try:
                await tracker.notify(
                    "⚠️ RPC is getting close to its limit\n"
                    f"{body}\n"
                    "Mints may be missed if it keeps climbing. "
                    "Consider upgrading the node or adding a backup to RPC_URLS."
                )
            except Exception:
                pass
            return True, now
        return True, last_alert

    if was_warned:
        tracker.rpc_health = "OK"
        try:
            await tracker.notify(
                "✅ RPC back to normal\n"
                f"{per_sec:.1f} requests per second, {avg_ms:.0f} ms average."
            )
        except Exception:
            pass
    return False, last_alert


async def handle_candidate(
    tracker: TelegramTracker,
    mint_copy: MintCopyService,
    candidate,
    state: dict,
) -> None:
    settings = tracker.settings
    if mint_copy.already_copied(candidate.source_tx_hash):
        return

    # Start copying before collection metadata or Telegram. Neither should
    # delay a short SeaDrop window.
    copy_task = asyncio.create_task(
        asyncio.to_thread(mint_copy.try_copy_all, candidate)
    )
    try:
        collection_name, collection_address = await asyncio.wait_for(
            asyncio.to_thread(mint_copy.collection_info, candidate),
            timeout=3.0,
        )
    except Exception:
        collection_name = "Unknown collection"
        collection_address = candidate.contract_address

    # Fire the "seen it" alert without waiting for Telegram's response.
    block_label = (
        "pending (mempool)"
        if candidate.block_number <= 0
        else str(candidate.block_number)
    )
    detect_msg = (
        "👀 Target mint activity\n"
        f"Collection: {collection_name}\n"
        f"Collection contract: {collection_address}\n"
        f"From: {candidate.target_wallet}\n"
        f"Block: {block_label}\n"
        f"Mint contract: {candidate.contract_address}\n"
        f"Value: {candidate.value_eth} ETH\n"
        f"Hint: {candidate.method_hint}\n"
        f"Source: {tracker.explorer_tx(candidate.source_tx_hash)}\n"
        f"Minting wallets: {len(mint_copy.my_wallets)} (parallel)\n"
        f"{'Simulating (DRY_RUN)…' if settings.dry_run else 'Copying…'}"
    )
    detect_task = asyncio.create_task(tracker.notify(detect_msg))

    results = await copy_task
    try:
        await detect_task
    except Exception:
        log.exception("Failed notifying mint detection")
    ok_n = sum(1 for _, ok, _, _ in results if ok)
    fail_n = len(results) - ok_n
    lines = [
        f"Done for this mint: {ok_n} ok, {fail_n} failed "
        f"(tried {len(results)} wallets).\n"
        f"Collection: {collection_name}\n"
        f"Collection contract: {collection_address}"
    ]
    fail_msgs = {msg for _, ok, msg, _ in results if not ok}
    if ok_n == 0 and len(fail_msgs) == 1:
        lines.append(f"❌ All {fail_n} wallets: {next(iter(fail_msgs))}")
    else:
        for wallet, ok, message, copy_hash in results:
            short = f"{wallet[:6]}…{wallet[-4:]}"
            if ok and copy_hash:
                lines.append(
                    f"✅ {short}\n"
                    f"{tracker.explorer_tx(copy_hash)}\n"
                    f"{message}"
                )
            elif ok:
                lines.append(f"✅ {short}: {message}")
            else:
                lines.append(f"❌ {short}: {message}")
    text = "\n\n".join(lines)
    try:
        if len(text) <= 4000:
            await tracker.notify(text)
        else:
            chunk: list[str] = [lines[0]]
            size = len(lines[0])
            for line in lines[1:]:
                add = len(line) + 2
                if size + add > 4000:
                    await tracker.notify("\n\n".join(chunk))
                    chunk = [line]
                    size = len(line)
                else:
                    chunk.append(line)
                    size += add
            if chunk:
                await tracker.notify("\n\n".join(chunk))
    except Exception:
        log.exception("Failed notifying mint results")

    rate_hits = sum(
        1
        for _, ok, msg, _ in results
        if (not ok)
        and ("429" in msg.lower() or "too many requests" in msg.lower())
    )
    if rate_hits:
        state["rpc_was_full"] = True
        tracker.rpc_health = "FULL"
        tracker.rpc_last_error = f"{rate_hits} wallet(s) hit RPC 429 during mint"
        now = asyncio.get_running_loop().time()
        if now - state["last_capacity_alert"] >= 10:
            state["last_capacity_alert"] = now
            try:
                await tracker.notify(
                    "🚨 RPC FULL / rate-limited\n"
                    f"{rate_hits} of {len(results)} minting wallets "
                    "got Too Many Requests during this mint.\n"
                    "Bot will keep retrying / use backup RPC if configured."
                )
            except Exception:
                pass


async def mint_worker(
    queue: asyncio.Queue,
    tracker: TelegramTracker,
    mint_copy: MintCopyService,
    state: dict,
) -> None:
    """Copy mints in the background so the block watcher never pauses."""
    while True:
        candidate = await queue.get()
        try:
            await handle_candidate(tracker, mint_copy, candidate, state)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Mint worker failed for %s", candidate.source_tx_hash)
            try:
                await tracker.notify(
                    f"⚠️ Mint worker error for {candidate.source_tx_hash}"
                )
            except Exception:
                pass
        finally:
            queue.task_done()


async def wallet_state_loop(mint_copy: MintCopyService) -> None:
    """Continuously pre-cache one wallet at a time without creating RPC bursts."""
    while True:
        wallets = list(mint_copy.my_wallets)
        if not wallets:
            await asyncio.sleep(1)
            continue
        spacing = max(
            0.2,
            mint_copy.settings.wallet_state_refresh_sec / len(wallets),
        )
        for wallet in wallets:
            try:
                await asyncio.to_thread(mint_copy.refresh_wallet_state, wallet)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Wallet state refresh failed for %s", wallet)
            await asyncio.sleep(spacing)


async def pending_watch_loop(
    pending_watcher: PendingWalletWatcher,
    watcher: WalletWatcher,
    mint_copy: MintCopyService,
    mint_queue: asyncio.Queue,
) -> None:
    """Queue target mints seen in the mempool before block confirmation."""

    async def _queue(candidate) -> None:
        if not watcher.enabled:
            return
        if mint_copy.already_copied(candidate.source_tx_hash):
            return
        await mint_queue.put(candidate)

    await pending_watcher.run(_queue)


async def watch_loop(
    tracker: TelegramTracker,
    watcher: WalletWatcher,
    mint_copy: MintCopyService,
    mint_queue: asyncio.Queue,
    state: dict,
) -> None:
    settings = tracker.settings
    last_blip_alert = 0.0
    provider = getattr(watcher.w3, "provider", None)
    seen_switches = getattr(provider, "switch_count", 0)
    last_load_check = 0.0
    last_load_alert = 0.0
    last_failback_check = 0.0
    load_warned = False
    last_lag_alert = 0.0
    while True:
        try:
            # Report both failover (to backup) and failback (to primary).
            switches = getattr(provider, "switch_count", 0)
            if switches > seen_switches:
                seen_switches = switches
                kind = getattr(provider, "last_switch_kind", "failover")
                try:
                    if kind == "failback":
                        await tracker.notify(
                            "↩️ Back on primary RPC (Chainstack)\n"
                            f"Now using: {provider.active_endpoint}\n"
                            "Backup Alchemy is idle again."
                        )
                    else:
                        await tracker.notify(
                            "🔁 Switched to backup RPC\n"
                            f"Now using: {provider.active_endpoint}\n"
                            f"Reason: {provider.last_failover_reason}\n"
                            "Will return to Chainstack automatically when it recovers."
                        )
                except Exception:
                    pass

            now = asyncio.get_running_loop().time()
            if provider is not None and now - last_failback_check >= 15:
                last_failback_check = now
                try:
                    did_failback = await asyncio.to_thread(provider.maybe_failback)
                except Exception:
                    log.exception("RPC failback probe failed")
                    did_failback = False
                if did_failback:
                    seen_switches = getattr(provider, "switch_count", seen_switches)
                    try:
                        await tracker.notify(
                            "↩️ Back on primary RPC (Chainstack)\n"
                            f"Now using: {provider.active_endpoint}\n"
                            "Backup Alchemy is idle again."
                        )
                    except Exception:
                        pass

            if provider is not None and now - last_load_check >= 10:
                last_load_check = now
                load_warned, last_load_alert = await check_rpc_load(
                    tracker, provider, now, load_warned, last_load_alert
                )

            if watcher.enabled:
                candidates = await asyncio.to_thread(watcher.poll)
                if state.get("rpc_was_full"):
                    state["rpc_was_full"] = False
                    tracker.rpc_health = "OK"
                    tracker.rpc_last_error = ""
                    try:
                        await tracker.notify(
                            "✅ RPC recovered — requests are working again."
                        )
                    except Exception:
                        pass

                if watcher.lag_blocks > settings.max_catchup_blocks and now - last_lag_alert > 120:
                    last_lag_alert = now
                    try:
                        await tracker.notify(
                            f"⏳ Catching up: {watcher.lag_blocks} blocks behind "
                            "(no blocks skipped — scanning as fast as RPC allows)."
                        )
                    except Exception:
                        pass

                for candidate in candidates:
                    if mint_copy.already_copied(candidate.source_tx_hash):
                        continue
                    await mint_queue.put(candidate)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            now = asyncio.get_running_loop().time()
            if is_rpc_capacity_error(exc):
                log.warning("RPC FULL / rate-limited (will retry): %s", exc)
                first_full = not state.get("rpc_was_full")
                state["rpc_was_full"] = True
                tracker.rpc_health = "FULL"
                tracker.rpc_last_error = str(exc)
                # Always alert on first hit; remind every 15s while still full.
                if first_full or now - state["last_capacity_alert"] >= 15:
                    state["last_capacity_alert"] = now
                    try:
                        await tracker.notify(rpc_capacity_message(exc))
                    except Exception:
                        pass
                await asyncio.sleep(max(settings.poll_interval_sec, 2.0))
                continue

            if is_transient_rpc_error(exc):
                log.warning("Transient RPC error (will retry): %s", exc)
                tracker.rpc_health = "BLIP"
                tracker.rpc_last_error = str(exc)
                if now - last_blip_alert > 120:
                    last_blip_alert = now
                    try:
                        await tracker.notify(
                            "⚠️ RPC connection blip (retrying).\n"
                            f"Detail: {exc}"
                        )
                    except Exception:
                        pass
                await asyncio.sleep(max(settings.poll_interval_sec, 2.0))
                continue

            log.exception("Poll loop error")
            tracker.rpc_health = "ERROR"
            tracker.rpc_last_error = str(exc)
            try:
                await tracker.notify(f"⚠️ Watcher error: {exc}")
            except Exception:
                pass

        # Poll faster when catching up so free mints are not delayed.
        delay = settings.poll_interval_sec
        if watcher.lag_blocks > 0:
            delay = min(delay, 0.35)
        await asyncio.sleep(delay)


async def async_main() -> int:
    try:
        settings = Settings.load()
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        print("Copy .env.example to .env and fill in the values.", file=sys.stderr)
        return 1

    w3 = build_web3(
        settings.rpc_urls,
        settings.rpc_load_balance,
        settings.rpc_rate_limit,
        settings.rpc_failback_sec,
    )
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
    # Warm all wallets before watching starts. Normal mint prep then performs
    # no balance/nonce RPC calls.
    try:
        await asyncio.to_thread(mint_copy.refresh_wallet_state)
    except Exception:
        log.exception("Initial wallet state warmup failed; live fallback remains enabled")
    pending_watcher = PendingWalletWatcher(settings, watcher)
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

        env_n = len(settings.private_keys)
        total_n = len(mint_copy.my_wallets)
        file_n = max(0, total_n - env_n)
        await tracker.notify(
            f"🟢 NFT copy bot online ({mode})\n"
            f"Build: {code_version()}\n"
            f"Robinhood Chain id {settings.chain_id}\n"
            f"Targets: {len(settings.target_wallets)}\n"
            f"Minting wallets: {total_n} "
            f"({env_n} from .env, {file_n} from mint_wallets.json)\n"
            f"Primary wallet: {mint_copy.my_wallet}\n"
            f"RPC endpoints: {len(settings.rpc_urls)}"
            f"{' (auto failover)' if len(settings.rpc_urls) > 1 else ''}\n"
            f"RPC alerts at {settings.rpc_warn_percent:.0f}% of "
            f"{settings.rpc_rate_limit:.0f} req/s\n"
            f"Pending detection: {'ON' if settings.pending_detection else 'OFF'}\n"
            f"Wallet state cache: {settings.wallet_state_refresh_sec:g}s refresh\n"
            f"Free mints only: {settings.free_mints_only}\n"
            f"Starting at block {head}"
        )

        watch_task = None
        worker_task = None
        cache_task = None
        pending_task = None
        mint_queue: asyncio.Queue = asyncio.Queue()
        state = {"rpc_was_full": False, "last_capacity_alert": 0.0}
        try:
            worker_task = asyncio.create_task(
                mint_worker(mint_queue, tracker, mint_copy, state)
            )
            cache_task = asyncio.create_task(wallet_state_loop(mint_copy))
            if settings.pending_detection:
                pending_task = asyncio.create_task(
                    pending_watch_loop(
                        pending_watcher,
                        watcher,
                        mint_copy,
                        mint_queue,
                    )
                )
            watch_task = asyncio.create_task(
                watch_loop(tracker, watcher, mint_copy, mint_queue, state)
            )
            await watch_task
        except asyncio.CancelledError:
            pass
        finally:
            for task in (watch_task, worker_task, cache_task, pending_task):
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
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
