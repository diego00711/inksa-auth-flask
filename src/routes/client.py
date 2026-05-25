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
            cur.execute("SELECT * FROM client_profiles WHERE user_id = %s LIMIT 1", (user_id,))
            profile = cur.fetchone()
            if not profile:
                # Auto-cria o perfil na primeira requisição autenticada
                try:
                    name_meta, phone_meta = '', ''
                    if supabase:
                        auth_header = request.headers.get('Authorization')
                        token = auth_header.split()[-1] if auth_header else None
                        if token:
                            try:
                                ur = supabase.auth.get_user(token)
                                if ur and ur.user:
                                    m = ur.user.user_metadata or {}
                                    name_meta = m.get('name', '')
                                    phone_meta = m.get('phone', '')
                            except Exception:
                                pass
                    name_parts = (name_meta or '').split(' ', 1)
                    first_name = name_parts[0] or ''
                    last_name = name_parts[1] if len(name_parts) > 1 else ''
                    cur.execute(
                        """INSERT INTO client_profiles (user_id, first_name, last_name, phone)
                           VALUES (%s, %s, %s, %s) RETURNING *""",
                        (user_id, first_name, last_name, phone_meta or None)
                    )
                    profile = cur.fetchone()
                    conn.commit()
                    logging.info(f"Perfil de cliente auto-criado para user_id={user_id}")
                except Exception as create_err:
                    logging.error(f"Erro ao auto-criar perfil de cliente: {create_err}")
                    return jsonify({"status": "error", "error": "Client profile not found"}), 404
            if not profile:
                return jsonify({"status": "error", "error": "Client profile not found"}), 404
            return jsonify({"status": "success", "data": dict(profile)})

    if request.method == 'PUT':
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "error": "No data provided"}), 400

        allowed_fields = [
            'first_name', 'last_name', 'phone', 'cpf', 'birth_date',
            'avatar_url',
            'address_street', 'address_number', 'address_complement',
            'address_neighborhood', 'address_city', 'address_state', 'address_zipcode'
        ]
        updates = {k: v for k, v in data.items() if k in allowed_fields}
        if not updates:
            return jsonify({"status": "error", "error": "No valid fields to update"}), 400

        # Converte strings vazias em None (evita erro de cast em campos date/etc.)
        # Mantém first_name/last_name como estão (NOT NULL na tabela)
        for k in list(updates.keys()):
            if updates[k] == '' and k not in ('first_name', 'last_name'):
                updates[k] = None
        # Remove first_name/last_name se vazios para não violar NOT NULL
        for k in ('first_name', 'last_name'):
            if k in updates and not updates[k]:
                del updates[k]

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Garante que a linha existe antes de atualizar
            cur.execute("SELECT id FROM client_profiles WHERE user_id = %s LIMIT 1", (user_id,))
            if not cur.fetchone():
                cur.execute(
                    """INSERT INTO client_profiles (user_id, first_name, last_name)
                       VALUES (%s, %s, %s)""",
                    (user_id, updates.get('first_name', ''), updates.get('last_name', ''))
                )

            set_clause = ", ".join([f"{k} = %s" for k in updates.keys()])
            values = list(updates.values()) + [user_id]
            cur.execute(
                f"UPDATE client_profiles SET {set_clause} WHERE user_id = %s RETURNING *",
                values
            )
            updated = cur.fetchone()
            conn.commit()
            if not updated:
                return jsonify({"status": "error", "error": "Client profile not found"}), 404
            return jsonify({"status": "success", "data": dict(updated)})


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
