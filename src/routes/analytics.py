# src/routes/analytics.py - VERSÃO FINAL, CORRIGIDA E COMPLETA

import logging
from flask import Blueprint, jsonify, request
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token
from functools import wraps

# Garante que o nome do blueprint seja único.
analytics_bp = Blueprint('analytics_bp', __name__)
logging.basicConfig(level=logging.INFO)

def handle_db_errors(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        conn = None
        try:
            conn = get_db_connection()
            if not conn:
                return jsonify({"status": "error", "error": "Database connection failed"}), 500
            return f(conn, *args, **kwargs)
        except Exception as e:
            logging.error(f"Analytics DB Error: {e}", exc_info=True)
            return jsonify({"status": "error", "error": str(e)}), 500
        finally:
            if conn:
                conn.close()
    return wrapper

@analytics_bp.route('/', methods=['GET'])
@handle_db_errors
def get_analytics_summary(conn):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'restaurant': return jsonify({"status": "error", "error": "Unauthorized"}), 403

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # CORREÇÃO CRÍTICA: Busca o ID do perfil do restaurante a partir do user_id do token.
        cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_id,))
        restaurant_profile = cur.fetchone()
        if not restaurant_profile:
            return jsonify({"status": "error", "error": "Restaurant profile not found"}), 404
        restaurant_id = restaurant_profile['id']

        # 1. Total de Vendas
        cur.execute(
            "SELECT COALESCE(SUM(total_amount), 0) AS total_vendas FROM orders WHERE restaurant_id = %s AND status = 'concluido'",
            (restaurant_id,)
        )
        total_vendas = cur.fetchone()['total_vendas']

        # 2. Total de Pedidos Concluídos
        cur.execute(
            "SELECT COUNT(*) AS pedidos_concluidos FROM orders WHERE restaurant_id = %s AND status = 'concluido'",
            (restaurant_id,)
        )
        pedidos_concluidos = cur.fetchone()['pedidos_concluidos']

        # 3. Item Mais Vendido
        cur.execute("""
            SELECT mi.name
            FROM order_items oi
            JOIN menu_items mi ON oi.menu_item_id = mi.id
            WHERE oi.restaurant_id = %s
            GROUP BY mi.name
            ORDER BY SUM(oi.quantity) DESC
            LIMIT 1
        """, (restaurant_id,))
        item_mais_vendido_row = cur.fetchone()
        item_mais_vendido = item_mais_vendido_row['name'] if item_mais_vendido_row else 'N/A'

        # 4. Vendas por Dia (últimos 7 dias)
        cur.execute("""
            SELECT 
                TO_CHAR(DATE(created_at), 'YYYY-MM-DD') AS dia, 
                SUM(total_amount) AS total
            FROM orders
            WHERE restaurant_id = %s AND status = 'concluido' AND created_at >= NOW() - INTERVAL '7 days'
            GROUP BY dia
            ORDER BY dia DESC
        """, (restaurant_id,))
        vendas_por_dia = [dict(row) for row in cur.fetchall()]

        # Monta o objeto de resposta final
        summary = {
            "total_vendas": float(total_vendas),
            "pedidos_concluidos": pedidos_concluidos,
            "item_mais_vendido": item_mais_vendido,
            "vendas_por_dia": vendas_por_dia
        }

        return jsonify({"status": "success", "data": summary})
