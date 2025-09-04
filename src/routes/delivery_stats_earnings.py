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
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    
    try: 
        # Use request.user_id em vez de g.profile_id
        user_id = request.user_id

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            today = date.today()
            
            # Primeiro, buscar o ID do perfil do entregador
            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_id,))
            delivery_profile = cur.fetchone()
            
            if not delivery_profile:
                return jsonify({"status": "error", "message": "Perfil de entregador não encontrado."}), 404
                
            profile_id = delivery_profile['id']

            cur.execute("""
                WITH today_stats AS (
                    SELECT 
                        COALESCE(SUM(delivery_fee), 0) AS earnings,
                        COUNT(id) AS deliveries
                    FROM orders 
                    WHERE delivery_id = %s
                    AND status = 'delivered'  -- ✅ CORRIGIDO: status em inglês
                    AND DATE(created_at) = %s
                ),
                active_orders_data AS (
                    SELECT 
                        o.id, o.status, 
                        COALESCE(rp.address_street || ', ' || rp.address_number, 'Endereço não disponível') AS pickup_address,
                        o.delivery_address, o.total_amount, o.delivery_fee, o.created_at,
                        COALESCE(cp.first_name || ' ' || cp.last_name, 'Cliente') AS client_name, 
                        COALESCE(cp.phone, 'Telefone não disponível') AS client_phone,
                        COALESCE(rp.restaurant_name, 'Restaurante') AS restaurant_name, 
                        COALESCE(rp.phone, 'Telefone não disponível') AS restaurant_phone
                    FROM orders o
                    LEFT JOIN client_profiles cp ON o.client_id = cp.id
                    LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                    WHERE o.delivery_id = %s
                    AND o.status IN ('pending', 'accepted', 'delivering')  -- ✅ CORRIGIDO: status em inglês
                    ORDER BY o.created_at ASC
                )
                SELECT 
                    dp.rating, dp.total_deliveries, ts.earnings AS today_earnings,
                    ts.deliveries AS today_deliveries,
                    (SELECT COALESCE(json_agg(a), '[]'::json) FROM active_orders_data a) AS active_orders
                FROM delivery_profiles dp
                CROSS JOIN today_stats ts
                WHERE dp.id = %s
            """, (profile_id, today, profile_id, profile_id))
            
            stats = cur.fetchone()
            
            if not stats:
                return jsonify({"status": "error", "message": "Estatísticas não encontradas."}), 404
            
            # Processar active_orders corretamente
            active_orders = stats['active_orders'] or []
            if isinstance(active_orders, str):
                import json
                active_orders = json.loads(active_orders)
            
            return jsonify({
                "status": "success",
                "data": {
                    "todayDeliveries": stats['today_deliveries'] or 0,
                    "todayEarnings": float(stats['today_earnings']) if stats['today_earnings'] is not None else 0.0,
                    "avgRating": float(stats['rating']) if stats['rating'] is not None else 0.0,
                    "totalDeliveries": stats['total_deliveries'] or 0, 
                    "activeOrders": active_orders  # ✅ CORRIGIDO: não usar serialize_delivery_data aqui
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
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    
    try:
        user_id = request.user_id
        
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
            start_date = end_date - timedelta(days=6)

            try:
                if start_date_str:
                    start_date = date.fromisoformat(start_date_str)
                if end_date_str:
                    end_date = date.fromisoformat(end_date_str)
            except ValueError:
                return jsonify({"status": "error", "message": "Formato de data inválido. Use YYYY-MM-DD."}), 400
            
            if start_date > end_date:
                return jsonify({"status": "error", "message": "A data de início não pode ser posterior à data de fim."}), 400
            
            cur.execute("""
                SELECT 
                    DATE(o.created_at) AS earning_date, 
                    COALESCE(SUM(o.delivery_fee), 0) AS total_earned_daily,
                    COUNT(o.id) AS total_deliveries_daily
                FROM orders o
                WHERE o.delivery_id = %s
                  AND o.status = 'delivered'  -- ✅ CORRIGIDO: status em inglês
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
                {
                    "earning_date": date_str,
                    "total_earned_daily": data["total_earned_daily"],
                    "total_deliveries_daily": data["total_deliveries_daily"]
                }
                for date_str, data in sorted(full_period_earnings.items())
            ]
            
            cur.execute("""
                SELECT 
                    o.id, o.status, 
                    COALESCE(rp.address_street || ', ' || rp.address_number, 'Endereço não disponível') AS pickup_address,
                    o.delivery_address, o.total_amount, o.delivery_fee, o.created_at,
                    COALESCE(cp.first_name || ' ' || cp.last_name, 'Cliente') AS client_name, 
                    COALESCE(cp.phone, 'Telefone não disponível') AS client_phone,
                    COALESCE(rp.restaurant_name, 'Restaurante') AS restaurant_name, 
                    COALESCE(rp.phone, 'Telefone não disponível') AS restaurant_phone
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.delivery_id = %s
                  AND o.status = 'delivered'  -- ✅ CORRIGIDO: status em inglês
                  AND o.created_at BETWEEN %s AND %s + INTERVAL '1 day' - INTERVAL '1 second'
                ORDER BY o.created_at DESC;
            """, (profile_id, start_date, end_date))
            detailed_deliveries = cur.fetchall()

            total_earnings_period = sum(d['total_earned_daily'] for d in ordered_daily_earnings)
            total_deliveries_period = sum(d['total_deliveries_daily'] for d in ordered_daily_earnings)
            
            response_data = {
                "periodStartDate": start_date.isoformat(),
                "periodEndDate": end_date.isoformat(),
                "totalEarningsPeriod": float(total_earnings_period),
                "totalDeliveriesPeriod": total_deliveries_period,
                "dailyEarnings": ordered_daily_earnings,  # ✅ CORRIGIDO: não usar serialize_delivery_data
                "detailedDeliveries": [dict(d) for d in detailed_deliveries]  # ✅ CORRIGIDO: não usar serialize_delivery_data
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
