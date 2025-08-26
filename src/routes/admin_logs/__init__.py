from flask import Blueprint, request, jsonify
import logging
from src.utils.helpers import get_user_id_from_token, get_db_connection

logger = logging.getLogger(__name__)
admin_logs_bp = Blueprint("admin_logs", __name__, url_prefix="/api/logs")

def _get_pagination():
    """
    Lê query params limit/page_size e page, aplica defaults e limites seguros.
    Retorna (limit, offset, page).
    """
    try:
        # Support both 'limit' and 'page_size' as aliases
        limit = int(request.args.get("limit") or request.args.get("page_size", 50))
    except ValueError:
        limit = 50
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1

    # Limites razoáveis
    if limit <= 0:
        limit = 50
    if limit > 200:
        limit = 200
    if page <= 0:
        page = 1

    offset = (page - 1) * limit
    return limit, offset, page

def _get_sort_order():
    """
    Lê query param sort, retorna ORDER BY clause segura.
    Suporta: timestamp, -timestamp (DESC default).
    """
    sort_param = request.args.get("sort", "-timestamp").strip()
    
    if sort_param == "timestamp":
        return "ORDER BY timestamp ASC"
    elif sort_param == "-timestamp" or sort_param == "timestamp_desc":
        return "ORDER BY timestamp DESC"
    else:
        # Default to timestamp DESC for any invalid sort
        return "ORDER BY timestamp DESC"

@admin_logs_bp.get("/")
def list_admin_logs():
    """
    GET /api/logs
    Lista logs de ações administrativas com paginação.
    Requer Authorization: Bearer <token> e user_type == 'admin'.
    Query params:
      - limit/page_size: int (default 50, máx 200)
      - page: int (default 1)
      - sort: string ('timestamp' for ASC, '-timestamp' for DESC default)
    Resposta:
      {
        "data": [ { "id": ..., "timestamp": ..., "admin": ..., "action": ..., "details": ... }, ... ],
        "pagination": { "page": 1, "per_page": 50, "total": 123 }
      }
    """
    auth_header = request.headers.get("Authorization")
    user_id, user_type, err = get_user_id_from_token(auth_header)
    if err:
        # err já é uma tupla (jsonify, status)
        return err
    if user_type != "admin":
        return jsonify({"error": "Acesso restrito a administradores"}), 403

    limit, offset, page = _get_pagination()
    sort_order = _get_sort_order()

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor() as cur:
            # Total de registros
            cur.execute("SELECT COUNT(*) FROM admin_logs")
            total = cur.fetchone()[0]

            # Lista paginada com colunas corretas
            query = f"""
                SELECT id, timestamp, admin, action, details
                FROM admin_logs
                {sort_order}
                LIMIT %s OFFSET %s
            """
            cur.execute(query, (limit, offset))
            rows = cur.fetchall()

            # Normalizar resposta
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
    Endpoint simples para verificação de saúde do módulo de logs.
    Requer autenticação de admin (mantém a mesma política de acesso).
    """
    auth_header = request.headers.get("Authorization")
    _, user_type, err = get_user_id_from_token(auth_header)
    if err:
        return err
    if user_type != "admin":
        return jsonify({"error": "Acesso restrito a administradores"}), 403

    return jsonify({"status": "ok"}), 200