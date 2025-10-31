# src/routes/payouts.py
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify
from psycopg2.extras import DictCursor
from ..utils.helpers import get_db_connection, get_user_id_from_token

# Se você tiver isso em outro módulo, pode mover:
from ..logic.payout_processor import process_payouts  # já usado no seu projeto

logger = logging.getLogger(__name__)
payouts_bp = Blueprint("payouts", __name__)

def _is_admin(user_type: str) -> bool:
    # ajuste conforme sua regra de admins
    return user_type == "admin"

@payouts_bp.before_request
def allow_cors_preflight():
    if request.method == "OPTIONS":
        resp = jsonify()
        resp.headers.add("Access-Control-Allow-Origin", request.headers.get("Origin", "*"))
        resp.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
        resp.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS")
        resp.headers.add("Access-Control-Allow-Credentials", "true")
        return resp

# -------------------------------------------------------------------
# POST /api/admin/payouts/process
# Body: { "partner_type": "restaurant"|"delivery", "cycle_type": "weekly"|"bi-weekly"|"monthly" }
# Gera os payouts agregados a partir dos orders 'delivered' não pagos.
# -------------------------------------------------------------------
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
        partner_type = (body.get("partner_type") or "").strip().lower()
        cycle_type = (body.get("cycle_type") or "weekly").strip().lower()

        if partner_type not in ("restaurant", "delivery"):
            return jsonify({"error": "partner_type inválido (restaurant|delivery)"}), 400
        if cycle_type not in ("weekly", "bi-weekly", "monthly"):
            return jsonify({"error": "cycle_type inválido (weekly|bi-weekly|monthly)"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        result = process_payouts(conn, partner_type=partner_type, cycle_type=cycle_type)
        conn.commit()

        return jsonify({
            "status": "success",
            "partner_type": partner_type,
            "cycle_type": cycle_type,
            "generated_count": len(result),
            "payouts": result
        }), 200

    except Exception as e:
        logger.exception("Erro ao processar payouts")
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno ao processar payouts"}), 500
    finally:
        if conn: conn.close()

# -------------------------------------------------------------------
# GET /api/admin/payouts
# Query params: partner_type, status, partner_id, start_date, end_date, limit, offset
# Retorna lista paginada de payouts.
# -------------------------------------------------------------------
@payouts_bp.route("", methods=["GET", "OPTIONS"])
def list_payouts():
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        partner_type = (request.args.get("partner_type") or "").strip().lower()  # restaurant|delivery|""(todos)
        status       = (request.args.get("status") or "").strip().lower()       # pending|paid|cancelled|""(todos)
        partner_id   = request.args.get("partner_id")
        start_date   = request.args.get("start_date")  # ISO
        end_date     = request.args.get("end_date")    # ISO
        limit        = int(request.args.get("limit") or 20)
        offset       = int(request.args.get("offset") or 0)

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        where = []
        params = []

        if partner_type in ("restaurant", "delivery"):
            where.append("partner_type = %s")
            params.append(partner_type)
        if status in ("pending", "paid", "cancelled"):
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
        order_sql = "ORDER BY created_at DESC"
        pag_sql   = "LIMIT %s OFFSET %s"

        with conn.cursor(cursor_factory=DictCursor) as cur:
            # total
            cur.execute(f"SELECT COUNT(*) AS total FROM payouts {where_sql}", tuple(params))
            total = int(cur.fetchone()["total"])

            # page
            cur.execute(
                f"""SELECT id, partner_type, partner_id, period_start, period_end,
                           total_gross, commission_fee, total_net, status,
                           payment_method, payment_ref,
                           created_at, updated_at
                    FROM payouts
                    {where_sql} {order_sql} {pag_sql}""",
                tuple(params + [limit, offset])
            )
            rows = [dict(r) for r in cur.fetchall()]

        # conversões leves
        for r in rows:
            for k in ("period_start", "period_end", "created_at", "updated_at"):
                if r.get(k) and hasattr(r[k], "isoformat"):
                    r[k] = r[k].isoformat()

        return jsonify({"status": "success", "items": rows, "total": total}), 200

    except Exception:
        logger.exception("Erro ao listar payouts")
        return jsonify({"error": "Erro interno ao listar payouts"}), 500
    finally:
        if conn: conn.close()

# -------------------------------------------------------------------
# GET /api/admin/payouts/<uuid:payout_id>
# Detalhes + itens por pedido
# -------------------------------------------------------------------
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
                """SELECT id, partner_type, partner_id, period_start, period_end,
                          total_gross, commission_fee, total_net, status,
                          payment_method, payment_ref, created_at, updated_at
                   FROM payouts WHERE id = %s""", (str(payout_id),)
            )
            head = cur.fetchone()
            if not head:
                return jsonify({"error": "Payout não encontrado"}), 404

            cur.execute(
                """SELECT id, order_id, order_total, delivery_fee,
                          commission_applied, net_amount
                   FROM payout_items
                   WHERE payout_id = %s
                   ORDER BY created_at ASC""",
                (str(payout_id),)
            )
            items = [dict(r) for r in cur.fetchall()]

        head = dict(head)
        for k in ("period_start","period_end","created_at","updated_at"):
            if head.get(k) and hasattr(head[k], "isoformat"):
                head[k] = head[k].isoformat()

        return jsonify({"status": "success", "payout": head, "items": items}), 200

    except Exception:
        logger.exception("Erro ao obter detalhe de payout")
        return jsonify({"error": "Erro interno ao obter payout"}), 500
    finally:
        if conn: conn.close()

# -------------------------------------------------------------------
# POST /api/admin/payouts/<uuid:payout_id>/mark-paid
# Body: { "payment_method": "pix|transfer|cash|...", "payment_ref": "txid123", "paid_at": "2025-10-31T10:30:00" (opcional) }
# -------------------------------------------------------------------
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
        paid_at = body.get("paid_at")  # opcional

        if not method:
            return jsonify({"error": "payment_method é obrigatório"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT status FROM payouts WHERE id = %s", (str(payout_id),))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Payout não encontrado"}), 404
            if row["status"] == "paid":
                return jsonify({"error": "Payout já está pago"}), 400
            if row["status"] == "cancelled":
                return jsonify({"error": "Payout está cancelado"}), 400

            if paid_at:
                try:
                    # valida formato
                    datetime.fromisoformat(paid_at.replace("Z","+00:00"))
                except Exception:
                    return jsonify({"error": "paid_at inválido (ISO8601)"}), 400

            cur.execute(
                """UPDATE payouts
                      SET status='paid',
                          payment_method=%s,
                          payment_ref=%s,
                          updated_at=NOW()
                    WHERE id=%s
                    RETURNING *""",
                (method, ref or None, str(payout_id))
            )
            updated = dict(cur.fetchone())
            conn.commit()

        for k in ("period_start","period_end","created_at","updated_at"):
            if updated.get(k) and hasattr(updated[k], "isoformat"):
                updated[k] = updated[k].isoformat()

        return jsonify({"status": "success", "payout": updated}), 200

    except Exception:
        logger.exception("Erro ao marcar payout como pago")
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno ao marcar payout como pago"}), 500
    finally:
        if conn: conn.close()

# -------------------------------------------------------------------
# POST /api/admin/payouts/<uuid:payout_id>/cancel
# -------------------------------------------------------------------
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
            cur.execute("SELECT status FROM payouts WHERE id = %s", (str(payout_id),))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Payout não encontrado"}), 404
            if row["status"] == "paid":
                return jsonify({"error": "Não é possível cancelar um payout já pago"}), 400
            if row["status"] == "cancelled":
                return jsonify({"error": "Payout já está cancelado"}), 400

            cur.execute(
                """UPDATE payouts
                      SET status='cancelled', updated_at=NOW()
                    WHERE id=%s
                    RETURNING *""",
                (str(payout_id),)
            )
            updated = dict(cur.fetchone())
            conn.commit()

        for k in ("period_start","period_end","created_at","updated_at"):
            if updated.get(k) and hasattr(updated[k], "isoformat"):
                updated[k] = updated[k].isoformat()

        return jsonify({"status": "success", "payout": updated}), 200

    except Exception:
        logger.exception("Erro ao cancelar payout")
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno ao cancelar payout"}), 500
    finally:
        if conn: conn.close()
