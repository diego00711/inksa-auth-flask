# src/routes/delivery_auth_profile.py - VERSÃO FINAL E LIMPA

import os
import uuid
import traceback
import json
import logging
import re
from flask import Blueprint, request, jsonify, g
import psycopg2
import psycopg2.extras
from datetime import datetime, date, time, timedelta
from decimal import Decimal
from functools import wraps
# ❌ REMOVIDO: A importação do cross_origin não é mais necessária aqui.
# from flask_cors import cross_origin 

from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

delivery_auth_profile_bp = Blueprint('delivery_auth_profile', __name__)

# ==============================================
# DECORATOR DE AUTENTICAÇÃO (COM A VERIFICAÇÃO CORRIGIDA)
# ==============================================
def delivery_token_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth_header = request.headers.get('Authorization')
        if not auth_header:
            return jsonify({"error": "Token de autorização ausente"}), 401

        token_result = get_user_id_from_token(auth_header)

        if isinstance(token_result, tuple) and len(token_result) == 2:
            user_auth_id, user_type = token_result
            
            # ✅ CORREÇÃO: Padronizado para 'delivery' para alinhar com o front-end.
            if user_type != 'delivery':
                return jsonify({"error": f"Acesso não autorizado. Rota para 'delivery', mas o tipo do usuário é '{user_type}'."}), 403
            
            g.user_auth_id = str(user_auth_id)
            
            return f(*args, **kwargs)
        else:
            return token_result

    return decorated_function

# ... (O resto do seu arquivo, incluindo helpers, continua o mesmo)
# ==============================================
# HELPERS (Mantidos como estavam)
# ==============================================
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal): return float(obj)
        if isinstance(obj, (datetime, date, time, timedelta)): return obj.isoformat()
        if isinstance(obj, uuid.UUID): return str(obj)
        return super().default(obj)

def serialize_data(data):
    return json.loads(json.dumps(data, cls=CustomJSONEncoder))

def sanitize_text(text):
    if not text: return text
    return re.sub(r'[\x00-\x1F\x7F]', '', text.strip())

# ==============================================
# ROTAS DE PERFIL (SEM DECORADORES DE CORS)
# ==============================================
@delivery_auth_profile_bp.route('/profile', methods=['GET', 'PUT'])
# ❌ REMOVIDO: O decorador @cross_origin() foi removido daqui.
@delivery_token_required
def handle_profile():
    conn = None
    try:
        user_id = g.user_auth_id
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM delivery_profiles WHERE user_id = %s", (user_id,))
            profile = cur.fetchone()

            if not profile:
                cur.execute(
                    """INSERT INTO delivery_profiles (user_id, first_name, phone) 
                       VALUES (%s, 'Novo Entregador', '00000000000') RETURNING *""",
                    (user_id,)
                )
                profile = cur.fetchone()
                conn.commit()
                logger.info(f"Novo perfil de entregador criado para user_id: {user_id}")

            profile_id = profile['id']

            if request.method == 'GET':
                return jsonify({"data": serialize_data(dict(profile))}), 200

            elif request.method == 'PUT':
                if not request.is_json:
                    return jsonify({"error": "Content-Type deve ser application/json"}), 400
                
                data = request.get_json()
                allowed_fields = [
                    'first_name', 'last_name', 'phone', 'cpf', 'birth_date', 'vehicle_type', 
                    'address_street', 'address_number', 'address_complement', 'address_neighborhood', 
                    'address_city', 'address_state', 'address_zipcode', 'is_available'
                ]
                
                update_data = {
                    field: sanitize_text(data[field]) if isinstance(data.get(field), str) else data.get(field)
                    for field in allowed_fields if field in data
                }

                if not update_data:
                    return jsonify({"error": "Nenhum campo válido para atualização"}), 400

                set_clauses = [f'"{field}" = %s' for field in update_data.keys()]
                params = list(update_data.values())
                params.append(profile_id)

                query = f"UPDATE delivery_profiles SET {', '.join(set_clauses)}, updated_at = NOW() WHERE id = %s RETURNING *"
                
                cur.execute(query, params)
                updated_profile = cur.fetchone()
                conn.commit()

                return jsonify({"data": serialize_data(dict(updated_profile))}), 200

    except Exception as e:
        logger.error(f"Erro em handle_profile: {e}", exc_info=True)
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn: conn.close()

# ==============================================
# ROTA DE UPLOAD DE AVATAR (SEM DECORADOR DE CORS)
# ==============================================
@delivery_auth_profile_bp.route('/upload-avatar', methods=['POST'])
# ❌ REMOVIDO: O decorador @cross_origin() foi removido daqui.
@delivery_token_required
def upload_avatar():
    if 'avatar' not in request.files or not request.files['avatar'].filename:
        return jsonify({"error": "Nenhum arquivo válido enviado"}), 400

    avatar_file = request.files['avatar']
    file_ext = avatar_file.filename.rsplit('.', 1)[1].lower()
    if file_ext not in {'png', 'jpg', 'jpeg', 'gif', 'webp'}:
        return jsonify({"error": "Tipo de arquivo não permitido"}), 400

    conn = None
    try:
        user_id = g.user_auth_id
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM delivery_profiles WHERE user_id = %s", (user_id,))
            profile = cur.fetchone()
            if not profile:
                return jsonify({"error": "Perfil não encontrado"}), 404
            profile_id = profile['id']

        bucket_name = "delivery-avatars"
        file_path = f"public/{profile_id}.{file_ext}"
        file_content = avatar_file.read()

        supabase.storage.from_(bucket_name).upload(
            path=file_path, file=file_content, 
            file_options={"content-type": avatar_file.content_type, "upsert": "true"}
        )
        public_url = supabase.storage.from_(bucket_name).get_public_url(file_path)

        with conn.cursor() as cur:
            cur.execute("UPDATE delivery_profiles SET avatar_url = %s WHERE id = %s", (public_url, profile_id))
            conn.commit()

        return jsonify({"avatar_url": public_url}), 200

    except Exception as e:
        logger.error(f"Erro no upload de avatar: {e}", exc_info=True)
        if conn: conn.rollback()
        return jsonify({"error": "Erro interno durante o upload"}), 500
    finally:
        if conn: conn.close()
