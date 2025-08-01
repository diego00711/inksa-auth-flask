# src/routes/menu.py (VERSÃO CORRIGIDA)

import os
import uuid
import traceback
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from supabase import create_client, Client
from datetime import date, time, datetime
import logging 

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- CONFIGURAÇÃO ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL") # Not used in get_user_from_token, but good to have

# Inicializa o cliente Supabase
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    logging.error("ERRO: SUPABASE_URL ou SUPABASE_SERVICE_KEY não configurados para menu.py. A integração com o Supabase pode falhar.")
    supabase = None # Define como None para evitar erro
else:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        logging.info("Cliente Supabase (menu.py) inicializado com sucesso.")
    except Exception as e:
        logging.error(f"ERRO ao inicializar cliente Supabase (menu.py): {e}")
        supabase = None


menu_bp = Blueprint('menu_bp', __name__, url_prefix='/api')

# --- FUNÇÕES DE AJUDA ---

def make_serializable(row):
    """
    Converte objetos datetime, date e time dentro de um dicionário (linha do banco)
    para strings no formato ISO 8601, que é compatível com JSON.
    """
    if row is None:
        return None
    serializable_row = dict(row)
    for key, value in serializable_row.items():
        if isinstance(value, (datetime, date, time)):
            serializable_row[key] = value.isoformat()
    return serializable_row

def get_db_connection():
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        logging.error(f"Erro ao conectar ao banco de dados: {e}")
        return None

def get_user_from_token(request_headers):
    auth_header = request_headers.get('Authorization')
    if not auth_header or not auth_header.startswith('Bearer '):
        return None, (jsonify({"status": "error", "error": "Token de autorização ausente"}), 401)
    
    token = auth_header.split(' ')[1]
    
    if supabase is None:
        return None, (jsonify({"status": "error", "error": "Serviço Supabase não inicializado."}), 500)

    try:
        user_response = supabase.auth.get_user(token)
        user = user_response.user
        
        # O user_metadata é um dicionário, então acesse a chave 'user_type'
        if 'user_type' not in user.user_metadata or user.user_metadata['user_type'] != 'restaurant':
             return None, (jsonify({"error": "Acesso não autorizado para este tipo de utilizador"}), 403)
        
        return str(user.id), None
    except Exception as e:
        logging.error(f"Erro ao validar token ou buscar usuário: {e}", exc_info=True)
        return None, (jsonify({"status": "error", "error": f"Token inválido ou expirado: {e}"}), 401)


# --- ROTAS DE MENU (CRUD COMPLETO) ---

@menu_bp.route('/menu', methods=['GET'])
def get_menu_items():
    user_id, error = get_user_from_token(request.headers)
    if error: return error
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Falha na conexão com a base de dados"}), 500
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM menu_items WHERE user_id = %s ORDER BY category, name", (user_id,))
                items_raw = [dict(row) for row in cur.fetchall()]
                items = [make_serializable(item) for item in items_raw]
        return jsonify({"status": "success", "data": items})
    except Exception as e:
        traceback.print_exc()
        logging.error(f"Erro interno ao buscar itens do menu: {e}", exc_info=True)
        return jsonify({"error": "Erro interno ao buscar itens do menu"}), 500

@menu_bp.route('/menu', methods=['POST'])
def add_menu_item():
    user_id, error = get_user_from_token(request.headers)
    if error: return error
    data = request.get_json()
    if not data or not all(k in data for k in ['name', 'price']): return jsonify({"error": "Dados incompletos"}), 400
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Falha na conexão com a base de dados"}), 500
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("INSERT INTO menu_items (user_id, name, description, price, category, is_available, image_url) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *;",(user_id, data.get('name'), data.get('description'), data.get('price'), data.get('category'), data.get('is_available', True), data.get('image_url')))
                new_item_raw = dict(cur.fetchone())
                new_item = make_serializable(new_item_raw)
        return jsonify({"status": "success", "data": new_item}), 201
    except Exception as e:
        traceback.print_exc()
        logging.error(f"Erro interno ao adicionar item: {e}", exc_info=True)
        return jsonify({"error": "Erro interno ao adicionar item"}), 500

@menu_bp.route('/menu/<string:item_id>', methods=['PUT'])
def update_menu_item(item_id):
    user_id, error = get_user_from_token(request.headers)
    if error: return error
    data = request.get_json()
    if not data: return jsonify({"error": "Nenhum dado fornecido"}), 400
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Falha na conexão com a base de dados"}), 500
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                allowed_fields = ['name', 'description', 'price', 'category', 'is_available', 'image_url']
                update_fields = [f"{key} = %s" for key in data if key in allowed_fields]
                if not update_fields: return jsonify({"error": "Nenhum campo válido fornecido"}), 400
                update_values = [data[key] for key in data if key in allowed_fields]
                update_values.extend([item_id, user_id])
                query = f"UPDATE menu_items SET {', '.join(update_fields)} WHERE id = %s AND user_id = %s RETURNING *;"
                cur.execute(query, tuple(update_values))
                updated_item_raw = cur.fetchone()
                if not updated_item_raw: return jsonify({"error": "Item não encontrado ou não pertence a este restaurante"}), 404
                updated_item = make_serializable(dict(updated_item_raw))
        return jsonify({"status": "success", "data": updated_item})
    except Exception as e:
        traceback.print_exc()
        logging.error(f"Erro interno ao atualizar item: {e}", exc_info=True)
        return jsonify({"error": "Erro interno ao atualizar item"}), 500

@menu_bp.route('/menu/<string:item_id>', methods=['DELETE'])
def delete_menu_item(item_id):
    user_id, error = get_user_from_token(request.headers)
    if error: return error
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Falha na conexão com a base de dados"}), 500
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM menu_items WHERE id = %s AND user_id = %s", (item_id, user_id))
                if cur.rowcount == 0: return jsonify({"error": "Item não encontrado ou não pertence a este restaurante"}), 404
        return jsonify({"status": "success", "message": "Item excluído com sucesso"})
    except Exception as e:
        traceback.print_exc()
        logging.error(f"Erro interno ao excluir item: {e}", exc_info=True)
        return jsonify({"error": "Erro interno ao excluir item"}), 500

@menu_bp.route('/menu/upload-image', methods=['POST'])
def upload_menu_item_image():
    user_id, error = get_user_from_token(request.headers)
    if error: return error
    if 'file' not in request.files: return jsonify({"error": "Nenhum ficheiro enviado"}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "Nome de ficheiro vazio"}), 400
    
    if supabase is None:
        logging.error("Serviço Supabase não inicializado para upload de imagem.")
        return jsonify({"error": "Serviço de upload indisponível."}), 500
    
    try:
        file_ext = os.path.splitext(file.filename)[1]
        unique_filename = f"item_{user_id}_{uuid.uuid4().hex}{file_ext}"
        supabase.storage.from_("menu-images").upload(
            file=file.read(), path=unique_filename, file_options={"content-type": file.mimetype}
        )
        public_url = supabase.storage.from_("menu-images").get_public_url(unique_filename)
        return jsonify({"image_url": public_url}), 200
    except Exception as e:
        traceback.print_exc()
        logging.error(f"Erro ao fazer upload da imagem: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


# --- ROTAS DE CATEGORIAS (CRUD COMPLETO) ---

@menu_bp.route('/categories', methods=['GET'])
def get_categories():
    user_id, error = get_user_from_token(request.headers)
    if error: return error
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Falha na conexão com a base de dados"}), 500
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM menu_categories WHERE restaurant_id = %s ORDER BY name", (user_id,))
                categories_raw = [dict(row) for row in cur.fetchall()]
                categories = [make_serializable(cat) for cat in categories_raw]
        return jsonify({"status": "success", "data": categories})
    except Exception as e:
        traceback.print_exc()
        logging.error(f"Erro interno ao buscar categorias: {e}", exc_info=True)
        return jsonify({"error": "Erro interno ao buscar categorias"}), 500

@menu_bp.route('/categories', methods=['POST'])
def add_category():
    user_id, error = get_user_from_token(request.headers)
    if error: return error
    data = request.get_json()
    name = data.get('name')
    if not name: return jsonify({"error": "Nome da categoria é obrigatório"}), 400
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Falha na conexão com a base de dados"}), 500
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("INSERT INTO menu_categories (restaurant_id, name) VALUES (%s, %s) RETURNING *;", (user_id, name))
                new_category_raw = dict(cur.fetchone())
                new_category = make_serializable(new_category_raw)
        return jsonify({"status": "success", "data": new_category}), 201
    except psycopg2.IntegrityError as ie:
        logging.error(f"Erro de integridade ao adicionar categoria: {ie}", exc_info=True)
        return jsonify({"error": "Categoria com este nome já existe"}), 409
    except Exception as e:
        traceback.print_exc()
        logging.error(f"Erro interno ao adicionar categoria: {e}", exc_info=True)
        return jsonify({"error": "Erro interno ao adicionar categoria"}), 500

@menu_bp.route('/categories/<string:category_id>', methods=['PUT'])
def update_category(category_id):
    user_id, error = get_user_from_token(request.headers)
    if error: return error
    data = request.get_json()
    name = data.get('name')
    if not name: return jsonify({"error": "Nome da categoria é obrigatório"}), 400
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Falha na conexão com a base de dados"}), 500
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                allowed_fields = ['name'] # Removido 'is_active' se não existe
                update_fields = [f"{key} = %s" for key in data if key in allowed_fields]
                if not update_fields: return jsonify({"error": "Nenhum campo válido fornecido"}), 400
                update_values = [data[key] for key in data if key in allowed_fields]
                update_values.extend([category_id, user_id])
                query = f"UPDATE menu_categories SET {', '.join(update_fields)} WHERE id = %s AND restaurant_id = %s RETURNING *;"
                cur.execute(query, tuple(update_values))
                updated_category_raw = cur.fetchone()
                if not updated_category_raw: return jsonify({"error": "Categoria não encontrada"}), 404
                updated_category = make_serializable(dict(updated_category_raw))
        return jsonify({"status": "success", "data": updated_category})
    except psycopg2.IntegrityError as ie:
        logging.error(f"Erro de integridade ao atualizar categoria: {ie}", exc_info=True)
        return jsonify({"error": "Categoria com este nome já existe"}), 409
    except Exception as e:
        traceback.print_exc()
        logging.error(f"Erro interno ao atualizar categoria: {e}", exc_info=True)
        return jsonify({"error": "Erro interno ao atualizar categoria"}), 500

@menu_bp.route('/categories/<string:category_id>', methods=['DELETE'])
def delete_category(category_id):
    user_id, error = get_user_from_token(request.headers)
    if error: return error
    conn = get_db_connection()
    if not conn: return jsonify({"error": "Falha na conexão com a base de dados"}), 500
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM menu_categories WHERE id = %s AND restaurant_id = %s", (category_id, user_id))
                if cur.rowcount == 0: return jsonify({"error": "Categoria não encontrada"}), 404
        return jsonify({"status": "success", "message": "Categoria excluída com sucesso"})
    except Exception as e:
        traceback.print_exc()
        logging.error(f"Erro interno ao excluir categoria: {e}", exc_info=True)
        return jsonify({"error": "Erro interno ao excluir categoria"}), 500