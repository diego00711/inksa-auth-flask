# src/utils/helpers.py — VERSÃO ROBUSTA (corrigida, sem uuid)

import os
import json
import uuid
import logging
import psycopg2
import psycopg2.extras
from psycopg2.extras import register_uuid
from flask import jsonify
from supabase import create_client, Client
from datetime import date, datetime, timedelta, time
from decimal import Decimal
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Supabase ---
# ATENÇÃO: existem DOIS clientes propositalmente separados.
#   `supabase`       -> uso geral (dados via PostgREST). Também é usado para
#                       sign_in_with_password / sign_up de usuários, o que
#                       SOBRESCREVE a sessão interna do cliente pela do usuário
#                       logado. Por isso ele NÃO pode ser usado para operações
#                       de admin (auth.admin.*), senão herda o token do último
#                       usuário logado -> erro "not_admin" / "session_not_found".
#   `supabase_admin` -> instância dedicada, service_role, que NUNCA faz
#                       sign_in/sign_up. Use SEMPRE este para auth.admin.*
#                       (delete_user, update_user_by_id, invite_user_by_email…).
supabase: Optional[Client] = None
supabase_admin: Optional[Client] = None
try:
    SUPABASE_URL = os.environ.get("SUPABASE_URL")
    SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise ValueError("SUPABASE_URL e SUPABASE_SERVICE_KEY são obrigatórias.")
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    supabase_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    logger.info("✅ Supabase client inicializado (geral + admin dedicado).")
except Exception as e:
    logger.error(f"❌ Falha ao inicializar Supabase: {e}")
    supabase = None
    supabase_admin = None


# --- DB ---
def get_db_connection():
    url = os.environ.get("DATABASE_URL")
    if not url:
        logger.error("❌ DATABASE_URL não encontrada.")
        return None
    # Timeouts defensivos. SEM eles, uma conexão/consulta travada (TCP
    # meio-aberto na latência cross-continente Oregon<->São Paulo, ou o SELECT
    # do keep-alive) segura o worker gevent único e o Render mata por
    # WORKER TIMEOUT/OOM — derrubando TODA a API (incidente 2026-07-11).
    #   keepalives*      -> detectam socket morto e abortam em ~1min
    #   connect_timeout  -> evita connect pendurado
    #   statement_timeout-> aborta query longa no servidor (30s)
    tcp_kwargs = dict(
        connect_timeout=10,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )
    conn = None
    try:
        conn = psycopg2.connect(url, options="-c statement_timeout=30000", **tcp_kwargs)
    except Exception as e_opt:
        # Alguns poolers (pgbouncer transaction mode) rejeitam 'options' no
        # startup — cai pra conexão sem statement_timeout, mas ainda com os
        # timeouts de TCP (que são o essencial contra o socket travado).
        logger.warning(f"⚠️ DB connect com statement_timeout falhou ({e_opt}); tentando sem.")
        try:
            conn = psycopg2.connect(url, **tcp_kwargs)
        except Exception as e:
            logger.error(f"❌ Conexão DB falhou: {e}", exc_info=True)
            return None
    try:
        register_uuid(None, conn)  # garante suporte a UUID no cursor
    except Exception as e:
        logger.warning(f"⚠️ register_uuid falhou: {e}")
    return conn


# --- Auth helper ---
def _extract_bearer_token(auth_header: str):
    """Extrai o token de um cabeçalho Authorization.
    Aceita:
      - 'Bearer <jwt>'
      - '<jwt>' (sem 'Bearer', comum quando front erra)
    """
    if not auth_header:
        return None
    parts = auth_header.strip().split()
    if len(parts) == 0:
        return None
    if parts[0].lower() == "bearer" and len(parts) >= 2:
        return parts[1]
    # se não veio 'Bearer', mas é um JWT, devolve assim mesmo
    return parts[0]


def get_user_id_from_token(auth_header):
    """
    Retorna (user_id:str, user_type:str|None, error_response|None)
    - Em caso de erro/autorização, o terceiro item é um tuple (json_response, status_code)
    """
    token = _extract_bearer_token(auth_header)
    if not token:
        return None, None, (jsonify({"error": "Authorization ausente ou inválido"}), 401)

    conn = None
    try:
        if not supabase:
            raise RuntimeError("Supabase client não inicializado.")

        # Valida o JWT no Supabase e extrai o user.id (UUID do auth)
        user_resp = supabase.auth.get_user(token)
        user = getattr(user_resp, "user", None)
        if not user:
            return None, None, (jsonify({"error": "Token inválido ou expirado"}), 401)

        user_id = str(user.id)

        conn = get_db_connection()
        if not conn:
            return None, None, (jsonify({"error": "Falha ao conectar para verificar permissões"}), 500)

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # ✅ versão segura: consulta SOMENTE por 'id' (remove OR uuid = %s)
            cur.execute(
                """
                SELECT user_type
                FROM public.users
                WHERE id = %s
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()

            # (Opcional) Fallback: verificar existência no catálogo do Supabase Auth
            if not row:
                try:
                    cur.execute(
                        """
                        SELECT id
                        FROM auth.users
                        WHERE id = %s
                        LIMIT 1
                        """,
                        (user_id,),
                    )
                    auth_row = cur.fetchone()
                    if auth_row:
                        # Usuário existe no auth, mas não tem permissão registrada na sua tabela
                        return None, None, (jsonify({"error": "Permissão não encontrada para este usuário"}), 403)
                except Exception:
                    # Se o role do banco não permite ler auth.users, ignore o fallback
                    pass

        if not row or not row.get("user_type"):
            return None, None, (jsonify({"error": "Permissão não encontrada para este usuário"}), 403)

        return user_id, row["user_type"], None

    except Exception as e:
        msg = str(e)
        logger.error(f"Erro ao processar token: {msg}", exc_info=True)
        if "invalid" in msg.lower() or "jwt" in msg.lower() or "token" in msg.lower():
            return None, None, (jsonify({"error": f"Erro de autenticação: {msg}"}), 401)
        return None, None, (jsonify({"error": "Erro interno ao validar token"}), 500)
    finally:
        if conn:
            conn.close()


def get_user_info():
    """
    Extrai email/id do usuário autenticado a partir do header Authorization
    da requisição Flask atual (contexto ambiente, sem precisar passá-lo).
    Usado pelo audit log (best-effort) para saber qual admin fez a ação.
    """
    from flask import request as _request
    token = _extract_bearer_token(_request.headers.get("Authorization"))
    if not token or not supabase:
        return None
    try:
        user_resp = supabase.auth.get_user(token)
        user = getattr(user_resp, "user", None)
        if not user:
            return None
        return {"id": str(user.id), "email": user.email}
    except Exception:
        return None


# --- JSON utils ---
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date, time)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)


def serialize_data(data):
    return json.loads(json.dumps(data, cls=CustomJSONEncoder))
