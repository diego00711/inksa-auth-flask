# src/utils/helpers.py

import os
import psycopg2
import psycopg2.extras
from flask import jsonify
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# --- Configuração do Supabase ---
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")

# Debug: Verifique se as variáveis estão sendo carregadas
print("=== DEBUG: Variáveis de ambiente ===")
print(f"SUPABASE_URL: {'✅' if url else '❌'} {url}")
print(f"SUPABASE_KEY: {'✅' if key else '❌'} {key[:20]}...{key[-20:] if key and len(key) > 40 else ''}")
print(f"DATABASE_URL: {'✅' if os.environ.get('DATABASE_URL') else '❌'}")
print("===================================")

if not url or not key:
    raise ValueError("Variáveis de ambiente SUPABASE_URL e SUPABASE_KEY são necessárias")

try:
    supabase: Client = create_client(url, key)
    print("✅ Cliente Supabase inicializado com sucesso")
except Exception as e:
    print(f"❌ Erro ao inicializar Supabase: {e}")
    supabase = None

# --- Conexão com o Banco de Dados ---
def get_db_connection():
    """Estabelece e retorna uma conexão com o banco de dados PostgreSQL usando DATABASE_URL."""
    try:
        database_url = os.environ.get("DATABASE_URL")
        
        if not database_url:
            print("Erro: Variável de ambiente DATABASE_URL não encontrada.")
            return None

        conn = psycopg2.connect(database_url)
        return conn
    except psycopg2.OperationalError as e:
        print(f"Erro de conexão com o banco de dados: {e}")
        return None
    except Exception as e:
        print(f"Erro inesperado na conexão com o banco: {e}")
        return None

# --- Função de Autenticação ---
def get_user_id_from_token(auth_header):
    """
    Valida o token JWT, extrai o ID do usuário e busca o tipo de usuário
    diretamente do banco de dados para maior segurança.
    """
    if not auth_header:
        return None, None, jsonify({"error": "Token de autorização ausente"}), 401
    
    parts = auth_header.split()
    if parts[0].lower() != 'bearer' or len(parts) != 2:
        return None, None, jsonify({"error": "Formato do token inválido"}), 401
        
    jwt_token = parts[1]
    
    try:
        # 1. Valida o token com o Supabase
        if not supabase:
            print("Erro: Cliente Supabase não inicializado. Verifique as variáveis de ambiente SUPABASE_URL e SUPABASE_KEY.")
            return None, None, jsonify({"error": "Configuração do servidor incompleta"}), 500

        user_response = supabase.auth.get_user(jwt_token)
        user = user_response.user
        if not user:
            raise ValueError("Token inválido ou expirado")
            
        user_id = str(user.id)

        # 2. Busca o tipo de usuário no nosso banco de dados (fonte da verdade)
        conn = get_db_connection()
        if not conn:
            return None, None, jsonify({"error": "Falha na conexão com o banco de dados"}), 500
            
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT user_type FROM users WHERE id = %s", (user_id,))
            db_user = cur.fetchone()
        
        conn.close()

        if not db_user:
            return None, None, jsonify({"error": "Usuário não encontrado no banco de dados local"}), 404
            
        user_type = db_user['user_type']
        
        # 3. Retorna os dados validados
        return user_id, user_type, None, None

    except Exception as e:
        print(f"Erro na validação do token: {e}")
        return None, None, jsonify({"error": "Token inválido ou expirado"}), 401

# --- Função auxiliar para verificar se o usuário é admin ---
def is_admin(user_id):
    """Verifica se o usuário é administrador."""
    try:
        conn = get_db_connection()
        if not conn:
            return False
            
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT user_type FROM users WHERE id = %s", (user_id,))
            db_user = cur.fetchone()
        
        conn.close()

        return db_user and db_user['user_type'] == 'admin'
        
    except Exception as e:
        print(f"Erro ao verificar se usuário é admin: {e}")
        return False

# --- Função auxiliar para verificar se o usuário é estabelecimento ---
def is_establishment(user_id):
    """Verifica se o usuário é estabelecimento."""
    try:
        conn = get_db_connection()
        if not conn:
            return False
            
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT user_type FROM users WHERE id = %s", (user_id,))
            db_user = cur.fetchone()
        
        conn.close()

        return db_user and db_user['user_type'] == 'establishment'
        
    except Exception as e:
        print(f"Erro ao verificar se usuário é estabelecimento: {e}")
        return False

# --- Função auxiliar para verificar se o usuário é cliente ---
def is_client(user_id):
    """Verifica se o usuário é cliente."""
    try:
        conn = get_db_connection()
        if not conn:
            return False
            
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT user_type FROM users WHERE id = %s", (user_id,))
            db_user = cur.fetchone()
        
        conn.close()

        return db_user and db_user['user_type'] == 'client'
        
    except Exception as e:
        print(f"Erro ao verificar se usuário é cliente: {e}")
        return False
