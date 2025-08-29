# src/routes/client.py - VERSÃO COM UPLOAD DE AVATAR

import logging
from flask import Blueprint, jsonify, request
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase
from functools import wraps
import os
import uuid

client_bp = Blueprint('client_bp', __name__)
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
            logging.error(f"Client Route DB Error: {e}", exc_info=True)
            return jsonify({"status": "error", "error": str(e)}), 500
        finally:
            if conn:
                conn.close()
    return wrapper

@client_bp.route('/profile', methods=['GET', 'PUT'])
@handle_db_errors
def handle_client_profile(conn):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'client': return jsonify({"status": "error", "error": "Unauthorized access"}), 403

    if request.method == 'GET':
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT * FROM client_profiles WHERE user_id = %s", (user_id,))
            profile = cur.fetchone()
            if not profile:
                return jsonify({"status": "error", "error": "Client profile not found"}), 404
            return jsonify({"status": "success", "data": dict(profile)})

    if request.method == 'PUT':
        data = request.get_json()
        # Lógica para atualizar o perfil do cliente (nome, endereço, etc.)
        # ... (pode ser implementada aqui no futuro) ...
        return jsonify({"status": "success", "message": "Profile updated successfully"})


# ✅ ROTA ADICIONADA: Rota para upload de avatar do cliente
@client_bp.route('/profile/upload-avatar', methods=['POST'])
@handle_db_errors
def upload_avatar(conn):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'client': return jsonify({"status": "error", "error": "Unauthorized"}), 403

    if 'file' not in request.files:
        return jsonify({"status": "error", "error": "Nenhum arquivo enviado com o campo 'file'."}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"status": "error", "error": "Nome de arquivo vazio."}), 400

    try:
        file_ext = os.path.splitext(file.filename)[1]
        # Cria um nome de arquivo único para evitar conflitos
        unique_filename = f"avatar_{user_id}_{uuid.uuid4()}{file_ext}"
        
        # Faz o upload para o bucket 'avatars' no Supabase Storage
        supabase.storage.from_("avatars").upload(
            path=unique_filename,
            file=file.read(),
            file_options={"content-type": file.mimetype, "upsert": "true"}
        )
        
        # Obtém a URL pública do arquivo que acabamos de enviar
        public_url = supabase.storage.from_("avatars").get_public_url(unique_filename)
        
        # Atualiza a coluna 'avatar_url' na tabela 'client_profiles'
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE client_profiles SET avatar_url = %s WHERE user_id = %s",
                (public_url, user_id)
            )
            conn.commit()

        return jsonify({"status": "success", "data": {"avatar_url": public_url}}), 200

    except Exception as e:
        logging.error(f"Avatar Upload Error: {e}", exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500
