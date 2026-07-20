namespace TelegramNftCopyBot.Config;

public sealed class BotSettings
{
    public string TelegramBotToken { get; init; } = "";
    public long TelegramOwnerId { get; init; }
    public string RpcUrl { get; init; } = "https://rpc.mainnet.chain.robinhood.com";
    public int ChainId { get; init; } = 4663;
    public string TargetWallet { get; init; } = "";
    public string PrivateKey { get; init; } = "";
    public bool FreeMintsOnly { get; init; } = true;
    public int PollIntervalMs { get; init; } = 1200;
    public long GasLimit { get; init; } = 500_000;

    public static BotSettings FromEnvironment()
    {
        static string Req(string key)
        {
            var value = Environment.GetEnvironmentVariable(key);
            if (string.IsNullOrWhiteSpace(value))
                throw new InvalidOperationException($"Missing required env var: {key}");
            return value.Trim();
        }

        static string Opt(string key, string fallback) =>
            Environment.GetEnvironmentVariable(key)?.Trim() is { Length: > 0 } v ? v : fallback;

        return new BotSettings
        {
            TelegramBotToken = Req("TELEGRAM_BOT_TOKEN"),
            TelegramOwnerId = long.Parse(Req("TELEGRAM_OWNER_ID")),
            RpcUrl = Opt("RPC_URL", "https://rpc.mainnet.chain.robinhood.com"),
            ChainId = int.Parse(Opt("CHAIN_ID", "4663")),
            TargetWallet = Req("TARGET_WALLET"),
            PrivateKey = Req("PRIVATE_KEY"),
            FreeMintsOnly = !bool.TryParse(Opt("FREE_MINTS_ONLY", "true"), out var free) || free,
            PollIntervalMs = int.TryParse(Opt("POLL_INTERVAL_MS", "1200"), out var poll) ? poll : 1200,
            GasLimit = long.TryParse(Opt("GAS_LIMIT", "500000"), out var gas) ? gas : 500_000
        };
    }
}
