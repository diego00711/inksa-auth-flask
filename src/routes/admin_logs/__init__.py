from flask import Blueprint, request, jsonify
import logging
from src.utils.helpers import get_user_id_from_token, get_db_connection

logger = logging.getLogger(__name__)
admin_logs_bp = Blueprint("admin_logs", __name__, url_prefix="/api/logs")

def _get_pagination():
    """
    Read query params for pagination with safe defaults and bounds.
    Supports both limit and page_size (page_size has priority for backward compat).
    Returns (limit, offset, page).
    """
    try:
        limit = int(request.args.get("page_size", request.args.get("limit", 50)))
    except (TypeError, ValueError):
        limit = 50
    try:
        page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        page = 1

    if limit <= 0:
        limit = 50
    if limit > 200:
        limit = 200
    if page <= 0:
        page = 1

    offset = (page - 1) * limit
    return limit, offset, page


def _get_sort_direction():
    """
    Parse sort parameter. Accepted values:
      - "-timestamp" => DESC (default)
      - "timestamp"  => ASC
    """
    sort = request.args.get("sort", "-timestamp")
    if sort == "timestamp":
        return "ASC"
    return "DESC"

@admin_logs_bp.get("/")
def list_admin_logs():
    """
    GET /api/logs
    Lists admin audit logs with pagination.
    Requires Authorization: Bearer <token> and user_type == 'admin'.

    Query params:
      - page: int (default 1)
      - limit | page_size: int (default 50, max 200)
      - sort: "-timestamp" (default) or "timestamp"

    Response:
      {
        "data": [ { "id": ..., "timestamp": "...", "admin": "...", "action": "...", "details": "..." }, ... ],
        "pagination": { "page": 1, "per_page": 50, "total": 123 }
      }
    """
    auth_header = request.headers.get("Authorization")
    user_id, user_type, err = get_user_id_from_token(auth_header)
    if err:
        return err
    if user_type != "admin":
        return jsonify({"error": "Acesso restrito a administradores"}), 403

    limit, offset, page = _get_pagination()
    order_dir = _get_sort_direction()

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor() as cur:
            # Count total
            cur.execute("SELECT COUNT(*) FROM admin_logs")
            total = cur.fetchone()[0]

            # Paged list matching the migration schema
            cur.execute(
                f"""
                SELECT id, timestamp, admin, action, details
                FROM admin_logs
                ORDER BY timestamp {order_dir}
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]
            data = [dict(zip(columns, row)) for row in rows]

        return jsonify(
            {
                "data": data,
                "pagination": {
                    "page": page,
                    "per_page": limit,
                    "total": total,
                },
            }
        ), 200
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
    """
    Simple health check for logs module. Requires admin authentication.
    """
    auth_header = request.headers.get("Authorization")
    _, user_type, err = get_user_id_from_token(auth_header)
    if err:
        return err
    if user_type != "admin":
        return jsonify({"error": "Acesso restrito a administradores"}), 403

    return jsonify({"status": "ok"}), 200