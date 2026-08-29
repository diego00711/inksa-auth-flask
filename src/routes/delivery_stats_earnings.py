# inksa-auth-flask/src/routes/delivery_stats_earnings.py - VERSÃO OTIMIZADA

from flask import Blueprint, request, jsonify
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import psycopg2.extras
import traceback
import logging

_TZ_SP = ZoneInfo('America/Sao_Paulo')

from ..utils.helpers import get_db_connection
from ..utils.decorators import delivery_token_required

delivery_stats_earnings_bp = Blueprint('delivery_stats_earnings_bp', __name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@delivery_stats_earnings_bp.route('/dashboard-stats', methods=['GET'])
@delivery_token_required 
def get_dashboard_stats(): 
    conn = None
    try: 
        user_id = request.user_id
        logger.info(f"📊 Buscando stats para user_id: {user_id}")
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # HOJE no fuso de São Paulo — date.today() no Render (UTC) já virava o
            # dia seguinte às 21h de SP, jogando a semana/gráfico pro dia errado.
            today = datetime.now(_TZ_SP).date()
            
            # Buscar perfil do entregador
            cur.execute("""
                SELECT id, is_available, daily_goal, rating, total_deliveries,
                       online_minutes_today, distance_today,
                       COALESCE(cash_debt, 0) AS cash_debt,
                       COALESCE(total_cash_received, 0) AS total_cash_received,
                       COALESCE(current_lat, latitude) AS lat,
                       COALESCE(current_lng, longitude) AS lng
                FROM delivery_profiles
                WHERE user_id = %s
            """, (user_id,))
            delivery_profile = cur.fetchone()
            
            if not delivery_profile:
                logger.error(f"❌ Perfil não encontrado para user_id: {user_id}")
                return jsonify({"status": "error", "message": "Perfil de entregador não encontrado."}), 404
                
            profile_id = delivery_profile['id']
            logger.info(f"✅ Profile ID encontrado: {profile_id}")

            response_data = {
                "todayDeliveries": 0,
                "todayEarnings": 0.0,
                "avgRating": 0.0,      # ao vivo abaixo (delivery_reviews)
                "totalDeliveries": 0,  # ao vivo abaixo (orders entregues)
                "available": 0,
                "activeOrders": [],
                "weeklyEarnings": [],
                "dailyGoal": float(delivery_profile.get('daily_goal') or 300.0),
                "onlineMinutes": delivery_profile.get('online_minutes_today') or 0,
                "ranking": 0,
                "totalDeliverers": 0,
                "distanceToday": float(delivery_profile.get('distance_today') or 0.0),
                "nextPayment": {"date": "--/--", "amount": 0.0},
                "streak": 0,
                "peakHours": {"start": "11:30", "end": "13:30", "bonus": 1.5},
                "is_available": delivery_profile.get('is_available', False),
                "cashDebt": float(delivery_profile.get('cash_debt') or 0.0),
                "totalCashReceived": float(delivery_profile.get('total_cash_received') or 0.0),
            }

            # ✅ GANHOS E ENTREGAS DE HOJE (fuso de São Paulo — com DATE(created_at)
            # em UTC uma entrega das 22h caía no dia seguinte e "hoje" mentia)
            logger.info(f"🔍 Buscando entregas de hoje para profile_id: {profile_id}")
            cur.execute("""
                SELECT
                    COALESCE(COUNT(id), 0) as count,
                    COALESCE(SUM(COALESCE(valor_repassado_entregador, delivery_fee)), 0) as total
                FROM orders
                WHERE delivery_id = %s
                AND status IN ('delivered', 'delivery_failed')
                AND (created_at AT TIME ZONE 'America/Sao_Paulo')::date
                    = (now() AT TIME ZONE 'America/Sao_Paulo')::date
            """, (profile_id,))

            today_stats = cur.fetchone()
            if today_stats:
                response_data["todayDeliveries"] = today_stats['count']
                response_data["todayEarnings"] = float(today_stats['total'])
                logger.info(f"💰 Ganhos hoje: R$ {response_data['todayEarnings']:.2f}")
                logger.info(f"📦 Entregas hoje: {response_data['todayDeliveries']}")

            # ✅ TOTAL DE ENTREGAS (desde o início) + AVALIAÇÃO MÉDIA — AO VIVO.
            # delivery_profiles.total_deliveries e .rating existem mas NUNCA são
            # escritas por lugar nenhum do backend (contadores mortos): ficavam
            # 0 pra sempre mesmo com o entregador já tendo entregas/avaliações.
            # Contamos direto da fonte, igual a tela de Ganhos já faz.
            cur.execute("""
                SELECT COUNT(*) AS total
                FROM orders
                WHERE delivery_id = %s
                AND status IN ('delivered', 'delivery_failed')
            """, (profile_id,))
            _tot = cur.fetchone()
            response_data["totalDeliveries"] = (_tot and _tot['total']) or 0

            cur.execute("""
                SELECT COALESCE(AVG(rating), 0) AS avg_rating
                FROM delivery_reviews
                WHERE delivery_id = %s
            """, (profile_id,))
            _rt = cur.fetchone()
            response_data["avgRating"] = float((_rt and _rt['avg_rating']) or 0.0)

            # ✅ PEDIDOS DISPONÍVEIS (sem entregador) — no MESMO raio da lista de
            # disponíveis, pra o contador do dashboard bater com o que o
            # entregador realmente vê (senão diria "3 disponíveis" com pedidos de
            # outra cidade e a lista viria vazia).
            _drv_lat = delivery_profile.get('lat')
            _drv_lng = delivery_profile.get('lng')
            if _drv_lat is not None and _drv_lng is not None:
                from ..utils.platform_settings import get_settings
                _radius_m = float(get_settings()["platform_max_delivery_radius"]) * 1000.0
                cur.execute("""
                    SELECT COUNT(*) as available_count
                    FROM orders o
                    LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                    WHERE (o.status = 'ready' OR o.status = 'accepted_by_delivery')
                      AND o.delivery_id IS NULL
                      AND (rp.latitude IS NULL OR rp.longitude IS NULL OR
                           earth_distance(ll_to_earth(rp.latitude, rp.longitude),
                                          ll_to_earth(%s, %s)) <= %s)
                """, (float(_drv_lat), float(_drv_lng), _radius_m))
            else:
                cur.execute("""
                    SELECT COUNT(*) as available_count
                    FROM orders
                    WHERE (status = 'ready' OR status = 'accepted_by_delivery')
                      AND delivery_id IS NULL
                """)
            available_result = cur.fetchone()
            if available_result:
                response_data["available"] = available_result['available_count']
                logger.info(f"🎯 Pedidos disponíveis: {response_data['available']}")

            # ✅ GANHOS SEMANAIS
            day_labels = ["Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb"]
            # Calcular início da semana (domingo)
            days_since_sunday = (today.weekday() + 1) % 7
            start_of_week = today - timedelta(days=days_since_sunday)
            
            logger.info(f"📅 Buscando ganhos desde: {start_of_week}")
            
            cur.execute("""
                SELECT
                    (created_at AT TIME ZONE 'America/Sao_Paulo')::date as day,
                    SUM(COALESCE(valor_repassado_entregador, delivery_fee)) as value
                FROM orders
                WHERE delivery_id = %s
                AND status IN ('delivered', 'delivery_failed')
                AND (created_at AT TIME ZONE 'America/Sao_Paulo')::date >= %s
                GROUP BY 1
                ORDER BY 1;
            """, (profile_id, start_of_week))
            
            earnings_by_day = {row['day']: float(row['value']) for row in cur.fetchall()}
            
            for i in range(7):
                current_day = start_of_week + timedelta(days=i)
                day_name = day_labels[current_day.weekday()]
                response_data["weeklyEarnings"].append({
                    "day": day_name,
                    "value": earnings_by_day.get(current_day, 0.0)
                })
            
            logger.info(f"📊 Dias com ganhos: {len(earnings_by_day)}")

            # ✅ PRÓXIMO PAGAMENTO
            cur.execute("""
                SELECT payment_date, amount 
                FROM payouts
                WHERE delivery_id = %s 
                AND status = 'pending' 
                ORDER BY payment_date ASC 
                LIMIT 1;
            """, (profile_id,))
            next_payment_data = cur.fetchone()
            if next_payment_data:
                response_data["nextPayment"] = {
                    "date": next_payment_data['payment_date'].strftime('%d/%m'),
                    "amount": float(next_payment_data['amount'])
                }

            # ✅ TOTAL DE ENTREGADORES
            cur.execute("SELECT COUNT(id) as total FROM delivery_profiles WHERE is_active = TRUE;")
            total_deliverers_data = cur.fetchone()
            if total_deliverers_data:
                response_data["totalDeliverers"] = total_deliverers_data['total']

            # ✅ PEDIDOS ATIVOS DO ENTREGADOR
            logger.info(f"🚚 Buscando pedidos ativos para profile_id: {profile_id}")
            cur.execute("""
                SELECT
                    o.id, o.numero, o.status, o.total_amount, o.delivery_fee, o.created_at,
                    o.valor_repassado_entregador,
                    o.delivery_address, o.pickup_code,
                    o.payment_method, o.change_for,
                    o.client_latitude, o.client_longitude,
                    CONCAT(cp.first_name, ' ', cp.last_name) as client_name,
                    rp.restaurant_name,
                    rp.address_street, rp.address_number,
                    rp.address_neighborhood, rp.address_city,
                    rp.latitude AS restaurant_latitude, rp.longitude AS restaurant_longitude
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.delivery_id = %s 
                AND o.status IN ('accepted_by_delivery', 'delivering')
                ORDER BY o.created_at ASC
            """, (profile_id,))
            
            active_orders = []
            for order in cur.fetchall():
                active_orders.append({
                    'id': str(order['id']),
                    'numero': order.get('numero'),
                    'status': order['status'],
                    'total_amount': float(order.get('total_amount') or 0.0),
                    'delivery_fee': float(order.get('delivery_fee') or 0.0),
                    # líquido do entregador (frete menos a taxa da plataforma) —
                    # o app mostra ISTO pro entregador, não o frete bruto
                    'valor_repassado_entregador': float(order.get('valor_repassado_entregador') or 0.0),
                    'created_at': order['created_at'].isoformat() if order.get('created_at') else None,
                    'delivery_address': order.get('delivery_address'),
                    'client_name': order.get('client_name'),
                    'client_latitude': float(order['client_latitude']) if order.get('client_latitude') is not None else None,
                    'client_longitude': float(order['client_longitude']) if order.get('client_longitude') is not None else None,
                    'restaurant_name': order.get('restaurant_name'),
                    'restaurant_street': order.get('address_street'),
                    'restaurant_number': order.get('address_number'),
                    'restaurant_neighborhood': order.get('address_neighborhood'),
                    'restaurant_city': order.get('address_city'),
                    'restaurant_latitude': float(order['restaurant_latitude']) if order.get('restaurant_latitude') is not None else None,
                    'restaurant_longitude': float(order['restaurant_longitude']) if order.get('restaurant_longitude') is not None else None,
                    'pickup_code': order.get('pickup_code'),
                    'payment_method': order.get('payment_method'),
                    'change_for': float(order.get('change_for') or 0.0),
                })
            
            response_data["activeOrders"] = active_orders
            logger.info(f"📋 Pedidos ativos encontrados: {len(active_orders)}")

            logger.info(f"✅ Stats completos retornados com sucesso!")
            return jsonify({"status": "success", "data": response_data}), 200
            
    except psycopg2.Error as e:
        logger.error(f"❌ Erro de banco de dados: {e}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    except Exception as e:
        logger.error(f"❌ Erro interno: {e}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno do servidor", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@delivery_stats_earnings_bp.route('/earnings-history', methods=['GET'])
@delivery_token_required
def get_earnings_history():
    conn = None
    try:
        user_id = request.user_id
        logger.info(f"💰 Buscando histórico de ganhos para user_id: {user_id}")
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_id,))
            delivery_profile = cur.fetchone()
            if not delivery_profile:
                return jsonify({"status": "error", "message": "Perfil de entregador não encontrado."}), 404
            profile_id = delivery_profile['id']

            start_date_str = request.args.get('start_date')
            end_date_str = request.args.get('end_date')

            end_date = date.today()
            start_date = end_date - timedelta(days=6)

            try:
                if start_date_str: start_date = date.fromisoformat(start_date_str)
                if end_date_str: end_date = date.fromisoformat(end_date_str)
            except ValueError:
                return jsonify({"status": "error", "message": "Formato de data inválido. Use YYYY-MM-DD."}), 400
            
            if start_date > end_date:
                return jsonify({"status": "error", "message": "A data de início não pode ser posterior à data de fim."}), 400
            
            logger.info(f"📅 Período: {start_date} até {end_date}")
            
            # Ganhos diários
            cur.execute("""
                SELECT
                    DATE(o.created_at) AS earning_date,
                    COALESCE(SUM(COALESCE(o.valor_repassado_entregador, o.delivery_fee)), 0) AS total_earned_daily,
                    COUNT(o.id) AS total_deliveries_daily
                FROM orders o
                WHERE o.delivery_id = %s
                AND o.status IN ('delivered', 'delivery_failed')
                AND o.created_at BETWEEN %s AND %s + INTERVAL '1 day' - INTERVAL '1 second'
                GROUP BY DATE(o.created_at)
                ORDER BY earning_date ASC;
            """, (profile_id, start_date, end_date))
            
            daily_earnings_data = cur.fetchall()
            full_period_earnings = {}
            current_day = start_date
            
            while current_day <= end_date:
                full_period_earnings[current_day.isoformat()] = {
                    "total_earned_daily": 0.0, 
                    "total_deliveries_daily": 0
                }
                current_day += timedelta(days=1)
            
            for row in daily_earnings_data:
                full_period_earnings[row['earning_date'].isoformat()] = {
                    "total_earned_daily": float(row['total_earned_daily']),
                    "total_deliveries_daily": row['total_deliveries_daily']
                }
            
            ordered_daily_earnings = [
                {"earning_date": date_str, **data} 
                for date_str, data in sorted(full_period_earnings.items())
            ]

            # Entregas detalhadas
            cur.execute("""
                SELECT
                    o.id, o.numero, o.status, o.total_amount,
                    COALESCE(o.valor_repassado_entregador, o.delivery_fee) AS delivery_fee,
                    o.created_at,
                    o.delivery_address,
                    CONCAT(cp.first_name, ' ', cp.last_name) as client_name,
                    rp.restaurant_name
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.delivery_id = %s
                AND o.status IN ('delivered', 'delivery_failed')
                AND o.created_at BETWEEN %s AND %s + INTERVAL '1 day' - INTERVAL '1 second'
                ORDER BY o.created_at DESC;
            """, (profile_id, start_date, end_date))
            
            detailed_deliveries = []
            for delivery in cur.fetchall():
                detailed_deliveries.append({
                    'id': str(delivery['id']),
                    'status': delivery['status'],
                    'total_amount': float(delivery.get('total_amount') or 0.0),
                    'delivery_fee': float(delivery.get('delivery_fee') or 0.0),
                    'created_at': delivery['created_at'].isoformat() if delivery.get('created_at') else None,
                    'delivery_address': delivery.get('delivery_address'),
                    'client_name': delivery.get('client_name'),
                    'restaurant_name': delivery.get('restaurant_name')
                })

            total_earnings_period = sum(d['total_earned_daily'] for d in ordered_daily_earnings)
            total_deliveries_period = sum(d['total_deliveries_daily'] for d in ordered_daily_earnings)
            
            logger.info(f"✅ Total período: R$ {total_earnings_period:.2f} em {total_deliveries_period} entregas")
            
            response_data = {
                "periodStartDate": start_date.isoformat(),
                "periodEndDate": end_date.isoformat(),
                "totalEarningsPeriod": float(total_earnings_period),
                "totalDeliveriesPeriod": total_deliveries_period,
                "dailyEarnings": ordered_daily_earnings,
                "detailedDeliveries": detailed_deliveries
            }
            
            return jsonify({"status": "success", "data": response_data}), 200
            
    except psycopg2.Error as e:
        logger.error(f"❌ Erro de banco de dados: {e}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    except Exception as e:
        logger.error(f"❌ Erro interno: {e}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno do servidor", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()
