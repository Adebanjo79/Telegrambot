from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from bot.config import Settings
from bot.mint_copy import MintCopyService
from bot.watcher import WalletWatcher

log = logging.getLogger(__name__)


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
            f"{self.settings.network_name} NFT copy bot\n\n"
            "/status — watcher + wallets\n"
            "/targets — wallets being copied\n"
            "/pause — stop copying\n"
            "/resume — start copying\n"
            f"/balance — your ETH on {self.settings.network_name}\n"
            "/help — this message\n\n"
            "Alerts are pushed here when a watched wallet mints."
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            await update.message.reply_text("Unauthorized.")
            return
        mode = "DRY_RUN" if self.settings.dry_run else "LIVE"
        await update.message.reply_text(
            f"Enabled: {self.watcher.enabled}\n"
            f"Mode: {mode}\n"
            f"Network: {self.settings.network_name} ({self.settings.chain_id})\n"
            f"RPC: {self.settings.rpc_url}\n"
            f"My wallet: {self.mint_copy.my_wallet}\n"
            f"Last block: {self.watcher.last_block}\n"
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
            bal = self.mint_copy.eth_balance()
            await update.message.reply_text(
                f"Balance: {bal} ETH\nWallet: {self.mint_copy.my_wallet}\n"
                f"{self.explorer_address(self.mint_copy.my_wallet)}"
            )
        except Exception as exc:
            await update.message.reply_text(f"Balance check failed: {exc}")
