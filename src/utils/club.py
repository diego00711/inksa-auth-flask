# -*- coding: utf-8 -*-
# src/utils/club.py
"""
Clube Inksa — níveis e benefícios CONFIGURÁVEIS por audiência
(client / delivery / restaurant), lidos da tabela `club_levels`.

Fonte única usada por:
  - club_routes.py         -> /api/club/levels e /api/club/status
  - payment.py             -> aplica benefícios do CLIENTE no checkout
  - delivery_orders.py     -> aplica benefícios do ENTREGADOR no repasse
  - public_restaurants.py  -> destaque do RESTAURANTE na listagem

Atividade do mês (define o nível):
  - client      = pedidos confirmados no mês (não conta cancelado/aguardando)
  - delivery    = entregas concluídas (delivered) no mês
  - restaurant  = pedidos entregues (delivered) no mês
"""
import logging
import psycopg2.extras
from .helpers import get_db_connection

logger = logging.getLogger(__name__)

_ACTIVITY_COL = {"client": "client_id", "delivery": "delivery_id", "restaurant": "restaurant_id"}


def fetch_levels(cur, audience):
    cur.execute("""
        SELECT level_order, name, emoji, color, min_activity, benefits
          FROM public.club_levels
         WHERE audience = %s AND is_active = TRUE
         ORDER BY level_order ASC
    """, (audience,))
    return [dict(r) for r in cur.fetchall()]


def level_for_activity(levels, activity):
    """O nível atual = o mais alto cujo min_activity <= atividade."""
    if not levels:
        return None
    current = levels[0]
    for lvl in levels:
        if activity >= int(lvl["min_activity"]):
            current = lvl
    return current


def next_level(levels, current):
    if not current:
        return None
    for i, lvl in enumerate(levels):
        if lvl["level_order"] == current["level_order"] and i + 1 < len(levels):
            return levels[i + 1]
    return None


def monthly_activity(cur, audience, profile_id):
    col = _ACTIVITY_COL[audience]
    if audience == "client":
        cur.execute(f"""
            SELECT COUNT(*)::int AS c FROM orders
             WHERE {col} = %s
               AND status NOT IN ('cancelled','canceled','awaiting_payment')
               AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())
        """, (profile_id,))
    else:
        cur.execute(f"""
            SELECT COUNT(*)::int AS c FROM orders
             WHERE {col} = %s AND status = 'delivered'
               AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())
        """, (profile_id,))
    row = cur.fetchone()
    return int(row["c"]) if row else 0


def get_status(audience, profile_id):
    """Abre conexão própria. Retorna {levels, activity, current, next} ou None."""
    if audience not in _ACTIVITY_COL or not profile_id:
        return None
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            levels = fetch_levels(cur, audience)
            activity = monthly_activity(cur, audience, profile_id)
            current = level_for_activity(levels, activity)
            return {
                "levels": levels,
                "activity": activity,
                "current": current,
                "next": next_level(levels, current),
            }
    except Exception:
        logger.exception("club.get_status failed")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _bnum(benefits, key, lo=0.0, hi=None):
    try:
        v = float((benefits or {}).get(key) or 0)
    except (TypeError, ValueError):
        v = 0.0
    v = max(lo, v)
    return min(v, hi) if hi is not None else v


def client_checkout_benefits(client_id):
    """Benefícios do CLIENTE para o checkout:
       {free_delivery, subtotal_discount_pct, level_name}.
       O 'número do pedido' considerado = atividade do mês + 1 (este pedido)."""
    st = get_status("client", client_id)
    if not st or not st["current"]:
        return {"free_delivery": False, "subtotal_discount_pct": 0.0, "level_name": None}
    b = st["current"].get("benefits") or {}
    order_number = int(st["activity"]) + 1
    free = bool(b.get("free_delivery_always"))
    if not free and b.get("free_delivery_from_nth") is not None:
        try:
            free = order_number >= int(b["free_delivery_from_nth"])
        except (TypeError, ValueError):
            pass
    return {
        "free_delivery": free,
        "subtotal_discount_pct": _bnum(b, "subtotal_discount_pct", 0.0, 100.0),
        # TETO EM REAIS do desconto percentual. A margem da Inksa é fixa por
        # pedido (comissão), mas a % cresce junto com o ticket: 5% num pedido de
        # R$300 custa R$15 de uma vez, mais que o dobro da receita dele. Com o
        # teto, a pior configuração possível fica limitada.
        # 0 ou ausente = sem teto (comportamento anterior).
        "max_discount_brl": _bnum(b, "max_discount_brl", 0.0, None),
        "level_name": st["current"]["name"],
    }


def delivery_level_benefits(delivery_id):
    """Benefícios do ENTREGADOR para o repasse:
       {per_delivery_bonus, freight_keep_extra_pct}."""
    st = get_status("delivery", delivery_id)
    if not st or not st["current"]:
        return {"per_delivery_bonus": 0.0, "freight_keep_extra_pct": 0.0}
    b = st["current"].get("benefits") or {}
    return {
        "per_delivery_bonus": _bnum(b, "per_delivery_bonus", 0.0),
        "freight_keep_extra_pct": _bnum(b, "freight_keep_extra_pct", 0.0, 100.0),
    }
