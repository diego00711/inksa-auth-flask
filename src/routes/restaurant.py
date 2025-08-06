from flask import request, jsonify
from ..utils.helpers import get_db_connection, get_user_id_from_token
import os
import traceback
from flask import Blueprint
import psycopg2
import psycopg2.extras
from ..utils.helpers import supabase
from functools import wraps
import uuid

restaurant_bp = Blueprint('restaurant_bp', __name__)

def handle_db_errors(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        conn = None
        try:
            conn = get_db_connection()
            if not conn:
                return jsonify({"status": "error", "error": "Database connection failed"}), 500
            return f(conn, *args, **kwargs)
        except psycopg2.Error as e:
            traceback.print_exc()
            return jsonify({"status": "error", "error": "Database operation failed"}), 500
        except Exception as e:
            traceback.print_exc()
            return jsonify({"status": "error", "error": str(e)}), 500
        finally:
            if conn:
                conn.close()
    return wrapper

@restaurant_bp.route('/', methods=['GET'])
@handle_db_errors
def get_all_restaurants_public(conn):
    user_lat = request.args.get('user_lat', type=float)
    user_lon = request.args.get('user_lon', type=float)
    
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        if user_lat and user_lon:
            cur.execute("""
                SELECT id, restaurant_name, logo_url, category, rating, delivery_time, 
                delivery_fee, minimum_order, is_open, delivery_type,
                ROUND((earth_distance(ll_to_earth(latitude, longitude), ll_to_earth(%s, %s)) / 1000)::numeric, 2) AS distance_km
                FROM restaurant_profiles
                WHERE latitude IS NOT NULL AND longitude IS NOT NULL
                ORDER BY distance_km
            """, (user_lat, user_lon))
        else:
            cur.execute("""
                SELECT id, restaurant_name, logo_url, category, rating, delivery_time,
                delivery_fee, minimum_order, is_open, delivery_type
                FROM restaurant_profiles
            """)
        restaurants = [dict(row) for row in cur.fetchall()]
        return jsonify({"status": "success", "data": restaurants})

@restaurant_bp.route('/<uuid:restaurant_id>', methods=['GET'])
@handle_db_errors
def get_restaurant_details(conn, restaurant_id):
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT * FROM restaurant_profiles WHERE id = %s", (str(restaurant_id),))
        restaurant = cur.fetchone()
        if not restaurant:
            return jsonify({"status": "error", "error": "Restaurant not found"}), 404
        
        cur.execute("SELECT * FROM menu_items WHERE restaurant_id = %s", (str(restaurant_id),))
        menu_items = [dict(item) for item in cur.fetchall()]
        
        return jsonify({
            "status": "success",
            "data": {
                **dict(restaurant),
                "menu_items": menu_items
            }
        })

@restaurant_bp.route('/profile', methods=['GET', 'PUT'])
def handle_profile():
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'restaurant':
        return jsonify({"status": "error", "error": "Unauthorized"}), 403

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "error": "Database connection failed"}), 500

        if request.method == 'GET':
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM restaurant_profiles WHERE user_id = %s", (user_id,))
                profile = cur.fetchone()
                if not profile:
                    return jsonify({"status": "error", "error": "Profile not found"}), 404
                return jsonify({"status": "success", "data": dict(profile)})

        elif request.method == 'PUT':
            data = request.get_json()
            if not data:
                return jsonify({"status": "error", "error": "No data provided"}), 400

            allowed_fields = [
                'restaurant_name', 'business_name', 'cnpj', 'phone', 'logo_url', 'address_street', 
                'address_number', 'address_complement', 'address_neighborhood', 'address_city', 
                'address_state', 'address_zipcode', 'latitude', 'longitude', 'category', 
                'delivery_time', 'cuisine_type', 'description', 'is_open', 'delivery_fee', 
                'minimum_order', 'payout_frequency', 'bank_name', 'bank_agency', 
                'bank_account_number', 'bank_account_type', 'pix_key', 'mp_account_id', 'delivery_type'
            ]
            updates = {k: v for k, v in data.items() if k in allowed_fields}
            if not updates:
                return jsonify({"status": "error", "error": "No valid fields to update"}), 400

            set_clause = ", ".join([f"{k} = %s" for k in updates.keys()])
            values = list(updates.values()) + [user_id]

            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    f"UPDATE restaurant_profiles SET {set_clause} WHERE user_id = %s RETURNING *",
                    values
                )
                updated = cur.fetchone()
                conn.commit()
                if not updated:
                    return jsonify({"status": "error", "error": "Profile not found"}), 404
                return jsonify({"status": "success", "data": dict(updated)})
    
    except Exception as e:
        if conn: 
            conn.rollback()
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        if conn: 
            conn.close()

@restaurant_bp.route('/upload-logo', methods=['POST'])
def upload_logo():
    # Inicializar conn como None
    conn = None
    
    try:
        # Verificar autenticação
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'restaurant':
            return jsonify({"status": "error", "error": "Unauthorized"}), 403
        
        # Verificar se arquivo foi enviado
        if 'logo' not in request.files:
            return jsonify({"status": "error", "error": "No file provided"}), 400

        file = request.files['logo']
        if not file.filename:
            return jsonify({"status": "error", "error": "Empty filename"}), 400

        # Validar tipo de arquivo
        allowed_extensions = ['.jpg', '.jpeg', '.png', '.gif']
        file_ext = os.path.splitext(file.filename)[1].lower()
        if file_ext not in allowed_extensions:
            return jsonify({"status": "error", "error": "Invalid file type. Only JPG, PNG and GIF allowed"}), 400

        # Gerar nome único do arquivo
        unique_filename = f"{user_id}_{str(uuid.uuid4())}{file_ext}"
        
        # Upload para o Supabase Storage (bucket "logos")
        upload_result = supabase.storage.from_("logos").upload(
            path=unique_filename,
            file=file.read(),
            file_options={"content-type": file.mimetype, "upsert": "true"}
        )
        
        # Obter URL pública
        public_url = supabase.storage.from_("logos").get_public_url(unique_filename)
        
        # Atualizar banco de dados
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "error": "Database connection failed"}), 500
            
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE restaurant_profiles SET logo_url = %s WHERE user_id = %s RETURNING logo_url",
                (public_url, user_id)
            )
            updated_row = cur.fetchone()
            if not updated_row:
                return jsonify({"status": "error", "error": "Restaurant profile not found"}), 404
                
            conn.commit()
            return jsonify({
                "status": "success", 
                "data": {
                    "logo_url": public_url,
                    "message": "Logo uploaded successfully"
                }
            })
    
    except Exception as e:
        if conn:
            conn.rollback()
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500
    finally:
        if conn: 
            conn.close()