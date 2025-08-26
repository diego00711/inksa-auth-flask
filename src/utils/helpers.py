import os
import psycopg2
from flask import request, jsonify
from supabase import create_client, Client
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicialização do cliente Supabase
try:
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise ValueError("Variáveis de ambiente SUPABASE_URL e SUPABASE_KEY são obrigatórias.")
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Cliente Supabase inicializado com sucesso")
except Exception as e:
    logger.error(f"❌ Falha ao inicializar o cliente Supabase: {e}")
    supabase = None

def get_db_connection():
    try:
        conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
        logger.info("✅ Conexão com banco de dados estabelecida com sucesso")
        return conn
    except Exception as e:
        logger.error(f"❌ Falha na conexão com o banco de dados: {e}")
        return None

def get_user_id_from_token(auth_header):
    """
    Valida o token JWT, pega o user_id via Supabase, 
    e confere se está na tabela users.
    """
    if not auth_header or not auth_header.startswith('Bearer '):
        return None, None, (jsonify({"error": "Cabeçalho de autorização inválido"}), 401)

    token = auth_header.split(' ')[1]
    
    try:
        # Obter usuário do Supabase pelo token
        user_response = supabase.auth.get_user(token)
        user = user_response.user
        
        if not user:
            return None, None, (jsonify({"error": "Token inválido ou expirado"}), 401)

        user_id = user.id

        # Conferir se user_id está na tabela users e pegar user_type
        conn = get_db_connection()
        if not conn:
            return None, None, (jsonify({"error": "Erro de conexão com o banco de dados"}), 500)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT user_type FROM users WHERE id = %s", (user_id,))
                result = cur.fetchone()
                if not result:
                    return None, None, (jsonify({"error": "Acesso não autorizado."}), 403)
                user_type = result[0]
        finally:
            conn.close()

        # Se passou, retorna user_id, tipo e None de erro
        return user_id, user_type, None

    except Exception as e:
        logger.error(f"Erro ao decodificar ou validar token: {e}", exc_info=True)
        return None, None, (jsonify({"error": "Erro interno ao processar o token"}), 500)

def get_user_info():
    """
    Extrai informações do usuário a partir do token JWT no header Authorization
    Retorna um dicionário com user_id, email e (opcionalmente) outros dados.
    """
    try:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return None
        token = auth_header.split(' ')[1]
        user_response = supabase.auth.get_user(token)
        user = user_response.user
        if not user:
            return None
        return {
            'user_id': user.id,
            'email': user.email,
        }
    except Exception as e:
        logger.error(f"Erro ao obter informações do usuário: {e}", exc_info=True)
        return None
