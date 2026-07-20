using DotNetEnv;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Telegram.Bot;
using TelegramNftCopyBot.Config;
using TelegramNftCopyBot.Services;

// Load .env from the project / output directory when present
var envPath = Path.Combine(AppContext.BaseDirectory, ".env");
if (File.Exists(envPath))
    Env.Load(envPath);
else if (File.Exists(".env"))
    Env.Load(".env");
else if (File.Exists(Path.Combine("TelegramNftCopyBot", ".env")))
    Env.Load(Path.Combine("TelegramNftCopyBot", ".env"));

BotSettings settings;
try
{
    settings = BotSettings.FromEnvironment();
}
catch (Exception ex)
{
    Console.Error.WriteLine("Config error: " + ex.Message);
    Console.Error.WriteLine("Copy TelegramNftCopyBot/.env.example to TelegramNftCopyBot/.env and fill values.");
    return 1;
}

if (!settings.TargetWallet.StartsWith("0x", StringComparison.OrdinalIgnoreCase) ||
    settings.TargetWallet.Length != 42)
{
    Console.Error.WriteLine("TARGET_WALLET must be a 0x-prefixed 42-character address.");
    return 1;
}

var host = Host.CreateDefaultBuilder(args)
    .ConfigureLogging(l =>
    {
        l.ClearProviders();
        l.AddSimpleConsole(o =>
        {
            o.SingleLine = true;
            o.TimestampFormat = "HH:mm:ss ";
        });
        l.SetMinimumLevel(LogLevel.Information);
    })
    .ConfigureServices(services =>
    {
        services.AddSingleton(settings);
        services.AddSingleton<ITelegramBotClient>(_ => new TelegramBotClient(settings.TelegramBotToken));
        services.AddSingleton<TelegramNotifier>();
        services.AddSingleton<MintCopyService>();
        services.AddSingleton<WalletWatcherService>();
        services.AddHostedService(sp => sp.GetRequiredService<WalletWatcherService>());
        services.AddHostedService<TelegramBotService>();
    })
    .Build();

Console.WriteLine("Starting Telegram NFT copy-mint bot on Robinhood Chain...");
Console.WriteLine($"Target wallet : {settings.TargetWallet}");
Console.WriteLine($"RPC           : {settings.RpcUrl}");
Console.WriteLine("Press Ctrl+C to stop.");

await host.RunAsync();
return 0;
