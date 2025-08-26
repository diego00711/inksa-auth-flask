# inksa-auth-flask/src/utils/helpers.py (VERSÃO CORRIGIDA E FINAL)

import os
import jwt
import psycopg2
import psycopg2.extras
from flask import request, jsonify
from supabase import create_client, Client
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
    if not auth_header or not auth_header.startswith('Bearer '):
        return jsonify({"error": "Cabeçalho de autorização inválido"}), 401

    token = auth_header.split(' ')[1]
    
    try:
        user_response = supabase.auth.get_user(token)
        user = user_response.user
        
        if not user:
            return jsonify({"error": "Token inválido ou expirado"}), 401

        user_id = user.id
        user_metadata = user.user_metadata or {}
        user_type = user_metadata.get('user_type')

        if not user_type:
            logger.error(f"user_type não encontrado nos metadados para o usuário {user_id}")
            return jsonify({"error": "Tipo de usuário não definido no token"}), 401
        
        # Retorna uma tupla com 2 valores em caso de sucesso
        return user_id, user_type

    except Exception as e:
        logger.error(f"Erro ao decodificar ou validar token: {e}", exc_info=True)
        return jsonify({"error": "Erro interno ao processar o token"}), 500
