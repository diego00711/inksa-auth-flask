# src/routes/fcm_routes.py
# PATCH /api/profile/fcm-token — atualiza fcm_token na tabela correta

import logging
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token

logger = logging.getLogger(__name__)

fcm_bp = Blueprint('fcm', __name__)

_TABLE_MAP = {
    'client': 'client_profiles',
    'restaurant': 'restaurant_profiles',
    'delivery': 'delivery_profiles',
}


@fcm_bp.route('/fcm-token', methods=['PATCH'])
def update_fcm_token():
    """
    PATCH /api/profile/fcm-token
    Body: { "fcm_token": "...", "user_type": "client|restaurant|delivery" }
    Retorna: { "success": true }
    """
    conn = None
    try:
        user_id, user_type_auth, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error

        data = request.get_json(silent=True) or {}
        fcm_token = data.get('fcm_token', '').strip()
        user_type = data.get('user_type', user_type_auth or '').strip().lower()

        if not fcm_token:
            return jsonify({"error": "fcm_token e obrigatorio"}), 400

        table = _TABLE_MAP.get(user_type)
        if not table:
            return jsonify({"error": f"user_type invalido: '{user_type}'. Use client, restaurant ou delivery"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexao com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Tenta atualizar; se coluna nao existir, retorna graciosamente
            try:
                cur.execute(
                    f"UPDATE {table} SET fcm_token = %s WHERE user_id = %s",
                    (fcm_token, user_id)
                )
                if cur.rowcount == 0:
                    return jsonify({"error": "Perfil nao encontrado para o usuario autenticado"}), 404
                conn.commit()
            except psycopg2.errors.UndefinedColumn:
                conn.rollback()
                logger.warning(f"fcm_token column nao existe em {table}. Execute a migration add_fcm_token.sql")
                return jsonify({
                    "success": False,
                    "warning": "Coluna fcm_token ainda nao existe. Execute a migration add_fcm_token.sql"
                }), 200

        return jsonify({"success": True}), 200

    except Exception as e:
        logger.error(f"Erro em update_fcm_token: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()
