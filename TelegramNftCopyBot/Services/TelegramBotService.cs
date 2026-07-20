using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Nethereum.Web3;
using Telegram.Bot;
using Telegram.Bot.Polling;
using Telegram.Bot.Types;
using Telegram.Bot.Types.Enums;
using TelegramNftCopyBot.Config;

namespace TelegramNftCopyBot.Services;

public sealed class TelegramBotService : BackgroundService
{
    private readonly ITelegramBotClient _bot;
    private readonly BotSettings _settings;
    private readonly WalletWatcherService _watcher;
    private readonly MintCopyService _mintCopy;
    private readonly ILogger<TelegramBotService> _logger;
    private readonly Web3 _web3;

    public TelegramBotService(
        ITelegramBotClient bot,
        BotSettings settings,
        WalletWatcherService watcher,
        MintCopyService mintCopy,
        ILogger<TelegramBotService> logger)
    {
        _bot = bot;
        _settings = settings;
        _watcher = watcher;
        _mintCopy = mintCopy;
        _logger = logger;
        _web3 = new Web3(settings.RpcUrl);
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        var me = await _bot.GetMe(stoppingToken);
        _logger.LogInformation("Telegram bot @{User} ready", me.Username);

        var receiverOptions = new ReceiverOptions
        {
            AllowedUpdates = [UpdateType.Message]
        };

        _bot.StartReceiving(
            HandleUpdateAsync,
            HandleErrorAsync,
            receiverOptions,
            stoppingToken);

        await Task.Delay(Timeout.Infinite, stoppingToken);
    }

    private async Task HandleUpdateAsync(ITelegramBotClient bot, Update update, CancellationToken ct)
    {
        var msg = update.Message;
        if (msg?.Text is null || msg.From is null)
            return;

        if (msg.From.Id != _settings.TelegramOwnerId)
        {
            await bot.SendMessage(msg.Chat.Id, "Unauthorized.", cancellationToken: ct);
            return;
        }

        var text = msg.Text.Trim();
        var parts = text.Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        var cmd = parts[0].Split('@')[0].ToLowerInvariant();

        switch (cmd)
        {
            case "/start":
            case "/help":
                await bot.SendMessage(
                    msg.Chat.Id,
                    "Robinhood Chain NFT copy-mint bot\n\n" +
                    "/status — watcher + wallets\n" +
                    "/pause — stop copying\n" +
                    "/resume — start copying\n" +
                    "/balance — your ETH on Robinhood Chain\n" +
                    "/help — this message\n\n" +
                    "Set TARGET_WALLET and PRIVATE_KEY in .env before running.",
                    cancellationToken: ct);
                break;

            case "/status":
                await bot.SendMessage(
                    msg.Chat.Id,
                    $"Enabled: {_watcher.Enabled}\n" +
                    $"Network: Robinhood Chain ({_settings.ChainId})\n" +
                    $"RPC: {_settings.RpcUrl}\n" +
                    $"Target: {_watcher.TargetWallet}\n" +
                    $"My wallet: {_mintCopy.MyWalletAddress}\n" +
                    $"Last block: {_watcher.LastBlock}\n" +
                    $"Free mints only: {_settings.FreeMintsOnly}",
                    cancellationToken: ct);
                break;

            case "/pause":
                _watcher.Enabled = false;
                await bot.SendMessage(msg.Chat.Id, "⏸ Watcher paused.", cancellationToken: ct);
                break;

            case "/resume":
                _watcher.Enabled = true;
                await bot.SendMessage(msg.Chat.Id, "▶️ Watcher resumed.", cancellationToken: ct);
                break;

            case "/balance":
            {
                var bal = await _web3.Eth.GetBalance.SendRequestAsync(_mintCopy.MyWalletAddress);
                var eth = Web3.Convert.FromWei(bal.Value);
                await bot.SendMessage(
                    msg.Chat.Id,
                    $"Balance: {eth} ETH\nWallet: {_mintCopy.MyWalletAddress}",
                    cancellationToken: ct);
                break;
            }

            default:
                await bot.SendMessage(msg.Chat.Id, "Unknown command. Try /help", cancellationToken: ct);
                break;
        }
    }

    private Task HandleErrorAsync(ITelegramBotClient bot, Exception exception, CancellationToken ct)
    {
        _logger.LogError(exception, "Telegram polling error");
        return Task.CompletedTask;
    }
}
