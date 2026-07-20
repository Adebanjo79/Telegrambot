namespace TelegramNftCopyBot.Models;

public sealed class MintCandidate
{
    public required string SourceTxHash { get; init; }
    public required string ContractAddress { get; init; }
    public required string InputData { get; init; }
    public required decimal ValueEth { get; init; }
    public required ulong BlockNumber { get; init; }
    public string? MethodHint { get; init; }
    public bool LooksLikeNftMint { get; init; }
}
