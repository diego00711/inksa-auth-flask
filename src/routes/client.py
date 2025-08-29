# src/routes/client.py - VERSÃO CORRIGIDA E FUNCIONAL

import logging
from flask import Blueprint, jsonify, request
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token
from functools import wraps

# Cria o Blueprint para as rotas do cliente
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

# ✅ ROTA ADICIONADA: Esta é a rota que o seu front-end está procurando.
@client_bp.route('/profile', methods=['GET'])
@handle_db_errors
def get_client_profile(conn):
    """
    Busca o perfil do cliente logado usando o token de autenticação.
    """
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error: return error
    
    # Garante que apenas usuários do tipo 'client' possam acessar esta rota
    if user_type != 'client': 
        return jsonify({"status": "error", "error": "Unauthorized access"}), 403

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # Busca o perfil na tabela 'client_profiles'
        # É importante que essa tabela exista no seu banco de dados.
        cur.execute("SELECT * FROM client_profiles WHERE user_id = %s", (user_id,))
        profile = cur.fetchone()
        
        if not profile:
            # Se o perfil não for encontrado, retorna um erro 404
            return jsonify({"status": "error", "error": "Client profile not found"}), 404
            
        # Se o perfil for encontrado, retorna os dados com sucesso
        return jsonify({"status": "success", "data": dict(profile)})

# Você pode adicionar outras rotas específicas do cliente aqui no futuro, como:
# @client_bp.route('/my-orders', methods=['GET'])
# def get_my_orders():
#     # ... lógica para buscar pedidos do cliente ...
#     return jsonify({"message": "Meus pedidos"})
