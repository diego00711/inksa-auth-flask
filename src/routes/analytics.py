# src/routes/analytics.py - VERSÃO CORRIGIDA

import logging
from flask import Blueprint, jsonify, request
import psycopg2.extras
from collections import Counter
from datetime import datetime, timedelta  # ✅ Import no lugar correto!
from functools import wraps

from ..utils.helpers import get_db_connection, get_user_id_from_token

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
    if user_type != 'restaurant': 
        return jsonify({"status": "error", "error": "Unauthorized"}), 403

    # ✅ NOVO: Ler parâmetro 'days' da query string
    days_param = request.args.get('days', '7')
    logging.info(f"📊 Buscando analytics para: {days_param} dias")
    
    # Converter para inteiro, se não for 'all'
    if days_param == 'all':
        days_filter = None  # Sem filtro de data
    else:
        try:
            days_filter = int(days_param)
        except ValueError:
            days_filter = 7  # Default

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # 1. Busca o ID do perfil do restaurante
        cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_id,))
        restaurant_profile = cur.fetchone()
        
        if not restaurant_profile:
            return jsonify({"status": "error", "error": "Restaurant profile not found"}), 404
        
        restaurant_id = restaurant_profile['id']
        logging.info(f"🏪 Restaurant ID: {restaurant_id}")

        # ✅ CORRIGIDO: Status 'delivered' e filtro de data dinâmico
        if days_filter:
            # Busca pedidos dos últimos X dias
            date_limit = datetime.now() - timedelta(days=days_filter)
            logging.info(f"📅 Filtrando desde: {date_limit}")
            
            cur.execute("""
                SELECT total_amount, items, created_at, client_id, estimated_prep_time
                FROM orders
                WHERE restaurant_id = %s 
                AND status = 'delivered'
                AND created_at >= %s
                ORDER BY created_at DESC
            """, (restaurant_id, date_limit))
        else:
            # Busca TODOS os pedidos (sem filtro de data)
            logging.info(f"📅 Buscando TODOS os pedidos")
            
            cur.execute("""
                SELECT total_amount, items, created_at, client_id, estimated_prep_time
                FROM orders
                WHERE restaurant_id = %s 
                AND status = 'delivered'
                ORDER BY created_at DESC
            """, (restaurant_id,))
        
        orders = cur.fetchall()
        logging.info(f"📦 Pedidos encontrados: {len(orders)}")

        # --- Cálculos em Python ---

        # 3. Total de Vendas e Pedidos
        total_vendas = sum(float(order['total_amount'] or 0) for order in orders)
        pedidos_concluidos = len(orders)
        
        logging.info(f"💰 Total vendas: R$ {total_vendas:.2f}")
        logging.info(f"📊 Pedidos concluídos: {pedidos_concluidos}")

        # 4. Item Mais Vendido
        all_item_names = []
        if orders:
            for order in orders:
                if order['items'] and isinstance(order['items'], list):
                    for item in order['items']:
                        # Itens sao gravados como {title, unit_price, quantity}.
                        # Ler item['name'] deixava item_mais_vendido sempre 'N/A'.
                        if isinstance(item, dict):
                            nome_item = item.get('title') or item.get('name')
                            if nome_item and str(nome_item).strip().lower() not in ('taxa de entrega', 'frete'):
                                all_item_names.extend([nome_item] * int(item.get('quantity', 1) or 1))
        
        if all_item_names:
            item_counts = Counter(all_item_names)
            item_mais_vendido = item_counts.most_common(1)[0][0]
        else:
            item_mais_vendido = 'N/A'
        
        logging.info(f"🍕 Item mais vendido: {item_mais_vendido}")

        # 5. Vendas por Dia
        sales_by_day = {}
        if orders:
            for order in orders:
                order_date = order['created_at'].date()
                day_str = order_date.strftime('%Y-%m-%d')
                sales_by_day[day_str] = sales_by_day.get(day_str, 0) + float(order['total_amount'])
        
        vendas_por_dia = [
            {"dia": day, "total": total} 
            for day, total in sorted(sales_by_day.items(), reverse=True)
        ]
        
        logging.info(f"📈 Dias com vendas: {len(vendas_por_dia)}")

        # 6. Métricas extras (o front lê analyticsData.metricas_extras — sem isto
        #    avaliação/clientes/preparo/cancelados ficavam sempre N/A/0).
        clientes_unicos = len({o['client_id'] for o in orders if o.get('client_id')})
        prep_times = [float(o['estimated_prep_time']) for o in orders if o.get('estimated_prep_time') is not None]
        tempo_medio_preparo = round(sum(prep_times) / len(prep_times)) if prep_times else None

        cur.execute(
            "SELECT COALESCE(AVG(rating), 0)::float AS media, COUNT(*) AS total "
            "FROM restaurant_reviews WHERE restaurant_id = %s",
            (restaurant_id,),
        )
        rev = cur.fetchone()
        avaliacao_media = round(float(rev['media']), 1) if rev and rev['total'] else None

        if days_filter:
            cur.execute(
                "SELECT COUNT(*) AS c FROM orders WHERE restaurant_id = %s "
                "AND status IN ('cancelled','canceled') AND created_at >= %s",
                (restaurant_id, date_limit),
            )
        else:
            cur.execute(
                "SELECT COUNT(*) AS c FROM orders WHERE restaurant_id = %s "
                "AND status IN ('cancelled','canceled')",
                (restaurant_id,),
            )
        pedidos_cancelados = int(cur.fetchone()['c'] or 0)

        metricas_extras = {
            "avaliacao_media": avaliacao_media,
            "tempo_medio_preparo": tempo_medio_preparo,
            "clientes_unicos": clientes_unicos,
            "pedidos_cancelados": pedidos_cancelados,
        }

        # Monta resposta final
        summary = {
            "total_vendas": total_vendas,
            "pedidos_concluidos": pedidos_concluidos,
            "item_mais_vendido": item_mais_vendido,
            "vendas_por_dia": vendas_por_dia,
            "metricas_extras": metricas_extras,
            "periodo_dias": days_param  # ✅ Retorna o período filtrado
        }

        return jsonify({"status": "success", "data": summary})
