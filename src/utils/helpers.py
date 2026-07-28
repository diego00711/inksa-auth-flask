# src/utils/helpers.py — VERSÃO ROBUSTA (corrigida, sem uuid)

import os
import json
import uuid
import logging
import jwt  # PyJWT — validação LOCAL do JWT do Supabase (sem bater no Auth remoto)
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
# Timeouts defensivos de conexão. SEM eles, uma conexão/consulta travada (TCP
# meio-aberto na latência cross-continente Oregon<->São Paulo, ou o SELECT do
# keep-alive) segura o worker gevent único e o Render mata por WORKER
# TIMEOUT/OOM — derrubando TODA a API (incidente 2026-07-11).
#   keepalives*      -> detectam socket morto e abortam em ~1min
#   connect_timeout  -> evita connect pendurado
#   statement_timeout-> aborta query longa no servidor (30s)
_DB_TCP_KWARGS = dict(
    connect_timeout=10,
    keepalives=1,
    keepalives_idle=30,
    keepalives_interval=10,
    keepalives_count=5,
)


def connect_hardened(url):
    """Abre uma conexão psycopg2 com os timeouts defensivos acima.

    LEVANTA em falha total (igual ao psycopg2.connect puro) — use onde o
    chamador espera uma conexão de verdade e trata a exceção (ex.: o
    DB_CONN_FACTORY da gamificação). Para o caminho que devolve None em vez
    de levantar, use get_db_connection()."""
    try:
        conn = psycopg2.connect(url, options="-c statement_timeout=30000", **_DB_TCP_KWARGS)
    except Exception as e_opt:
        # Alguns poolers (pgbouncer transaction mode) rejeitam 'options' no
        # startup — cai pra conexão sem statement_timeout, mas ainda com os
        # timeouts de TCP (que são o essencial contra o socket travado).
        logger.warning(f"⚠️ DB connect com statement_timeout falhou ({e_opt}); tentando sem.")
        conn = psycopg2.connect(url, **_DB_TCP_KWARGS)
    try:
        register_uuid(None, conn)  # garante suporte a UUID no cursor
    except Exception as e:
        logger.warning(f"⚠️ register_uuid falhou: {e}")
    return conn


def get_db_connection():
    url = os.environ.get("DATABASE_URL")
    if not url:
        logger.error("❌ DATABASE_URL não encontrada.")
        return None
    try:
        return connect_hardened(url)
    except Exception as e:
        logger.error(f"❌ Conexão DB falhou: {e}", exc_info=True)
        return None


# --- Validação LOCAL do JWT (corta a ida-e-volta cross-continente do Auth) ---
# O Supabase assina os access tokens com este segredo (HS256). Dashboard →
# Settings → API → JWT Settings → "JWT Secret". Com ele setado, validamos o
# token localmente (assinatura + expiração), SEM chamar supabase.auth.get_user()
# (um HTTP pro Auth em São Paulo) a CADA request autenticado. Sem o segredo, o
# código cai no caminho remoto de antes — então é seguro subir antes de configurar.
_SUPABASE_JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET")
if _SUPABASE_JWT_SECRET:
    logger.info("✅ SUPABASE_JWT_SECRET presente — validação de token LOCAL ativa.")
else:
    logger.warning("⚠️ SUPABASE_JWT_SECRET ausente — validando token via Auth REMOTO (mais lento).")


def _verify_jwt_local(token):
    """Valida o JWT do Supabase localmente (HS256 + exp), sem rede.

    Retorna o user_id (claim 'sub') em caso de sucesso, ou None se não der pra
    validar localmente (segredo ausente, assinatura inválida, expirado, sem
    'sub'/'exp', audience diferente) — nesses casos o chamador cai no Auth
    remoto, que é autoritativo. Nunca levanta."""
    if not _SUPABASE_JWT_SECRET or not token:
        return None
    try:
        claims = jwt.decode(
            token,
            _SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
            options={"require": ["exp", "sub"]},
        )
        sub = claims.get("sub")
        return str(sub) if sub else None
    except Exception:
        # Expirado/assinatura errada/aud diferente/etc. — deixa o remoto decidir.
        return None


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
        # 1) Tenta validar o JWT LOCALMENTE (rápido, sem rede). 2) Se não rolar
        #    (sem segredo, expirado, etc.), cai no Auth REMOTO do Supabase, que é
        #    autoritativo mas cross-continente. O caminho local corta uma
        #    ida-e-volta a São Paulo de todo request autenticado.
        user_id = _verify_jwt_local(token)
        if not user_id:
            if not supabase:
                raise RuntimeError("Supabase client não inicializado.")
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
