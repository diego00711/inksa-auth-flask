# inksa-auth-flask/src/routes/delivery_stats_earnings.py - VERSÃO FINAL E ROBUSTA

from flask import Blueprint, request, jsonify
from datetime import date, timedelta
import psycopg2.extras
import traceback
 # ✅ CORREÇÃO: cross_origin não é mais necessário aqui, pois o CORS é global
from ..utils.helpers import get_db_connection, delivery_token_required

delivery_stats_earnings_bp = Blueprint('delivery_stats_earnings_bp', __name__)

@delivery_stats_earnings_bp.route('/dashboard-stats', methods=['GET'])
# ✅ CORREÇÃO: Decorador @cross_origin removido
@delivery_token_required 
def get_dashboard_stats(): 
    conn = None
    try: 
        user_id = request.user_id
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            today = date.today()
            
            # 1. Buscar perfil do entregador. Esta é a consulta mais crítica.
            cur.execute("""
                SELECT id, is_available, daily_goal, rating, total_deliveries, online_minutes_today, distance_today
                FROM delivery_profiles 
                WHERE user_id = %s
            """, (user_id,))
            delivery_profile = cur.fetchone()
            
            if not delivery_profile:
                return jsonify({"status": "error", "message": "Perfil de entregador não encontrado."}), 404
                
            profile_id = delivery_profile['id']

            # ✅ INICIALIZAÇÃO SEGURA DOS DADOS: Garante que sempre teremos valores padrão.
            response_data = {
                "todayDeliveries": 0,
                "todayEarnings": 0.0,
                "avgRating": float(delivery_profile.get('rating') or 0.0),
                "totalDeliveries": delivery_profile.get('total_deliveries') or 0,
                "activeOrders": [],
                "weeklyEarnings": [],
                "dailyGoal": float(delivery_profile.get('daily_goal') or 300.0),
                "onlineMinutes": delivery_profile.get('online_minutes_today') or 0,
                "ranking": 0, # Mockado por enquanto
                "totalDeliverers": 0,
                "distanceToday": float(delivery_profile.get('distance_today') or 0.0),
                "nextPayment": {"date": "--/--", "amount": 0.0},
                "streak": 0, # Mockado por enquanto
                "peakHours": {"start": "11:30", "end": "13:30", "bonus": 1.5}, # Mockado
                "is_available": delivery_profile.get('is_available', False)
            }

            # 2. Ganhos e Entregas de Hoje
            cur.execute("""
                SELECT COALESCE(COUNT(id), 0) as count, COALESCE(SUM(delivery_fee), 0) as total
                FROM orders WHERE delivery_id = %s AND status = 'delivered' AND DATE(created_at) = %s
            """, (profile_id, today))
            today_stats = cur.fetchone()
            if today_stats:
                response_data["todayDeliveries"] = today_stats['count']
                response_data["todayEarnings"] = float(today_stats['total'])

            # 3. Ganhos da Semana (para o gráfico)
            day_labels = ["Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb"]
            start_of_week = today - timedelta(days=today.weekday() + 1 if today.weekday() != 6 else 0)
            cur.execute("""
                SELECT DATE_TRUNC('day', created_at)::date as day, SUM(delivery_fee) as value
                FROM orders WHERE delivery_id = %s AND status = 'delivered' AND created_at >= %s
                GROUP BY 1 ORDER BY 1;
            """, (profile_id, start_of_week))
            earnings_by_day = {row['day']: float(row['value']) for row in cur.fetchall()}
            for i in range(7):
                current_day = start_of_week + timedelta(days=i)
                day_name = day_labels[current_day.weekday()]
                response_data["weeklyEarnings"].append({
                    "day": day_name,
                    "value": earnings_by_day.get(current_day, 0.0)
                })

            # 4. Próximo Pagamento
            cur.execute("""
                SELECT payment_date, amount FROM payouts
                WHERE delivery_id = %s AND status = 'pending' ORDER BY payment_date ASC LIMIT 1;
            """, (profile_id,))
            next_payment_data = cur.fetchone()
            if next_payment_data:
                response_data["nextPayment"] = {
                    "date": next_payment_data['payment_date'].strftime('%d/%m'),
                    "amount": float(next_payment_data['amount'])
                }

            # 5. Total de Entregadores
            cur.execute("SELECT COUNT(id) as total FROM delivery_profiles WHERE is_active = TRUE;")
            total_deliverers_data = cur.fetchone()
            if total_deliverers_data:
                response_data["totalDeliverers"] = total_deliverers_data['total']

            # 6. Pedidos Ativos
            cur.execute("""
                SELECT o.id, o.status, o.total_amount, o.delivery_fee, o.created_at, o.delivery_address,
                       CONCAT(cp.first_name, ' ', cp.last_name) as client_name, rp.restaurant_name,
                       rp.address_street, rp.address_number, rp.address_neighborhood
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.user_id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.user_id
                WHERE o.delivery_id = %s AND o.status IN ('accepted', 'preparing', 'ready', 'delivering')
                ORDER BY o.created_at ASC
            """, (profile_id,))
            
            active_orders = []
            for order in cur.fetchall():
                active_orders.append({
                    'id': order['id'],
                    'status': order['status'],
                    'total_amount': float(order.get('total_amount') or 0.0),
                    'delivery_fee': float(order.get('delivery_fee') or 0.0),
                    'created_at': order['created_at'].isoformat() if order.get('created_at') else None,
                    'delivery_address': order.get('delivery_address'),
                    'client_name': order.get('client_name'),
                    'restaurant_name': order.get('restaurant_name'),
                    'restaurant_street': order.get('address_street'),
                    'restaurant_number': order.get('address_number'),
                    'restaurant_neighborhood': order.get('address_neighborhood')
                })
            response_data["activeOrders"] = active_orders

            # ✅ RETORNO SEGURO: Envia a estrutura de dados completa e preenchida.
            return jsonify({"status": "success", "data": response_data}), 200
            
    except psycopg2.Error as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno do servidor", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@delivery_stats_earnings_bp.route('/earnings-history', methods=['GET'])
# ✅ CORREÇÃO: Decorador @cross_origin removido
@delivery_token_required
def get_earnings_history():
    conn = None
    try:
        user_id = request.user_id
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
            
            cur.execute("""
                SELECT DATE(o.created_at) AS earning_date, 
                       COALESCE(SUM(o.delivery_fee), 0) AS total_earned_daily,
                       COUNT(o.id) AS total_deliveries_daily
                FROM orders o
                WHERE o.delivery_id = %s AND o.status = 'delivered'
                  AND o.created_at BETWEEN %s AND %s + INTERVAL '1 day' - INTERVAL '1 second'
                GROUP BY DATE(o.created_at) ORDER BY earning_date ASC;
            """, (profile_id, start_date, end_date))
            
            daily_earnings_data = cur.fetchall()
            full_period_earnings = {}
            current_day = start_date
            while current_day <= end_date:
                full_period_earnings[current_day.isoformat()] = {"total_earned_daily": 0.0, "total_deliveries_daily": 0}
                current_day += timedelta(days=1)
            
            for row in daily_earnings_data:
                full_period_earnings[row['earning_date'].isoformat()] = {
                    "total_earned_daily": float(row['total_earned_daily']),
                    "total_deliveries_daily": row['total_deliveries_daily']
                }
            
            ordered_daily_earnings = [{"earning_date": date_str, **data} for date_str, data in sorted(full_period_earnings.items())]

            cur.execute("""
                SELECT o.id, o.status, o.total_amount, o.delivery_fee, o.created_at, o.delivery_address,
                       CONCAT(cp.first_name, ' ', cp.last_name) as client_name, rp.restaurant_name
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.user_id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.user_id
                WHERE o.delivery_id = %s AND o.status = 'delivered'
                  AND o.created_at BETWEEN %s AND %s + INTERVAL '1 day' - INTERVAL '1 second'
                ORDER BY o.created_at DESC;
            """, (profile_id, start_date, end_date))
            
            detailed_deliveries = []
            for delivery in cur.fetchall():
                detailed_deliveries.append({
                    'id': delivery['id'],
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
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro de banco de dados", "detail": str(e)}), 500
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Erro interno do servidor", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()
