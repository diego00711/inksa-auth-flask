import os
import traceback
import logging
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase

logger = logging.getLogger(__name__)

client_bp = Blueprint('client_bp', __name__, url_prefix='/auth')

@client_bp.route('/profile', methods=['GET', 'PUT'])
def handle_client_profile():
    # Log simples para confirmar qual handler está atendendo
    logger.info('[client.py] handle_client_profile chamado (%s)', request.method)

    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'client':
        return jsonify({"error": "Acesso não autorizado"}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

    try:
        if request.method == 'GET':
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM client_profiles WHERE user_id = %s", (user_id,))
                profile_raw = cur.fetchone()

            if not profile_raw:
                return jsonify({"error": "Perfil de cliente não encontrado"}), 404

            return jsonify({"status": "success", "data": dict(profile_raw)}), 200

        # PUT
        data = request.get_json()
        if not data:
            return jsonify({"error": "Nenhum dado fornecido"}), 400

        allowed_fields = [
            'first_name', 'last_name', 'phone', 'birth_date', 'cpf',
            'address_zipcode', 'address_street', 'address_number',
            'address_complement', 'address_neighborhood', 'address_city',
            'address_state'
        ]
        update_fields = [f"{field} = %s" for field in allowed_fields if field in data]

        if not update_fields:
            return jsonify({"error": "Nenhum campo válido para atualizar"}), 400

        update_values = [data[field] for field in allowed_fields if field in data]
        sql = f"UPDATE client_profiles SET {', '.join(update_fields)} WHERE user_id = %s RETURNING *"
        update_values.append(user_id)

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, tuple(update_values))
            updated_profile = cur.fetchone()
            conn.commit()

        if updated_profile:
            return jsonify({"status": "success", "data": dict(updated_profile)}), 200
        else:
            return jsonify({"error": "Perfil não encontrado para atualizar"}), 404

    except Exception as e:
        if conn:
            conn.rollback()
        logger.exception('Erro em handle_client_profile')
        return jsonify({"error": "Erro interno no servidor.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()

@client_bp.route('/profile/upload-avatar', methods=['POST'])
def upload_avatar():
    logger.info('[client.py] upload_avatar chamado')
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'client':
        return jsonify({"error": "Acesso não autorizado"}), 403

    if 'file' not in request.files:
        return jsonify({"error": "Nenhum ficheiro enviado"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Nome de ficheiro vazio"}), 400

    try:
        file_ext = os.path.splitext(file.filename)[1] or ''
        unique_filename = f"avatar_{user_id}{file_ext}"

        # Upload (permite overwrite com upsert)
        supabase.storage.from_("avatars").upload(
            file=file.read(),
            path=unique_filename,
            file_options={"content-type": file.mimetype, "upsert": "true"}
        )

        # Obter URL pública (compatível com diferentes retornos do SDK)
        public_resp = supabase.storage.from_("avatars").get_public_url(unique_filename)
        if isinstance(public_resp, dict):
            public_url = (
                public_resp.get('data', {}).get('publicUrl')
                or public_resp.get('public_url')
                or public_resp.get('publicUrl')
            )
        else:
            public_url = public_resp

        if not public_url:
            return jsonify({"error": "Falha ao obter URL pública do avatar"}), 500

        # Atualiza avatar no perfil
        supabase.table('client_profiles').update({'avatar_url': public_url}).eq('user_id', user_id).execute()

        return jsonify({"avatar_url": public_url}), 200

    except Exception as e:
        logger.exception('Erro em upload_avatar')
        return jsonify({"error": str(e)}), 500
