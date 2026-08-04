from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class MintCandidate:
    source_tx_hash: str
    contract_address: str
    input_data: str
    value_wei: int
    value_eth: Decimal
    block_number: int
    method_hint: str
    target_wallet: str
