# src/utils/helpers.py

import os
from flask import jsonify
import psycopg2
import psycopg2.extras
from supabase import create_client, Client

# --- Configuração Centralizada do Cliente Supabase e BD ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")

# Instância do Supabase que será importada por outros arquivos
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Função de conexão com o BD centralizada
def get_db_connection():
    """Cria e retorna uma nova conexão com o banco de dados."""
    try:
        return psycopg2.connect(DATABASE_URL)
    except Exception as e:
        print(f"Erro ao conectar ao banco de dados: {e}")
        return None

# Versão completa e definitiva da função de validação de token
def get_user_id_from_token(auth_header):
    """
    Valida o token JWT, retorna o ID do usuário e o TIPO do usuário.
    Retorna uma tupla (user_id, user_type, error_response).
    """
    if not auth_header or not auth_header.startswith('Bearer '):
        return None, None, (jsonify({"status": "error", "error": "Token de autorização ausente"}), 401)
    
    token = auth_header.split(' ')[1]
    try:
        user_response = supabase.auth.get_user(token)
        user = user_response.user
        if not user:
            return None, None, (jsonify({"error": "Token inválido ou sessão expirada."}), 401)
        
        user_id = str(user.id)
        
        conn = get_db_connection()
        if not conn:
            return None, None, (jsonify({"error": "Falha na conexão interna com o banco de dados."}), 500)

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT user_type FROM users WHERE id = %s", (user_id,))
            db_user = cur.fetchone()
        conn.close()

        if not db_user:
            return None, None, (jsonify({"error": "Usuário autenticado não encontrado na base de dados local."}), 404)
        
        user_type = db_user['user_type']
        return user_id, user_type, None

    except Exception as e:
        return None, None, (jsonify({"status": "error", "error": "Erro na validação do token.", "detalhes": str(e)}), 401)