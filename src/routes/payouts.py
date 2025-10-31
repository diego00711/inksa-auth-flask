# src/routes/payouts.py
import logging
from datetime import datetime
from uuid import UUID

from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras

from ..utils.helpers import get_db_connection, get_user_id_from_token
from ..logic.payout_processor import process_payouts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

payouts_bp = Blueprint("payouts", __name__)

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def _is_admin(user_type: str) -> bool:
    # ajuste se seu sistema utilizar outra checagem (ex: role em tabela users)
    return (user_type or "").lower() == "admin"

def _ok(data=None, status=200):
    return jsonify({"status": "success", "data": data}), status

def _err(msg, status=400):
    return jsonify({"status": "error", "error": msg}), status

def _to_uuid(value):
    try:
        return str(UUID(str(value)))
    except Exception:
        return None

def _parse_date(s):
    if not s:
        return None
    try:
        # aceita "YYYY-MM-DD" ou ISO
        return datetime.fromisoformat(s).date()
    except Exception:
        return None

def _row_to_dict(row):
    """Converte registro DictRow para dict serializável."""
    if row is None:
        return None
    d = dict(row)
    for k, v in list(d.items()):
        if isinstance(v, (datetime,)):
            d[k] = v.isoformat()
        # tipos date/time/etc. o psycopg2 já serializa bem quando vira string
        # IDs UUID -> string
        if hasattr(v, "hex") and len(getattr(v, "hex", "")) == 32:
            d[k] = str(v)
    return d

# CORS preflight local
@payouts_bp.before_request
def handle_options():
    if request.method == "OPTIONS":
        resp = jsonify()
        resp.headers.add("Access-Control-Allow-Origin", request.headers.get("Origin", "*"))
        resp.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
        resp.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS")
        resp.headers.add("Access-Control-Allow-Credentials", "true")
        return resp

# -------------------------------------------------------------------
# POST /process  -> gera payouts a partir das views/tabelas (processor)
# -------------------------------------------------------------------
@payouts_bp.route("/process", methods=["POST"])
def process_payouts_route():
    """
    POST /api/admin/payouts/process
    Body:
      {
        "partner_type": "restaurant" | "delivery",
        "cycle_type": "weekly" | "bi-weekly" | "monthly"
      }
    """
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return _err("Acesso negado. Somente administradores.", 403)

        body = request.get_json(silent=True) or {}
        partner_type = (body.get("partner_type") or "").strip().lower()
        cycle_type = (body.get("cycle_type") or "weekly").strip().lower()

        if partner_type not in ("restaurant", "delivery"):
            return _err("partner_type inválido (restaurant | delivery)")
        if cycle_type not in ("weekly", "bi-weekly", "monthly"):
            return _err("cycle_type inválido (weekly | bi-weekly | monthly)")

        conn = get_db_connection()
        if not conn:
            return _err("Erro de conexão com banco de dados", 500)

        logger.info("➡️ Iniciando processamento de payouts: partner_type=%s, cycle_type=%s", partner_type, cycle_type)
        result = process_payouts(conn, partner_type=partner_type, cycle_type=cycle_type)
        conn.commit()

        payload = {
            "partner_type": partner_type,
            "cycle_type": cycle_type,
            "generated_count": len(result),
            "payouts": result,
        }
        return _ok(payload, 200)

    except Exception as e:
        logger.exception("Erro ao processar payouts")
        if conn:
            conn.rollback()
        return _err("Erro interno ao processar payouts", 500)
    finally:
        if conn:
            conn.close()

# -------------------------------------------------------------------
# GET /list  -> lista payouts com filtros
# -------------------------------------------------------------------
@payouts_bp.route("/list", methods=["GET"])
def list_payouts():
    """
    GET /api/admin/payouts/list?partner_type=restaurant|delivery&status=pending|paid|cancelled
                               &from=YYYY-MM-DD&to=YYYY-MM-DD
                               &partner_id=<uuid>
                               &limit=50&offset=0
    """
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return _err("Acesso negado. Somente administradores.", 403)

        partner_type = (request.args.get("partner_type") or "").strip().lower() or None
        status = (request.args.get("status") or "").strip().lower() or None
        partner_id = _to_uuid(request.args.get("partner_id"))
        d_from = _parse_date(request.args.get("from"))
        d_to = _parse_date(request.args.get("to"))
        try:
            limit = min(max(int(request.args.get("limit", 50)), 1), 200)
            offset = max(int(request.args.get("offset", 0)), 0)
        except Exception:
            limit, offset = 50, 0

        wh = []
        params = []

        if partner_type in ("restaurant", "delivery"):
            wh.append("partner_type = %s")
            params.append(partner_type)
        if status in ("pending", "paid", "cancelled"):
            wh.append("status = %s")
            params.append(status)
        if partner_id:
            wh.append("partner_id = %s")
            params.append(partner_id)
        if d_from:
            wh.append("period_end >= %s")
            params.append(d_from)
        if d_to:
            wh.append("period_start <= %s")
            params.append(d_to)

        where_clause = (" WHERE " + " AND ".join(wh)) if wh else ""
        sql = f"""
            SELECT
              id, partner_type, partner_id, period_start, period_end,
              gross_amount, commission_amount, net_amount,
              status, created_at, updated_at, paid_at, paid_by_user_id,
              payment_method, payment_ref
            FROM payouts
            {where_clause}
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
        """
        params.extend([limit, offset])

        conn = get_db_connection()
        if not conn:
            return _err("Erro de conexão com banco de dados", 500)

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            data = [_row_to_dict(r) for r in rows]

        return _ok({"items": data, "limit": limit, "offset": offset, "count": len(data)})

    except Exception as e:
        logger.exception("Erro ao listar payouts")
        return _err("Erro interno ao listar payouts", 500)
    finally:
        if conn:
            conn.close()

# -------------------------------------------------------------------
# GET /<payout_id>/details  -> detalhe + itens (view) se existir
# -------------------------------------------------------------------
@payouts_bp.route("/<uuid:payout_id>/details", methods=["GET"])
def payout_details(payout_id):
    """
    GET /api/admin/payouts/<payout_id>/details
    Retorna o payout + itens (se a view payouts_items_v existir).
    """
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return _err("Acesso negado. Somente administradores.", 403)

        conn = get_db_connection()
        if not conn:
            return _err("Erro de conexão com banco de dados", 500)

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT
                  id, partner_type, partner_id, period_start, period_end,
                  gross_amount, commission_amount, net_amount,
                  status, created_at, updated_at, paid_at, paid_by_user_id,
                  payment_method, payment_ref
                FROM payouts
                WHERE id = %s
            """, (str(payout_id),))
            row = cur.fetchone()
            if not row:
                return _err("Payout não encontrado", 404)
            payout = _row_to_dict(row)

            # tenta buscar itens na view consolidada (se existir)
            items = []
            try:
                cur.execute("""
                    SELECT order_id, partner_type, partner_id, order_amount,
                           delivery_fee, commission_amount, net_amount,
                           created_at, status
                    FROM payouts_items_v
                    WHERE payout_id = %s
                    ORDER BY created_at ASC
                """, (str(payout_id),))
                items = [_row_to_dict(r) for r in cur.fetchall()]
            except Exception:
                # fallback (opcional): pode tentar buscar direto em orders com período
                logger.info("View payouts_items_v não encontrada, retornando sem itens")

        return _ok({"payout": payout, "items": items})

    except Exception as e:
        logger.exception("Erro ao obter detalhes do payout")
        return _err("Erro interno ao obter detalhes do payout", 500)
    finally:
        if conn:
            conn.close()

# -------------------------------------------------------------------
# POST /<payout_id>/pay  -> marca payout como pago
# -------------------------------------------------------------------
@payouts_bp.route("/<uuid:payout_id>/pay", methods=["POST"])
def pay_payout(payout_id):
    """
    POST /api/admin/payouts/<payout_id>/pay
    Body:
      {
        "payment_method": "pix" | "manual" | "transfer" | ...,
        "payment_ref": "comprovante / txid / id",
        "paid_at": "ISO(optional)"   # se não vier, NOW()
      }
    Efeito:
      - Atualiza payout -> status='paid', paid_at, paid_by_user_id, payment_method, payment_ref
    """
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return _err("Acesso negado. Somente administradores.", 403)

        body = request.get_json(silent=True) or {}
        payment_method = (body.get("payment_method") or "manual").strip().lower()
        payment_ref = (body.get("payment_ref") or "").strip()
        paid_at_iso = body.get("paid_at")
        paid_at = None
        if paid_at_iso:
            try:
                paid_at = datetime.fromisoformat(paid_at_iso)
            except Exception:
                return _err("paid_at inválido (use ISO 8601)")

        conn = get_db_connection()
        if not conn:
            return _err("Erro de conexão com banco de dados", 500)

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id, status FROM payouts WHERE id = %s", (str(payout_id),))
            row = cur.fetchone()
            if not row:
                return _err("Payout não encontrado", 404)

            current_status = (row["status"] or "").lower()
            if current_status == "paid":
                return _err("Payout já está marcado como pago", 409)
            if current_status == "cancelled":
                return _err("Payout cancelado não pode ser pago", 409)

            cur.execute("""
                UPDATE payouts
                   SET status = 'paid',
                       paid_at = COALESCE(%s, NOW()),
                       paid_by_user_id = %s,
                       payment_method = %s,
                       payment_ref = %s,
                       updated_at = NOW()
                 WHERE id = %s
                RETURNING *
            """, (paid_at, user_id, payment_method, payment_ref, str(payout_id)))
            updated = cur.fetchone()
            conn.commit()

        return _ok(_row_to_dict(updated))

    except Exception as e:
        logger.exception("Erro ao marcar payout como pago")
        if conn:
            conn.rollback()
        return _err("Erro interno ao marcar payout como pago", 500)
    finally:
        if conn:
            conn.close()

# -------------------------------------------------------------------
# (Opcional) DELETE /<payout_id> -> cancelar payout
# -------------------------------------------------------------------
@payouts_bp.route("/<uuid:payout_id>", methods=["DELETE"])
def cancel_payout(payout_id):
    """
    DELETE /api/admin/payouts/<payout_id>
    Marca payout como 'cancelled' (apenas se ainda não pago).
    """
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return _err("Acesso negado. Somente administradores.", 403)

        conn = get_db_connection()
        if not conn:
            return _err("Erro de conexão com banco de dados", 500)

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id, status FROM payouts WHERE id = %s", (str(payout_id),))
            row = cur.fetchone()
            if not row:
                return _err("Payout não encontrado", 404)

            if (row["status"] or "").lower() == "paid":
                return _err("Payout pago não pode ser cancelado", 409)

            cur.execute("""
                UPDATE payouts
                   SET status='cancelled', updated_at=NOW()
                 WHERE id = %s
                RETURNING *
            """, (str(payout_id),))
            updated = cur.fetchone()
            conn.commit()

        return _ok(_row_to_dict(updated))

    except Exception as e:
        logger.exception("Erro ao cancelar payout")
        if conn:
            conn.rollback()
        return _err("Erro interno ao cancelar payout", 500)
    finally:
        if conn:
            conn.close()
