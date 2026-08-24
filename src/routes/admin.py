# src/routes/admin.py
import os
import re
import csv
import io
import logging
from functools import wraps

import requests
from flask import Blueprint, request, jsonify, current_app, Response
from flask_cors import CORS
import psycopg2
import psycopg2.extras

from gotrue.errors import AuthApiError

from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase, supabase_admin, _extract_bearer_token
from ..utils.audit import log_admin_action, log_admin_action_auto
from ..utils.email_service import send_email, render_simple
from ..utils.platform_settings import get_settings
from src.extensions import limiter

logger = logging.getLogger(__name__)

admin_bp = Blueprint("admin_bp", __name__)

CORS(
    admin_bp,
    origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        re.compile(r"^https://.*\.vercel\.app$"),
        "https://admin.inksadelivery.com.br",
        "https://clientes.inksadelivery.com.br",
        "https://restaurantes.inksadelivery.com.br",
        "https://entregadores.inksadelivery.com.br",
    ],
    supports_credentials=True,
)

ORDERS_TABLE = "orders"
CLIENTS_TABLE = "client_profiles"
RESTAURANTS_TABLE = "restaurant_profiles"
DELIVERY_TABLE = "delivery_profiles"

# --------- helpers de auth ---------
def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        user_id, user_type, error_response = get_user_id_from_token(auth_header)
        if error_response:
            return error_response
        if user_type != "admin":
            return jsonify({"status": "error", "message": "Acesso não autorizado."}), 403
        return fn(*args, **kwargs)
    return wrapper

# --------- helpers de SQL resilientes (cada select no seu cursor) ---------
def _safe_float(v, default=0.0):
    try:
        return float(v or 0)
    except Exception:
        return default

def _safe_int(v, default=0):
    try:
        return int(v or 0)
    except Exception:
        return default

def _fetchval(conn, sql, params=None, default=None):
    params = params or ()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if not row:
                return default
            return list(row.values())[0] if isinstance(row, dict) else row[0]
    except Exception:
        logger.exception("SQL falhou (fetchval)")
        try: conn.rollback()
        except Exception: pass
        return default

def _fetchrow(conn, sql, params=None):
    params = params or ()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception:
        logger.exception("SQL falhou (fetchrow)")
        try: conn.rollback()
        except Exception: pass
        return None

def _fetchall(conn, sql, params=None):
    params = params or ()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]
    except Exception:
        logger.exception("SQL falhou (fetchall)")
        try: conn.rollback()
        except Exception: pass
        return []

def _HOJE_SP(coluna):
    """Predicado SQL: a coluna (timestamptz) cai no dia de HOJE em São Paulo.

    O banco roda em UTC, então CURRENT_DATE vira o dia seguinte às 21h de SP —
    bem no pico do delivery. Isso zerava os contadores de "hoje" no meio do
    movimento (crítico no painel de TV, que fica aberto direto).
    """
    tz = "America/Sao_Paulo"
    return (f"({coluna} AT TIME ZONE '{tz}')::date "
            f"= (now() AT TIME ZONE '{tz}')::date")


# Espelha a trava do motor de despacho (orders.py `_run_dispatch_tick`, CTE
# `elegiveis`). Estar "online" NÃO é o mesmo que estar apto a receber pedido:
# o entregador liga o botão, o app diz que ele está trabalhando, e ele fica
# fora de todas as ofertas porque falta coordenada ou dado do cadastro. Ele
# espera a tarde inteira sem entender por quê.
#
# ⚠️ A coluna de "está online" é `is_available`. `is_online` existe na tabela
# mas é ESCRITA UMA VEZ no cadastro (auth.py, sempre False) e nunca mais —
# contar por ela dá zero pra sempre.
_GATE_COORD = ("COALESCE(current_lat, latitude) IS NOT NULL "
               "AND COALESCE(current_lng, longitude) IS NOT NULL")
_GATE_CADASTRO = ("NULLIF(TRIM(first_name),'') IS NOT NULL "
                  "AND NULLIF(TRIM(cpf),'') IS NOT NULL "
                  "AND NULLIF(TRIM(vehicle_type),'') IS NOT NULL "
                  "AND (vehicle_type NOT IN ('moto','carro') "
                  "     OR (NULLIF(TRIM(vehicle_plate),'') IS NOT NULL "
                  "         AND NULLIF(TRIM(cnh),'') IS NOT NULL))")
_SQL_ENTREGADORES = f"""
    SELECT
      COUNT(*) FILTER (WHERE _on)::int                                   AS online,
      COUNT(*) FILTER (WHERE _on AND {_GATE_COORD} AND {_GATE_CADASTRO})::int AS aptos,
      COUNT(*) FILTER (WHERE _on AND NOT ({_GATE_COORD}))::int           AS sem_coord,
      COUNT(*) FILTER (WHERE _on AND {_GATE_COORD}
                             AND NOT ({_GATE_CADASTRO}))::int            AS incompletos
      FROM (SELECT *, (is_available IS TRUE AND approved IS TRUE) AS _on
              FROM delivery_profiles) d
"""


def _build_dashboard_payload(conn, date_from=None, date_to=None, limit=10, with_operacao=False):
    # leitura apenas -> autocommit evita “aborted transaction”
    try: conn.autocommit = True
    except Exception: pass

    payload = {
        "kpis": {
            "totalRevenue": 0.0,
            "ordersToday": 0,
            "averageTicket": 0.0,
            "newClientsToday": 0,
            "ordersInProgress": 0,
            "ordersCanceled": 0,
            "restaurantsPending": 0,
            "activeDeliverymen": 0,
            # Receita REAL da plataforma = comissão + margem de frete
            "platformCommission": 0.0,
            "deliveryMargin": 0.0,
            "platformRevenue": 0.0,
        },
        "chartData": [],
        "recentOrders": [],
        "ordersStatus": {},
        "clientsGrowth": [],
    }

    # KPIs
    payload["kpis"]["totalRevenue"] = _safe_float(_fetchval(
        conn, f"SELECT COALESCE(SUM(total_amount),0) FROM {ORDERS_TABLE} WHERE status IN ('delivered','completed')", default=0.0))
    payload["kpis"]["averageTicket"] = _safe_float(_fetchval(
        conn, f"SELECT COALESCE(AVG(total_amount),0) FROM {ORDERS_TABLE} WHERE status IN ('delivered','completed')", default=0.0))
    payload["kpis"]["ordersToday"] = _safe_int(_fetchval(
        conn, f"SELECT COUNT(*)::int FROM {ORDERS_TABLE} WHERE {_HOJE_SP('created_at')}", default=0))
    payload["kpis"]["newClientsToday"] = _safe_int(_fetchval(
        conn, f"SELECT COUNT(*)::int FROM {CLIENTS_TABLE} WHERE {_HOJE_SP('created_at')}", default=0))

    row = _fetchrow(conn, f"""
        SELECT
          SUM(CASE WHEN status IN ('preparing','on_the_way','in_progress') THEN 1 ELSE 0 END)::int AS in_progress,
          SUM(CASE WHEN status IN ('cancelled','canceled') THEN 1 ELSE 0 END)::int AS canceled
        FROM {ORDERS_TABLE}
    """) or {}
    payload["kpis"]["ordersInProgress"] = _safe_int(row.get("in_progress"))
    payload["kpis"]["ordersCanceled"]   = _safe_int(row.get("canceled"))

    # IS NOT TRUE (nao IS FALSE): as colunas aceitam NULL, e um restaurante com
    # approved NULL esta esperando aprovacao igual aos outros.
    #
    # DESATIVADO NAO ESPERA APROVACAO. Sem o filtro de active, os 4 parceiros
    # de teste que o Diego desativou em 18/08 continuaram aparecendo como
    # "aguardando aprovacao" no painel — alerta que ninguem pode resolver, e
    # que treina a pessoa a ignorar o contador.
    payload["kpis"]["restaurantsPending"] = _safe_int(_fetchval(
        conn,
        f"""SELECT COUNT(*)::int FROM {RESTAURANTS_TABLE}
             WHERE ((approved IS NOT TRUE) OR (status='pending'))
               AND COALESCE(active, TRUE) = TRUE""",
        default=0))
    payload["kpis"]["activeDeliverymen"] = _safe_int(_fetchval(
        conn, f"SELECT COUNT(*)::int FROM {DELIVERY_TABLE} WHERE active IS TRUE", default=0))

    # Receita REAL da plataforma (comissão + margem de frete) sobre pedidos
    # concluídos. Mesma janela dos demais KPIs (all-time), pra ficar coerente
    # com "Receita Total" exibida ao lado. margem_frete pode ser negativa.
    rev_row = _fetchrow(conn, f"""
        SELECT COALESCE(SUM(comissao_plataforma),0) AS commission,
               COALESCE(SUM(margem_frete),0)        AS margin
          FROM {ORDERS_TABLE}
         WHERE status IN ('delivered','completed')
    """) or {}
    _commission = _safe_float(rev_row.get("commission"))
    _margin = _safe_float(rev_row.get("margin"))
    payload["kpis"]["platformCommission"] = _commission
    payload["kpis"]["deliveryMargin"] = _margin
    payload["kpis"]["platformRevenue"] = round(_commission + _margin, 2)

    # Série receita
    if date_from and date_to:
        chart_rows = _fetchall(conn, f"""
            SELECT to_char(d::date,'DD/MM') AS formatted_date,
                   COALESCE(SUM(o.total_amount),0) AS daily_revenue,
                   (SELECT COUNT(*) FROM {CLIENTS_TABLE} c
                     WHERE (c.created_at AT TIME ZONE 'America/Sao_Paulo')::date <= d::date)::int AS total_clients
              FROM generate_series(%s::date, %s::date, '1 day') AS d
         LEFT JOIN {ORDERS_TABLE} o
                ON (o.created_at AT TIME ZONE 'America/Sao_Paulo')::date = d::date
               AND o.status IN ('delivered','completed')
          GROUP BY d ORDER BY d
        """, (date_from, date_to))
    else:
        chart_rows = _fetchall(conn, f"""
            WITH hoje AS (
              SELECT (now() AT TIME ZONE 'America/Sao_Paulo')::date AS d0
            ), days AS (
              SELECT generate_series(d0 - INTERVAL '6 day', d0, INTERVAL '1 day')::date AS d
                FROM hoje
            )
            SELECT to_char(d,'DD/MM') AS formatted_date,
                   COALESCE((
                     SELECT SUM(o.total_amount)
                       FROM {ORDERS_TABLE} o
                      WHERE o.status IN ('delivered','completed')
                        AND (o.created_at AT TIME ZONE 'America/Sao_Paulo')::date = d
                   ),0) AS daily_revenue,
                   (SELECT COUNT(*) FROM {CLIENTS_TABLE} c
                     WHERE (c.created_at AT TIME ZONE 'America/Sao_Paulo')::date <= d)::int AS total_clients
              FROM days ORDER BY d
        """)
    for r in chart_rows:
        r["daily_revenue"] = _safe_float(r.get("daily_revenue"))
        r["total_clients"] = _safe_int(r.get("total_clients"))
    payload["chartData"] = chart_rows

    # Recentes
    params, where = [], []
    if date_from:
        where.append("(o.created_at AT TIME ZONE 'America/Sao_Paulo')::date >= %s"); params.append(date_from)
    if date_to:
        where.append("(o.created_at AT TIME ZONE 'America/Sao_Paulo')::date <= %s"); params.append(date_to)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    # client_name/restaurant_name NÃO existem em orders — vêm dos perfis via
    # JOIN (senão caía sempre no fallback "Cliente"/"Restaurante").
    recent_rows = _fetchall(conn, f"""
        SELECT o.id,
               NULLIF(TRIM(CONCAT_WS(' ', cp.first_name, cp.last_name)), '') AS client_name,
               rp.restaurant_name AS restaurant_name,
               o.total_amount, o.status, o.created_at,
               o.payment_method,
               o.comissao_plataforma, o.margem_frete
          FROM {ORDERS_TABLE} o
          LEFT JOIN client_profiles cp     ON o.client_id = cp.id
          LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
        {where_sql}
      ORDER BY o.created_at DESC
         LIMIT %s
    """, (*params, limit))
    payload["recentOrders"] = [{
        "id": str(r.get("id")),
        "client_name": r.get("client_name") or "Cliente",
        "restaurant_name": r.get("restaurant_name") or "Restaurante",
        "total_amount": _safe_float(r.get("total_amount")),
        "payment_method": r.get("payment_method") or "—",
        "platform_commission": _safe_float(r.get("comissao_plataforma")),
        "delivery_margin": _safe_float(r.get("margem_frete")),
        "status": r.get("status") or "desconhecido",
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
    } for r in recent_rows]

    # Status
    status_rows = _fetchall(conn, f"SELECT status, COUNT(*)::int AS c FROM {ORDERS_TABLE} GROUP BY status")
    payload["ordersStatus"] = {(r.get("status") or "desconhecido"): _safe_int(r.get("c")) for r in status_rows}

    # Crescimento clientes
    payload["clientsGrowth"] = _fetchall(conn, f"""
        WITH hoje AS (
          SELECT (now() AT TIME ZONE 'America/Sao_Paulo')::date AS d0
        ), days AS (
          SELECT generate_series(d0 - INTERVAL '6 day', d0, INTERVAL '1 day')::date AS d
            FROM hoje
        )
        SELECT to_char(d,'DD/MM') AS formatted_date,
               COALESCE((SELECT COUNT(*) FROM {CLIENTS_TABLE} c
                          WHERE (c.created_at AT TIME ZONE 'America/Sao_Paulo')::date <= d),0)::int AS total_clients
          FROM days ORDER BY d
    """)

    # --------- Operação: o que precisa de AÇÃO agora ---------
    # KPI é retrovisor; isto é a lista de pendências. Cada item vira um card
    # clicável no dashboard que leva direto pra página onde se resolve.
    # Os helpers _fetch* já engolem erro e devolvem default, então uma tabela
    # que mude de schema derruba só o próprio número, não o dashboard.
    # `with_operacao=False` por padrão: /metrics, /revenue-series e
    # /transactions NÃO usam este bloco, e o backend tem 1 worker com o banco
    # do outro lado do continente — cada query é uma ida-e-volta cara. Só
    # /overview e /tv/stats (que a TV do escritório chama a cada 30s, 24/7)
    # pedem o bloco.
    if with_operacao:
        # TUDO num round-trip só. Antes eram 9 queries separadas; multiplicado
        # pelo polling da TV, virava carga constante no worker único.
        row = _fetchrow(conn, f"""
            SELECT
              (SELECT COUNT(*)::int FROM payouts
                WHERE status IN ('pending','processing'))                    AS repasses,
              (SELECT COALESCE(SUM(COALESCE(total_net, amount)),0) FROM payouts
                WHERE status IN ('pending','processing'))                    AS repasses_valor,
              (SELECT COUNT(*)::int FROM delivery_incidents
                WHERE resolved_at IS NULL)                                   AS ocorrencias,
              (SELECT COUNT(*)::int FROM support_tickets
                WHERE status IN ('open','pending','in_progress'))            AS tickets,
              -- Dinheiro que a plataforma tem A RECEBER (pedido em dinheiro).
              (SELECT COALESCE(SUM(commission_debt),0) FROM {RESTAURANTS_TABLE}
                WHERE COALESCE(commission_debt,0) > 0)                       AS divida_parceiros,
              (SELECT COALESCE(SUM(cash_debt),0) FROM {DELIVERY_TABLE}
                WHERE COALESCE(cash_debt,0) > 0)                             AS divida_entregadores,
              -- Loja aberta agora: zero em horário de pico = nada entra, e o
              -- motivo não aparecia em lugar nenhum.
              (SELECT COUNT(*)::int FROM {RESTAURANTS_TABLE}
                WHERE is_open IS TRUE AND approved IS TRUE AND active IS TRUE) AS lojas_abertas,
              (SELECT COUNT(*)::int FROM {RESTAURANTS_TABLE}
                WHERE approved IS TRUE AND active IS TRUE)                   AS lojas_aprovadas,
              -- CLIENTE: "online" aqui não é um botão que ele aperta (não
              -- existe); é presença — app aberto batendo heartbeat.
              (SELECT COUNT(*)::int FROM {CLIENTS_TABLE}
                WHERE last_seen > NOW() - INTERVAL '5 minutes')              AS clientes_online,
              (SELECT COUNT(*)::int FROM {CLIENTS_TABLE}
                WHERE (last_seen AT TIME ZONE 'America/Sao_Paulo')::date
                      = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date)      AS clientes_hoje,
              -- CARRINHO PARADO: montou o pedido e não finalizou. É o número
              -- que denuncia atrito no checkout ANTES de alguém reclamar —
              -- cliente que desiste não abre ticket, só some.
              --
              -- TETO DE 48h. Antes só havia piso (15 min), e carrinho de 5 dias
              -- ficava no painel pra sempre — o alerta de "precisa de você"
              -- enchia de entulho que não é trabalho de ninguém, e número que
              -- nunca zera é número que se aprende a ignorar. A janela é a de
              -- quem ainda dá pra recuperar; a lista completa, com idade e sem
              -- corte, fica em /api/admin/carrinhos.
              (SELECT COUNT(*)::int FROM {CLIENTS_TABLE}
                WHERE COALESCE(cart_items_count, 0) > 0
                  AND cart_updated_at <  NOW() - INTERVAL '15 minutes'
                  AND cart_updated_at >= NOW() - INTERVAL '48 hours')        AS carrinhos_parados,
              (SELECT COALESCE(SUM(cart_value), 0) FROM {CLIENTS_TABLE}
                WHERE COALESCE(cart_items_count, 0) > 0
                  AND cart_updated_at <  NOW() - INTERVAL '15 minutes'
                  AND cart_updated_at >= NOW() - INTERVAL '48 hours')        AS carrinhos_valor
        """) or {}
        ent = _fetchrow(conn, _SQL_ENTREGADORES) or {}

        payload["operacao"] = {
            "parceirosPendentes":        payload["kpis"]["restaurantsPending"],
            "repassesPendentes":         _safe_int(row.get("repasses")),
            "repassesValor":             _safe_float(row.get("repasses_valor")),
            "ocorrenciasAbertas":        _safe_int(row.get("ocorrencias")),
            "ticketsAbertos":            _safe_int(row.get("tickets")),
            "dividaParceiros":           _safe_float(row.get("divida_parceiros")),
            "dividaEntregadores":        _safe_float(row.get("divida_entregadores")),
            "lojasAbertas":              _safe_int(row.get("lojas_abertas")),
            "lojasAprovadas":            _safe_int(row.get("lojas_aprovadas")),
            "entregadoresOnline":        _safe_int(ent.get("online")),
            "entregadoresAptos":         _safe_int(ent.get("aptos")),
            "entregadoresSemCoordenada": _safe_int(ent.get("sem_coord")),
            "entregadoresIncompletos":   _safe_int(ent.get("incompletos")),
            "clientesOnline":            _safe_int(row.get("clientes_online")),
            "clientesHoje":              _safe_int(row.get("clientes_hoje")),
            "carrinhosParados":          _safe_int(row.get("carrinhos_parados")),
            "carrinhosValor":            _safe_float(row.get("carrinhos_valor")),
        }

    return payload

# --------- Auth ---------
@admin_bp.route("/login", methods=["POST"])
@limiter.limit("10 per minute")
def admin_login():
    data = request.get_json() or {}
    email = data.get("email")
    password = data.get("password")
    if not email or not password:
        return jsonify({"status": "error", "message": "Email e senha são obrigatórios"}), 400

    try:
        response = supabase.auth.sign_in_with_password({"email": email, "password": password})
        user = response.user

        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Falha na conexão com a base de dados."}), 500

        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT user_type FROM users WHERE id = %s", (str(user.id),))
            db_user = cur.fetchone()

        if not db_user or db_user["user_type"] != "admin":
            supabase.auth.sign_out()
            return jsonify({"status": "error", "message": "Acesso permitido apenas a administradores."}), 403

        log_admin_action(user.email, "Login", "Admin login successful", request)

        return jsonify({
            "status": "success",
            "message": "Login de administrador realizado",
            "access_token": response.session.access_token,
            # Sem o refresh_token o front não consegue renovar a sessão e o
            # admin caía no login quando o access_token expirava (~1h). Isso
            # inviabilizava o painel de TV, que fica aberto 24/7.
            "refresh_token": response.session.refresh_token,
            "data": {"user": {"id": user.id, "email": user.email, "user_type": db_user["user_type"]}},
        }), 200
    except AuthApiError:
        return jsonify({"status": "error", "message": "Credenciais inválidas"}), 401
    except Exception as e:
        logger.exception("Erro no admin_login")
        return jsonify({"status": "error", "message": f"Erro inesperado: {str(e)}"}), 500


# A renovação de sessão vive em POST /api/auth/refresh (auth.py), que é o login
# usado pelos 4 apps — não duplicar aqui.

@admin_bp.route("/logout", methods=["POST"])
@admin_required
def admin_logout():
    try:
        from ..utils.audit import log_admin_action_auto
        log_admin_action_auto("Logout", "Admin logout")
        supabase.auth.sign_out()
        return jsonify({"status": "success", "message": "Logout realizado com sucesso"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": f"Erro durante logout: {str(e)}"}), 500

# --------- Users / Restaurants ---------
@admin_bp.route("/users", methods=["GET"])
@admin_required
def get_all_users():
    filter_user_type = request.args.get("user_type")
    filter_city = request.args.get("city")

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            params, where = [], []
            sql = """
                SELECT
                    u.id, u.email, u.user_type, u.created_at, u.is_active,
                    COALESCE(cp.first_name || ' ' || cp.last_name,
                             rp.restaurant_name,
                             dp.first_name || ' ' || dp.last_name) AS full_name,
                    COALESCE(cp.address_city, rp.address_city, dp.address_city) AS city,
                    COALESCE(cp.phone, rp.phone, dp.phone) AS phone,
                    COALESCE(rp.fundador, false) AS fundador,
                    COALESCE(dp.approved, false) AS courier_approved
                FROM users u
                LEFT JOIN client_profiles   cp ON u.id = cp.user_id AND u.user_type = 'client'
                LEFT JOIN restaurant_profiles rp ON u.id = rp.user_id AND u.user_type = 'restaurant'
                LEFT JOIN delivery_profiles   dp ON u.id = dp.user_id AND u.user_type = 'delivery'
            """
            if filter_user_type and filter_user_type.lower() != "todos":
                where.append("u.user_type = %s"); params.append(filter_user_type)
            if filter_city:
                where.append("COALESCE(cp.address_city, rp.address_city, dp.address_city) ILIKE %s")
                params.append(f"%{filter_city}%")
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY u.created_at DESC;"
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"status": "success", "data": rows}), 200
    except Exception as e:
        logger.exception("Erro em get_all_users")
        return jsonify({"status": "error", "message": "Erro interno ao buscar usuários.", "detail": str(e)}), 500
    finally:
        conn.close()

@admin_bp.route("/restaurants", methods=["GET"])
@admin_required
def get_all_restaurants():
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT rp.*, u.created_at,
                       COALESCE(rr.avg_rating, 0)::float AS average_rating,
                       COALESCE(rr.review_count, 0)::int AS total_reviews,
                       up.total_points AS gamification_points,
                       l.level_name AS gamification_level
                  FROM restaurant_profiles rp
                  JOIN users u ON rp.user_id = u.id
                  LEFT JOIN (
                      SELECT restaurant_id, AVG(rating) AS avg_rating, COUNT(*) AS review_count
                        FROM restaurant_reviews
                       GROUP BY restaurant_id
                  ) rr ON rr.restaurant_id = rp.id
                  LEFT JOIN public.user_points up ON up.user_id = rp.id
                  LEFT JOIN public.levels l ON l.level_number = up.current_level
              ORDER BY u.created_at DESC;
            """)
            rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"status": "success", "data": rows}), 200
    except Exception as e:
        logger.exception("Erro em get_all_restaurants")
        return jsonify({"status": "error", "message": "Erro interno ao buscar restaurantes.", "detail": str(e)}), 500
    finally:
        conn.close()


def _send_restaurant_welcome_email(to_email: str, restaurant_name: str) -> None:
    """E-mail de boas-vindas quando o restaurante é aprovado: explica o app, a
    comissão (puxada do platform_settings, nunca desatualiza) e os repasses.
    Nunca levanta — o chamador envolve em try/except pra não afetar a aprovação."""
    from decimal import Decimal
    try:
        rate = Decimal(str(get_settings().get("commission_rate") or "0.15"))
    except Exception:
        rate = Decimal("0.15")
    pct = (f"{rate * 100:.2f}".rstrip("0").rstrip(".")).replace(".", ",") + "%"
    nome = (restaurant_name or "Seu restaurante").strip()

    body = f"""
      <p>Boa notícia! O seu restaurante <strong>{nome}</strong> foi <strong>aprovado</strong>
      e já aparece para os clientes no app Inksa. 🚀</p>

      <div style="background:#FFF4EC;border-left:4px solid #FF6B35;padding:12px 16px;border-radius:8px;margin:18px 0">
        <strong>Sem mensalidade.</strong> Você só paga quando vende — nada de taxa fixa no fim do mês.
      </div>

      <p><strong>Como funciona</strong><br>
      Os pedidos chegam no app <strong>Inksa Restaurante</strong>: você aceita → prepara →
      o entregador retira → o cliente acompanha a entrega em tempo real. Você controla
      quando fica <strong>aberto/fechado</strong>.</p>

      <p><strong>Comissão de {pct}</strong><br>
      A Inksa cobra <strong>{pct} sobre o valor dos itens</strong> de cada pedido. O
      <strong>frete é pago à parte pelo cliente e vai integral para o entregador</strong> —
      não sai da sua comissão.</p>

      <p><strong>Repasses</strong><br>
      São <strong>semanais e automáticos, via PIX</strong> na chave que você cadastrar.
      Você recebe o valor dos pedidos do período menos a comissão. Acompanhe tudo na aba
      <strong>Financeiro</strong> do app.</p>

      <p><strong>Primeiros passos</strong><br>
      1) Cadastre sua <strong>chave PIX</strong> (é como você recebe)<br>
      2) Monte o <strong>cardápio</strong> (fotos e preços)<br>
      3) Configure os <strong>horários</strong><br>
      4) <strong>Abra</strong> o restaurante para começar a vender</p>

      <p>Qualquer dúvida, responda este e-mail ou fale com
      <strong>suporte@inksadelivery.com.br</strong>. Bem-vindo(a) à Inksa! 🧡</p>
    """
    html = render_simple(
        title=f"{nome} foi aprovado! 🎉",
        body_html=body,
        cta_text="Acessar meu painel",
        cta_url="https://restaurantes.inksadelivery.com.br",
    )
    send_email(
        to=to_email,
        subject=f"🎉 {nome} foi aprovado na Inksa Delivery!",
        html=html,
        reply_to="suporte@inksadelivery.com.br",
    )


@admin_bp.route("/couriers/<uuid:user_id>/approve", methods=["POST"])
@admin_required
def set_courier_approval(user_id):
    """Aprova ou reprova um entregador (delivery_profiles.approved). Entregador
    não aprovado NÃO recebe pedidos (o gate em get_available_orders bloqueia).
    Body opcional: {"approved": bool} — default true. Chaveado por user_id."""
    data = request.get_json(silent=True) or {}
    approved = bool(data.get("approved", True))
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """UPDATE delivery_profiles
                      SET approved = %s, updated_at = NOW()
                    WHERE user_id = %s
                RETURNING user_id, approved""",
                (approved, str(user_id)),
            )
            row = cur.fetchone()
        if not row:
            conn.rollback()
            return jsonify({"status": "error", "message": "Entregador não encontrado"}), 404
        conn.commit()
        try:
            log_admin_action_auto(
                "ApproveCourier" if approved else "UnapproveCourier",
                f"{'Aprovou' if approved else 'Reprovou'} o entregador {user_id}",
            )
        except Exception:
            pass
        return jsonify({
            "status": "success",
            "message": "Entregador aprovado." if approved else "Aprovação removida.",
            "data": {"approved": approved},
        }), 200
    except Exception as e:
        if conn:
            conn.rollback()
        return jsonify({"status": "error", "message": "Erro ao aprovar entregador.", "detail": str(e)}), 500
    finally:
        if conn:
            conn.close()


@admin_bp.route("/restaurants/<uuid:restaurant_id>/approve", methods=["POST"])
@admin_required
def set_restaurant_approval(restaurant_id):
    """Aprova ou reprova um restaurante. Controla a visibilidade pública
    (o cardápio público filtra por approved). Body opcional: {"approved": bool}
    — default true. Reprovar (approved=false) some o restaurante do app do cliente."""
    data = request.get_json(silent=True) or {}
    approved = bool(data.get("approved", True))
    # Mantém o `status` coerente com o flag: aprovar sai de 'pending' (senão o
    # card "Restaurantes Pendentes" continua contando, pois ele olha
    # approved IS FALSE OR status='pending'). Reprovar volta pra 'pending'.
    new_status = "approved" if approved else "pending"
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Estado anterior — o e-mail de boas-vindas só dispara na TRANSIÇÃO
            # de não-aprovado -> aprovado (não reenvia se reclicar em Aprovar).
            cur.execute("SELECT approved FROM restaurant_profiles WHERE id = %s", (str(restaurant_id),))
            _prev = cur.fetchone()
            was_approved = bool(_prev and _prev["approved"])

            cur.execute(
                """UPDATE restaurant_profiles
                      SET approved = %s, status = %s, updated_at = NOW()
                    WHERE id = %s
                RETURNING id, restaurant_name, approved, status""",
                (approved, new_status, str(restaurant_id)),
            )
            row = cur.fetchone()
        if not row:
            conn.rollback()
            return jsonify({"status": "error", "message": "Restaurante não encontrado"}), 404
        conn.commit()
        try:
            log_admin_action_auto(
                "ApproveRestaurant" if approved else "UnapproveRestaurant",
                f"{'Aprovou' if approved else 'Reprovou'} o restaurante {row['restaurant_name']} ({restaurant_id})",
            )
        except Exception:
            pass
        # E-mail de boas-vindas ao aprovar — nunca bloqueia a resposta da aprovação
        if approved and not was_approved:
            try:
                with conn.cursor() as _ec:
                    _ec.execute(
                        """SELECT u.email FROM public.users u
                             JOIN restaurant_profiles rp ON rp.user_id = u.id
                            WHERE rp.id = %s""",
                        (str(restaurant_id),),
                    )
                    _er = _ec.fetchone()
                _to = _er[0] if _er else None
                if _to:
                    _send_restaurant_welcome_email(_to, row["restaurant_name"])
                else:
                    logger.warning("Aprovação: sem e-mail p/ restaurante %s — boas-vindas não enviado", restaurant_id)
            except Exception:
                logger.exception("Falha ao enviar e-mail de boas-vindas ao restaurante %s", restaurant_id)
        return jsonify({"status": "success", "data": dict(row)}), 200
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("Erro ao aprovar/reprovar restaurante")
        return jsonify({"status": "error", "message": "Erro interno ao atualizar aprovação."}), 500
    finally:
        conn.close()


@admin_bp.route("/restaurants/<uuid:restaurant_id>/founding", methods=["POST"])
@admin_required
def set_restaurant_founding(restaurant_id):
    """Marca/desmarca o restaurante como Parceiro Fundador (campanha: comissão
    pela metade até a data global configurada). Body: {"fundador": bool}."""
    data = request.get_json(silent=True) or {}
    fundador = bool(data.get("fundador", True))
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """UPDATE restaurant_profiles
                      SET fundador = %s,
                          -- A JANELA É DE 6 MESES A PARTIR DA MARCAÇÃO, por
                          -- parceiro. Antes só existia a data global
                          -- (founding_partner_until = 31/01/2027), o que fazia
                          -- a campanha encolher conforme o tempo passava: quem
                          -- entrasse em agosto ganhava 5 meses, em novembro
                          -- ganharia 2. O site promete "6 meses"; o sistema
                          -- passa a cumprir 6 meses pra qualquer um que entre.
                          fundador_desde = CASE WHEN %s THEN COALESCE(fundador_desde, NOW()) ELSE fundador_desde END,
                          fundador_ate   = CASE WHEN %s
                                                THEN (COALESCE(fundador_desde, NOW())::date + INTERVAL '6 months')::date
                                                ELSE fundador_ate END,
                          updated_at = NOW()
                    WHERE id = %s
                RETURNING id, restaurant_name, fundador, fundador_ate""",
                (fundador, fundador, fundador, str(restaurant_id)),
            )
            row = cur.fetchone()
        if not row:
            conn.rollback()
            return jsonify({"status": "error", "message": "Restaurante não encontrado"}), 404
        conn.commit()
        try:
            log_admin_action_auto(
                "SetRestaurantFounding",
                f"{'Marcou' if fundador else 'Removeu'} o selo de Parceiro Fundador do restaurante {row['restaurant_name']} ({restaurant_id})",
            )
        except Exception:
            pass
        return jsonify({"status": "success", "data": dict(row)}), 200
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("Erro ao atualizar selo de fundador")
        return jsonify({"status": "error", "message": "Erro interno ao atualizar fundador."}), 500
    finally:
        conn.close()


@admin_bp.route("/restaurants/<uuid:restaurant_id>", methods=["PUT"])
@admin_required
def update_restaurant(restaurant_id):
    """Edita os dados do restaurante pelo admin (modal 'Editar Restaurante').

    Esta rota nao existia: o front chamava PUT /api/admin/restaurants/<id> e
    tomava 404 ('Endpoint nao encontrado'). So havia GET e /approve.

    Allowlist obrigatoria: o front manda o objeto inteiro do GET (id, user_id,
    latitude, created_at, average_rating de JOIN...) — escrever isso quebraria.
    Mapeia tambem address_postal_code (nome do form) -> address_zipcode (coluna
    real), senao o CEP nao salvaria mesmo com a rota existindo.
    """
    data = request.get_json(silent=True) or {}

    # front -> coluna real. So o que esta aqui pode ser gravado.
    CAMPOS = {
        "restaurant_name": "restaurant_name",
        "cnpj": "cnpj",
        "phone": "phone",
        "address_postal_code": "address_zipcode",
        "address_zipcode": "address_zipcode",
        "address_city": "address_city",
        "address_street": "address_street",
        "address_number": "address_number",
        "address_neighborhood": "address_neighborhood",
        "address_complement": "address_complement",
        "address_state": "address_state",
    }

    sets, params = [], []
    for campo, coluna in CAMPOS.items():
        if campo in data and coluna not in [s.split(" =")[0] for s in sets]:
            sets.append(f"{coluna} = %s")
            v = data.get(campo)
            params.append(v.strip() if isinstance(v, str) else v)

    if not sets:
        return jsonify({"status": "error", "message": "Nenhum campo editável enviado"}), 400

    sets.append("updated_at = NOW()")
    params.append(str(restaurant_id))

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                f"""UPDATE restaurant_profiles
                       SET {", ".join(sets)}
                     WHERE id = %s
                 RETURNING id, restaurant_name""",
                tuple(params),
            )
            row = cur.fetchone()
        if not row:
            conn.rollback()
            return jsonify({"status": "error", "message": "Restaurante não encontrado"}), 404
        conn.commit()
        try:
            log_admin_action_auto(
                "UpdateRestaurant",
                f"Editou o restaurante {row['restaurant_name']} ({restaurant_id})",
            )
        except Exception:
            pass
        return jsonify({"status": "success", "data": dict(row)}), 200
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("Erro ao atualizar restaurante")
        return jsonify({"status": "error", "message": "Erro interno ao atualizar restaurante."}), 500
    finally:
        conn.close()


# --------- Dashboard + rotas de compat ---------
def _is_admin(user_type: str) -> bool:
    return user_type == "admin"

@admin_bp.route("/dashboard", methods=["GET", "OPTIONS"])
def admin_dashboard():
    if request.method == "OPTIONS":
        return jsonify({}), 204
    _, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
    if error: return error
    if not _is_admin(user_type):
        return jsonify({"error": "Acesso negado"}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com banco"}), 500
    try:
        data = _build_dashboard_payload(conn)
        return jsonify(data), 200
    except Exception:
        logger.exception("Erro no /api/admin/dashboard")
        return jsonify({"kpis":{}, "chartData":[], "recentOrders":[], "ordersStatus":{}, "clientsGrowth":[]}), 200
    finally:
        conn.close()

@admin_bp.route("/alerts-summary", methods=["GET", "OPTIONS"])
def admin_alerts_summary():
    """Contadores leves pros AVISOS do admin (sino + badges do menu): tickets de
    suporte não resolvidos (e quantos AGUARDANDO resposta do admin), ocorrências
    de entrega pendentes e restaurantes aguardando aprovação. Uma chamada barata
    (só COUNTs) que o front faz em polling — cada COUNT no seu cursor, então se
    uma tabela não existir o resto ainda responde."""
    if request.method == "OPTIONS":
        return jsonify({}), 204
    _, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
    if error:
        return error
    if not _is_admin(user_type):
        return jsonify({"error": "Acesso negado"}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com banco"}), 500
    try:
        tickets_open = _safe_int(_fetchval(
            conn, "SELECT COUNT(*)::int FROM support_tickets WHERE status <> 'resolvido'", default=0))
        tickets_waiting = _safe_int(_fetchval(
            conn, "SELECT COUNT(*)::int FROM support_tickets WHERE status = 'aguardando'", default=0))
        incidents_pending = _safe_int(_fetchval(
            conn, "SELECT COUNT(*)::int FROM delivery_incidents WHERE resolution = 'pending'", default=0))
        restaurants_pending = _safe_int(_fetchval(
            conn,
            f"""SELECT COUNT(*)::int FROM {RESTAURANTS_TABLE}
                 WHERE ((approved IS NOT TRUE) OR (status='pending'))
                   AND COALESCE(active, TRUE) = TRUE""",
            default=0))
        return jsonify({
            "tickets_open": tickets_open,
            "tickets_waiting": tickets_waiting,
            "incidents_pending": incidents_pending,
            "restaurants_pending": restaurants_pending,
            "total": tickets_open + incidents_pending + restaurants_pending,
        }), 200
    except Exception:
        logger.exception("Erro no /api/admin/alerts-summary")
        return jsonify({"tickets_open": 0, "tickets_waiting": 0,
                        "incidents_pending": 0, "restaurants_pending": 0, "total": 0}), 200
    finally:
        conn.close()

@admin_bp.route("/tv/stats", methods=["GET", "OPTIONS"])
@admin_required
def admin_tv_stats():
    """Painel de TV do escritório (rota /tv do admin).

    Reaproveita o _build_dashboard_payload (KPIs, status e feed) e complementa
    com os recortes que só fazem sentido num painel ao vivo: faturamento de
    HOJE (o payload padrão é all-time) e quem está online AGORA.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 204

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB connection error"}), 500
    try:
        base = _build_dashboard_payload(conn, limit=8, with_operacao=True)
        k = base.get("kpis", {})

        today = _fetchrow(conn, f"""
            SELECT COALESCE(SUM(total_amount),0)        AS revenue,
                   COALESCE(SUM(comissao_plataforma),0) AS commission,
                   COALESCE(SUM(margem_frete),0)        AS margin
              FROM {ORDERS_TABLE}
             WHERE {_HOJE_SP('created_at')}
               AND status IN ('delivered','completed')
        """) or {}
        revenue_today = _safe_float(today.get("revenue"))
        platform_today = round(
            _safe_float(today.get("commission")) + _safe_float(today.get("margin")), 2
        )

        # "Online" não quer dizer que recebe pedido. `aptos` aplica a mesma
        # trava do motor de despacho; a diferença entre os dois é gente que
        # ligou o app e está invisível sem saber.
        ent = _fetchrow(conn, _SQL_ENTREGADORES) or {}
        deliverymen_online = _safe_int(ent.get("online"))
        deliverymen_eligible = _safe_int(ent.get("aptos"))

        restaurants_open = _safe_int(_fetchval(
            conn,
            f"SELECT COUNT(*)::int FROM {RESTAURANTS_TABLE} "
            f"WHERE is_open IS TRUE AND active IS TRUE AND approved IS TRUE",
            default=0))
        restaurants_live = _safe_int(_fetchval(
            conn,
            f"SELECT COUNT(*)::int FROM {RESTAURANTS_TABLE} "
            f"WHERE active IS TRUE AND approved IS TRUE",
            default=0))

        # Placar da base. Antes do lancamento e o unico numero que se move: e o
        # retorno das campanhas de pre-cadastro que aparece aqui, nao em pedidos.
        base_row = _fetchrow(conn, f"""
            SELECT
              (SELECT COUNT(*)::int FROM {RESTAURANTS_TABLE})   AS rest_total,
              (SELECT COUNT(*)::int FROM {DELIVERY_TABLE})      AS deliv_total,
              (SELECT COUNT(*)::int FROM {CLIENTS_TABLE})       AS cli_total,
              (SELECT COUNT(*)::int FROM {RESTAURANTS_TABLE}
                WHERE {_HOJE_SP('created_at')})                 AS rest_hoje,
              (SELECT COUNT(*)::int FROM {DELIVERY_TABLE}
                WHERE {_HOJE_SP('created_at')})                 AS deliv_hoje
        """) or {}

        return jsonify({"status": "success", "data": {
            "ordersToday":          k.get("ordersToday", 0),
            "ordersInProgress":     k.get("ordersInProgress", 0),
            "revenueToday":         revenue_today,
            "platformRevenueToday": platform_today,
            "revenueTotal":         k.get("totalRevenue", 0.0),
            "platformRevenueTotal": k.get("platformRevenue", 0.0),
            "deliverymenOnline":    deliverymen_online,
            "deliverymenEligible":  deliverymen_eligible,
            "deliverymenNoCoord":   _safe_int(ent.get("sem_coord")),
            "deliverymenIncomplete": _safe_int(ent.get("incompletos")),
            "restaurantsOpen":      restaurants_open,
            "restaurantsLive":      restaurants_live,

            # Pendências — o mesmo bloco do dashboard, pra TV virar painel de
            # status e não só placar. Reaproveita o `operacao` já calculado.
            "pendencias":           base.get("operacao", {}),

            # Base cadastrada (o placar do pre-lancamento)
            "restaurantsTotal":     _safe_int(base_row.get("rest_total")),
            "restaurantsPending":   k.get("restaurantsPending", 0),
            "restaurantsToday":     _safe_int(base_row.get("rest_hoje")),
            "deliverymenTotal":     _safe_int(base_row.get("deliv_total")),
            "deliverymenToday":     _safe_int(base_row.get("deliv_hoje")),
            "clientsTotal":         _safe_int(base_row.get("cli_total")),
            "clientsToday":         k.get("newClientsToday", 0),

            "chartData":            base.get("chartData", []),
            "ordersStatus":         base.get("ordersStatus", {}),
            "recentOrders":         base.get("recentOrders", []),
        }}), 200
    except Exception:
        logger.exception("Erro no /api/admin/tv/stats")
        return jsonify({"status": "error", "message": "Erro ao montar o painel"}), 500
    finally:
        conn.close()


@admin_bp.route("/overview", methods=["GET", "OPTIONS"])
@admin_required
def admin_overview():
    """Dashboard inteiro numa chamada só.

    /metrics, /revenue-series e /transactions montam o MESMO payload cada um
    por sua conta — o conjunto inteiro de queries rodava 3x por carregamento.

    ATENÇÃO ao mexer aqui: existe um `analytics_admin.py` com uma cópia
    paralela destas rotas, mas ele é registrado em **/api/analytics**, não em
    /api/admin. O front do admin fala com ESTE arquivo.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 204
    date_from = request.args.get("from")
    date_to   = request.args.get("to")
    limit     = int(request.args.get("limit", 20))
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB connection error"}), 500
    try:
        p = _build_dashboard_payload(conn, date_from, date_to, limit, with_operacao=True)
        return jsonify({"status": "success", "data": {
            "kpis": p["kpis"],
            "chartData": p["chartData"],
            "recentOrders": p["recentOrders"],
            # ordersStatus mora FORA de kpis. O front lia kpis.ordersStatus, que
            # nunca existiu — era por isso que a rosca "Pedidos por Status"
            # jamais aparecia. Aqui vai no nível certo.
            "ordersStatus": p["ordersStatus"],
            "clientsGrowth": p["clientsGrowth"],
            "operacao": p.get("operacao", {}),
        }}), 200
    finally:
        conn.close()


@admin_bp.route("/metrics", methods=["GET", "OPTIONS"])
@admin_required
def admin_metrics():
    if request.method == "OPTIONS":
        return jsonify({}), 204
    date_from = request.args.get("from")
    date_to   = request.args.get("to")
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB connection error"}), 500
    try:
        data = _build_dashboard_payload(conn, date_from, date_to)
        return jsonify({"status": "success", "data": data["kpis"]}), 200
    finally:
        conn.close()

@admin_bp.route("/revenue-series", methods=["GET", "OPTIONS"])
@admin_required
def admin_revenue_series():
    if request.method == "OPTIONS":
        return jsonify({}), 204
    date_from = request.args.get("from")
    date_to   = request.args.get("to")
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB connection error"}), 500
    try:
        data = _build_dashboard_payload(conn, date_from, date_to)
        return jsonify({"status": "success", "data": data["chartData"]}), 200
    finally:
        conn.close()

@admin_bp.route("/reports/export", methods=["GET", "OPTIONS"])
@admin_required
def admin_reports_export():
    """CSV dos pedidos do período (from/to em YYYY-MM-DD, dias no fuso de SP).

    O botão "Exportar CSV" da tela de Relatórios chamava esta rota desde
    sempre, mas ela nunca existiu — dava 404 "Endpoint não encontrado".
    Formato pensado pro Excel pt-BR: separador ';', BOM UTF-8 (acentos) e
    valores monetários com vírgula decimal.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 204
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    # request.args.get("scope"): reservado; hoje só existe o escopo 'orders'.

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB connection error"}), 500
    try:
        where, params = ["1=1"], []
        if date_from:
            where.append("(o.created_at AT TIME ZONE 'America/Sao_Paulo')::date >= %s")
            params.append(date_from)
        if date_to:
            where.append("(o.created_at AT TIME ZONE 'America/Sao_Paulo')::date <= %s")
            params.append(date_to)

        rows = _fetchall(conn, f"""
            SELECT o.id::text AS id,
                   to_char(o.created_at AT TIME ZONE 'America/Sao_Paulo', 'DD/MM/YYYY HH24:MI') AS criado_em,
                   COALESCE(rp.restaurant_name, '')                       AS restaurante,
                   TRIM(COALESCE(dp.first_name,'') || ' ' || COALESCE(dp.last_name,'')) AS entregador,
                   o.status,
                   COALESCE(o.status_pagamento, '')                       AS status_pagamento,
                   COALESCE(o.payment_method, '')                         AS forma_pagamento,
                   COALESCE(o.total_amount_items, 0)                      AS total_itens,
                   COALESCE(o.delivery_fee, 0)                            AS frete,
                   COALESCE(o.total_amount, 0)                            AS total,
                   COALESCE(o.comissao_plataforma, 0)                     AS comissao_plataforma,
                   COALESCE(o.valor_repassado_restaurante, 0)             AS repasse_restaurante,
                   COALESCE(o.valor_repassado_entregador, 0)              AS repasse_entregador,
                   COALESCE(o.margem_frete, 0)                            AS margem_frete
              FROM {ORDERS_TABLE} o
              LEFT JOIN {RESTAURANTS_TABLE} rp ON rp.id = o.restaurant_id
              LEFT JOIN {DELIVERY_TABLE}    dp ON dp.id = o.delivery_id
             WHERE {" AND ".join(where)}
             ORDER BY o.created_at
        """, tuple(params))

        money_cols = {"total_itens", "frete", "total", "comissao_plataforma",
                      "repasse_restaurante", "repasse_entregador", "margem_frete"}
        headers = ["id", "criado_em", "restaurante", "entregador", "status",
                   "status_pagamento", "forma_pagamento", "total_itens", "frete",
                   "total", "comissao_plataforma", "repasse_restaurante",
                   "repasse_entregador", "margem_frete"]

        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        writer.writerow(headers)
        for r in rows:
            writer.writerow([
                (f"{float(r.get(h) or 0):.2f}".replace(".", ",") if h in money_cols else r.get(h, ""))
                for h in headers
            ])

        fname = f"relatorio_{date_from or 'inicio'}_{date_to or 'hoje'}.csv"
        return Response(
            "" + buf.getvalue(),  # BOM: Excel pt-BR abre os acentos certos
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={fname}"},
        )
    except Exception:
        logger.exception("Erro ao gerar o export CSV")
        return jsonify({"status": "error", "message": "Erro ao gerar o CSV"}), 500
    finally:
        conn.close()


@admin_bp.route("/user-metrics", methods=["GET", "OPTIONS"])
@admin_required
def admin_user_metrics():
    """Métricas agregadas de usuários: totais por tipo, ativos, novos cadastros."""
    if request.method == "OPTIONS":
        return jsonify({}), 204
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB connection error"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Tipo de usuário vem de public.users (fonte autoritativa usada no
            # resto do app); auth.users.raw_user_meta_data->>'user_type' é NULL
            # para alguns cadastros (ex.: admins), o que zerava o card.
            cur.execute("""
                SELECT
                  COUNT(*) AS total,
                  COUNT(*) FILTER (WHERE COALESCE(pu.user_type, au.raw_user_meta_data->>'user_type') = 'client') AS clientes,
                  COUNT(*) FILTER (WHERE COALESCE(pu.user_type, au.raw_user_meta_data->>'user_type') = 'restaurant') AS restaurantes,
                  COUNT(*) FILTER (WHERE COALESCE(pu.user_type, au.raw_user_meta_data->>'user_type') = 'delivery') AS entregadores,
                  COUNT(*) FILTER (WHERE COALESCE(pu.user_type, au.raw_user_meta_data->>'user_type') = 'admin') AS admins,
                  COUNT(*) FILTER (WHERE au.last_sign_in_at > NOW() - INTERVAL '15 minutes') AS online_agora,
                  COUNT(*) FILTER (WHERE au.last_sign_in_at > NOW() - INTERVAL '24 hours') AS ativos_24h,
                  COUNT(*) FILTER (WHERE au.last_sign_in_at > NOW() - INTERVAL '7 days') AS ativos_7d,
                  COUNT(*) FILTER (WHERE au.created_at > NOW() - INTERVAL '24 hours') AS novos_24h,
                  COUNT(*) FILTER (WHERE au.created_at > NOW() - INTERVAL '7 days') AS novos_7d,
                  COUNT(*) FILTER (WHERE au.created_at > NOW() - INTERVAL '30 days') AS novos_30d
                FROM auth.users au
                LEFT JOIN public.users pu ON pu.id = au.id
                WHERE au.deleted_at IS NULL;
            """)
            totals = dict(cur.fetchone())

            cur.execute("""
                SELECT
                  TO_CHAR((created_at AT TIME ZONE 'America/Sao_Paulo')::date, 'YYYY-MM-DD') AS dia,
                  TO_CHAR((created_at AT TIME ZONE 'America/Sao_Paulo')::date, 'DD/MM') AS rotulo,
                  COUNT(*) AS qtd
                FROM auth.users
                WHERE deleted_at IS NULL
                  AND created_at > NOW() - INTERVAL '14 days'
                GROUP BY 1, 2
                ORDER BY 1 ASC;
            """)
            serie = [dict(r) for r in cur.fetchall()]

            cur.execute("""
                SELECT
                  au.email,
                  COALESCE(pu.user_type, au.raw_user_meta_data->>'user_type') AS tipo,
                  TO_CHAR(au.created_at AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD"T"HH24:MI:SS') AS criado_em,
                  TO_CHAR(au.last_sign_in_at AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD"T"HH24:MI:SS') AS ultimo_login
                FROM auth.users au
                LEFT JOIN public.users pu ON pu.id = au.id
                WHERE au.deleted_at IS NULL
                ORDER BY au.created_at DESC
                LIMIT 10;
            """)
            recentes = [dict(r) for r in cur.fetchall()]

            return jsonify({
                "status": "success",
                "data": {
                    "totals": totals,
                    "registros_por_dia": serie,
                    "cadastros_recentes": recentes,
                }
            }), 200
    except Exception:
        logger.exception("Erro em admin_user_metrics")
        return jsonify({"status": "error", "message": "Erro ao buscar métricas"}), 500
    finally:
        conn.close()


@admin_bp.route("/transactions", methods=["GET", "OPTIONS"])
@admin_required
def admin_transactions():
    if request.method == "OPTIONS":
        return jsonify({}), 204
    date_from = request.args.get("from")
    date_to   = request.args.get("to")
    limit     = int(request.args.get("limit", 20))
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB connection error"}), 500
    try:
        data = _build_dashboard_payload(conn, date_from, date_to, limit=limit)
        return jsonify({"status": "success", "data": data["recentOrders"]}), 200
    finally:
        conn.close()

# --------- Profile ---------

@admin_bp.route("/profile", methods=["GET"])
@admin_required
def get_admin_profile():
    token = _extract_bearer_token(request.headers.get("Authorization"))
    try:
        user_resp = supabase.auth.get_user(token)
        user = getattr(user_resp, "user", None)
        if not user:
            return jsonify({"status": "error", "message": "Usuário não encontrado"}), 404

        user_id = str(user.id)
        email = user.email

        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão"}), 500

        try:
            user_row = _fetchrow(conn, "SELECT user_type, created_at FROM users WHERE id = %s", (user_id,))
            extra = _fetchrow(conn, "SELECT name, cargo, phone, avatar_url FROM admin_profiles WHERE user_id = %s", (user_id,)) or {}
            recent_logs = _fetchall(conn, """
                SELECT timestamp, action, details
                  FROM admin_logs
                 WHERE admin = %s
              ORDER BY timestamp DESC
                 LIMIT 10
            """, (email,))

            profile = {
                "id": user_id,
                "email": email,
                "user_type": user_row.get("user_type", "admin") if user_row else "admin",
                "created_at": user_row["created_at"].isoformat() if user_row and user_row.get("created_at") else None,
                "name": extra.get("name"),
                "cargo": extra.get("cargo"),
                "phone": extra.get("phone"),
                "avatar_url": extra.get("avatar_url"),
                "recent_actions": [
                    {
                        "timestamp": r["timestamp"].isoformat() if r.get("timestamp") else None,
                        "action": r.get("action"),
                        "details": (r.get("details") or "")[:120],
                    }
                    for r in recent_logs
                ],
            }
            return jsonify({"status": "success", "data": profile}), 200
        finally:
            conn.close()
    except Exception as e:
        logger.exception("Erro em get_admin_profile")
        return jsonify({"status": "error", "message": str(e)}), 500


@admin_bp.route("/profile", methods=["PUT"])
@admin_required
def update_admin_profile():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip() or None
    cargo = (data.get("cargo") or "").strip() or None
    phone = (data.get("phone") or "").strip() or None

    token = _extract_bearer_token(request.headers.get("Authorization"))
    try:
        user_resp = supabase.auth.get_user(token)
        user = getattr(user_resp, "user", None)
        if not user:
            return jsonify({"status": "error", "message": "Usuário não encontrado"}), 404

        user_id = str(user.id)
        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão"}), 500
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO admin_profiles (user_id, name, cargo, phone, updated_at)
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE
                        SET name = EXCLUDED.name,
                            cargo = EXCLUDED.cargo,
                            phone = EXCLUDED.phone,
                            updated_at = NOW()
                """, (user_id, name, cargo, phone))
            conn.commit()
            log_admin_action_auto("UpdateProfile", f"Atualizou perfil: nome={name}, cargo={cargo}")
            return jsonify({"status": "success", "data": {"name": name, "cargo": cargo, "phone": phone}}), 200
        finally:
            conn.close()
    except Exception as e:
        logger.exception("Erro em update_admin_profile")
        return jsonify({"status": "error", "message": str(e)}), 500


@admin_bp.route("/profile/avatar", methods=["POST"])
@admin_required
def upload_admin_avatar():
    if "avatar" not in request.files:
        return jsonify({"status": "error", "message": "Nenhum arquivo enviado"}), 400

    file = request.files["avatar"]
    if not file or file.filename == "":
        return jsonify({"status": "error", "message": "Arquivo inválido"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "png"
    if ext not in {"png", "jpg", "jpeg", "gif", "webp"}:
        return jsonify({"status": "error", "message": "Tipo de arquivo não permitido"}), 400

    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > 5 * 1024 * 1024:
        return jsonify({"status": "error", "message": "Arquivo muito grande (máx 5MB)"}), 400

    token = _extract_bearer_token(request.headers.get("Authorization"))
    try:
        user_resp = supabase.auth.get_user(token)
        user = getattr(user_resp, "user", None)
        if not user:
            return jsonify({"status": "error", "message": "Usuário não encontrado"}), 404

        user_id = str(user.id)
        import uuid as _uuid
        filename = f"admin_{user_id}_{_uuid.uuid4().hex}.{ext}"

        supabase.storage.from_("banner-images").upload(
            path=filename,
            file=file.read(),
            file_options={"content-type": f"image/{ext}", "upsert": "true"},
        )

        import os as _os
        supabase_url = (_os.environ.get("SUPABASE_URL") or "").rstrip("/")
        avatar_url = f"{supabase_url}/storage/v1/object/public/banner-images/{filename}"

        conn = get_db_connection()
        if not conn:
            return jsonify({"status": "error", "message": "Erro de conexão"}), 500
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO admin_profiles (user_id, avatar_url, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE
                        SET avatar_url = EXCLUDED.avatar_url,
                            updated_at = NOW()
                """, (user_id, avatar_url))
            conn.commit()
            log_admin_action_auto("UpdateAvatar", "Admin atualizou foto de perfil")
            return jsonify({"status": "success", "data": {"avatar_url": avatar_url}}), 200
        finally:
            conn.close()
    except Exception as e:
        logger.exception("Erro em upload_admin_avatar")
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        logger.exception("Erro em get_admin_profile")
        return jsonify({"status": "error", "message": str(e)}), 500


@admin_bp.route("/profile/change-password", methods=["POST"])
@admin_required
def change_admin_password():
    data = request.get_json() or {}
    new_password = (data.get("new_password") or "").strip()

    if len(new_password) < 6:
        return jsonify({"status": "error", "message": "A senha deve ter pelo menos 6 caracteres"}), 400

    token = _extract_bearer_token(request.headers.get("Authorization"))
    try:
        user_resp = supabase.auth.get_user(token)
        user = getattr(user_resp, "user", None)
        if not user:
            return jsonify({"status": "error", "message": "Usuário não encontrado"}), 404

        supabase_admin.auth.admin.update_user_by_id(str(user.id), {"password": new_password})
        log_admin_action_auto("ChangePassword", "Admin alterou sua senha")
        return jsonify({"status": "success", "message": "Senha alterada com sucesso"}), 200
    except Exception as e:
        logger.exception("Erro ao alterar senha do admin")
        return jsonify({"status": "error", "message": str(e)}), 500


# --------- Admins management ---------

@admin_bp.route("/admins", methods=["GET"])
@admin_required
def list_admins():
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão"}), 500
    try:
        rows = _fetchall(conn, """
            SELECT id, email, user_type, created_at
              FROM users
             WHERE user_type = 'admin'
          ORDER BY created_at DESC
        """)
        result = [
            {
                "id": str(r.get("id")),
                "email": r.get("email") or "",
                "user_type": r.get("user_type"),
                "role": "Administrador",
                "status": "active",
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            }
            for r in rows
        ]
        return jsonify({"status": "success", "data": result}), 200
    except Exception as e:
        logger.exception("Erro em list_admins")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()


@admin_bp.route("/admins", methods=["POST"])
@admin_required
def create_admin():
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()

    if not email:
        return jsonify({"status": "error", "message": "Email é obrigatório"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão"}), 500

    try:
        existing = _fetchrow(conn, "SELECT id FROM users WHERE email = %s", (email,))
        if existing:
            return jsonify({"status": "error", "message": "Já existe um usuário com esse email"}), 409

        result = supabase_admin.auth.admin.invite_user_by_email(email)
        invited_user = getattr(result, "user", None)
        if not invited_user:
            return jsonify({"status": "error", "message": "Falha ao enviar convite"}), 500

        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO users (id, email, user_type)
                   VALUES (%s, %s, 'admin')
                   ON CONFLICT (id) DO UPDATE SET user_type = 'admin'""",
                (str(invited_user.id), email),
            )
            conn.commit()

        log_admin_action_auto("InviteAdmin", f"Convidou novo admin: {email}")

        return jsonify({
            "status": "success",
            "message": f"Convite enviado para {email}",
            "data": {"id": str(invited_user.id), "email": email, "status": "invited"},
        }), 201
    except Exception as e:
        logger.exception("Erro ao criar admin")
        try: conn.rollback()
        except Exception: pass
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()


# --------- Ocorrências de entrega ---------
@admin_bp.route("/incidents", methods=["GET"])
@admin_required
def list_delivery_incidents():
    """Lista ocorrências de entrega para a equipe tratar."""
    resolution = (request.args.get("resolution") or "").strip()
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão"}), 500
    try:
        where, params = "", []
        if resolution:
            where = "WHERE di.resolution = %s"
            params.append(resolution)
        rows = _fetchall(conn, f"""
            SELECT di.id, di.order_id, di.delivery_id, di.reason, di.notes, di.photo_url,
                   di.contact_attempts, di.resolution, di.outcome,
                   di.fault, di.refund_amount, di.refund_status, di.created_at, di.resolved_at,
                   di.return_code, di.return_confirmed_at, di.courier_charge, di.auto_decided,
                   o.total_amount, o.status AS order_status,
                   COALESCE(cp.first_name || ' ' || cp.last_name, '') AS client_name,
                   cp.phone AS client_phone,
                   COALESCE(dp.first_name || ' ' || dp.last_name, '') AS courier_name,
                   dp.phone AS courier_phone
              FROM delivery_incidents di
              LEFT JOIN orders o ON o.id = di.order_id
              LEFT JOIN client_profiles cp ON cp.user_id = o.client_id
              LEFT JOIN delivery_profiles dp ON dp.user_id = di.delivery_id
             {where}
          ORDER BY di.created_at DESC
             LIMIT 200
        """, params)
        result = [{
            "id": str(r.get("id")),
            "order_id": str(r.get("order_id")) if r.get("order_id") else None,
            "reason": r.get("reason"),
            "notes": r.get("notes"),
            "photo_url": r.get("photo_url"),
            "contact_attempts": r.get("contact_attempts"),
            "resolution": r.get("resolution"),
            "outcome": r.get("outcome"),
            "fault": r.get("fault"),
            "refund_amount": _safe_float(r.get("refund_amount")),
            "refund_status": r.get("refund_status"),
            "return_code": r.get("return_code"),
            "return_confirmed_at": r["return_confirmed_at"].isoformat() if r.get("return_confirmed_at") else None,
            "courier_charge": _safe_float(r.get("courier_charge")),
            "auto_decided": bool(r.get("auto_decided")),
            "order_status": r.get("order_status"),
            "total_amount": _safe_float(r.get("total_amount")),
            "client_name": (r.get("client_name") or "").strip(),
            "client_phone": r.get("client_phone"),
            "courier_name": (r.get("courier_name") or "").strip(),
            "courier_phone": r.get("courier_phone"),
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            "resolved_at": r["resolved_at"].isoformat() if r.get("resolved_at") else None,
        } for r in rows]
        return jsonify({"status": "success", "data": result}), 200
    except Exception as e:
        logger.exception("Erro em list_delivery_incidents")
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()


_INCIDENT_RESOLUTIONS = {"pending", "returned", "discarded", "refunded", "retry", "closed"}

@admin_bp.route("/incidents/<uuid:incident_id>/resolve", methods=["POST"])
@admin_required
def resolve_delivery_incident(incident_id):
    """Define a resolução de uma ocorrência (retornado, reembolsado, etc.)."""
    data = request.get_json() or {}
    resolution = (data.get("resolution") or "").strip()
    if resolution not in _INCIDENT_RESOLUTIONS:
        return jsonify({"status": "error", "message": "Resolução inválida"}), 400
    note = (data.get("note") or "").strip()
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if note:
                cur.execute(
                    "UPDATE delivery_incidents SET resolution = %s, resolved_at = NOW(), "
                    "notes = COALESCE(notes,'') || %s WHERE id = %s RETURNING id",
                    (resolution, f"\n[admin] {note}", str(incident_id)),
                )
            else:
                cur.execute(
                    "UPDATE delivery_incidents SET resolution = %s, resolved_at = NOW() WHERE id = %s RETURNING id",
                    (resolution, str(incident_id)),
                )
            row = cur.fetchone()
            conn.commit()
            if not row:
                return jsonify({"status": "error", "message": "Ocorrência não encontrada"}), 404
        return jsonify({"status": "success", "message": "Ocorrência atualizada"}), 200
    except Exception as e:
        logger.exception("Erro em resolve_delivery_incident")
        try: conn.rollback()
        except Exception: pass
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()


@admin_bp.route("/incidents/<uuid:incident_id>/charge-courier", methods=["POST"])
@admin_required
def charge_incident_courier(incident_id):
    """Desconta um valor do entregador por uma ocorrência (culpa dele): lança na
    dívida (delivery_profiles.cash_debt), abatida do próximo repasse online. Quem
    decide se cabe e quanto é o admin, caso a caso. Idempotente por ocorrência."""
    data = request.get_json() or {}
    try:
        amount = round(float(data.get("amount") or 0), 2)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Valor inválido"}), 400
    if amount <= 0:
        return jsonify({"status": "error", "message": "Informe um valor maior que zero"}), 400
    note = (data.get("note") or "").strip()
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT delivery_id, courier_charge FROM delivery_incidents WHERE id = %s", (str(incident_id),))
            inc = cur.fetchone()
            if not inc:
                return jsonify({"status": "error", "message": "Ocorrência não encontrada"}), 404
            if not inc["delivery_id"]:
                return jsonify({"status": "error", "message": "Ocorrência sem entregador atribuído"}), 400
            if float(inc["courier_charge"] or 0) > 0:
                return jsonify({"status": "error", "message": "Este entregador já foi descontado nesta ocorrência"}), 400
            cur.execute(
                "UPDATE delivery_profiles SET cash_debt = COALESCE(cash_debt,0) + %s WHERE id = %s RETURNING cash_debt",
                (amount, str(inc["delivery_id"])),
            )
            drow = cur.fetchone()
            if not drow:
                return jsonify({"status": "error", "message": "Entregador não encontrado"}), 404
            if note:
                cur.execute(
                    "UPDATE delivery_incidents SET courier_charge = %s, courier_charge_at = NOW(), "
                    "notes = COALESCE(notes,'') || %s WHERE id = %s",
                    (amount, f"\n[admin] desconto do entregador R${amount:.2f}: {note}", str(incident_id)),
                )
            else:
                cur.execute(
                    "UPDATE delivery_incidents SET courier_charge = %s, courier_charge_at = NOW() WHERE id = %s",
                    (amount, str(incident_id)),
                )
            conn.commit()
        return jsonify({"status": "success", "message": "Desconto lançado na dívida do entregador",
                        "new_cash_debt": float(drow["cash_debt"] or 0)}), 200
    except Exception as e:
        logger.exception("Erro em charge_incident_courier")
        try: conn.rollback()
        except Exception: pass
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()


@admin_bp.route("/incidents/<uuid:incident_id>/refund", methods=["POST"])
@admin_required
def refund_delivery_incident(incident_id):
    """Processa o reembolso ao cliente (Mercado Pago) de uma ocorrência pendente."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT di.refund_status, di.order_id, o.id_transacao_mp, o.payment_provider
                  FROM delivery_incidents di
                  LEFT JOIN orders o ON o.id = di.order_id
                 WHERE di.id = %s
            """, (str(incident_id),))
            row = cur.fetchone()
            if not row:
                return jsonify({"status": "error", "message": "Ocorrência não encontrada"}), 404
            if row["refund_status"] == "done":
                return jsonify({"status": "error", "message": "Reembolso já processado"}), 400
            if row["refund_status"] != "pending":
                return jsonify({"status": "error", "message": "Sem reembolso pendente para esta ocorrência"}), 400
            payment_id = row["id_transacao_mp"]
            if not payment_id:
                return jsonify({"status": "error", "message": "Pedido sem transação no gateway de pagamento"}), 400

            from ..utils.gateway import refund_order_payment
            ok_refund, refund_detail = refund_order_payment(dict(row), current_app.mp_sdk)
            if not ok_refund:
                logger.error("Gateway recusou reembolso: %s", refund_detail)
                return jsonify({"status": "error", "message": "O gateway de pagamento recusou o reembolso"}), 400

            cur.execute(
                "UPDATE delivery_incidents SET refund_status = 'done', "
                "resolution = CASE WHEN resolution = 'pending' THEN 'refunded' ELSE resolution END, "
                "resolved_at = COALESCE(resolved_at, NOW()) WHERE id = %s",
                (str(incident_id),),
            )
            cur.execute(
                "UPDATE orders SET status_pagamento = 'refunded', updated_at = NOW() WHERE id = %s",
                (str(row["order_id"]),),
            )
            conn.commit()
        return jsonify({"status": "success", "message": "Reembolso processado"}), 200
    except Exception as e:
        logger.exception("Erro em refund_delivery_incident")
        try: conn.rollback()
        except Exception: pass
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        conn.close()


@admin_bp.route("/integrations/asaas/check", methods=["GET"])
@admin_required
def check_asaas_integration():
    """Testa a conexão REAL com o Asaas (não só se a variável existe).

    Existe por causa de uma armadilha concreta: o /api/health responde
    "asaas: configured" olhando apenas se ASAAS_API_KEY está setada. Quando a
    conta muda (PF→PJ, chave regerada, ambiente trocado), a variável continua lá
    e o painel segue verde — o erro só aparece pro cliente no primeiro pedido.

    Aqui a gente bate no Asaas de verdade e devolve de qual conta a chave é,
    junto com o saldo (sem saldo o PIX de repasse falha na hora de pagar).
    A chave nunca é devolvida na resposta.
    """
    from ..utils import asaas

    ok, info = asaas.check_account()
    if not ok:
        return jsonify({"status": "error", "conectado": False, **info}), 200

    saldo_ok, saldo = asaas.get_balance()
    return jsonify({
        "status": "success",
        "conectado": True,
        "conta": info,
        "saldo": saldo if saldo_ok else None,
        "payout_provider": (os.environ.get("PAYOUT_PROVIDER") or "mock").strip().lower(),
        "webhook_token_configurado": bool(os.environ.get("ASAAS_WEBHOOK_TOKEN")),
    }), 200


@admin_bp.route("/integrations/push/check", methods=["GET"])
@admin_required
def check_push_integration():
    """Diz se o push está de pé DOS DOIS LADOS: credencial no servidor e tokens no banco.

    Mesma armadilha do /integrations/asaas/check: dava pra ter tudo "certo" no
    código e nada funcionando. Do lado do aparelho, o token só existia se cinco
    coisas dessem certo em sequência; do lado do servidor, faltando o Secret
    File do Firebase o envio devolve False e ninguém acima olha esse retorno.
    Push que não sai não vira erro em lugar nenhum.
    """
    from ..services.notification_service import status_firebase

    st = status_firebase()
    conn = get_db_connection()
    try:
        tokens = {}
        for rotulo, tabela in (("clientes", CLIENTS_TABLE),
                               ("parceiros", RESTAURANTS_TABLE),
                               ("entregadores", DELIVERY_TABLE)):
            tokens[rotulo] = {
                "com_token": _safe_int(_fetchval(
                    conn, f"SELECT COUNT(*) FROM {tabela} WHERE COALESCE(fcm_token,'') <> ''", default=0)),
                "total": _safe_int(_fetchval(conn, f"SELECT COUNT(*) FROM {tabela}", default=0)),
            }
    finally:
        conn.close()

    return jsonify({
        "status": "success",
        "servidor": st,
        "aparelhos": tokens,
        "pronto": st["pode_enviar"] and any(v["com_token"] > 0 for v in tokens.values()),
    }), 200


@admin_bp.route("/integrations/push/test", methods=["POST"])
@admin_required
def test_push_send():
    """Dispara um push de teste pra um usuário e devolve o MOTIVO se falhar.

    Body: {"user_id": "<uuid>", "user_type": "client|restaurant|delivery"}
    """
    from ..services.notification_service import enviar_teste

    body = request.get_json(silent=True) or {}
    user_id = (body.get("user_id") or "").strip()
    user_type = (body.get("user_type") or "client").strip().lower()
    tabela = {"client": CLIENTS_TABLE,
              "restaurant": RESTAURANTS_TABLE,
              "delivery": DELIVERY_TABLE}.get(user_type)
    if not user_id or not tabela:
        return jsonify({"status": "error", "message": "Informe user_id e user_type válido."}), 400

    conn = get_db_connection()
    try:
        token = _fetchval(conn, f"SELECT fcm_token FROM {tabela} WHERE id = %s", (user_id,))
    finally:
        conn.close()

    if not token:
        return jsonify({"status": "error", "message": "Esse usuário não tem token salvo — o aparelho dele nunca registrou."}), 404

    resultado = enviar_teste(token)
    log_admin_action_auto(
        "PushTest",
        f"{user_type} {user_id} — enviado={resultado.get('enviado')} {resultado.get('erro') or ''}".strip(),
    )
    return jsonify({"status": "success" if resultado.get("enviado") else "error", **resultado}), 200


# ─────────────────────────────────────────────────────────────────────────────
# PRONTIDÃO DA PRAÇA
# Responde uma pergunta só: "se um cliente abrir o app agora, ele consegue
# pedir?" — e, quando não consegue, QUEM está faltando.
#
# Existe porque os números que importam estavam espalhados e nenhum deles
# aparecia em lugar nenhum. Dava pra ter 12 lojas "aprovadas e ativas" no
# painel e o cliente ver três, sendo uma vazia — e ninguém notava, porque o
# painel contava cadastro, não vitrine.
#
# As regras aqui são as MESMAS da listagem pública (public_restaurants.py):
# aprovada + ativa + dono ativo + COM COORDENADA. Loja sem lat/lng é escondida
# do cliente de propósito (sem coordenada não dá pra calcular frete e ela
# escaparia do filtro de raio), então ela não pode ser contada como pronta.
# ─────────────────────────────────────────────────────────────────────────────

@admin_bp.route("/prontidao", methods=["GET"])
@admin_required
def readiness():
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Banco indisponível."}), 500
    try:
        lojas = _fetchall(conn, """
            SELECT rp.restaurant_name AS nome,
                   NULLIF(TRIM(rp.address_city), '')  AS cidade,
                   UPPER(NULLIF(TRIM(rp.address_state), '')) AS uf,
                   COALESCE(rp.approved, TRUE)  AS aprovada,
                   COALESCE(rp.active, TRUE)    AS ativa,
                   COALESCE(u.is_active, TRUE)  AS dono_ativo,
                   (rp.latitude IS NOT NULL AND rp.longitude IS NOT NULL) AS tem_coordenada,
                   (SELECT COUNT(*) FROM menu_items m WHERE m.restaurant_id = rp.id) AS itens,
                   COALESCE(rp.delivery_type, 'platform') AS tipo_entrega
              FROM restaurant_profiles rp
              LEFT JOIN users u ON u.id = rp.user_id
             ORDER BY rp.restaurant_name
        """)
        for l in lojas:
            faltas = []
            if not (l["aprovada"] and l["ativa"] and l["dono_ativo"]):
                faltas.append("aprovação/ativação")
            if not l["tem_coordenada"]:
                faltas.append("endereço no mapa")
            if int(l["itens"] or 0) == 0:
                faltas.append("cardápio")
            if not l["uf"]:
                faltas.append("estado (UF)")
            l["itens"] = int(l["itens"] or 0)
            l["faltas"] = faltas
            # "Vendável" = o cliente VÊ e tem o que pedir. As duas coisas.
            l["vendavel"] = (l["aprovada"] and l["ativa"] and l["dono_ativo"]
                             and l["tem_coordenada"] and l["itens"] > 0)

        entregadores = _fetchall(conn, """
            SELECT COALESCE(NULLIF(TRIM(CONCAT_WS(' ', dp.first_name, dp.last_name)), ''),
                            'sem nome') AS nome,
                   NULLIF(TRIM(dp.address_city), '') AS cidade,
                   COALESCE(dp.approved, FALSE) AS aprovado,
                   NULLIF(TRIM(dp.vehicle_type), '') AS veiculo,
                   (dp.latitude IS NOT NULL AND dp.longitude IS NOT NULL) AS tem_coordenada,
                   NULLIF(TRIM(dp.phone), '') AS telefone,
                   NULLIF(TRIM(dp.cpf), '')   AS cpf
              FROM delivery_profiles dp
             ORDER BY 1
        """)
        for e in entregadores:
            faltas = []
            if not e["aprovado"]:
                faltas.append("aprovação do admin")
            if not e["tem_coordenada"]:
                # É a causa nº1 de "estou online e não chega nada": o filtro de
                # raio precisa de onde ele está.
                faltas.append("endereço no mapa")
            if not e["veiculo"]:
                # O filtro de carga é fail-closed pra veículo desconhecido:
                # sem isso ele não recebe pedido NENHUM, e em silêncio.
                faltas.append("tipo de veículo")
            if not e["telefone"]:
                faltas.append("telefone")
            if not e["cpf"]:
                faltas.append("CPF")
            e["faltas"] = faltas
            e["pode_receber"] = not faltas

        # Itens sem peso nos segmentos onde ele decide frete e veículo. Sem
        # isso, um pedido de 60 kg calcula 0 kg: sai com frete de moto e a
        # trava de carga (fail-open em peso zero) deixa passar.
        itens_sem_peso = _fetchall(conn, """
            SELECT rp.restaurant_name AS loja,
                   LOWER(TRIM(rp.segment)) AS segmento,
                   COUNT(*) AS itens
              FROM menu_items m
              JOIN restaurant_profiles rp ON rp.id = m.restaurant_id
             WHERE COALESCE(m.peso_kg, 0) <= 0
               AND LOWER(TRIM(COALESCE(rp.segment, ''))) IN
                   ('pet','mercado','agropecuaria','bebidas')
             GROUP BY 1, 2
             ORDER BY 3 DESC
        """)
        for i in itens_sem_peso:
            i["itens"] = int(i["itens"] or 0)

        clientes = _fetchrow(conn, """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE NULLIF(TRIM(phone), '') IS NOT NULL)     AS com_telefone,
                   COUNT(*) FILTER (WHERE NULLIF(TRIM(fcm_token), '') IS NOT NULL) AS com_push,
                   COUNT(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM client_addresses ca WHERE ca.user_id = client_profiles.user_id
                   )) AS com_endereco
              FROM client_profiles
        """) or {}

        pedidos = _fetchrow(conn, """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status IN ('delivered','completed')) AS entregues,
                   COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days') AS ultimos_7_dias,
                   MAX(created_at) AS ultimo
              FROM orders
        """) or {}
        if pedidos.get("ultimo") is not None:
            pedidos["ultimo"] = pedidos["ultimo"].isoformat()

        # Praças = onde há loja vendável. É a lista que o cliente enxerga,
        # não a lista de cadastros.
        pracas = {}
        for l in lojas:
            if not l["vendavel"]:
                continue
            chave = f'{l["cidade"] or "sem cidade"}{" - " + l["uf"] if l["uf"] else ""}'
            pracas[chave] = pracas.get(chave, 0) + 1

        return jsonify({"status": "success", "data": {
            "lojas": lojas,
            "itens_sem_peso": itens_sem_peso,
            "entregadores": entregadores,
            "clientes": {k: int(v or 0) for k, v in clientes.items()},
            "pedidos": {k: (int(v or 0) if k != "ultimo" else v) for k, v in pedidos.items()},
            "pracas": [{"praca": k, "lojas_vendaveis": v} for k, v in sorted(pracas.items())],
            "resumo": {
                "lojas_cadastradas": len(lojas),
                "lojas_vendaveis": sum(1 for l in lojas if l["vendavel"]),
                "entregadores_cadastrados": len(entregadores),
                "entregadores_prontos": sum(1 for e in entregadores if e["pode_receber"]),
            },
        }}), 200
    finally:
        try: conn.close()
        except Exception: pass


# ─────────────────────────────────────────────────────────────────────────────
# CARRINHOS PARADOS
# O alerta do dashboard existia e apontava pra /usuarios — uma lista de gente,
# sem dizer QUEM parou nem deixar fazer nada. Era um aviso sem saída.
#
# Aqui dá pra ver quem é e mandar UM lembrete. O lembrete é sobre o carrinho da
# própria pessoa, não propaganda de terceiro — a mesma distinção que separa
# push de serviço de push de anúncio, e a razão de isto não cair na trava de
# campanha (que existe pra cupom de loja).
# ─────────────────────────────────────────────────────────────────────────────

_CARRINHO_MIN = 15


@admin_bp.route("/carrinhos", methods=["GET"])
@admin_required
def carrinhos_parados():
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Banco indisponível."}), 500
    try:
        linhas = _fetchall(conn, f"""
            SELECT cp.id,
                   COALESCE(NULLIF(TRIM(CONCAT_WS(' ', cp.first_name, cp.last_name)), ''),
                            'sem nome') AS nome,
                   NULLIF(TRIM(cp.phone), '') AS telefone,
                   COALESCE(cp.cart_items_count, 0) AS itens,
                   COALESCE(cp.cart_value, 0)       AS valor,
                   cp.cart_updated_at,
                   ROUND(EXTRACT(EPOCH FROM (NOW() - cp.cart_updated_at)) / 60)::int AS minutos,
                   (NULLIF(TRIM(cp.fcm_token), '') IS NOT NULL) AS tem_push,
                   EXISTS (SELECT 1 FROM push_campaign_log l
                            WHERE l.client_id = cp.id
                              AND l.campanha = 'cart:' ||
                                  (NOW() AT TIME ZONE 'America/Sao_Paulo')::date::text
                   ) AS ja_lembrado_hoje
              FROM {CLIENTS_TABLE} cp
             WHERE COALESCE(cp.cart_items_count, 0) > 0
               AND cp.cart_updated_at < NOW() - INTERVAL '{_CARRINHO_MIN} minutes'
             ORDER BY cp.cart_value DESC NULLS LAST
        """)
        for l in linhas:
            l["id"] = str(l["id"])
            l["valor"] = float(l["valor"] or 0)
            l["itens"] = int(l["itens"] or 0)
            l["minutos"] = int(l["minutos"] or 0)
            l["cart_updated_at"] = (l["cart_updated_at"].isoformat()
                                    if l.get("cart_updated_at") else None)
        return jsonify({"status": "success", "data": {
            "carrinhos": linhas,
            "total_valor": round(sum(l["valor"] for l in linhas), 2),
            "minutos_corte": _CARRINHO_MIN,
        }}), 200
    finally:
        try: conn.close()
        except Exception: pass


@admin_bp.route("/carrinhos/<client_id>/lembrar", methods=["POST"])
@admin_required
@limiter.limit("30 per minute")
def lembrar_carrinho(client_id):
    """Manda UM push sobre o carrinho parado da pessoa.

    Uma vez por dia por pessoa, garantido por índice único em
    push_campaign_log (client_id, campanha) com a campanha carimbada com a
    data. Dois cliques no botão não viram dois pushes.
    """
    from ..services.notification_service import send_campaign

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Banco indisponível."}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(f"""
                SELECT id, NULLIF(TRIM(fcm_token), '') AS token,
                       COALESCE(cart_value, 0) AS valor,
                       COALESCE(cart_items_count, 0) AS itens
                  FROM {CLIENTS_TABLE} WHERE id = %s
            """, (client_id,))
            cli = cur.fetchone()
            if not cli:
                return jsonify({"status": "error", "message": "Cliente não encontrado."}), 404
            if not cli["token"]:
                return jsonify({"status": "error",
                                "message": "Este cliente não tem avisos ligados — não dá pra lembrar."}), 400
            if int(cli["itens"] or 0) <= 0:
                return jsonify({"status": "error",
                                "message": "O carrinho já não tem itens."}), 400

            cur.execute("SELECT (NOW() AT TIME ZONE 'America/Sao_Paulo')::date::text AS d")
            campanha = "cart:" + cur.fetchone()["d"]

            # Reserva ANTES de enviar: se dois cliques chegarem juntos, o
            # segundo bate no índice único e não manda. Melhor não lembrar do
            # que lembrar duas vezes.
            cur.execute(
                "INSERT INTO push_campaign_log (client_id, campanha, tipo) "
                "VALUES (%s, %s, 'cart') ON CONFLICT (client_id, campanha) DO NOTHING "
                "RETURNING id",
                (client_id, campanha),
            )
            if cur.fetchone() is None:
                conn.commit()
                return jsonify({"status": "error",
                                "message": "Esta pessoa já foi lembrada hoje."}), 409
            conn.commit()

        itens = int(cli["itens"])
        valor = float(cli["valor"] or 0)
        corpo = (f"{itens} {'item' if itens == 1 else 'itens'} esperando"
                 + (f" — R$ {valor:.2f}".replace(".", ",") if valor > 0 else "")
                 + ". Finalizar agora?")
        res = send_campaign([(client_id, cli["token"])],
                            "Seu pedido ficou pela metade", corpo,
                            {"type": "cart", "url": "/carrinho"})

        # Token recusado = app desinstalado. Limpa e devolve o direito do dia,
        # senão a pessoa fica marcada como lembrada por um push que não saiu.
        if str(client_id) in {str(x) for x in (res.get("invalidos") or [])}:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE {CLIENTS_TABLE} SET fcm_token = NULL WHERE id = %s", (client_id,))
                cur.execute("DELETE FROM push_campaign_log WHERE client_id = %s AND campanha = %s",
                            (client_id, campanha))
            conn.commit()
            return jsonify({"status": "error",
                            "message": "O aparelho recusou o aviso (app desinstalado). Token limpo."}), 400

        return jsonify({"status": "success", "message": "Lembrete enviado."}), 200
    finally:
        try: conn.close()
        except Exception: pass


@admin_bp.route("/carrinhos/<client_id>", methods=["DELETE"])
@admin_required
@limiter.limit("60 per minute")
def descartar_carrinho(client_id):
    """Tira o carrinho da lista, zerando o registro no perfil.

    ⚠️ ISTO NÃO ESVAZIA O CARRINHO DA PESSOA. Estes campos são um ESPELHO: o
    app do cliente manda o conteúdo do carrinho de tempos em tempos
    (`/api/client/heartbeat`), e é isso que preenche as colunas. Zerar aqui
    limpa o painel agora; se a pessoa abrir o app com o carrinho ainda
    montado, o próximo heartbeat regrava e ela reaparece.

    Serve pro que o Diego precisa: sumir com carrinho velho de testador, que
    ninguém vai reabrir. Esvaziar o carrinho de alguém pelas costas seria
    outra coisa — e uma coisa ruim.
    """
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Banco indisponível."}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(f"""
                UPDATE {CLIENTS_TABLE}
                   SET cart_items_count = 0, cart_value = 0, cart_updated_at = NULL
                 WHERE id = %s
                RETURNING COALESCE(NULLIF(TRIM(CONCAT_WS(' ', first_name, last_name)), ''),
                                   'sem nome') AS nome
            """, (client_id,))
            linha = cur.fetchone()
            if not linha:
                return jsonify({"status": "error", "message": "Cliente não encontrado."}), 404
            conn.commit()
        log_admin_action_auto("DescartarCarrinho",
                              f"Carrinho parado descartado: {linha['nome']} ({client_id})")
        return jsonify({"status": "success", "message": "Carrinho tirado da lista."}), 200
    finally:
        try: conn.close()
        except Exception: pass


@admin_bp.route("/carrinhos/antigos", methods=["DELETE"])
@admin_required
@limiter.limit("10 per minute")
def descartar_carrinhos_antigos():
    """Tira da lista todos os carrinhos com mais de N horas (padrão 48).

    Mesma ressalva do individual: é o espelho que some, não o carrinho da
    pessoa. Só alcança os ANTIGOS de propósito — um botão que limpasse tudo
    apagaria justamente os recentes, que são os que ainda dá pra recuperar.
    """
    try:
        horas = int(request.args.get("horas", 48))
    except (TypeError, ValueError):
        horas = 48
    horas = max(1, min(horas, 24 * 365))

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Banco indisponível."}), 500
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                UPDATE {CLIENTS_TABLE}
                   SET cart_items_count = 0, cart_value = 0, cart_updated_at = NULL
                 WHERE COALESCE(cart_items_count, 0) > 0
                   AND cart_updated_at < NOW() - (%s * INTERVAL '1 hour')
            """, (horas,))
            n = cur.rowcount
            conn.commit()
        log_admin_action_auto("DescartarCarrinhosAntigos",
                              f"{n} carrinho(s) com mais de {horas}h descartado(s)")
        return jsonify({"status": "success", "descartados": n,
                        "message": f"{n} carrinho(s) tirado(s) da lista."}), 200
    finally:
        try: conn.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# Monitor de saúde sob demanda
# ---------------------------------------------------------------------------
@admin_bp.route("/monitor", methods=["GET", "OPTIONS"])
def admin_monitor():
    """Roda AGORA as mesmas checagens do monitor horário e devolve o resultado.

    Existe por um motivo prático: um monitor que só se prova de hora em hora é
    um monitor em que ninguém confia. Com isto o Diego abre e vê o que ele
    veria — inclusive quando está tudo certo, que é a resposta mais difícil de
    obter de um sistema de alerta.

    NÃO envia e-mail: é consulta. O envio (com o silêncio de 6h por assunto)
    fica só no ciclo automático, senão testar aqui gastaria o silêncio e o
    alerta de verdade não sairia depois.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 204
    _, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
    if error:
        return error
    if not _is_admin(user_type):
        return jsonify({"error": "Acesso negado"}), 403

    # Import local (e não no topo): datetime não estava importado neste
    # arquivo, e um NameError aqui só apareceria em execução, no dia em que o
    # Diego abrisse a tela — exatamente o tipo de erro que a compilação não
    # pega e que já mordeu neste projeto.
    import datetime as _dt
    try:
        from src.logic.monitor import coletar_alertas, CRITICO
    except Exception as e:
        return jsonify({"error": f"Monitor indisponível: {e}"}), 500

    alertas = coletar_alertas()
    return jsonify({
        "status": "success",
        "verificado_em": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "tudo_ok": len(alertas) == 0,
        "criticos": sum(1 for g, _, _ in alertas if g == CRITICO),
        "alertas": [{"gravidade": g, "titulo": t, "detalhe": d} for g, t, d in alertas],
    }), 200


# ---------------------------------------------------------------------------
# Sugestões de restaurante — a fila de prospecção ordenada pela demanda real
# ---------------------------------------------------------------------------
@admin_bp.route("/sugestoes", methods=["GET", "OPTIONS"])
def admin_sugestoes():
    """Quais lojas os clientes estão pedindo, e quantos pediram cada uma.

    É a lista de prospecção que se constrói sozinha. Chegar num restaurante
    dizendo "sete pessoas do meu app pediram vocês esta semana" vale mais que
    qualquer argumento de comissão — deixa de ser venda e vira recado.

    Traz também `ja_existe`: se a loja sugerida JÁ está cadastrada, o problema
    não é falta de parceiro, é a loja estar invisível (sem cardápio, sem
    coordenada ou desativada). São dois problemas diferentes e a mesma tela
    precisa separar, senão o Diego vai prospectar quem já é parceiro.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 204
    _, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
    if error:
        return error
    if not _is_admin(user_type):
        return jsonify({"error": "Acesso negado"}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT s.nome_chave,
                       MIN(s.nome)              AS nome,
                       count(*)::int            AS pedidos,
                       max(s.created_at)        AS ultimo,
                       count(*) FILTER (WHERE s.contato IS NOT NULL)::int AS com_contato,
                       (array_agg(s.contato) FILTER (WHERE s.contato IS NOT NULL))[1] AS contato,
                       EXISTS (
                         SELECT 1 FROM restaurant_profiles rp
                          WHERE chave_nome_loja(rp.restaurant_name) = s.nome_chave
                       ) AS ja_existe,
                       bool_or(s.atendida_em IS NOT NULL) AS atendida
                  FROM restaurant_suggestions s
                 GROUP BY s.nome_chave
                 ORDER BY count(*) DESC, max(s.created_at) DESC
                 LIMIT 200
            """)
            linhas = [dict(r) for r in cur.fetchall()]
        for l in linhas:
            if l.get("ultimo"):
                l["ultimo"] = l["ultimo"].isoformat()
        return jsonify({"status": "success", "sugestoes": linhas}), 200
    except Exception:
        logger.exception("Erro ao listar sugestões")
        return jsonify({"error": "Erro ao listar sugestões"}), 500
    finally:
        try: conn.close()
        except Exception: pass


@admin_bp.route("/sugestoes/atendida", methods=["POST", "OPTIONS"])
def admin_sugestao_atendida():
    """Marca (ou desmarca) "já falei com esse".

    Sem isto a fila só cresce e some a diferença entre "ninguém procurou ainda"
    e "procurei e não fechou" — que é justamente a informação que decide o que
    fazer amanhã. Marca por nome_chave, não por linha: a tela agrupa as
    sugestões da mesma loja, então desmarcar uma linha e deixar as outras
    marcadas produziria um estado que a tela não sabe mostrar.

    Não apaga nada. O contador de demanda continua valendo depois da visita.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 204
    _, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
    if error:
        return error
    if not _is_admin(user_type):
        return jsonify({"error": "Acesso negado"}), 403

    body = request.get_json(silent=True) or {}
    chave = (body.get("nome_chave") or "").strip()
    if not chave:
        return jsonify({"error": "nome_chave é obrigatório"}), 400
    atendida = bool(body.get("atendida", True))

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 503
    try:
        with conn.cursor() as cur:
            # NOW() do banco, não do processo: created_at já vem de lá, e duas
            # fontes de relógio numa mesma tabela é o tipo de detalhe que só
            # aparece quando a data fica estranha e ninguém sabe por quê.
            cur.execute(
                """UPDATE restaurant_suggestions
                      SET atendida_em = CASE WHEN %s THEN NOW() ELSE NULL END
                    WHERE nome_chave = %s""",
                (atendida, chave),
            )
            n = cur.rowcount
        conn.commit()
        return jsonify({
            "status": "success",
            "atualizadas": n,
            "message": "Marcada como atendida." if atendida else "Voltou pra fila.",
        }), 200
    except Exception:
        logger.exception("Erro ao marcar sugestão")
        try: conn.rollback()
        except Exception: pass
        return jsonify({"error": "Erro ao marcar sugestão"}), 500
    finally:
        try: conn.close()
        except Exception: pass


@admin_bp.route("/sugestoes", methods=["DELETE", "OPTIONS"])
def admin_apagar_sugestao():
    """Apaga uma sugestão da fila de prospecção.

    Existe porque o campo é texto livre e texto livre gera entulho: erro de
    digitação vira uma loja nova ("Pqdaria mullre" ao lado de "Padaria
    muller"), e duas linhas pra mesma padaria estragam o número que é a razão
    da tela existir.

    Apaga por nome_chave, não por linha: a tela agrupa por nome_chave, então
    apagar uma linha de um grupo deixaria a mesma loja na lista com o contador
    menor — pior que não apagar.

    NÃO é o mesmo que "já atendi". Atendida some da fila e mantém o histórico;
    apagar é pra lixo, e não tem volta.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 204
    _, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
    if error:
        return error
    if not _is_admin(user_type):
        return jsonify({"error": "Acesso negado"}), 403

    chave = (request.args.get("nome_chave") or "").strip()
    if not chave:
        return jsonify({"error": "nome_chave é obrigatório"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Banco indisponível"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM restaurant_suggestions WHERE nome_chave = %s", (chave,))
            n = cur.rowcount
        conn.commit()
        return jsonify({
            "status": "success",
            "apagadas": n,
            "message": "Sugestão apagada." if n else "Nada para apagar.",
        }), 200
    except Exception:
        logger.exception("Erro ao apagar sugestão")
        try: conn.rollback()
        except Exception: pass
        return jsonify({"error": "Erro ao apagar"}), 500
    finally:
        try: conn.close()
        except Exception: pass
