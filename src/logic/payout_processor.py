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


def get_delivery_platform_share() -> Decimal:
    """
    Returns the platform's share of the delivery fee (e.g. 0.15 = 15% to platform, 85% to courier).

    NOTE: this is used by the legacy share-of-fee model. The new repasse model is
    `delivery_base_fee + delivery_per_km_fee * distance` (calculated upstream and
    written to orders.valor_repassado_entregador).
    """
    s = get_settings()
    return s.get("delivery_platform_share", Decimal("0.15"))


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
    """Returns list of partner IDs whose payout_cycle matches *cycle_type*."""
    table = "restaurants" if partner_type == "restaurant" else "delivery_profiles"
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                f"""
                SELECT id AS partner_id
                FROM {table}
                WHERE COALESCE(payout_cycle, 'weekly') = %s
                  AND is_active = true
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
                    f"SELECT id AS partner_id FROM {table} WHERE is_active = true"
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
                       COALESCE({amount_col}, 0)   AS repasse,
                       COALESCE(delivery_fee, 0)   AS delivery_fee
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
                       COALESCE({amount_col}, 0) AS repasse,
                       COALESCE(delivery_fee, 0) AS delivery_fee
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
    """Returns (total_gross, commission_fee, total_net, per_order list)."""
    delivery_platform_share = get_delivery_platform_share()
    per_order = []
    total_gross = Decimal("0")
    total_net = Decimal("0")

    for order in orders:
        net = Decimal(str(order["repasse"] or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if partner_type == "restaurant":
            # net = gross * (1 - commission_rate)  →  gross = net / (1 - rate)
            divisor = (Decimal("1") - commission_rate)
            gross = (net / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            comm = (gross - net).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            # delivery: net = gross * 0.85  →  gross = net / 0.85
            divisor = (Decimal("1") - delivery_platform_share)
            gross = (net / divisor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            comm = (gross - net).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

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
        cur.execute(
            """
            INSERT INTO payouts (
                id, partner_id, partner_type,
                total_gross, commission_fee, total_net,
                period_start, period_end,
                status, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending_transfer', NOW(), NOW())
            """,
            (payout_id, partner_id, partner_type,
             total_gross, commission_fee, total_net,
             period_start, period_end),
        )

        # 2. Insert payout_items
        for item in per_order:
            cur.execute(
                """
                INSERT INTO payout_items (
                    id, payout_id, order_id,
                    order_total, delivery_fee, commission_applied, net_amount,
                    created_at
                ) VALUES (gen_random_uuid(), %s, %s, %s, %s, %s, %s, NOW())
                """,
                (payout_id, item["order_id"],
                 item["order_total"], item["delivery_fee"],
                 item["commission_applied"], item["net_amount"]),
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


# ---------------------------------------------------------------------------
# Legacy function — kept for backward compatibility
# ---------------------------------------------------------------------------

def process_payouts(conn, partner_type: str, cycle_type: str) -> list:
    """Legacy manual payout function (no commission breakdown, no payout_items).

    Prefer process_automatic_payouts() for new code.
    """
    period_start, period_end = _period_bounds(cycle_type)

    if partner_type == "delivery":
        amount_col  = "valor_repassado_entregador"
        payout_col  = "delivery_payout_id"
        partner_col = "delivery_id"
    else:
        amount_col  = "valor_repassado_restaurante"
        payout_col  = "restaurant_payout_id"
        partner_col = "restaurant_id"

    created = []

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            f"""
            SELECT DISTINCT o.{partner_col} AS partner_id
            FROM orders o
            WHERE o.{partner_col} IS NOT NULL
              AND o.status IN ('delivered', 'delivery_failed')
              AND (o.status_pagamento = 'approved' OR o.status = 'delivery_failed')
              AND COALESCE(o.{amount_col}, 0) > 0
              AND o.{payout_col} IS NULL
              AND o.updated_at >= %s AND o.updated_at <= %s
            FOR UPDATE SKIP LOCKED
            """,
            (period_start, period_end),
        )
        partners = [row["partner_id"] for row in cur.fetchall()]
        logger.info("process_payouts (legacy): %d partners", len(partners))

        for partner_id in partners:
            cur.execute(
                f"""
                SELECT id, {amount_col} AS repasse
                FROM orders
                WHERE {partner_col} = %s
                  AND status IN ('delivered', 'delivery_failed')
                  AND (status_pagamento = 'approved' OR status = 'delivery_failed')
                  AND COALESCE({amount_col}, 0) > 0
                  AND {payout_col} IS NULL
                  AND updated_at >= %s AND updated_at <= %s
                ORDER BY updated_at ASC
                """,
                (partner_id, period_start, period_end),
            )
            rows = cur.fetchall()
            if not rows:
                continue

            total_net = sum(float(r["repasse"] or 0) for r in rows)
            order_ids = [r["id"] for r in rows]
            payout_id = str(uuidlib.uuid4())

            cur.execute(
                """
                INSERT INTO payouts (
                    id, partner_id, partner_type,
                    total_net, period_start, period_end,
                    status, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'pending', NOW(), NOW())
                RETURNING id, partner_id, partner_type, total_net, period_start, period_end, status
                """,
                (payout_id, str(partner_id), partner_type, total_net, period_start, period_end),
            )
            row = dict(cur.fetchone())

            cur.execute(
                f"UPDATE orders SET {payout_col} = %s WHERE id = ANY(%s)",
                (payout_id, order_ids),
            )

            created.append({
                "payout_id": str(row["id"]),
                "partner_type": row["partner_type"],
                "partner_id": str(row["partner_id"]),
                "amount": float(row["total_net"]),
                "period_start": row["period_start"].isoformat(),
                "period_end": row["period_end"].isoformat(),
                "status": row["status"],
                "orders_count": len(order_ids),
            })

    logger.info("process_payouts (legacy): %d created", len(created))
    return created
