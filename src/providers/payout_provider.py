# src/providers/payout_provider.py
from abc import ABC, abstractmethod
from typing import TypedDict, Optional

class PayoutResult(TypedDict):
    ok: bool
    txid: Optional[str]
    raw: dict

class PayoutProvider(ABC):
    @abstractmethod
    def transfer_pix(self, *, amount_cents: int, pix_key: str, description: str) -> PayoutResult:
        """Envia um PIX e retorna txid do provedor."""
        raise NotImplementedError()


class MockPayoutProvider(PayoutProvider):
    """Provider de testes: não envia dinheiro, só simula sucesso."""
    def transfer_pix(self, *, amount_cents: int, pix_key: str, description: str) -> PayoutResult:
        # gera um TXID fake estável
        fake_txid = f"MOCK-{abs(hash((amount_cents, pix_key, description)))%10_000_000}"
        return {"ok": True, "txid": fake_txid, "raw": {"mode": "mock"}}
