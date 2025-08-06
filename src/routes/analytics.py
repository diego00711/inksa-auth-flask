# src/routes/analytics.py
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from collections import Counter
from ..utils.helpers import get_db_connection, get_user_id_from_token

analytics_bp = Blueprint('analytics', __name__)

@analytics_bp.route('/', methods=['GET'])
def get_analytics():
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    
    if user_type != 'restaurant':
        return jsonify({"error": "Acesso não autorizado"}), 403
    
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
    
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT o.total_amount, o.items, o.status 
                FROM orders o
                WHERE o.restaurant_id = %s
            """, (user_id,))
            
            orders = cur.fetchall()
            
            # Cálculos de analytics
            total_sales = sum(float(order['total_amount'] or 0) for order in orders if order['status'] == 'Concluído')
            
            # Itens mais vendidos
            all_items = []
            for order in orders:
                if order['items'] and isinstance(order['items'], list):
                    for item in order['items']:
                        if 'name' in item:
                            all_items.append(item['name'])
            
            item_counts = Counter(all_items)
            top_items = [{"item": item, "count": count} for item, count in item_counts.most_common(5)]
            
            return jsonify({
                "status": "success",
                "data": {
                    "total_sales": total_sales,
                    "total_orders": len(orders),
                    "top_items": top_items
                }
            })
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn:
            conn.close()