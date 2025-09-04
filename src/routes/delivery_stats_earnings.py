# inksa-auth-flask/src/routes/delivery_stats_earnings.py - VERSÃO CORRIGIDA

from flask import Blueprint, request, jsonify
from datetime import date, timedelta
import psycopg2.extras
import traceback
from flask_cors import cross_origin
from ..utils.helpers import get_db_connection, delivery_token_required

delivery_stats_earnings_bp = Blueprint('delivery_stats_earnings_bp', __name__)

@delivery_stats_earnings_bp.route('/dashboard-stats', methods=['GET'])
@cross_origin()
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
            
            # Buscar o ID do perfil do entregador
            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_id,))
            delivery_profile = cur.fetchone()
            
            if not delivery_profile:
                return jsonify({"status": "error", "message": "Perfil de entregador não encontrado."}), 404
                
            profile_id = delivery_profile['id']

            # ✅ CORREÇÃO: Query simplificada e corrigida
            cur.execute("""
                -- Estatísticas de hoje
                SELECT 
                    COALESCE(COUNT(o.id), 0) as today_deliveries,
                    COALESCE(SUM(o.delivery_fee), 0) as today_earnings
                FROM orders o
                WHERE o.delivery_id = %s
                AND o.status = 'delivered'
                AND DATE(o.created_at) = %s

                -- Estatísticas totais do perfil
                SELECT 
                    COALESCE(rating, 0) as rating,
                    COALESCE(total_deliveries, 0) as total_deliveries
                FROM delivery_profiles 
                WHERE id = %s

                -- Pedidos ativos
                SELECT 
                    o.id,
                    o.status,
                    o.total_amount,
                    o.delivery_fee,
                    o.created_at,
                    o.delivery_address,
                    CONCAT(cp.first_name, ' ', cp.last_name) as client_name,
                    cp.phone as client_phone,
                    rp.restaurant_name,
                    rp.phone as restaurant_phone,
                    CONCAT(rp.address_street, ', ', rp.address_number, ' - ', rp.address_neighborhood) as pickup_address
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.delivery_id = %s
                AND o.status IN ('accepted', 'preparing', 'ready', 'delivering')
                ORDER BY o.created_at ASC
            """, (profile_id, today, profile_id, profile_id))
            
            # Primeiro resultado: estatísticas de hoje
            today_stats = cur.fetchone()
            if not today_stats:
                today_deliveries = 0
                today_earnings = 0.0
            else:
                today_deliveries = today_stats['today_deliveries']
                today_earnings = float(today_stats['today_earnings']) if today_stats['today_earnings'] else 0.0

            # Segundo resultado: estatísticas do perfil
            cur.nextset()
            profile_stats = cur.fetchone()
            if not profile_stats:
                avg_rating = 0.0
                total_deliveries = 0
            else:
                avg_rating = float(profile_stats['rating']) if profile_stats['rating'] else 0.0
                total_deliveries = profile_stats['total_deliveries'] or 0

            # Terceiro resultado: pedidos ativos
            cur.nextset()
            active_orders = cur.fetchall()
            serialized_active_orders = []
            
            for order in active_orders:
                serialized_active_orders.append({
                    'id': order['id'],
                    'status': order['status'],
                    'total_amount': float(order['total_amount']) if order['total_amount'] else 0.0,
                    'delivery_fee': float(order['delivery_fee']) if order['delivery_fee'] else 0.0,
                    'created_at': order['created_at'].isoformat() if order['created_at'] else None,
                    'delivery_address': order['delivery_address'],
                    'client_name': order['client_name'],
                    'client_phone': order['client_phone'],
                    'restaurant_name': order['restaurant_name'],
                    'restaurant_phone': order['restaurant_phone'],
                    'pickup_address': order['pickup_address']
                })
            
            # ✅ FORMATO CORRETO esperado pelo frontend
            return jsonify({
                "status": "success",
                "data": {
                    "todayDeliveries": today_deliveries,
                    "todayEarnings": today_earnings,
                    "avgRating": avg_rating,
                    "totalDeliveries": total_deliveries,
                    "activeOrders": serialized_active_orders
                }
            }), 200
            
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
@cross_origin()
@delivery_token_required
def get_earnings_history():
    conn = None
    
    try:
        user_id = request.user_id
        
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Buscar o ID do perfil do entregador
            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_id,))
            delivery_profile = cur.fetchone()
            
            if not delivery_profile:
                return jsonify({"status": "error", "message": "Perfil de entregador não encontrado."}), 404
                
            profile_id = delivery_profile['id']

            start_date_str = request.args.get('start_date')
            end_date_str = request.args.get('end_date')

            end_date = date.today()
            start_date = end_date - timedelta(days=6)  # Últimos 7 dias por padrão

            try:
                if start_date_str:
                    start_date = date.fromisoformat(start_date_str)
                if end_date_str:
                    end_date = date.fromisoformat(end_date_str)
            except ValueError:
                return jsonify({"status": "error", "message": "Formato de data inválido. Use YYYY-MM-DD."}), 400
            
            if start_date > end_date:
                return jsonify({"status": "error", "message": "A data de início não pode ser posterior à data de fim."}), 400
            
            # Ganhos diários
            cur.execute("""
                SELECT 
                    DATE(o.created_at) AS earning_date, 
                    COALESCE(SUM(o.delivery_fee), 0) AS total_earned_daily,
                    COUNT(o.id) AS total_deliveries_daily
                FROM orders o
                WHERE o.delivery_id = %s
                  AND o.status = 'delivered'
                  AND o.created_at BETWEEN %s AND %s + INTERVAL '1 day' - INTERVAL '1 second'
                GROUP BY DATE(o.created_at)
                ORDER BY earning_date ASC;
            """, (profile_id, start_date, end_date))
            
            daily_earnings_data = cur.fetchall()

            # Preencher todos os dias do período, mesmo sem entregas
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
                {
                    "earning_date": date_str,
                    "total_earned_daily": data["total_earned_daily"],
                    "total_deliveries_daily": data["total_deliveries_daily"]
                }
                for date_str, data in sorted(full_period_earnings.items())
            ]

            # Entregas detalhadas
            cur.execute("""
                SELECT 
                    o.id,
                    o.status,
                    o.total_amount,
                    o.delivery_fee,
                    o.created_at,
                    o.delivery_address,
                    CONCAT(cp.first_name, ' ', cp.last_name) as client_name,
                    cp.phone as client_phone,
                    rp.restaurant_name,
                    rp.phone as restaurant_phone,
                    CONCAT(rp.address_street, ', ', rp.address_number, ' - ', rp.address_neighborhood) as pickup_address
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.delivery_id = %s
                  AND o.status = 'delivered'
                  AND o.created_at BETWEEN %s AND %s + INTERVAL '1 day' - INTERVAL '1 second'
                ORDER BY o.created_at DESC;
            """, (profile_id, start_date, end_date))
            
            detailed_deliveries = cur.fetchall()
            serialized_detailed_deliveries = []
            
            for delivery in detailed_deliveries:
                serialized_detailed_deliveries.append({
                    'id': delivery['id'],
                    'status': delivery['status'],
                    'total_amount': float(delivery['total_amount']) if delivery['total_amount'] else 0.0,
                    'delivery_fee': float(delivery['delivery_fee']) if delivery['delivery_fee'] else 0.0,
                    'created_at': delivery['created_at'].isoformat() if delivery['created_at'] else None,
                    'delivery_address': delivery['delivery_address'],
                    'client_name': delivery['client_name'],
                    'client_phone': delivery['client_phone'],
                    'restaurant_name': delivery['restaurant_name'],
                    'restaurant_phone': delivery['restaurant_phone'],
                    'pickup_address': delivery['pickup_address']
                })

            total_earnings_period = sum(d['total_earned_daily'] for d in ordered_daily_earnings)
            total_deliveries_period = sum(d['total_deliveries_daily'] for d in ordered_daily_earnings)
            
            # ✅ FORMATO CORRETO para o histórico
            response_data = {
                "periodStartDate": start_date.isoformat(),
                "periodEndDate": end_date.isoformat(),
                "totalEarningsPeriod": float(total_earnings_period),
                "totalDeliveriesPeriod": total_deliveries_period,
                "dailyEarnings": ordered_daily_earnings,
                "detailedDeliveries": serialized_detailed_deliveries
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
