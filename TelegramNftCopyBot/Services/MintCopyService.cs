using System.Numerics;
using Nethereum.Hex.HexTypes;
using Nethereum.RPC.Eth.DTOs;
using Nethereum.Web3;
using Nethereum.Web3.Accounts;
using TelegramNftCopyBot.Config;
using TelegramNftCopyBot.Models;

namespace TelegramNftCopyBot.Services;

public sealed class MintCopyService
{
    private readonly BotSettings _settings;
    private readonly Web3 _web3;
    private readonly Account _account;
    private readonly HashSet<string> _copiedSourceTxs = new(StringComparer.OrdinalIgnoreCase);
    private readonly object _lock = new();

    public MintCopyService(BotSettings settings)
    {
        _settings = settings;
        _account = new Account(settings.PrivateKey, settings.ChainId);
        _web3 = new Web3(_account, settings.RpcUrl);
    }

    public string MyWalletAddress => _account.Address;

    public bool AlreadyCopied(string sourceTxHash)
    {
        lock (_lock)
            return _copiedSourceTxs.Contains(sourceTxHash);
    }

    public async Task<(bool Ok, string Message, string? TxHash)> TryCopyMintAsync(
        MintCandidate candidate,
        CancellationToken ct)
    {
        lock (_lock)
        {
            if (!_copiedSourceTxs.Add(candidate.SourceTxHash))
                return (false, "Already processed this source tx.", null);
        }

        if (_settings.FreeMintsOnly && candidate.ValueEth > 0)
            return (false, $"Skipped paid mint ({candidate.ValueEth} ETH). FREE_MINTS_ONLY=true.", null);

        try
        {
            var balanceWei = await _web3.Eth.GetBalance.SendRequestAsync(_account.Address);
            var balanceEth = Web3.Convert.FromWei(balanceWei.Value);
            if (balanceEth <= 0)
                return (false, "Your wallet has 0 ETH for gas on Robinhood Chain.", null);

            var gasPrice = await _web3.Eth.GasPrice.SendRequestAsync();
            var nonce = await _web3.Eth.Transactions.GetTransactionCount.SendRequestAsync(
                _account.Address,
                BlockParameter.CreatePending());

            var tx = new TransactionInput
            {
                From = _account.Address,
                To = candidate.ContractAddress,
                Data = candidate.InputData,
                Value = new HexBigInteger(Web3.Convert.ToWei(candidate.ValueEth)),
                Gas = new HexBigInteger(new BigInteger(_settings.GasLimit)),
                GasPrice = gasPrice,
                Nonce = nonce,
                ChainId = new HexBigInteger(_settings.ChainId)
            };

            // Dry-run first so we do not burn gas on obvious reverts (whitelist, sold out, etc.)
            try
            {
                await _web3.Eth.Transactions.Call.SendRequestAsync(tx);
            }
            catch (Exception callEx)
            {
                return (false, $"Simulation reverted (likely whitelist/signature/sold out): {callEx.Message}", null);
            }

            var txHash = await _web3.Eth.TransactionManager.SendTransactionAsync(tx);
            return (true, "Copy mint submitted.", txHash);
        }
        catch (Exception ex)
        {
            return (false, $"Copy mint failed: {ex.Message}", null);
        }
    }
}
