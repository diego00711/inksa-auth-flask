from flask import Blueprint, request, jsonify
import logging
from src.utils.helpers import get_user_id_from_token, get_db_connection

logger = logging.getLogger(__name__)
admin_logs_bp = Blueprint("admin_logs", __name__, url_prefix="/api/logs")


def _require_admin():
    auth_header = request.headers.get("Authorization")
    user_id, user_type, err = get_user_id_from_token(auth_header)
    if err:
        return None, err
    if user_type != "admin":
        return None, (jsonify({"error": "Acesso restrito a administradores"}), 403)
    return user_id, None


@admin_logs_bp.get("")
@admin_logs_bp.get("/")
def list_admin_logs():
    """
    GET /api/admin/logs
    Lista logs de ações administrativas com paginação e filtros.
    Query params:
      - page: int (default 1)
      - per_page: int (default 25, máx 200)
      - q: busca livre em action, admin, details
      - action: filtro exato por action
    Retorna: { data, total, page, per_page, total_pages }
    """
    _, err = _require_admin()
    if err:
        return err

    try:
        page = max(int(request.args.get("page", 1)), 1)
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = min(max(int(request.args.get("per_page", 25)), 1), 200)
    except (ValueError, TypeError):
        per_page = 25
    offset = (page - 1) * per_page
    q = (request.args.get("q") or "").strip()
    action_filter = (request.args.get("action") or "").strip()

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor() as cur:
            where_clauses = []
            params = []

            if q:
                where_clauses.append(
                    "(action ILIKE %s OR admin ILIKE %s OR details ILIKE %s)"
                )
                like = f"%{q}%"
                params += [like, like, like]

            if action_filter:
                where_clauses.append("action = %s")
                params.append(action_filter)

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            cur.execute(f"SELECT COUNT(*) FROM admin_logs {where_sql}", params)
            total = cur.fetchone()[0]

            cur.execute(
                f"""
                SELECT id, timestamp, admin, action, details, actor_id, resource, metadata
                FROM admin_logs
                {where_sql}
                ORDER BY timestamp DESC
                LIMIT %s OFFSET %s
                """,
                params + [per_page, offset],
            )
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            data = []
            for row in rows:
                d = dict(zip(columns, row))
                if d.get("timestamp") and hasattr(d["timestamp"], "isoformat"):
                    d["timestamp"] = d["timestamp"].isoformat()
                data.append(d)

        total_pages = max(1, (total + per_page - 1) // per_page)
        return jsonify({
            "data": data,
            "total": total,
            "page": page,
            "per_page": per_page,
            "total_pages": total_pages,
        }), 200

    except Exception as e:
        logger.exception("Erro ao consultar admin_logs: %s", e)
        return jsonify({"error": "Erro ao consultar logs"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@admin_logs_bp.get("/health")
def logs_health():
    _, err = _require_admin()
    if err:
        return err
    return jsonify({"status": "ok"}), 200
