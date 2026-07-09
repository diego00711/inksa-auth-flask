# src/routes/social_admin.py
# CRUD (admin) do historico de eventos do Dia I (Inksa Social).
# O historico alimenta a pagina publica de prestacao de contas (/dia-i na landing)
# via GET /api/public/social-day/history.

import logging
from functools import wraps

import psycopg2.extras
from flask import Blueprint, jsonify, request

from ..utils.helpers import get_db_connection, get_user_id_from_token

logger = logging.getLogger(__name__)
social_admin_bp = Blueprint("social_admin_bp", __name__)


def _admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        _uid, user_type, err = get_user_id_from_token(request.headers.get("Authorization"))
        if err:
            return err
        if user_type != "admin":
            return jsonify({"error": "Acesso não autorizado"}), 403
        return fn(*args, **kwargs)
    return wrapper


def _row_to_event(r):
    return {
        "id": str(r["id"]),
        "date": r["event_date"].isoformat() if r["event_date"] else None,
        "start_time": r["start_time"],
        "end_time": r["end_time"],
        "raised": float(r["raised"] or 0),
        "orders_count": int(r["orders_count"] or 0),
        "destination": r["destination"] or "",
        "proof_url": r["proof_url"] or "",
    }


@social_admin_bp.post("/events")
@_admin_required
def create_event():
    """Registra um Dia I no historico (normalmente com os numeros do painel ao vivo)."""
    data = request.get_json(silent=True) or {}
    event_date = (data.get("date") or "").strip()
    if not event_date:
        return jsonify({"error": "Campo 'date' é obrigatório (YYYY-MM-DD)"}), 400
    try:
        raised = round(max(float(data.get("raised") or 0), 0), 2)
        orders_count = max(int(data.get("orders_count") or 0), 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Valores numéricos inválidos"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                INSERT INTO social_day_events
                    (event_date, start_time, end_time, raised, orders_count, destination, proof_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    event_date,
                    (data.get("start_time") or "").strip() or None,
                    (data.get("end_time") or "").strip() or None,
                    raised,
                    orders_count,
                    (data.get("destination") or "").strip() or None,
                    (data.get("proof_url") or "").strip() or None,
                ),
            )
            row = cur.fetchone()
        conn.commit()
        return jsonify({"data": _row_to_event(row)}), 201
    except Exception:
        conn.rollback()
        logger.exception("create_event (social) falhou")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        conn.close()


@social_admin_bp.put("/events/<uuid:event_id>")
@_admin_required
def update_event(event_id):
    """Edita um evento do historico (destino da doação, link, valores)."""
    data = request.get_json(silent=True) or {}
    allowed = {}
    if "destination" in data:
        allowed["destination"] = (data.get("destination") or "").strip() or None
    if "proof_url" in data:
        allowed["proof_url"] = (data.get("proof_url") or "").strip() or None
    if "raised" in data:
        try:
            allowed["raised"] = round(max(float(data.get("raised") or 0), 0), 2)
        except (TypeError, ValueError):
            return jsonify({"error": "Valor inválido em 'raised'"}), 400
    if "orders_count" in data:
        try:
            allowed["orders_count"] = max(int(data.get("orders_count") or 0), 0)
        except (TypeError, ValueError):
            return jsonify({"error": "Valor inválido em 'orders_count'"}), 400
    if not allowed:
        return jsonify({"error": "Nenhum campo válido para atualizar"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            sets = ", ".join(f"{k} = %s" for k in allowed)
            cur.execute(
                f"UPDATE social_day_events SET {sets}, updated_at = NOW() WHERE id = %s RETURNING *",
                (*allowed.values(), str(event_id)),
            )
            row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"error": "Evento não encontrado"}), 404
        return jsonify({"data": _row_to_event(row)}), 200
    except Exception:
        conn.rollback()
        logger.exception("update_event (social) falhou")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        conn.close()


@social_admin_bp.delete("/events/<uuid:event_id>")
@_admin_required
def delete_event(event_id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM social_day_events WHERE id = %s", (str(event_id),))
            deleted = cur.rowcount
        conn.commit()
        if not deleted:
            return jsonify({"error": "Evento não encontrado"}), 404
        return jsonify({"message": "Evento removido"}), 200
    except Exception:
        conn.rollback()
        logger.exception("delete_event (social) falhou")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        conn.close()
