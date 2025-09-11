# src/utils/helpers.py - VERSÃO FINAL E COMPLETA

import os
import logging
import psycopg2
from psycopg2.extras import register_uuid
from flask import request, jsonify
from functools import wraps
from supabase import create_client, Client
import json
from datetime import date, datetime, timedelta, time
from decimal import Decimal
import uuid

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AUDIT_DEBUG = os.environ.get("AUDIT_DEBUG", "false").lower() in ("true", "1", "yes")

# --- Inicialização do Supabase ---
supabase: Client = None
try:
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("As variáveis de ambiente SUPABASE_URL e SUPABASE_SERVICE_KEY são obrigatórias.")
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    logger.info("✅ Cliente Supabase inicializado com sucesso usando Service Role Key")
except Exception as e:
    logger.error(f"❌ Falha ao inicializar o cliente Supabase: {e}")
    supabase = None

# --- Funções de Banco de Dados ---
def get_db_connection():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("❌ DATABASE_URL não encontrada nas variáveis de ambiente.")
        return None
    try:
        conn = psycopg2.connect(database_url)
        register_uuid(conn)
        logger.info("✅ Conexão com banco de dados estabelecida com sucesso")
        return conn
    except Exception as e:
        logger.error(f"❌ Falha na conexão com o banco de dados: {e}", exc_info=True)
        return None

# ======================================================================
# ✅ FUNÇÃO PRINCIPAL CORRIGIDA
# ======================================================================
def get_user_id_from_token(auth_header):
    """
    Valida o token JWT, extrai o user_id e busca o user_type no banco de dados.
    """
    if request.method == "OPTIONS":
        return None, None, None
        
    if not auth_header or not auth_header.startswith('Bearer '):
        return None, None, (jsonify({"error": "Cabeçalho de autorização inválido"}), 401)

    token = auth_header.split(' ')[1]
    conn = None
    
    try:
        if not supabase:
            raise Exception("Cliente Supabase não inicializado.")

        # 1. Validar o token e obter o usuário do Supabase
        user_response = supabase.auth.get_user(token)
        user = user_response.user
        
        if not user:
            return None, None, (jsonify({"error": "Token inválido ou expirado"}), 401)

        user_id = str(user.id)
        
        # 2. Conectar ao nosso banco de dados para buscar o user_type
        conn = get_db_connection()
        if not conn:
            return None, None, (jsonify({"error": "Falha na conexão com o banco de dados para verificar permissões"}), 500)

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT user_type FROM users WHERE id = %s", (user_id,))
            db_user = cur.fetchone()

        if not db_user or not db_user['user_type']:
            return None, None, (jsonify({"error": "Tipo de usuário (user_type) não encontrado no banco de dados"}), 403)

        user_type = db_user['user_type']
        
        # 3. Retornar os dados corretos
        return user_id, user_type, None

    except Exception as e:
        logger.error(f"Erro ao decodificar token ou buscar permissões: {e}", exc_info=True)
        # Distingue entre erro de token e outros erros
        if "JWT" in str(e) or "Token" in str(e):
             return None, None, (jsonify({"error": f"Erro de autenticação: {e}"}), 401)
        return None, None, (jsonify({"error": "Erro interno ao processar o token"}), 500)
    finally:
        if conn:
            conn.close()

# --- Funções Auxiliares e Decoradores ---

def get_user_info():
    """
    Extrai informações básicas do usuário a partir do token JWT.
    """
    try:
        auth_header = request.headers.get('Authorization')
        if not auth_header or not auth_header.startswith('Bearer '):
            return None
        token = auth_header.split(' ')[1]
        if not supabase:
            raise Exception("Cliente Supabase não inicializado.")
        user_response = supabase.auth.get_user(token)
        user = user_response.user
        return {'user_id': user.id, 'email': user.email} if user else None
    except Exception as e:
        logger.error(f"Erro ao obter informações do usuário: {e}", exc_info=True)
        return None

class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date, timedelta, time)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)

def serialize_data(data):
    return json.loads(json.dumps(data, cls=CustomJSONEncoder))
