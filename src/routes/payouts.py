# src/routes/payouts.py
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify
from psycopg2.extras import DictCursor
from ..utils.helpers import get_db_connection, get_user_id_from_token

logger = logging.getLogger(__name__)

# Todas as rotas ficam em /api/admin/payouts/...
payouts_bp = Blueprint("payouts", __name__, url_prefix="/api/admin/payouts")


# -------------------------------------------------------------------
# Utilidades
# -------------------------------------------------------------------
def _is_admin(user_type: str) -> bool:
    # ajuste se você tiver outros perfis administrativos
    return user_type in ("admin", "superadmin")


def _ok(data, status=200):
    return jsonify(data), status


def _err(msg, status=400):
    return jsonify({"error": msg}), status


@payouts_bp.before_request
def _cors_preflight():
    # permite OPTIONS para CORS
    if request.method == "OPTIONS":
        resp = jsonify()
        origin = request.headers.get("Origin", "*")
        resp.headers.add("Access-Control-Allow-Origin", origin)
        resp.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization")
        resp.headers.add("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        resp.headers.add("Access-Control-Allow-Credentials", "true")
        return resp


# -------------------------------------------------------------------
# GET /api/admin/payouts
# Filtros: status, courier_id, date_from, date_to, limit, offset
# -------------------------------------------------------------------
@payouts_bp.get("")
def list_payouts():
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return _err("Acesso negado", 403)

        status = (request.args.get("status") or "").strip().lower()  # pending|approved|paid|rejected
        courier_id = request.args.get("courier_id")
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        limit = int(request.args.get("limit") or 50)
        offset = int(request.args.get("offset") or 0)

        conn = get_db_connection()
        if not conn:
            return _err("Erro de conexão com banco de dados", 500)

        where = []
        params = []

        if status in ("pending", "approved", "paid", "rejected"):
            where.append("status = %s")
            params.append(status)
        if courier_id:
            where.append("courier_id = %s")
            params.append(courier_id)
        if date_from:
            where.append("created_at >= %s")
            params.append(date_from)
        if date_to:
            where.append("created_at <= %s")
            params.append(date_to)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        order_sql = "ORDER BY created_at DESC"
        pag_sql = "LIMIT %s OFFSET %s"

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(f"SELECT COUNT(*) AS total FROM public.payouts {where_sql}", tuple(params))
            total = int(cur.fetchone()["total"])

            cur.execute(
                f"""
                SELECT id, courier_id, amount, status, method, external_ref, notes, created_at, updated_at
                FROM public.payouts
                {where_sql}
                {order_sql}
                {pag_sql}
                """,
                tuple(params + [limit, offset]),
            )
            rows = [dict(r) for r in cur.fetchall()]

        # formata datas
        for r in rows:
            for k in ("created_at", "updated_at"):
                if r.get(k) and hasattr(r[k], "isoformat"):
                    r[k] = r[k].isoformat()

        return _ok({"status": "success", "items": rows, "total": total})
    except Exception:
        logger.exception("Erro ao listar payouts")
        return _err("Erro interno ao listar payouts", 500)
    finally:
        if conn:
            conn.close()


# -------------------------------------------------------------------
# GET /api/admin/payouts/<uuid:payout_id>
# -------------------------------------------------------------------
@payouts_bp.get("/<uuid:payout_id>")
def get_payout(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return _err("Acesso negado", 403)

        conn = get_db_connection()
        if not conn:
            return _err("Erro de conexão com banco de dados", 500)

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """
                SELECT id, courier_id, amount, status, method, external_ref, notes, created_at, updated_at
                FROM public.payouts
                WHERE id = %s
                """,
                (str(payout_id),),
            )
            row = cur.fetchone()
            if not row:
                return _err("Payout não encontrado", 404)
            data = dict(row)
            for k in ("created_at", "updated_at"):
                if data.get(k) and hasattr(data[k], "isoformat"):
                    data[k] = data[k].isoformat()
        return _ok({"status": "success", "payout": data})
    except Exception:
        logger.exception("Erro ao obter payout")
        return _err("Erro interno ao obter payout", 500)
    finally:
        if conn:
            conn.close()


# -------------------------------------------------------------------
# POST /api/admin/payouts
# body: { courier_id, amount, method? (pix|manual...), notes? }
# cria payout com status 'pending'
# -------------------------------------------------------------------
@payouts_bp.post("")
def create_payout():
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return _err("Acesso negado", 403)

        body = request.get_json(silent=True) or {}
        courier_id = (body.get("courier_id") or "").strip()
        amount = body.get("amount")
        method = (body.get("method") or "pix").strip().lower()
        notes = body.get("notes") or None

        if not courier_id or amount is None:
            return _err("courier_id e amount são obrigatórios", 400)

        try:
            amount = float(amount)
            if amount <= 0:
                return _err("amount deve ser maior que zero", 400)
        except Exception:
            return _err("amount inválido", 400)

        conn = get_db_connection()
        if not conn:
            return _err("Erro de conexão com banco de dados", 500)

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """
                INSERT INTO public.payouts (id, courier_id, amount, status, method, external_ref, notes, created_at, updated_at)
                VALUES (gen_random_uuid(), %s, %s, 'pending', %s, NULL, %s, NOW(), NOW())
                RETURNING id, courier_id, amount, status, method, external_ref, notes, created_at, updated_at
                """,
                (courier_id, amount, method, notes),
            )
            row = dict(cur.fetchone())
            conn.commit()

        for k in ("created_at", "updated_at"):
            if row.get(k) and hasattr(row[k], "isoformat"):
                row[k] = row[k].isoformat()
        return _ok({"status": "success", "payout": row}, 201)
    except Exception:
        logger.exception("Erro ao criar payout")
        if conn:
            conn.rollback()
        return _err("Erro interno ao criar payout", 500)
    finally:
        if conn:
            conn.close()


# -------------------------------------------------------------------
# POST /api/admin/payouts/<uuid:payout_id>/approve
# permite de 'pending' -> 'approved'
# -------------------------------------------------------------------
@payouts_bp.post("/<uuid:payout_id>/approve")
def approve_payout(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return _err("Acesso negado", 403)

        conn = get_db_connection()
        if not conn:
            return _err("Erro de conexão com banco de dados", 500)

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """
                UPDATE public.payouts
                   SET status='approved', updated_at=NOW()
                 WHERE id = %s AND status = 'pending'
                RETURNING id, courier_id, amount, status, method, external_ref, notes, created_at, updated_at
                """,
                (str(payout_id),),
            )
            row = cur.fetchone()
            conn.commit()

        if not row:
            return _err("Payout não encontrado ou status inválido", 404)

        data = dict(row)
        for k in ("created_at", "updated_at"):
            if data.get(k) and hasattr(data[k], "isoformat"):
                data[k] = data[k].isoformat()
        return _ok({"status": "success", "payout": data})
    except Exception:
        logger.exception("Erro ao aprovar payout")
        if conn:
            conn.rollback()
        return _err("Erro interno ao aprovar payout", 500)
    finally:
        if conn:
            conn.close()


# -------------------------------------------------------------------
# POST /api/admin/payouts/<uuid:payout_id>/reject
# permite de 'pending'/'approved' -> 'rejected'
# body: { reason?: string }
# -------------------------------------------------------------------
@payouts_bp.post("/<uuid:payout_id>/reject")
def reject_payout(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return _err("Acesso negado", 403)

        body = request.get_json(silent=True) or {}
        reason = (body.get("reason") or "").strip()

        conn = get_db_connection()
        if not conn:
            return _err("Erro de conexão com banco de dados", 500)

        note_tag = f"\n[rejected {datetime.utcnow().isoformat()}] {reason}" if reason else "\n[rejected]"
        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """
                UPDATE public.payouts
                   SET status='rejected',
                       notes = COALESCE(notes,'') || %s,
                       updated_at=NOW()
                 WHERE id = %s AND status IN ('pending','approved')
                RETURNING id, courier_id, amount, status, method, external_ref, notes, created_at, updated_at
                """,
                (note_tag, str(payout_id)),
            )
            row = cur.fetchone()
            conn.commit()

        if not row:
            return _err("Payout não encontrado ou status inválido", 404)

        data = dict(row)
        for k in ("created_at", "updated_at"):
            if data.get(k) and hasattr(data[k], "isoformat"):
                data[k] = data[k].isoformat()
        return _ok({"status": "success", "payout": data})
    except Exception:
        logger.exception("Erro ao rejeitar payout")
        if conn:
            conn.rollback()
        return _err("Erro interno ao rejeitar payout", 500)
    finally:
        if conn:
            conn.close()


# -------------------------------------------------------------------
# POST /api/admin/payouts/<uuid:payout_id>/pay
# permite de 'pending'/'approved' -> 'paid'
# body: { external_ref?: string }
# -------------------------------------------------------------------
@payouts_bp.post("/<uuid:payout_id>/pay")
def pay_payout(payout_id):
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
        if error:
            return error
        if not _is_admin(user_type):
            return _err("Acesso negado", 403)

        body = request.get_json(silent=True) or {}
        external_ref = (body.get("external_ref") or "").strip() or None

        conn = get_db_connection()
        if not conn:
            return _err("Erro de conexão com banco de dados", 500)

        with conn.cursor(cursor_factory=DictCursor) as cur:
            cur.execute(
                """
                UPDATE public.payouts
                   SET status='paid',
                       external_ref = COALESCE(%s, external_ref),
                       updated_at=NOW()
                 WHERE id = %s AND status IN ('pending','approved')
                RETURNING id, courier_id, amount, status, method, external_ref, notes, created_at, updated_at
                """,
                (external_ref, str(payout_id)),
            )
            row = cur.fetchone()
            conn.commit()

        if not row:
            return _err("Payout não encontrado ou status inválido", 404)

        data = dict(row)
        for k in ("created_at", "updated_at"):
            if data.get(k) and hasattr(data[k], "isoformat"):
                data[k] = data[k].isoformat()
        return _ok({"status": "success", "payout": data})
    except Exception:
        logger.exception("Erro ao marcar payout como pago")
        if conn:
            conn.rollback()
        return _err("Erro interno ao marcar payout como pago", 500)
    finally:
        if conn:
            conn.close()
