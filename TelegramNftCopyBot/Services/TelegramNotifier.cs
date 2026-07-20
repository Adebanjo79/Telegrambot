using Telegram.Bot;
using Telegram.Bot.Types.Enums;
using TelegramNftCopyBot.Config;

namespace TelegramNftCopyBot.Services;

public sealed class TelegramNotifier
{
    private readonly ITelegramBotClient _bot;
    private readonly long _ownerId;

    public TelegramNotifier(ITelegramBotClient bot, BotSettings settings)
    {
        _bot = bot;
        _ownerId = settings.TelegramOwnerId;
    }

    public async Task NotifyOwnerAsync(string text, CancellationToken ct = default)
    {
        try
        {
            await _bot.SendMessage(
                chatId: _ownerId,
                text: text,
                parseMode: ParseMode.Markdown,
                cancellationToken: ct);
        }
        catch
        {
            // Fallback without markdown if user id formatting breaks parse mode
            await _bot.SendMessage(
                chatId: _ownerId,
                text: text.Replace("`", ""),
                cancellationToken: ct);
        }
    }
}
