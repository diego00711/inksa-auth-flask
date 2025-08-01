# src/routes/restaurant.py

import os
import traceback
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from collections import Counter
# ✅ CORREÇÃO: Importa as funções centralizadas do helpers.py
from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase

restaurant_bp = Blueprint('restaurant_bp', __name__)

@restaurant_bp.route('/restaurants', methods=['GET'])
def get_all_restaurants_public():
    user_lat = request.args.get('user_lat', type=float)
    user_lon = request.args.get('user_lon', type=float)
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if user_lat is not None and user_lon is not None:
                sql_query = """
                    SELECT 
                        id, restaurant_name, logo_url, category, rating, delivery_time, 
                        delivery_fee, minimum_order, is_open, delivery_type,
                        ROUND((earth_distance(ll_to_earth(latitude, longitude), ll_to_earth(%s, %s)) / 1000)::numeric, 2) AS distance_km
                    FROM restaurant_profiles
                    WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                    ORDER BY distance_km;
                """
                cur.execute(sql_query, (user_lat, user_lon))
            else:
                sql_query = """
                    SELECT 
                        id, restaurant_name, logo_url, category, rating, delivery_time, 
                        delivery_fee, minimum_order, is_open, delivery_type
                    FROM restaurant_profiles;
                """
                cur.execute(sql_query)
            restaurants = [dict(r) for r in cur.fetchall()]
        return jsonify({"status": "success", "data": restaurants})
    except Exception as e:
        traceback.print_exc()
        if "function earth_distance" in str(e) or "ll_to_earth" in str(e):
            return jsonify({
                "error": "A extensão 'earthdistance' parece não estar ativada no seu banco de dados.",
                "solution": "Por favor, execute 'CREATE EXTENSION IF NOT EXISTS earthdistance;' no SQL Editor da Supabase."
            }), 500
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        if conn: conn.close()

@restaurant_bp.route('/restaurants/<uuid:restaurant_id>', methods=['GET'])
def get_restaurant_details_with_menu_public(restaurant_id):
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM restaurant_profiles WHERE id = %s", (str(restaurant_id),))
            restaurant_profile = cur.fetchone()
            if not restaurant_profile:
                return jsonify({"error": "Restaurante não encontrado"}), 404
            
            cur.execute("SELECT * FROM menu_items WHERE user_id = %s", (str(restaurant_id),))
            menu_items = [dict(item) for item in cur.fetchall()]
            
            response_data = dict(restaurant_profile)
            response_data['menu_items'] = menu_items
        
        return jsonify({"status": "success", "data": response_data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        if conn: conn.close()

@restaurant_bp.route('/restaurant/profile', methods=['GET', 'PUT', 'OPTIONS'])
def handle_restaurant_profile():
    if request.method == 'OPTIONS':
        return jsonify({"status": "ok"}), 200

    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'restaurant': return jsonify({"error": "Acesso não autorizado."}), 403

    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

    try:
        if request.method == 'GET':
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM restaurant_profiles WHERE id = %s", (user_id,)) 
                profile = cur.fetchone()
            if not profile: return jsonify({"error": "Perfil de restaurante não encontrado."}), 404
            return jsonify({"status": "success", "data": dict(profile)})

        if request.method == 'PUT':
            data = request.get_json()
            if not data: return jsonify({"error": "Nenhum dado fornecido para atualização."}), 400

            allowed_fields = [
                'restaurant_name', 'business_name', 'cnpj', 'phone', 'logo_url',
                'address_street', 'address_number', 'address_complement', 
                'address_neighborhood', 'address_city', 'address_state', 'address_zipcode',
                'latitude', 'longitude', 'category', 'delivery_time', 'cuisine_type',
                'description', 'is_open', 'delivery_fee', 'minimum_order',
                'payout_frequency', 'bank_name', 'bank_agency', 'bank_account_number', 
                'bank_account_type', 'pix_key', 'mp_account_id',
                'delivery_type'
            ]

            update_fields = [f"{field} = %s" for field in allowed_fields if field in data]
            if not update_fields: return jsonify({"status": "error", "message": "Nenhum campo válido."}), 400
            
            update_values = [data[field] for field in allowed_fields if field in data]
            sql = f"UPDATE restaurant_profiles SET {', '.join(update_fields)} WHERE id = %s RETURNING *"
            update_values.append(user_id)

            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(sql, tuple(update_values))
                updated_profile = cur.fetchone()
                conn.commit()

            if updated_profile:
                return jsonify({"status": "success", "message": "Perfil atualizado com sucesso.", "data": dict(updated_profile)})
            else:
                return jsonify({"status": "error", "message": "Perfil não encontrado."}), 404

    except Exception as e:
        if conn: conn.rollback()
        traceback.print_exc()
        return jsonify({"status": "error", "error": "Erro interno do servidor.", "detail": str(e)}), 500
    finally:
        if conn: conn.close()

@restaurant_bp.route('/restaurant/upload-logo', methods=['POST'])
def upload_logo():
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'restaurant': return jsonify({"error": "Acesso não autorizado."}), 403
    if 'logo' not in request.files: return jsonify({"error": "Nenhum ficheiro de logo enviado"}), 400
    file = request.files['logo'] 
    if file.filename == '': return jsonify({"error": "Nome de ficheiro vazio"}), 400
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Erro de conexão com o banco de dados"}), 500
    try:
        file_ext = os.path.splitext(file.filename)[1]
        unique_filename = f"logo_{user_id}{file_ext}"
        file_content = file.read()
        supabase.storage.from_("logos").upload(
            path=unique_filename, file=file_content, 
            file_options={"content-type": file.mimetype, "upsert": "true"} 
        )
        public_url = supabase.storage.from_("logos").get_public_url(unique_filename) 
        with conn.cursor() as cur:
            cur.execute("UPDATE restaurant_profiles SET logo_url = %s WHERE id = %s", (public_url, user_id))
            conn.commit()
        return jsonify({"logo_url": public_url, "message": "Logo atualizado com sucesso."}), 200 
    except Exception as e:
        if conn: conn.rollback()
        traceback.print_exc() 
        return jsonify({"status": "error", "error": f"Falha ao fazer upload do logo: {str(e)}"}), 500
    finally:
        if conn: conn.close()

@restaurant_bp.route('/analytics', methods=['GET'])
def get_analytics():
    user_id, user_type, error_response = get_user_id_from_token(request.headers.get('Authorization'))
    if error_response: return error_response
    if user_type != 'restaurant': return jsonify({"status": "error", "error": "Acesso não autorizado."}), 403
    restaurant_id = user_id
    conn = get_db_connection()
    if not conn: return jsonify({"status": "error", "error": "Erro de conexão com o banco de dados"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT total_amount, items, status FROM orders WHERE restaurant_id = %s", (restaurant_id,))
            orders = cur.fetchall()
        total_sales = sum(float(o['total_amount'] or 0) for o in orders if o['status'] == 'Concluído')
        all_ordered_items = []
        for order in orders:
            if isinstance(order['items'], list):
                for item in order['items']:
                    if isinstance(item, dict) and 'name' in item:
                        all_ordered_items.extend([item['name']] * item.get('quantity', 1))
        item_counts = Counter(all_ordered_items)
        most_sold_items_data = [{"item_name": item, "sold_count": count} for item, count in item_counts.most_common(5)]
        return jsonify({
            "status": "success",
            "data": { "total_sales": round(total_sales, 2), "total_orders_count": len(orders), "most_sold_items": most_sold_items_data }
        }), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        if conn: conn.close()