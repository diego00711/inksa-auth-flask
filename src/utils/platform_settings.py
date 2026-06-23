"""
Leitura de configurações da plataforma (tabela platform_settings) com cache em memória.

A tabela tem schema chave/valor (TEXT). Esta camada interpreta os tipos
para o restante do backend e mantém um cache simples por TTL para evitar
ir ao Postgres a cada cálculo de frete/repasse.

Uso:
    from src.utils.platform_settings import get_settings, get_decimal, invalidate_cache
    s = get_settings()
    fixed_fee = s["fixed_delivery_fee"]  # Decimal
"""
import logging
import os
import threading
import time
from decimal import Decimal, InvalidOperation

from .helpers import get_db_connection

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = int(os.environ.get("PLATFORM_SETTINGS_TTL", "60"))
_cache: dict | None = None
_cache_expires_at: float = 0.0
_lock = threading.Lock()


# Valores tipados que o restante do código consome.
# Defaults batem com os valores semeados na migration.
_DEFAULTS: dict[str, Decimal] = {
    "fixed_delivery_fee":         Decimal("3.00"),
    "per_km_delivery_fee":        Decimal("1.50"),
    "free_delivery_threshold_km": Decimal("2.00"),
    "commission_rate":            Decimal("0.10"),  # 0..1, não 10
    "delivery_base_fee":          Decimal("5.00"),
    "delivery_per_km_fee":        Decimal("1.00"),
}


def _to_decimal(raw, default: Decimal) -> Decimal:
    if raw is None or raw == "":
        return default
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError):
        return default


def _normalize(rows: list[tuple[str, str]]) -> dict[str, Decimal]:
    """Converte rows (key, value) text→Decimal aplicando regras por campo."""
    raw = {k: v for k, v in rows}
    out: dict[str, Decimal] = dict(_DEFAULTS)

    # Campos em R$ ou km (números diretos)
    for k in ("fixed_delivery_fee", "per_km_delivery_fee", "free_delivery_threshold_km",
              "delivery_base_fee", "delivery_per_km_fee"):
        out[k] = _to_decimal(raw.get(k), _DEFAULTS[k])

    # commission_rate é guardado como percentual humano (10 = 10%);
    # converte para fração 0..1 que o resto do código usa.
    raw_comm = raw.get("commission_rate")
    if raw_comm is not None and raw_comm != "":
        try:
            v = Decimal(str(raw_comm))
            # Heurística: valor > 1 significa que está em "percent humano" (ex: 10 = 10%).
            out["commission_rate"] = (v / Decimal("100")) if v > Decimal("1") else v
        except (InvalidOperation, TypeError):
            pass

    return out


def _load_from_db() -> dict[str, Decimal]:
    conn = get_db_connection()
    if not conn:
        logger.warning("platform_settings: DB indisponível, usando defaults")
        return dict(_DEFAULTS)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, value FROM platform_settings WHERE key = ANY(%s)",
                (list(_DEFAULTS.keys()),),
            )
            rows = cur.fetchall()
        return _normalize(rows)
    except Exception:
        logger.exception("platform_settings: falha ao ler do DB, usando defaults")
        return dict(_DEFAULTS)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_settings() -> dict[str, Decimal]:
    """Retorna dict tipado com todos os settings de plataforma (cache TTL)."""
    global _cache, _cache_expires_at
    now = time.time()
    if _cache is not None and now < _cache_expires_at:
        return _cache
    with _lock:
        # double-check inside lock
        if _cache is not None and now < _cache_expires_at:
            return _cache
        _cache = _load_from_db()
        _cache_expires_at = now + _CACHE_TTL_SECONDS
        return _cache


def get_decimal(key: str) -> Decimal:
    """Atalho para um campo específico (com fallback no default)."""
    return get_settings().get(key, _DEFAULTS.get(key, Decimal("0")))


def invalidate_cache() -> None:
    """Força a próxima leitura a buscar no DB. Chamar após PUT /settings."""
    global _cache, _cache_expires_at
    with _lock:
        _cache = None
        _cache_expires_at = 0.0


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def calculate_courier_payout(delivery_distance_km, delivery_fee=None) -> Decimal:
    """
    Calcula quanto o entregador recebe por uma entrega.

    Modelo novo (admin-configurável):
        delivery_base_fee + delivery_per_km_fee * distance_km

    Se `delivery_distance_km` não estiver disponível (None ou 0 e nenhuma flag
    de retirada local), cai no fallback de pagar 100% do `delivery_fee`
    para não quebrar pedidos antigos.
    """
    s = get_settings()
    try:
        km = Decimal(str(delivery_distance_km)) if delivery_distance_km is not None else None
    except (InvalidOperation, TypeError):
        km = None

    if km is None or km < 0:
        if delivery_fee is None:
            return Decimal("0.00")
        try:
            return Decimal(str(delivery_fee))
        except (InvalidOperation, TypeError):
            return Decimal("0.00")

    payout = s["delivery_base_fee"] + (s["delivery_per_km_fee"] * km)
    return payout.quantize(Decimal("0.01"))


def calculate_platform_commission(subtotal) -> Decimal:
    """Calcula a comissão da plataforma sobre o subtotal do pedido."""
    try:
        sub = Decimal(str(subtotal))
    except (InvalidOperation, TypeError):
        return Decimal("0.00")
    rate = get_settings()["commission_rate"]
    return (sub * rate).quantize(Decimal("0.01"))
