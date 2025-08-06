# src/routes/menu.py
from flask import request, jsonify, Blueprint
import os
import uuid
import traceback
import psycopg2
import psycopg2.extras
from datetime import datetime, date, time
import logging
from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase
from functools import wraps
from flask_cors import CORS, cross_origin

logging.basicConfig(level=logging.INFO)
menu_bp = Blueprint('menu_bp', __name__)

# Habilita o CORS para todas as rotas neste Blueprint.
# Isso permite que requisições de outras origens (como o seu frontend em localhost:5174)
# sejam processadas pelo seu backend em localhost:5000.
CORS(menu_bp) 

def make_serializable(data):
    if isinstance(data, dict): return {k: make_serializable(v) for k, v in data.items()}
    if isinstance(data, list): return [make_serializable(item) for item in data]
    if isinstance(data, (datetime, date, time)): return data.isoformat()
    return data

def handle_db_errors(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        conn = None
        try:
            conn = get_db_connection()
            if not conn: return jsonify({"status": "error", "error": "Database connection failed"}), 500
            return f(conn, *args, **kwargs)
        except Exception as e:
            traceback.print_exc()
            return jsonify({"status": "error", "error": str(e)}), 500
        finally:
            if conn: conn.close()
    return wrapper

@menu_bp.route('/', methods=['GET'])
@handle_db_errors
def get_menu_items(conn):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'restaurant': return jsonify({"status": "error", "error": "Unauthorized"}), 403
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT id, name, description, price, category, is_available, image_url FROM menu_items WHERE user_id = %s ORDER BY category, name", (user_id,))
        items = [make_serializable(dict(row)) for row in cur.fetchall()]
        return jsonify({"status": "success", "data": items})

@menu_bp.route('/', methods=['POST'])
@handle_db_errors
def add_menu_item(conn):
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'restaurant': return jsonify({"status": "error", "error": "Unauthorized"}), 403
    data = request.get_json()
    required = ['name', 'price', 'category']
    if not all(field in data for field in required):
        return jsonify({"status": "error", "error": f"Missing required fields: {required}"}), 400
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "INSERT INTO menu_items (user_id, name, description, price, category, is_available, image_url) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                (user_id, data['name'], data.get('description', ''), float(data['price']), data['category'], data.get('is_available', True), data.get('image_url', None))
            )
            new_item = make_serializable(dict(cur.fetchone()))
            conn.commit()
            return jsonify({"status": "success", "data": new_item}), 201
    except psycopg2.Error as e:
        conn.rollback()
        return jsonify({"status": "error", "error": "Database error"}), 500

# >> NOVA ROTA ADICIONADA <<
@menu_bp.route('/<int:item_id>', methods=['DELETE'])
@handle_db_errors
def delete_menu_item(conn, item_id):
    """
    Exclui um item do cardápio pelo seu ID.
    
    Parâmetros:
    - item_id: O ID do item a ser excluído.
    """
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'restaurant': return jsonify({"status": "error", "error": "Unauthorized"}), 403
    
    try:
        with conn.cursor() as cur:
            # Exclui o item do banco de dados, verificando se pertence ao usuário
            # A cláusula `AND user_id = %s` previne que um usuário exclua itens de outro.
            cur.execute("DELETE FROM menu_items WHERE id = %s AND user_id = %s RETURNING id", (item_id, user_id))
            deleted_id = cur.fetchone()
            conn.commit()
            
            if deleted_id:
                # O item foi encontrado e excluído com sucesso
                return jsonify({"status": "success", "message": f"Item com ID {item_id} excluído com sucesso."}), 200
            else:
                # O item não foi encontrado ou não pertence a este usuário
                return jsonify({"status": "error", "message": "Item não encontrado ou não autorizado."}), 404
                
    except psycopg2.Error as e:
        conn.rollback()
        logging.error(f"Erro de banco de dados ao excluir item: {e}")
        return jsonify({"status": "error", "message": "Erro de banco de dados"}), 500

@menu_bp.route('/upload-image', methods=['POST'])
def upload_menu_item_image():
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    if user_type != 'restaurant': return jsonify({"status": "error", "error": "Unauthorized"}), 403
    if 'file' not in request.files: return jsonify({"status": "error", "error": "Nenhum ficheiro de imagem enviado com o campo 'file'."}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"status": "error", "error": "Nome de ficheiro vazio."}), 400
    try:
        file_ext = os.path.splitext(file.filename)[1]
        unique_filename = f"{user_id}-{uuid.uuid4()}{file_ext}"
        path_on_storage = f"public/{unique_filename}"
        supabase.storage.from_("menu-images").upload(path=path_on_storage, file=file.read(), file_options={"content-type": file.mimetype})
        public_url = supabase.storage.from_("menu-images").get_public_url(path_on_storage)
        return jsonify({"status": "success", "data": {"image_url": public_url}}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "error": str(e)}), 500