# src/routes/payouts.py
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify
from psycopg2.extras import DictCursor
from ..utils.helpers import get_db_connection, get_user_id_from_token

from ..logic.payout_processor import process_payouts  # já existente no seu projeto
from ..providers.mp_payouts import get_payout_provider

logger = logging.getLogger(__name__)
payouts_bp = Blueprint("payouts", __name__)

def _is_admin(user_type: str) -> bool:
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

# Utilitário: busca PIX key e nome do parceiro
def _get_partner_pix_data(conn, *, partner_type: str, partner_id: str):
    with conn.cursor(cursor_factory=DictCursor) as cur:
        if partner_type == "delivery":
            # ajuste o nome da tabela/campos conforme seu schema
            cur.execute("""
                SELECT dp.pix_key, dp.full_name
                FROM delivery_profiles dp
                WHERE dp.id = %s OR dp.user_id = %s
                LIMIT 1
            """, (partner_id, partner_id))
        else:
            # restaurant
            cur.execute("""
                SELECT COALESCE(r.pix_key, r.bank_pix_key) AS pix_key, r.trade_name AS full_name
                FROM restaurants r
                WHERE r.id = %s OR r.user_id = %s
                LIMIT 1
            """, (partner_id, partner_id))
        row = cur.fetchone()
    if not row:
        return None, None
    return row.get("pix_key"), row.get("full_name")

# -------------------------------------------------------------------
# POST /api/admin/payouts/process
# -------------------------------------------------------------------
@payouts_bp.route("/process", methods=["POST", "OPTIONS"])
def process_payouts_route():
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error: return error
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
        if not conn: return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        result = process_payouts(conn, partner_type=partner_type, cycle_type=cycle_type)
        conn.commit()

        return jsonify({
            "status": "success",
            "partner_type": partner_type,
            "cycle_type": cycle_type,
            "generated_count": len(result),
            "payouts": result
        }), 200
    except Exception:
        logger.exception("Erro ao processar payouts")
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno ao processar payouts"}), 500
    finally:
        if conn: conn.close()

# -------------------------------------------------------------------
# GET /api/admin/payouts (lista paginada)
# -------------------------------------------------------------------
@payouts_bp.route("", methods=["GET", "OPTIONS"])
def list_payouts():
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error: return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        partner_type = (request.args.get("partner_type") or "").strip().lower()
        status       = (request.args.get("status") or "").strip().lower()
        partner_id   = request.args.get("partner_id")
        start_date   = request.args.get("start_date")
        end_date     = request.args.get("end_date")
        limit        = int(request.args.get("limit") or 20)
        offset       = int(request.args.get("offset") or 0)

        conn = get_db_connection()
        if not conn: return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        where, params = [], []
        if partner_type in ("restaurant", "delivery"):
            where.append("partner_type = %s"); params.append(partner_type)
        if status in ("pending", "paid", "cancelled"):
            where.append("status = %s"); params.append(status)
        if partner_id:
            where.append("partner_id = %s"); params.append(partner_id)
        if start_date:
            where.append("created_at >= %s"); params.append(start_date)
        if end_date:
            where.append("created_at <= %s"); params.append(end_date)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        order_sql = "ORDER BY created_at DESC"
        pag_sql   = "LIMIT %s OFFSET %s"

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM payouts {where_sql}", tuple(params))
            total = int(cur.fetchone()["total"])
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
# GET /api/admin/payouts/<id> (detalhe)
# -------------------------------------------------------------------
@payouts_bp.route("/<uuid:payout_id>", methods=["GET", "OPTIONS"])
def get_payout_detail(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error: return error
        if not _is_admin(user_type):
            return jsonify({"error": "Acesso negado"}), 403

        conn = get_db_connection()
        if not conn: return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """SELECT id, partner_type, partner_id, period_start, period_end,
                          total_gross, commission_fee, total_net, status,
                          payment_method, payment_ref, created_at, updated_at
                   FROM payouts WHERE id = %s""", (str(payout_id),)
            )
            head = cur.fetchone()
            if not head: return jsonify({"error": "Payout não encontrado"}), 404

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
# POST /api/admin/payouts/<id>/mark-paid (manual)
# -------------------------------------------------------------------
@payouts_bp.route("/<uuid:payout_id>/mark-paid", methods=["POST", "OPTIONS"])
def mark_payout_paid(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error: return error
        if not _is_admin(user_type): return jsonify({"error": "Acesso negado"}), 403

        body = request.get_json(silent=True) or {}
        method = (body.get("payment_method") or "").strip()
        ref    = (body.get("payment_ref") or "").strip()

        if not method:
            return jsonify({"error": "payment_method é obrigatório"}), 400

        conn = get_db_connection()
        if not conn: return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT status FROM payouts WHERE id = %s", (str(payout_id),))
            row = cur.fetchone()
            if not row: return jsonify({"error": "Payout não encontrado"}), 404
            if row["status"] in ("paid","cancelled"):
                return jsonify({"error": f"Não permitido no status atual: {row['status']}"}), 400

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
# POST /api/admin/payouts/<id>/cancel
# -------------------------------------------------------------------
@payouts_bp.route("/<uuid:payout_id>/cancel", methods=["POST", "OPTIONS"])
def cancel_payout(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error: return error
        if not _is_admin(user_type): return jsonify({"error": "Acesso negado"}), 403

        conn = get_db_connection()
        if not conn: return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute("SELECT status FROM payouts WHERE id = %s", (str(payout_id),))
            row = cur.fetchone()
            if not row: return jsonify({"error": "Payout não encontrado"}), 404
            if row["status"] in ("paid","cancelled"):
                return jsonify({"error": f"Não permitido no status atual: {row['status']}"}), 400

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

# -------------------------------------------------------------------
# POST /api/admin/payouts/<id>/auto-pay
# Usa o provider configurado para enviar PIX e marcar como pago automaticamente.
# Body opcional: { "description": "Repasse semanal" }
# -------------------------------------------------------------------
@payouts_bp.route("/<uuid:payout_id>/auto-pay", methods=["POST", "OPTIONS"])
def auto_pay_payout(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error: return error
        if not _is_admin(user_type): return jsonify({"error": "Acesso negado"}), 403

        body = request.get_json(silent=True) or {}
        description = (body.get("description") or "Repasse Inksa").strip()

        conn = get_db_connection()
        if not conn: return jsonify({"error": "Erro de conexão com banco de dados"}), 500

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                "SELECT id, partner_type, partner_id, total_net, status FROM payouts WHERE id = %s",
                (str(payout_id),)
            )
            row = cur.fetchone()
            if not row: return jsonify({"error": "Payout não encontrado"}), 404
            row = dict(row)

            if row["status"] != "pending":
                return jsonify({"error": f"Somente payouts 'pending' podem ser pagos (atual: {row['status']})"}), 400

            pix_key, full_name = _get_partner_pix_data(
                conn,
                partner_type=row["partner_type"],
                partner_id=row["partner_id"]
            )
            if not pix_key:
                return jsonify({"error": "Parceiro sem PIX cadastrado"}), 400

            amount_cents = int(round(float(row["total_net"]) * 100))
            provider = get_payout_provider()
            result = provider.transfer_pix(
                amount_cents=amount_cents,
                pix_key=pix_key,
                description=f"{description} - {full_name or row['partner_id']}"
            )

            if not result["ok"]:
                logger.error(f"Falha no provider ao pagar payout {payout_id}: {result['raw']}")
                return jsonify({"error": "Falha ao executar repasse no provedor"}), 502

            txid = result["txid"]

            cur.execute(
                """UPDATE payouts
                      SET status='paid',
                          payment_method='pix',
                          payment_ref=%s,
                          updated_at=NOW()
                    WHERE id=%s
                    RETURNING *""",
                (txid, str(payout_id))
            )
            updated = dict(cur.fetchone())
            conn.commit()

        # normalização de datas
        for k in ("period_start","period_end","created_at","updated_at"):
            if updated.get(k) and hasattr(updated[k], "isoformat"):
                updated[k] = updated[k].isoformat()

        return jsonify({"status": "success", "payout": updated, "provider_txid": txid}), 200

    except NotImplementedError as e:
        logger.warning(f"Provider não implementado: {e}")
        return jsonify({"error": str(e)}), 501
    except Exception:
        logger.exception("Erro no auto-pay")
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno no auto-pay"}), 500
    finally:
        if conn: conn.close()
