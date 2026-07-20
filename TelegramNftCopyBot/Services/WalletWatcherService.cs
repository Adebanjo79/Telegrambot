using System.Numerics;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Nethereum.Hex.HexTypes;
using Nethereum.RPC.Eth.DTOs;
using Nethereum.Web3;
using TelegramNftCopyBot.Config;
using TelegramNftCopyBot.Models;

namespace TelegramNftCopyBot.Services;

/// <summary>
/// Polls Robinhood Chain for transactions from the target wallet and
/// attempts to copy free NFT mint-style calls with your wallet.
/// </summary>
public sealed class WalletWatcherService : BackgroundService
{
    // Common mint / claim selectors (first 4 bytes of keccak)
    private static readonly HashSet<string> MintSelectors = new(StringComparer.OrdinalIgnoreCase)
    {
        "0xa0712d68", // mint(uint256)
        "0x40c10f19", // mint(address,uint256)
        "0x6a627842", // mint(address)
        "0x94bf804d", // mint(uint256,address)
        "0xa22cb465", // setApprovalForAll — ignore separately
        "0x1249c58b", // mint()
        "0x449a52f8", // mintTo(address,uint256)
        "0x2db11544", // publicMint(uint256)
        "0x9b4f3af5", // publicMint()
        "0x4e71d92d", // claim()
        "0x84bb1e42", // claim(uint256,...)
        "0x1e83409a", // claim(address)
        "0xd37c353b", // mintPublic(uint256)
    };

    private static readonly string ZeroAddress = "0x0000000000000000000000000000000000000000";

    private readonly BotSettings _settings;
    private readonly MintCopyService _mintCopy;
    private readonly TelegramNotifier _notifier;
    private readonly ILogger<WalletWatcherService> _logger;
    private readonly Web3 _web3;
    private BigInteger _lastBlock;
    private bool _enabled = true;

    public WalletWatcherService(
        BotSettings settings,
        MintCopyService mintCopy,
        TelegramNotifier notifier,
        ILogger<WalletWatcherService> logger)
    {
        _settings = settings;
        _mintCopy = mintCopy;
        _notifier = notifier;
        _logger = logger;
        _web3 = new Web3(settings.RpcUrl);
    }

    public bool Enabled
    {
        get => _enabled;
        set => _enabled = value;
    }

    public string TargetWallet => _settings.TargetWallet;
    public BigInteger LastBlock => _lastBlock;

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        _logger.LogInformation(
            "Watcher started. ChainId={ChainId} Target={Target} MyWallet={Mine}",
            _settings.ChainId,
            _settings.TargetWallet,
            _mintCopy.MyWalletAddress);

        await _notifier.NotifyOwnerAsync(
            $"🟢 NFT copy bot online on Robinhood Chain ({_settings.ChainId})\n" +
            $"Target: `{_settings.TargetWallet}`\n" +
            $"My wallet: `{_mintCopy.MyWalletAddress}`\n" +
            $"Free mints only: {_settings.FreeMintsOnly}",
            stoppingToken);

        var latest = await _web3.Eth.Blocks.GetBlockNumber.SendRequestAsync();
        _lastBlock = latest.Value;

        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                if (_enabled)
                    await PollOnceAsync(stoppingToken);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Poll loop error");
                await _notifier.NotifyOwnerAsync($"⚠️ Watcher error: {ex.Message}", stoppingToken);
            }

            await Task.Delay(_settings.PollIntervalMs, stoppingToken);
        }
    }

    private async Task PollOnceAsync(CancellationToken ct)
    {
        var latest = await _web3.Eth.Blocks.GetBlockNumber.SendRequestAsync();
        var head = latest.Value;
        if (head <= _lastBlock)
            return;

        // Cap catch-up so a long pause does not spam
        var from = BigInteger.Max(_lastBlock + 1, head - 20);

        for (var n = from; n <= head; n++)
        {
            ct.ThrowIfCancellationRequested();
            var block = await _web3.Eth.Blocks.GetBlockWithTransactionsByNumber.SendRequestAsync(
                new HexBigInteger(n));

            if (block?.Transactions == null)
                continue;

            foreach (var tx in block.Transactions)
            {
                if (tx.From == null || tx.To == null)
                    continue;

                if (!tx.From.Equals(_settings.TargetWallet, StringComparison.OrdinalIgnoreCase))
                    continue;

                // Skip self-sends / empty calls
                if (string.IsNullOrWhiteSpace(tx.Input) || tx.Input is "0x" or "0x0")
                    continue;

                var valueEth = Web3.Convert.FromWei(tx.Value?.Value ?? 0);
                if (_settings.FreeMintsOnly && valueEth > 0)
                {
                    _logger.LogInformation("Skip paid tx {Hash} value={Value}", tx.TransactionHash, valueEth);
                    continue;
                }

                var candidate = await BuildCandidateAsync(tx, (ulong)n, valueEth, ct);
                if (candidate == null)
                    continue;

                if (_mintCopy.AlreadyCopied(candidate.SourceTxHash))
                    continue;

                await _notifier.NotifyOwnerAsync(
                    $"👀 Target activity detected\n" +
                    $"Block: {candidate.BlockNumber}\n" +
                    $"Contract: `{candidate.ContractAddress}`\n" +
                    $"Value: {candidate.ValueEth} ETH\n" +
                    $"Hint: {candidate.MethodHint ?? "unknown"}\n" +
                    $"Source: `{candidate.SourceTxHash}`\n" +
                    $"Copying with my wallet…",
                    ct);

                var (ok, message, copyHash) = await _mintCopy.TryCopyMintAsync(candidate, ct);
                if (ok)
                {
                    await _notifier.NotifyOwnerAsync(
                        $"✅ Copy mint sent\nTx: `{copyHash}`\n{message}",
                        ct);
                }
                else
                {
                    await _notifier.NotifyOwnerAsync(
                        $"❌ Copy mint not sent\n{message}",
                        ct);
                }
            }
        }

        _lastBlock = head;
    }

    private async Task<MintCandidate?> BuildCandidateAsync(
        Transaction tx,
        ulong blockNumber,
        decimal valueEth,
        CancellationToken ct)
    {
        var input = tx.Input ?? "0x";
        var selector = input.Length >= 10 ? input[..10] : input;
        var methodHint = MintSelectors.Contains(selector) ? $"selector {selector}" : $"call {selector}";

        var looksLikeMint = MintSelectors.Contains(selector);
        var receiptMint = false;

        try
        {
            var receipt = await _web3.Eth.Transactions.GetTransactionReceipt.SendRequestAsync(tx.TransactionHash);
            if (receipt?.Logs != null)
            {
                // ERC-721/1155 Transfer topic0
                const string transferTopic =
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef";

                foreach (var log in receipt.Logs)
                {
                    // Nethereum may surface logs as FilterLog or JToken depending on version
                    var topics = ExtractTopics(log);
                    if (topics.Count < 4)
                        continue;

                    if (!string.Equals(topics[0], transferTopic, StringComparison.OrdinalIgnoreCase))
                        continue;

                    var fromTopic = topics[1];
                    if (fromTopic.EndsWith(ZeroAddress[2..], StringComparison.OrdinalIgnoreCase) ||
                        fromTopic.Equals(ZeroAddress, StringComparison.OrdinalIgnoreCase) ||
                        fromTopic.Contains(new string('0', 40), StringComparison.OrdinalIgnoreCase))
                    {
                        receiptMint = true;
                        looksLikeMint = true;
                        methodHint = "ERC-721 mint (Transfer from 0x0)";
                        break;
                    }
                }
            }
        }
        catch (Exception ex)
        {
            _logger.LogDebug(ex, "Receipt check failed for {Hash}", tx.TransactionHash);
        }

        // Only act on mint-looking activity to avoid copying random approvals/swaps
        if (!looksLikeMint && !receiptMint)
        {
            _logger.LogInformation(
                "Target tx {Hash} to {To} ignored (not a recognized mint).",
                tx.TransactionHash,
                tx.To);
            return null;
        }

        ct.ThrowIfCancellationRequested();

        return new MintCandidate
        {
            SourceTxHash = tx.TransactionHash,
            ContractAddress = tx.To!,
            InputData = input,
            ValueEth = valueEth,
            BlockNumber = blockNumber,
            MethodHint = methodHint,
            LooksLikeNftMint = looksLikeMint
        };
    }

    private static List<string> ExtractTopics(object log)
    {
        var result = new List<string>();
        try
        {
            if (log is FilterLog filterLog && filterLog.Topics != null)
            {
                foreach (var t in filterLog.Topics)
                    result.Add(t?.ToString() ?? "");
                return result;
            }

            // JToken / dynamic JSON log shape: { topics: ["0x..", ...] }
            var topicsProp = log.GetType().GetProperty("Topics");
            if (topicsProp?.GetValue(log) is System.Collections.IEnumerable topicsEnum)
            {
                foreach (var t in topicsEnum)
                    result.Add(t?.ToString() ?? "");
                return result;
            }

            var token = Newtonsoft.Json.Linq.JToken.FromObject(log);
            foreach (var t in token["topics"] ?? token["Topics"] ?? new Newtonsoft.Json.Linq.JArray())
                result.Add(t?.ToString() ?? "");
        }
        catch
        {
            // ignore malformed logs
        }

        return result;
    }
}
