# src/utils/helpers.py - VERSÃO CORRIGIDA E LIMPA

import os
import logging
import psycopg2
from psycopg2.extras import register_uuid
from flask import request, jsonify
from supabase import create_client, Client
import json
from datetime import date, datetime, timedelta, time
from decimal import Decimal
import uuid

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Inicialização do Supabase ---
supabase: Client = None
try:
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("As variáveis de ambiente SUPABASE_URL e SUPABASE_SERVICE_KEY são obrigatórias.")
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    logger.info("✅ Cliente Supabase inicializado com sucesso.")
except Exception as e:
    logger.error(f"❌ Falha ao inicializar o cliente Supabase: {e}")
    supabase = None

# --- Funções de Banco de Dados ---
def get_db_connection():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("❌ DATABASE_URL não encontrada.")
        return None
    try:
        conn = psycopg2.connect(database_url)
        register_uuid(conn)
        return conn
    except Exception as e:
        logger.error(f"❌ Falha na conexão com o banco de dados: {e}", exc_info=True)
        return None

# --- Função de Validação de Token ---
def get_user_id_from_token(auth_header):
    if not auth_header or not auth_header.startswith('Bearer '):
        return None, None, (jsonify({"error": "Cabeçalho de autorização inválido"}), 401)

    token = auth_header.split(' ')[1]
    conn = None
    try:
        if not supabase:
            raise Exception("Cliente Supabase não inicializado.")
        
        user_response = supabase.auth.get_user(token)
        user = user_response.user
        if not user:
            return None, None, (jsonify({"error": "Token inválido ou expirado"}), 401)

        user_id = str(user.id)
        
        conn = get_db_connection()
        if not conn:
            return None, None, (jsonify({"error": "Falha na conexão para verificar permissões"}), 500)

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT user_type FROM users WHERE id = %s", (user_id,))
            db_user = cur.fetchone()

        if not db_user or not db_user['user_type']:
            return None, None, (jsonify({"error": "Tipo de usuário não encontrado no banco de dados"}), 403)

        return user_id, db_user['user_type'], None
    except Exception as e:
        logger.error(f"Erro ao processar token: {e}", exc_info=True)
        if "JWT" in str(e) or "Token" in str(e):
             return None, None, (jsonify({"error": f"Erro de autenticação: {e}"}), 401)
        return None, None, (jsonify({"error": "Erro interno ao processar o token"}), 500)
    finally:
        if conn:
            conn.close()

# --- Funções de Serialização ---
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal): return float(obj)
        if isinstance(obj, (datetime, date, time)): return obj.isoformat()
        if isinstance(obj, uuid.UUID): return str(obj)
        return super().default(obj)

def serialize_data(data):
    return json.loads(json.dumps(data, cls=CustomJSONEncoder))
