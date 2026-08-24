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
  - restaurant  = FATURAMENTO entregue no mês, em reais

POR QUE O PARCEIRO CONTA DINHEIRO E NÃO PEDIDO: o benefício dele é desconto na
comissão, e o que esse desconto custa à Inksa acompanha o faturamento, não a
quantidade de pedidos. Contando pedido, a lanchonete de ticket R$25 e o pet
shop de ticket R$150 chegariam ao mesmo nível gerando receitas seis vezes
diferentes — e quem paga a conta seria a Inksa. Qualificação e custo na mesma
unidade é o que mantém a escada em pé quando entrarem outros ramos.
"""
import logging
import psycopg2.extras
from .helpers import get_db_connection

logger = logging.getLogger(__name__)

_ACTIVITY_COL = {"client": "client_id", "delivery": "delivery_id", "restaurant": "restaurant_id"}

# Em que unidade cada público é medido. Os apps usam isto pra escrever "R$ 1.500"
# em vez de "1500 pedidos".
ACTIVITY_UNIT = {"client": "orders", "delivery": "orders", "restaurant": "brl"}

# Teto do desconto de comissão, em pontos percentuais. Não é desconfiança do
# Diego — é que este número é digitado numa tela e um zero a mais zeraria a
# receita da plataforma em silêncio, pedido a pedido, até alguém reparar.
MAX_COMMISSION_DISCOUNT_PP = 10.0


def restaurant_month_volume_sql(id_expr="%s"):
    """SQL do faturamento do mês do parceiro (subtotal dos pedidos entregues).

    Devolvido como texto porque DOIS lugares precisam da mesma conta: o nível do
    Clube (aqui) e o destaque na listagem (public_restaurants, que precisa dela
    correlacionada com `rp.id`). Duas versões da mesma regra divergiriam, e o
    parceiro veria um nível na tela dele e outro no destaque.

    `total_amount_items` é o subtotal gravado; o fallback existe pros pedidos
    antigos que não têm essa coluna preenchida — nunca o total cheio, senão o
    frete entraria no faturamento e inflaria o nível.
    """
    return f"""
        SELECT COALESCE(SUM(COALESCE(o.total_amount_items,
                                     o.total_amount - COALESCE(o.delivery_fee, 0),
                                     0)), 0)
          FROM orders o
         WHERE o.restaurant_id = {id_expr}
           AND o.status = 'delivered'
           AND DATE_TRUNC('month', o.created_at) = DATE_TRUNC('month', NOW())
    """


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
    if audience == "restaurant":
        # Parceiro conta REAIS (ver cabeçalho do módulo).
        cur.execute(restaurant_month_volume_sql() + " ", (profile_id,))
        row = cur.fetchone()
        return float(row[0] or 0) if row else 0.0
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


def restaurant_commission_discount_pp(restaurant_id):
    """Desconto de comissão do parceiro, em PONTOS PERCENTUAIS (ex.: 2.0 = -2pp).

    Chamada no caminho do checkout, então tem conexão própria e query enxuta —
    não usa get_status (que busca a tabela de níveis inteira).

    Fail-safe deliberado: qualquer erro devolve 0, ou seja, comissão CHEIA.
    Errar pra baixo aqui seria dar desconto que ninguém autorizou, em silêncio,
    em todo pedido — e só apareceria no fechamento do mês.
    """
    if not restaurant_id:
        return 0.0
    conn = get_db_connection()
    if not conn:
        return 0.0
    try:
        with conn.cursor() as cur:
            cur.execute(restaurant_month_volume_sql(), (str(restaurant_id),))
            row = cur.fetchone()
            volume = float(row[0] or 0) if row else 0.0

            # O nível mais alto que o faturamento alcança. `->>` devolve texto e
            # a conversão é feita aqui: cast em SQL estouraria a query inteira se
            # alguém digitasse "2%" na tela do admin.
            cur.execute("""
                SELECT benefits->>'commission_discount_pp'
                  FROM public.club_levels
                 WHERE audience = 'restaurant' AND is_active = TRUE
                   AND min_activity <= %s
                 ORDER BY level_order DESC
                 LIMIT 1
            """, (volume,))
            row = cur.fetchone()
            if not row or row[0] in (None, ""):
                return 0.0
            pp = float(row[0])
    except Exception:
        logger.exception("club.restaurant_commission_discount_pp failed")
        return 0.0
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if pp != pp or pp < 0:   # NaN ou negativo
        return 0.0
    return min(pp, MAX_COMMISSION_DISCOUNT_PP)


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
