# src/routes/analytics.py - VERSÃO FINAL E DEFINITIVA

import logging
from flask import Blueprint, jsonify, request
import psycopg2.extras
from collections import Counter # Usaremos o Counter para facilitar a contagem

from ..utils.helpers import get_db_connection, get_user_id_from_token
from functools import wraps

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
        # 1. Busca o ID do perfil do restaurante a partir do user_id (essencial e correto)
        cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_id,))
        restaurant_profile = cur.fetchone()
        if not restaurant_profile:
            return jsonify({"status": "error", "error": "Restaurant profile not found"}), 404
        restaurant_id = restaurant_profile['id']

        # 2. Busca todos os pedidos concluídos para o restaurante
        cur.execute(
            "SELECT total_amount, items, created_at FROM orders WHERE restaurant_id = %s AND status = 'concluido'",
            (restaurant_id,)
        )
        orders = cur.fetchall()

        # --- Início dos Cálculos em Python ---

        # 3. Total de Vendas e Pedidos
        total_vendas = sum(float(order['total_amount'] or 0) for order in orders)
        pedidos_concluidos = len(orders)

        # 4. Item Mais Vendido (processando o JSON)
        all_item_names = []
        if orders:
            for order in orders:
                # Garante que 'items' é uma lista e não é nula
                if order['items'] and isinstance(order['items'], list):
                    for item in order['items']:
                        # Garante que o item é um dicionário e tem nome e quantidade
                        if isinstance(item, dict) and 'name' in item and 'quantity' in item:
                            # Adiciona o nome do item repetido pela sua quantidade
                            all_item_names.extend([item['name']] * item.get('quantity', 1))
        
        if all_item_names:
            item_counts = Counter(all_item_names)
            item_mais_vendido = item_counts.most_common(1)[0][0]
        else:
            item_mais_vendido = 'N/A'

        # 5. Vendas por Dia (processando em Python)
        sales_by_day = {}
        seven_days_ago = datetime.now().date() - timedelta(days=7)
        if orders:
            for order in orders:
                order_date = order['created_at'].date()
                if order_date > seven_days_ago:
                    day_str = order_date.strftime('%Y-%m-%d')
                    sales_by_day[day_str] = sales_by_day.get(day_str, 0) + float(order['total_amount'])
        
        vendas_por_dia = [{"dia": day, "total": total} for day, total in sorted(sales_by_day.items(), reverse=True)]

        # Monta o objeto de resposta final
        summary = {
            "total_vendas": total_vendas,
            "pedidos_concluidos": pedidos_concluidos,
            "item_mais_vendido": item_mais_vendido,
            "vendas_por_dia": vendas_por_dia
        }

        return jsonify({"status": "success", "data": summary})

# Adicionado para o cálculo de datas
from datetime import datetime, timedelta
