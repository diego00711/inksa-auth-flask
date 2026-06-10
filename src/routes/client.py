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


# ─── Endereços do cliente (múltiplos) ────────────────────────────────────────
ADDRESS_FIELDS = [
    'label', 'street', 'number', 'complement', 'neighborhood',
    'city', 'state', 'zipcode', 'reference', 'latitude', 'longitude',
]


def _auth_client():
    """Retorna (user_id, None) se for um cliente válido, ou (None, error_response)."""
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return None, error
    if user_type != 'client':
        return None, (jsonify({"status": "error", "error": "Unauthorized access"}), 403)
    return user_id, None


@client_bp.route('/addresses', methods=['GET'])
@handle_db_errors
def list_addresses(conn):
    user_id, err = _auth_client()
    if err:
        return err
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            "SELECT * FROM client_addresses WHERE user_id = %s ORDER BY is_default DESC, created_at DESC",
            (user_id,),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return jsonify({"status": "success", "data": rows}), 200


@client_bp.route('/addresses', methods=['POST'])
@handle_db_errors
def create_address(conn):
    user_id, err = _auth_client()
    if err:
        return err
    data = request.get_json() or {}
    payload = {k: data.get(k) for k in ADDRESS_FIELDS if k in data}
    payload.setdefault('label', 'Endereço')

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # Primeiro endereço do cliente vira o padrão automaticamente
        cur.execute("SELECT COUNT(*) AS c FROM client_addresses WHERE user_id = %s", (user_id,))
        is_first = cur.fetchone()['c'] == 0
        make_default = bool(data.get('is_default')) or is_first
        if make_default:
            cur.execute("UPDATE client_addresses SET is_default = false WHERE user_id = %s", (user_id,))

        cols = ['user_id'] + list(payload.keys()) + ['is_default']
        vals = [user_id] + list(payload.values()) + [make_default]
        placeholders = ', '.join(['%s'] * len(cols))
        cur.execute(
            f"INSERT INTO client_addresses ({', '.join(cols)}) VALUES ({placeholders}) RETURNING *",
            vals,
        )
        row = dict(cur.fetchone())
        conn.commit()
    return jsonify({"status": "success", "data": row}), 201


@client_bp.route('/addresses/<uuid:address_id>', methods=['PUT'])
@handle_db_errors
def update_address(conn, address_id):
    user_id, err = _auth_client()
    if err:
        return err
    data = request.get_json() or {}
    updates = {k: data.get(k) for k in ADDRESS_FIELDS if k in data}
    if not updates:
        return jsonify({"status": "error", "error": "No valid fields to update"}), 400

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        set_clause = ", ".join([f"{k} = %s" for k in updates.keys()])
        values = list(updates.values()) + [str(address_id), user_id]
        cur.execute(
            f"UPDATE client_addresses SET {set_clause} WHERE id = %s AND user_id = %s RETURNING *",
            values,
        )
        row = cur.fetchone()
        conn.commit()
        if not row:
            return jsonify({"status": "error", "error": "Endereço não encontrado"}), 404
    return jsonify({"status": "success", "data": dict(row)}), 200


@client_bp.route('/addresses/<uuid:address_id>', methods=['DELETE'])
@handle_db_errors
def delete_address(conn, address_id):
    user_id, err = _auth_client()
    if err:
        return err
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            "DELETE FROM client_addresses WHERE id = %s AND user_id = %s RETURNING is_default",
            (str(address_id), user_id),
        )
        deleted = cur.fetchone()
        if not deleted:
            return jsonify({"status": "error", "error": "Endereço não encontrado"}), 404
        # Se removeu o padrão, promove o endereço mais recente a padrão
        if deleted['is_default']:
            cur.execute(
                """UPDATE client_addresses SET is_default = true
                   WHERE id = (SELECT id FROM client_addresses WHERE user_id = %s
                               ORDER BY created_at DESC LIMIT 1)""",
                (user_id,),
            )
        conn.commit()
    return jsonify({"status": "success"}), 200


@client_bp.route('/addresses/<uuid:address_id>/default', methods=['POST'])
@handle_db_errors
def set_default_address(conn, address_id):
    user_id, err = _auth_client()
    if err:
        return err
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT id FROM client_addresses WHERE id = %s AND user_id = %s", (str(address_id), user_id))
        if not cur.fetchone():
            return jsonify({"status": "error", "error": "Endereço não encontrado"}), 404
        cur.execute("UPDATE client_addresses SET is_default = false WHERE user_id = %s", (user_id,))
        cur.execute("UPDATE client_addresses SET is_default = true WHERE id = %s AND user_id = %s", (str(address_id), user_id))
        conn.commit()
    return jsonify({"status": "success"}), 200
