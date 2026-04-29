# src/routes/payouts.py
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify
from psycopg2.extras import DictCursor

from ..utils.helpers import get_db_connection, get_user_id_from_token
from ..utils.audit import log_admin_action
from ..logic.payout_processor import process_automatic_payouts, process_payouts
from ..providers.mp_payouts import get_payout_provider

logger = logging.getLogger(__name__)
payouts_bp = Blueprint("payouts", __name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_admin(user_type: str) -> bool:
    return user_type == "admin"


def _normalize_dates(row: dict) -> dict:
    for key in ("period_start", "period_end", "created_at", "updated_at"):
        val = row.get(key)
        if val and hasattr(val, "isoformat"):
            row[key] = val.isoformat()
    return row


def _get_admin_identifier(user_id: str, conn) -> str:
    """Returns admin email if available, falls back to user_id."""
    try:
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT email FROM users WHERE id = %s LIMIT 1", (user_id,))
            row = cur.fetchone()
            if row and row.get("email"):
                return row["email"]
    except Exception:
        pass
    return str(user_id)


# ---------------------------------------------------------------------------
# CORS pre-flight (blueprint-level)
# ---------------------------------------------------------------------------

@payouts_bp.before_request
def allow_cors_preflight():
    if request.method == "OPTIONS":
        resp = jsonify()
        resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
        resp.headers["Access-Control-Allow-Methods"] = "GET,PUT,POST,PATCH,DELETE,OPTIONS"
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        return resp


# ---------------------------------------------------------------------------
# GET /api/admin/payouts/partner-pix — utility (internal)
# ---------------------------------------------------------------------------

def _get_partner_pix_data(conn, *, partner_type: str, partner_id: str):
    with conn.cursor(cursor_factory=DictCursor) as cur:
        if partner_type == "delivery":
            cur.execute(
                """
                SELECT dp.pix_key, dp.full_name
                FROM delivery_profiles dp
                WHERE dp.id = %s OR dp.user_id = %s
                LIMIT 1
                """,
                (partner_id, partner_id),
            )
        else:
            cur.execute(
                """
                SELECT COALESCE(r.pix_key, r.bank_pix_key) AS pix_key,
                       r.trade_name AS full_name
                FROM restaurants r
                WHERE r.id = %s OR r.user_id = %s
                LIMIT 1
                """,
                (partner_id, partner_id),
            )
        row = cur.fetchone()
    if not row:
        return None, None
    return row.get("pix_key"), row.get("full_name")


# ---------------------------------------------------------------------------
# POST /api/admin/payouts/process
# ---------------------------------------------------------------------------

@payouts_bp.route("/process", methods=["POST", "OPTIONS"])
def process_payouts_route():
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado. Somente administradores."}), 403

        body = request.get_json(silent=True) or {}
        partner_type = (body.get("partner_type") or "").strip().lower() or None
        cycle_type   = (body.get("cycle_type") or "weekly").strip().lower()
        dry_run      = bool(body.get("dry_run", False))

        if partner_type and partner_type not in ("restaurant", "delivery"):
            return jsonify({"error": "partner_type inválido (restaurant|delivery)"}), 400
        if cycle_type not in ("weekly", "bi-weekly", "monthly"):
            return jsonify({"error": "cycle_type inválido (weekly|bi-weekly|monthly)"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        result = process_automatic_payouts(
            conn,
            force_cycle=cycle_type,
            partner_type=partner_type,
            dry_run=dry_run,
        )

        admin = _get_admin_identifier(user_id, conn)
        log_admin_action(
            admin,
            "ProcessPayouts",
            (
                f"cycle={cycle_type} partner_type={partner_type or 'all'} "
                f"dry_run={dry_run} generated={result.get('total_payouts', 0)}"
            ),
            request,
        )

        return jsonify({"status": "success", **result}), 200

    except Exception:
        logger.exception("Erro ao processar payouts")
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno ao processar payouts"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# GET /api/admin/payouts
# ---------------------------------------------------------------------------

@payouts_bp.route("", methods=["GET", "OPTIONS"])
def list_payouts():
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        partner_type = (request.args.get("partner_type") or "").strip().lower()
        status       = (request.args.get("status") or "").strip().lower()
        partner_id   = request.args.get("partner_id")
        start_date   = request.args.get("start_date")
        end_date     = request.args.get("end_date")
        limit        = min(int(request.args.get("limit") or 20), 200)
        offset       = int(request.args.get("offset") or 0)

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        valid_statuses = ("pending", "pending_transfer", "paid", "cancelled")
        where, params = [], []

        if partner_type in ("restaurant", "delivery"):
            where.append("partner_type = %s")
            params.append(partner_type)
        if status in valid_statuses:
            where.append("status = %s")
            params.append(status)
        if partner_id:
            where.append("partner_id = %s")
            params.append(partner_id)
        if start_date:
            where.append("created_at >= %s")
            params.append(start_date)
        if end_date:
            where.append("created_at <= %s")
            params.append(end_date)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM payouts {where_sql}", tuple(params))
            total = int(cur.fetchone()["total"])

            cur.execute(
                f"""
                SELECT id, partner_type, partner_id,
                       period_start, period_end,
                       total_gross, commission_fee, total_net,
                       status, payment_method, payment_ref,
                       created_at, updated_at
                FROM payouts
                {where_sql}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [limit, offset]),
            )
            rows = [_normalize_dates(dict(r)) for r in cur.fetchall()]

        return jsonify({"status": "success", "items": rows, "total": total}), 200

    except Exception:
        logger.exception("Erro ao listar payouts")
        return jsonify({"error": "Erro interno ao listar payouts"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# GET /api/admin/payouts/<id>
# ---------------------------------------------------------------------------

@payouts_bp.route("/<uuid:payout_id>", methods=["GET", "OPTIONS"])
def get_payout_detail(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """
                SELECT id, partner_type, partner_id,
                       period_start, period_end,
                       total_gross, commission_fee, total_net,
                       status, payment_method, payment_ref,
                       created_at, updated_at
                FROM payouts
                WHERE id = %s
                """,
                (str(payout_id),),
            )
            head = cur.fetchone()
            if not head:
                return jsonify({"error": "Payout não encontrado"}), 404

            cur.execute(
                """
                SELECT id, order_id, order_total, delivery_fee,
                       commission_applied, net_amount
                FROM payout_items
                WHERE payout_id = %s
                ORDER BY created_at ASC
                """,
                (str(payout_id),),
            )
            items = [dict(r) for r in cur.fetchall()]

        return jsonify({
            "status": "success",
            "payout": _normalize_dates(dict(head)),
            "items": items,
        }), 200

    except Exception:
        logger.exception("Erro ao obter detalhe de payout")
        return jsonify({"error": "Erro interno ao obter payout"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# PATCH /api/admin/payouts/<id>/status
# Allows transitioning status: pending_transfer → paid | cancelled
#                               pending          → paid | cancelled
# ---------------------------------------------------------------------------

ALLOWED_TRANSITIONS = {
    "pending":          {"paid", "cancelled"},
    "pending_transfer": {"paid", "cancelled"},
}


@payouts_bp.route("/<uuid:payout_id>/status", methods=["PATCH", "OPTIONS"])
def update_payout_status(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        body = request.get_json(silent=True) or {}
        new_status     = (body.get("status") or "").strip().lower()
        payment_method = (body.get("payment_method") or "").strip()
        payment_ref    = (body.get("payment_ref") or "").strip()

        if new_status not in ("paid", "cancelled"):
            return jsonify({"error": "status inválido — use 'paid' ou 'cancelled'"}), 400
        if new_status == "paid" and not payment_method:
            return jsonify({"error": "payment_method é obrigatório ao marcar como 'paid'"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "SELECT id, status, partner_type, partner_id, total_net FROM payouts WHERE id = %s",
                (str(payout_id),),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Payout não encontrado"}), 404

            current_status = row["status"]
            allowed = ALLOWED_TRANSITIONS.get(current_status, set())
            if new_status not in allowed:
                return jsonify({
                    "error": f"Transição '{current_status}' → '{new_status}' não permitida"
                }), 400

            cur.execute(
                """
                UPDATE payouts
                   SET status = %s,
                       payment_method = COALESCE(NULLIF(%s, ''), payment_method),
                       payment_ref    = COALESCE(NULLIF(%s, ''), payment_ref),
                       updated_at = NOW()
                 WHERE id = %s
                 RETURNING *
                """,
                (new_status, payment_method or None, payment_ref or None, str(payout_id)),
            )
            updated = _normalize_dates(dict(cur.fetchone()))
            conn.commit()

        admin = _get_admin_identifier(user_id, conn)
        log_admin_action(
            admin,
            "UpdatePayoutStatus",
            (
                f"payout={payout_id} {current_status}→{new_status} "
                f"partner={row['partner_type']}:{row['partner_id']} "
                f"net={row['total_net']}"
            ),
            request,
        )

        return jsonify({"status": "success", "payout": updated}), 200

    except Exception:
        logger.exception("Erro ao atualizar status do payout")
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno ao atualizar status"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# POST /api/admin/payouts/<id>/mark-paid
# ---------------------------------------------------------------------------

@payouts_bp.route("/<uuid:payout_id>/mark-paid", methods=["POST", "OPTIONS"])
def mark_payout_paid(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        body = request.get_json(silent=True) or {}
        method = (body.get("payment_method") or "").strip()
        ref    = (body.get("payment_ref") or "").strip()

        if not method:
            return jsonify({"error": "payment_method é obrigatório"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "SELECT status, partner_type, partner_id, total_net FROM payouts WHERE id = %s",
                (str(payout_id),),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Payout não encontrado"}), 404
            if row["status"] in ("paid", "cancelled"):
                return jsonify({"error": f"Não permitido no status atual: {row['status']}"}), 400

            cur.execute(
                """
                UPDATE payouts
                   SET status = 'paid',
                       payment_method = %s,
                       payment_ref = %s,
                       updated_at = NOW()
                 WHERE id = %s
                 RETURNING *
                """,
                (method, ref or None, str(payout_id)),
            )
            updated = _normalize_dates(dict(cur.fetchone()))
            conn.commit()

        admin = _get_admin_identifier(user_id, conn)
        log_admin_action(
            admin,
            "MarkPayoutPaid",
            (
                f"payout={payout_id} method={method} "
                f"partner={row['partner_type']}:{row['partner_id']} "
                f"net={row['total_net']}"
            ),
            request,
        )

        return jsonify({"status": "success", "payout": updated}), 200

    except Exception:
        logger.exception("Erro ao marcar payout como pago")
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno ao marcar payout como pago"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# POST /api/admin/payouts/<id>/cancel
# ---------------------------------------------------------------------------

@payouts_bp.route("/<uuid:payout_id>/cancel", methods=["POST", "OPTIONS"])
def cancel_payout(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "SELECT status, partner_type, partner_id, total_net FROM payouts WHERE id = %s",
                (str(payout_id),),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Payout não encontrado"}), 404
            if row["status"] in ("paid", "cancelled"):
                return jsonify({"error": f"Não permitido no status atual: {row['status']}"}), 400

            cur.execute(
                """
                UPDATE payouts
                   SET status = 'cancelled', updated_at = NOW()
                 WHERE id = %s
                 RETURNING *
                """,
                (str(payout_id),),
            )
            updated = _normalize_dates(dict(cur.fetchone()))
            conn.commit()

        admin = _get_admin_identifier(user_id, conn)
        log_admin_action(
            admin,
            "CancelPayout",
            (
                f"payout={payout_id} "
                f"partner={row['partner_type']}:{row['partner_id']} "
                f"net={row['total_net']}"
            ),
            request,
        )

        return jsonify({"status": "success", "payout": updated}), 200

    except Exception:
        logger.exception("Erro ao cancelar payout")
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno ao cancelar payout"}), 500
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------------
# POST /api/admin/payouts/<id>/auto-pay
# Executes PIX transfer via configured provider and marks payout as paid.
# ---------------------------------------------------------------------------

@payouts_bp.route("/<uuid:payout_id>/auto-pay", methods=["POST", "OPTIONS"])
def auto_pay_payout(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        body = request.get_json(silent=True) or {}
        description = (body.get("description") or "Repasse Inksa").strip()

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """
                SELECT id, partner_type, partner_id, total_net, status
                FROM payouts
                WHERE id = %s
                """,
                (str(payout_id),),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Payout não encontrado"}), 404
            row = dict(row)

            if row["status"] not in ("pending", "pending_transfer"):
                return jsonify({
                    "error": f"Somente payouts 'pending' ou 'pending_transfer' podem ser pagos (atual: {row['status']})"
                }), 400

            pix_key, full_name = _get_partner_pix_data(
                conn, partner_type=row["partner_type"], partner_id=row["partner_id"]
            )
            if not pix_key:
                return jsonify({"error": "Parceiro sem PIX cadastrado"}), 400

            amount_cents = int(round(float(row["total_net"]) * 100))
            provider = get_payout_provider()
            result = provider.transfer_pix(
                amount_cents=amount_cents,
                pix_key=pix_key,
                description=f"{description} - {full_name or row['partner_id']}",
            )

            if not result["ok"]:
                logger.error("Falha no provider ao pagar payout %s: %s", payout_id, result["raw"])
                return jsonify({"error": "Falha ao executar repasse no provedor"}), 502

            txid = result["txid"]

            cur.execute(
                """
                UPDATE payouts
                   SET status = 'paid',
                       payment_method = 'pix',
                       payment_ref = %s,
                       updated_at = NOW()
                 WHERE id = %s
                 RETURNING *
                """,
                (txid, str(payout_id)),
            )
            updated = _normalize_dates(dict(cur.fetchone()))
            conn.commit()

        admin = _get_admin_identifier(user_id, conn)
        log_admin_action(
            admin,
            "AutoPayPayout",
            (
                f"payout={payout_id} txid={txid} "
                f"partner={row['partner_type']}:{row['partner_id']} "
                f"net={row['total_net']}"
            ),
            request,
        )

        return jsonify({"status": "success", "payout": updated, "provider_txid": txid}), 200

    except NotImplementedError as exc:
        logger.warning("Provider não implementado: %s", exc)
        return jsonify({"error": str(exc)}), 501
    except Exception:
        logger.exception("Erro no auto-pay")
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno no auto-pay"}), 500
    finally:
        if conn:
            conn.close()
