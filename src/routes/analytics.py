# src/routes/analytics.py - VERSÃO CORRIGIDA

import logging
from flask import Blueprint, jsonify, request
import psycopg2.extras
from collections import Counter
from datetime import datetime, timedelta  # ✅ Import no lugar correto!
from functools import wraps

from ..utils.helpers import get_db_connection, get_user_id_from_token
from ..utils.pedido_itens import eh_linha_de_frete

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
                            # Mesma regra do resto do sistema (utils/pedido_itens): exige tambem
                            # ausencia de menu_item_id, senao um produto REAL chamado 'frete'
                            # sumia do relatorio de vendas.
                            if nome_item and not eh_linha_de_frete(item):
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

        # 7. Insights acionáveis — o que alimenta a parte de baixo da tela.
        #    Tudo sai dos pedidos JÁ carregados acima: nenhuma query nova.
        ticket_medio = round(total_vendas / pedidos_concluidos, 2) if pedidos_concluidos else 0.0

        _total_periodo = pedidos_concluidos + pedidos_cancelados
        taxa_cancelamento = (
            round(pedidos_cancelados * 100.0 / _total_periodo, 1) if _total_periodo else 0.0
        )

        # Top 5 (o card de cima mostra só o 1º; o parceiro precisa da lista pra
        # decidir compra de insumo e o que empurrar no cardápio).
        top_itens = [
            {"nome": nome, "quantidade": qtd}
            for nome, qtd in Counter(all_item_names).most_common(5)
        ]

        # Faturamento por hora e por dia da semana: diz quando reforçar a equipe
        # e quando vale promover.
        por_hora, por_dow = {}, {}
        for order in orders:
            dt = order['created_at']
            valor = float(order['total_amount'] or 0)
            por_hora[dt.hour] = por_hora.get(dt.hour, 0.0) + valor
            por_dow[dt.weekday()] = por_dow.get(dt.weekday(), 0.0) + valor

        vendas_por_hora = [
            {"hora": h, "total": round(por_hora.get(h, 0.0), 2)} for h in range(24)
        ]
        _DIAS = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo']
        vendas_por_dia_semana = [
            {"dia": _DIAS[d], "total": round(por_dow.get(d, 0.0), 2)} for d in range(7)
        ]

        # Recorrência: clientes com 2+ pedidos no período. Num delivery é a
        # métrica que mais importa — cliente que volta custa zero pra adquirir.
        _por_cliente = Counter(o['client_id'] for o in orders if o.get('client_id'))
        clientes_recorrentes = sum(1 for n in _por_cliente.values() if n >= 2)
        taxa_recorrencia = (
            round(clientes_recorrentes * 100.0 / len(_por_cliente), 1) if _por_cliente else 0.0
        )

        insights = {
            "ticket_medio": ticket_medio,
            "taxa_cancelamento": taxa_cancelamento,
            "top_itens": top_itens,
            "vendas_por_hora": vendas_por_hora,
            "vendas_por_dia_semana": vendas_por_dia_semana,
            "clientes_recorrentes": clientes_recorrentes,
            "taxa_recorrencia": taxa_recorrencia,
        }

        # Monta resposta final
        summary = {
            "total_vendas": total_vendas,
            "pedidos_concluidos": pedidos_concluidos,
            "item_mais_vendido": item_mais_vendido,
            "vendas_por_dia": vendas_por_dia,
            "metricas_extras": metricas_extras,
            "insights": insights,
            "periodo_dias": days_param  # ✅ Retorna o período filtrado
        }

        return jsonify({"status": "success", "data": summary})
