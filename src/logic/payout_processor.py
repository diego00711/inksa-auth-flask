# src/logic/payout_processor.py
import logging
import uuid as uuidlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import psycopg2.extras

from ..utils.platform_settings import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def get_commission_rate() -> Decimal:
    """Returns platform commission rate from platform_settings (admin editable)."""
    try:
        rate = get_settings()["commission_rate"]
        if not (Decimal("0") < rate < Decimal("1")):
            raise ValueError("must be between 0 and 1")
        return rate
    except Exception:
        return Decimal("0.10")


def is_payout_day(cycle_type: str, reference_date: date = None) -> bool:
    """Returns True if *reference_date* (defaults to today) is a scheduled payout day.

    - weekly   → every Friday
    - bi-weekly → every other Friday (even ISO-week numbers)
    - monthly  → 1st of each month
    """
    today = reference_date or date.today()
    if cycle_type == "monthly":
        return today.day == 1
    if cycle_type == "bi-weekly":
        return today.weekday() == 4 and today.isocalendar()[1] % 2 == 0
    if cycle_type == "weekly":
        return today.weekday() == 4
    return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _period_bounds(cycle_type: str, reference_date: date = None):
    """Returns (period_start, period_end) UTC datetimes for the given cycle."""
    today = reference_date or date.today()
    now = datetime.now(timezone.utc)
    if cycle_type == "monthly":
        start = datetime(today.year, today.month, 1, 0, 0, 0, tzinfo=timezone.utc)
    elif cycle_type == "bi-weekly":
        start = (now - timedelta(days=14)).replace(microsecond=0)
    else:  # weekly
        start = (now - timedelta(days=7)).replace(microsecond=0)
    return start, now


def _get_partners_for_cycle(conn, partner_type: str, cycle_type: str) -> list:
    """Returns list of partner IDs whose payout_cycle matches *cycle_type*.

    Ambas as tabelas de perfil (restaurant_profiles / delivery_profiles) tem
    as colunas `active` e `payout_cycle`. Antes isto apontava para uma tabela
    `restaurants` inexistente e para uma coluna `is_active` que
    restaurant_profiles nao tem -- entao repasse de restaurante nunca era
    gerado (a query dava erro, caia no except e retornava lista vazia).
    """
    table = "restaurant_profiles" if partner_type == "restaurant" else "delivery_profiles"
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                f"""
                SELECT id AS partner_id
                FROM {table}
                WHERE COALESCE(payout_cycle, 'weekly') = %s
                  AND COALESCE(active, true) = true
                """,
                (cycle_type,),
            )
            return [str(row["partner_id"]) for row in cur.fetchall()]
    except Exception as exc:
        # payout_cycle column may not exist yet — fall back gracefully
        conn.rollback()
        if "payout_cycle" in str(exc) and cycle_type == "weekly":
            logger.warning("payout_cycle column missing on %s; treating all as 'weekly'", table)
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    f"SELECT id AS partner_id FROM {table} WHERE COALESCE(active, true) = true"
                )
                return [str(row["partner_id"]) for row in cur.fetchall()]
        return []


def _get_eligible_orders(conn, partner_type: str, partner_id: str, period_start, period_end) -> list:
    """Returns orders eligible for payout: delivered, payment approved, not yet in a payout."""
    if partner_type == "restaurant":
        partner_col = "restaurant_id"
        amount_col = "valor_repassado_restaurante"
        payout_col = "restaurant_payout_id"
    else:
        partner_col = "delivery_id"
        amount_col = "valor_repassado_entregador"
        payout_col = "delivery_payout_id"

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        try:
            cur.execute(
                f"""
                SELECT id,
                       COALESCE({amount_col}, 0)      AS repasse,
                       COALESCE(delivery_fee, 0)      AS delivery_fee,
                       COALESCE(comissao_plataforma, 0) AS comissao_historica
                FROM orders
                WHERE {partner_col} = %s
                  AND status IN ('delivered', 'delivery_failed')
                  AND (status_pagamento = 'approved' OR status = 'delivery_failed')
                  AND (payout_status = 'pending' OR payout_status IS NULL)
                  AND {payout_col} IS NULL
                  AND COALESCE({amount_col}, 0) > 0
                  AND updated_at >= %s AND updated_at <= %s
                ORDER BY updated_at ASC
                """,
                (partner_id, period_start, period_end),
            )
        except Exception:
            conn.rollback()
            # Fallback: no payout_status column
            cur.execute(
                f"""
                SELECT id,
                       COALESCE({amount_col}, 0)      AS repasse,
                       COALESCE(delivery_fee, 0)      AS delivery_fee,
                       COALESCE(comissao_plataforma, 0) AS comissao_historica
                FROM orders
                WHERE {partner_col} = %s
                  AND status IN ('delivered', 'delivery_failed')
                  AND (status_pagamento = 'approved' OR status = 'delivery_failed')
                  AND {payout_col} IS NULL
                  AND COALESCE({amount_col}, 0) > 0
                  AND updated_at >= %s AND updated_at <= %s
                ORDER BY updated_at ASC
                """,
                (partner_id, period_start, period_end),
            )
        return cur.fetchall()


def _calculate_amounts(orders, partner_type: str, commission_rate: Decimal):
    """Returns (total_gross, commission_fee, total_net, per_order list).

    Usa a comissao HISTORICA de cada pedido (orders.comissao_plataforma,
    gravada no momento da compra) em vez de reconstruir "gross" reaplicando
    a taxa de comissao ATUAL sobre o valor liquido -- se a taxa mudar entre
    a data do pedido e o dia do repasse, reaplicar a taxa atual produzia um
    detalhamento (bruto/comissao) matematicamente inconsistente pra pedidos
    antigos (o valor liquido pago sempre esteve correto, so o detalhamento
    exibido no admin que ficava errado).
    """
    per_order = []
    total_gross = Decimal("0")
    total_net = Decimal("0")

    for order in orders:
        net = Decimal(str(order["repasse"] or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if partner_type == "restaurant":
            comm = Decimal(str(order.get("comissao_historica") or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if comm <= 0:
                # Fallback so para pedidos antigos sem comissao_plataforma
                # gravada (antes dessa coluna existir) -- reconstroi com a
                # taxa atual como melhor esforco.
                divisor = (Decimal("1") - commission_rate)
                gross = (net / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                comm = (gross - net).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            else:
                gross = (net + comm).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            # Entregador: modelo atual paga base + por km, sem "comissao"
            # descontada do valor do entregador -- gross = net, sem desconto.
            gross = net
            comm = Decimal("0.00")

        total_gross += gross
        total_net += net
        per_order.append({
            "order_id": str(order["id"]),
            "order_total": float(gross),
            "delivery_fee": float(order.get("delivery_fee") or 0),
            "commission_applied": float(comm),
            "net_amount": float(net),
        })

    total_gross = total_gross.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    total_net = total_net.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    commission_fee = (total_gross - total_net).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(total_gross), float(commission_fee), float(total_net), per_order


def _insert_payout(conn, partner_type, partner_id, period_start, period_end,
                   total_gross, commission_fee, total_net, per_order):
    """Inserts payouts + payout_items rows, updates orders. Returns payout summary dict."""
    payout_id = str(uuidlib.uuid4())
    payout_col = "restaurant_payout_id" if partner_type == "restaurant" else "delivery_payout_id"
    order_ids = [item["order_id"] for item in per_order]

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # 1. Insert payout
        # `amount` é NOT NULL sem default no schema (coluna legada) -- precisa
        # ser preenchida senao o INSERT falha e nenhum repasse e criado.
        # Usamos o valor liquido (o que efetivamente sera pago).
        cur.execute(
            """
            INSERT INTO payouts (
                id, partner_id, partner_type,
                amount, total_gross, commission_fee, total_net,
                period_start, period_end,
                status, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending_transfer', NOW(), NOW())
            """,
            (payout_id, partner_id, partner_type,
             total_net, total_gross, commission_fee, total_net,
             period_start, period_end),
        )

        # 2. Insert payout_items
        # Colunas reais: partner_type/partner_id (NOT NULL), gross_amount, fee,
        # net_amount. Antes inseria em order_total/delivery_fee/commission_applied
        # que NAO existem -- o INSERT quebrava e derrubava a geracao inteira.
        for item in per_order:
            cur.execute(
                """
                INSERT INTO payout_items (
                    id, payout_id, order_id, partner_type, partner_id,
                    gross_amount, fee, net_amount, created_at
                ) VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, %s, NOW())
                """,
                (payout_id, item["order_id"], partner_type, partner_id,
                 item["order_total"], item["commission_applied"], item["net_amount"]),
            )

        # 3. Mark orders as processed
        try:
            cur.execute(
                f"""
                UPDATE orders
                   SET {payout_col} = %s,
                       payout_status = 'processed',
                       updated_at = NOW()
                 WHERE id = ANY(%s)
                """,
                (payout_id, order_ids),
            )
        except Exception:
            # payout_status column may not exist
            conn.rollback()
            cur.execute(
                f"""
                UPDATE orders
                   SET {payout_col} = %s,
                       updated_at = NOW()
                 WHERE id = ANY(%s)
                """,
                (payout_id, order_ids),
            )

    return {
        "payout_id": payout_id,
        "partner_type": partner_type,
        "partner_id": partner_id,
        "total_gross": total_gross,
        "commission_fee": commission_fee,
        "total_net": total_net,
        "orders_count": len(order_ids),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "status": "pending_transfer",
    }


def _log_to_admin_logs(created: list, cycles: list):
    """Best-effort: write summary to admin_logs via Supabase."""
    try:
        from ..utils.helpers import supabase
        if not supabase or not created:
            return
        details = (
            f"Payouts automáticos gerados: {len(created)} | "
            f"Ciclos: {', '.join(cycles)} | "
            f"IDs: {', '.join(p['payout_id'] for p in created[:10])}"
        )
        supabase.table("admin_logs").insert({
            "admin": "scheduler",
            "action": "AutomaticPayouts",
            "details": details[:16384],
        }).execute()
    except Exception as exc:
        logger.warning("Failed to write payout summary to admin_logs: %s", exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def process_automatic_payouts(
    conn,
    force_cycle: str = None,
    partner_type: str = None,
    dry_run: bool = False,
) -> dict:
    """Generates payouts for all partners whose payout cycle is due today.

    Args:
        conn:          Active psycopg2 connection (uncommitted).
        force_cycle:   If set, skip the is_payout_day check and process this cycle.
        partner_type:  If set ('restaurant'|'delivery'), process only that type.
        dry_run:       If True, all DB changes are rolled back (for testing).

    Returns:
        dict with keys: created, cycles_processed, total_payouts, today, dry_run
    """
    today = date.today()
    commission_rate = get_commission_rate()
    created = []

    # Determine which cycles are due
    if force_cycle:
        if force_cycle not in ("weekly", "bi-weekly", "monthly"):
            raise ValueError(f"Invalid cycle_type: {force_cycle!r}")
        cycles_due = [force_cycle]
    else:
        cycles_due = [c for c in ("weekly", "bi-weekly", "monthly") if is_payout_day(c, today)]

    if not cycles_due:
        logger.info("process_automatic_payouts: no cycles due today (%s)", today.isoformat())
        return {"created": [], "cycles_due": [], "today": today.isoformat(), "dry_run": dry_run}

    logger.info(
        "process_automatic_payouts: cycles=%s partner_type=%s date=%s",
        cycles_due, partner_type or "all", today.isoformat(),
    )

    partner_types = [partner_type] if partner_type else ["restaurant", "delivery"]

    for ptype in partner_types:
        for cycle in cycles_due:
            period_start, period_end = _period_bounds(cycle, today)
            partners = _get_partners_for_cycle(conn, ptype, cycle)
            logger.info("  %s/%s: %d partners found", ptype, cycle, len(partners))

            for pid in partners:
                orders = _get_eligible_orders(conn, ptype, pid, period_start, period_end)
                if not orders:
                    continue

                gross, comm, net, per_order = _calculate_amounts(orders, ptype, commission_rate)
                if net <= 0:
                    continue

                record = _insert_payout(
                    conn, ptype, pid, period_start, period_end,
                    gross, comm, net, per_order,
                )
                created.append(record)
                logger.info(
                    "    payout %s: %s %s | net=%.2f orders=%d",
                    record["payout_id"], ptype, pid, net, len(per_order),
                )

    if dry_run:
        conn.rollback()
        logger.info("process_automatic_payouts: DRY RUN — rolled back %d payouts", len(created))
    else:
        conn.commit()
        _log_to_admin_logs(created, cycles_due)

    logger.info("process_automatic_payouts: done — %d payouts created", len(created))
    return {
        "created": created,
        "cycles_processed": cycles_due,
        "total_payouts": len(created),
        "today": today.isoformat(),
        "dry_run": dry_run,
    }
