# src/utils/coupons.py
"""
Lógica ÚNICA de validação/cálculo de cupom, compartilhada entre:
  - /api/coupons/validate  (preview do cliente no carrinho)
  - payment.py             (aplicação real no fechamento do pedido)

Antes cada lugar tinha a sua cópia e elas divergiam: o payment.py lia a coluna
errada (min_order_amount, que não existe -> mínimo nunca valia), não checava
validade nem limite de usos, e não tratava frete grátis. Centralizar garante
que o desconto mostrado ao cliente seja EXATAMENTE o desconto cobrado.

Regras (tudo configurável pelo admin, tabela `coupons`):
  - is_active = true
  - valid_until >= agora (se preenchido)
  - uses_count < max_uses (se preenchido)
  - subtotal >= min_order_value (se preenchido)
  - desconto por tipo:
      percentage    -> subtotal * value/100        (limitado ao subtotal)
      fixed         -> min(value, subtotal)
      free_delivery -> delivery_fee (isenta o frete)
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _parse_dt(v):
    """Aceita datetime (psycopg2) ou string ISO (supabase). Retorna aware UTC."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def evaluate_coupon(coupon, subtotal, delivery_fee=0.0, now=None):
    """Valida um cupom já buscado (dict da linha) e calcula o desconto.

    `coupon` pode vir do psycopg2 (DictCursor) ou do supabase (select '*') —
    ambos têm as mesmas chaves. Retorna dict:
      { valid: bool, discount_amount: float, discount_type: str|None, message: str }
    discount_amount é 0.0 quando inválido.
    """
    now = now or datetime.now(timezone.utc)
    subtotal = _to_float(subtotal)
    delivery_fee = _to_float(delivery_fee)

    if not coupon:
        return {"valid": False, "discount_amount": 0.0, "discount_type": None,
                "message": "Cupom não encontrado"}

    disc_type = (coupon.get('discount_type') or '').lower()

    if not coupon.get('is_active'):
        return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                "message": "Este cupom não está ativo"}

    vu = _parse_dt(coupon.get('valid_until'))
    if vu and vu < now:
        return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                "message": "Este cupom expirou"}

    max_uses = coupon.get('max_uses')
    uses_count = int(coupon.get('uses_count') or 0)
    if max_uses is not None and uses_count >= int(max_uses):
        return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                "message": "Este cupom atingiu o limite de usos"}

    min_val = _to_float(coupon.get('min_order_value'))
    if subtotal < min_val:
        return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                "message": f"Pedido mínimo para este cupom é R$ {min_val:.2f}"}

    value = _to_float(coupon.get('discount_value'))
    if disc_type == 'percentage':
        discount = min(round(subtotal * value / 100.0, 2), subtotal)
    elif disc_type == 'fixed':
        discount = round(min(value, subtotal), 2)
    elif disc_type == 'free_delivery':
        discount = round(delivery_fee, 2)
    else:
        return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                "message": "Tipo de cupom inválido"}

    return {"valid": True, "discount_amount": max(0.0, discount),
            "discount_type": disc_type, "message": "Cupom válido!"}


def consume_coupon(coupon_id):
    """Incrementa uses_count de forma atômica (para o limite de usos valer).
    Best-effort: falha não deve derrubar o pedido. Usa conexão psycopg2 própria."""
    from .helpers import get_db_connection
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.coupons SET uses_count = COALESCE(uses_count, 0) + 1 WHERE id = %s",
                (str(coupon_id),),
            )
        conn.commit()
    except Exception as exc:
        logger.warning("Falha ao incrementar uses_count do cupom %s: %s", coupon_id, exc)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
