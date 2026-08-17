from __future__ import annotations

import logging
import re

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from bot.config import Settings
from bot.mint_copy import MintCopyService
from bot.version import BOT_VERSION
from bot.watcher import WalletWatcher

log = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


class TelegramTracker:
    """Owner-only Telegram commands + push alerts for copy activity."""

    def __init__(
        self,
        settings: Settings,
        watcher: WalletWatcher,
        mint_copy: MintCopyService,
    ) -> None:
        self.settings = settings
        self.watcher = watcher
        self.mint_copy = mint_copy
        self.rpc_health = "OK"
        self.rpc_last_error = ""
        self.rpc_per_sec = 0.0
        self.rpc_avg_ms = 0.0
        self.app = (
            Application.builder()
            .token(settings.telegram_bot_token)
            .build()
        )
        self.app.add_handler(CommandHandler("start", self.cmd_help))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("status", self.cmd_status))
        self.app.add_handler(CommandHandler("pause", self.cmd_pause))
        self.app.add_handler(CommandHandler("resume", self.cmd_resume))
        self.app.add_handler(CommandHandler("balance", self.cmd_balance))
        self.app.add_handler(CommandHandler("targets", self.cmd_targets))
        self.app.add_handler(CommandHandler("wallets", self.cmd_wallets))
        self.app.add_handler(CommandHandler("addwallet", self.cmd_addwallet))
        self.app.add_handler(CommandHandler("removewallet", self.cmd_removewallet))
        # Also accept a bare private key message from owner (DM only recommended)
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text_key)
        )

    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        return bool(user and user.id == self.settings.telegram_owner_id)

    async def notify(self, text: str) -> None:
        try:
            await self.app.bot.send_message(
                chat_id=self.settings.telegram_owner_id,
                text=text,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Failed to send Telegram notify")

    def explorer_tx(self, tx_hash: str) -> str:
        return f"{self.settings.explorer_url}/tx/{tx_hash}"

    def explorer_address(self, address: str) -> str:
        return f"{self.settings.explorer_url}/address/{address}"

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await update.message.reply_text("Unauthorized.")
            return
        await update.message.reply_text(
            "Robinhood Chain NFT copy bot\n\n"
            "/status — watcher + wallets + build\n"
            "/targets — wallets being copied\n"
            "/wallets — your minting wallets\n"
            "/addwallet <private_key> — add minting wallet\n"
            "/removewallet <address> — remove Telegram-added wallet\n"
            "/pause — stop copying\n"
            "/resume — start copying\n"
            "/balance — ETH balances for minting wallets\n"
            "/help — this message\n\n"
            "⚠️ Only send private keys in a private chat with this bot.\n"
            "Delete the message after adding."
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await update.message.reply_text("Unauthorized.")
            return
        mode = "DRY_RUN" if self.settings.dry_run else "LIVE"
        provider = getattr(self.mint_copy.w3, "provider", None)
        active_rpc = getattr(provider, "active_endpoint", self.settings.rpc_url)
        balancing = bool(getattr(provider, "load_balance", False))
        node_count = len(self.settings.rpc_urls)
        rpc_line = f"RPC health: {self.rpc_health}"
        if node_count > 1:
            mode = "sharing load across" if balancing else "failover across"
            rpc_line += f" ({mode} {node_count} nodes)"
        limit = self.settings.rpc_rate_limit * (node_count if balancing else 1)
        used_pct = (self.rpc_per_sec / limit * 100.0) if limit > 0 else 0.0
        rpc_line += (
            f"\nRPC load: {self.rpc_per_sec:.1f}/{limit:.0f} req/s "
            f"({used_pct:.0f}% of plan), {self.rpc_avg_ms:.0f} ms avg"
        )
        counts = getattr(provider, "endpoint_requests", None)
        if counts and node_count > 1:
            split = ", ".join(
                f"{url.split('/')[2].split('.')[0]}: {n}" for url, n in counts.items()
            )
            rpc_line += f"\nRequests per node: {split}"
        if self.rpc_last_error:
            rpc_line += f"\nRPC last error: {self.rpc_last_error}"
        await update.message.reply_text(
            f"Build: {BOT_VERSION}\n"
            f"Enabled: {self.watcher.enabled}\n"
            f"Mode: {mode}\n"
            f"Network: Robinhood Chain ({self.settings.chain_id})\n"
            f"RPC: {active_rpc}\n"
            f"{rpc_line}\n"
            f"Minting wallets: {len(self.mint_copy.my_wallets)} "
            f"({len(self.settings.private_keys)} .env + "
            f"{max(0, len(self.mint_copy.my_wallets) - len(self.settings.private_keys))} file)\n"
            f"Primary wallet: {self.mint_copy.my_wallet}\n"
            f"Last block: {self.watcher.last_block}\n"
            f"Catch-up lag: {getattr(self.watcher, 'lag_blocks', 0)} blocks\n"
            f"Free mints only: {self.settings.free_mints_only}\n"
            f"Targets: {len(self.settings.target_wallets)}"
        )

    async def cmd_targets(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await update.message.reply_text("Unauthorized.")
            return
        lines = ["Watching:"]
        for addr in self.settings.target_wallets:
            lines.append(f"• {addr}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_wallets(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await update.message.reply_text("Unauthorized.")
            return
        lines = ["Minting wallets:"]
        for addr in self.mint_copy.my_wallets:
            lines.append(f"• {addr}")
        await update.message.reply_text("\n".join(lines))

    async def cmd_addwallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await update.message.reply_text("Unauthorized.")
            return
        if not context.args:
            await update.message.reply_text(
                "Usage:\n/addwallet 0xyourprivatekey\n\n"
                "Or just paste the private key alone in this private chat."
            )
            return
        raw = context.args[0].strip()
        try:
            addr = self.mint_copy.add_private_key(raw)
        except Exception as exc:
            await update.message.reply_text(f"Failed to add wallet: {exc}")
            return
        await update.message.reply_text(
            f"✅ Added minting wallet:\n{addr}\n"
            f"Total minting wallets: {len(self.mint_copy.my_wallets)}\n\n"
            "Please delete your /addwallet message that contains the key."
        )

    async def cmd_removewallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await update.message.reply_text("Unauthorized.")
            return
        if not context.args:
            await update.message.reply_text("Usage:\n/removewallet 0xAddress")
            return
        try:
            removed = self.mint_copy.remove_wallet(context.args[0].strip())
        except Exception as exc:
            await update.message.reply_text(f"Failed: {exc}")
            return
        if not removed:
            await update.message.reply_text("Wallet not found in Telegram-added list.")
            return
        await update.message.reply_text(
            f"🗑 Removed.\nMinting wallets now: {len(self.mint_copy.my_wallets)}"
        )

    async def on_text_key(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update) or not update.message or not update.message.text:
            return
        text = update.message.text.strip()
        if not _KEY_RE.match(text):
            return
        try:
            addr = self.mint_copy.add_private_key(text)
        except Exception as exc:
            await update.message.reply_text(f"Failed to add wallet: {exc}")
            return
        await update.message.reply_text(
            f"✅ Added minting wallet:\n{addr}\n"
            f"Total minting wallets: {len(self.mint_copy.my_wallets)}\n\n"
            "Please delete the message that contained your private key."
        )

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await update.message.reply_text("Unauthorized.")
            return
        self.watcher.enabled = False
        await update.message.reply_text("⏸ Watcher paused.")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await update.message.reply_text("Unauthorized.")
            return
        self.watcher.enabled = True
        await update.message.reply_text("▶️ Watcher resumed.")

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await update.message.reply_text("Unauthorized.")
            return
        try:
            lines = ["Balances:"]
            for wallet, bal in self.mint_copy.all_balances():
                lines.append(f"{wallet}\n{bal} ETH")
            await update.message.reply_text("\n\n".join(lines))
        except Exception as exc:
            await update.message.reply_text(f"Balance check failed: {exc}")
