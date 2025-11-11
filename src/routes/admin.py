# src/routes/admin.py
import os
import re
import logging
import traceback
from functools import wraps
from datetime import datetime

from flask import Blueprint, request, jsonify
from flask_cors import CORS
import psycopg2
import psycopg2.extras

from gotrue.errors import AuthApiError

from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase
from ..utils.audit import log_admin_action

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Blueprint (prefix definido no main.py com: app.register_blueprint(admin_bp, url_prefix='/api/admin'))
# -------------------------------------------------------------------
admin_bp = Blueprint("admin_bp", __name__)

# CORS específico do blueprint
CORS(
    admin_bp,
    origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        re.compile(r"^https://.*\.vercel\.app$"),  # qualquer preview do Vercel
        "https://admin.inksadelivery.com.br",
        "https://clientes.inksadelivery.com.br",
        "https://restaurantes.inksadelivery.com.br",
        "https://entregadores.inksadelivery.com.br",
    ],
    supports_credentials=True,
)

# Tabelas do schema (centralizado)
ORDERS_TABLE = "orders"
CLIENTS_TABLE = "client_profiles"
RESTAURANTS_TABLE = "restaurant_profiles"
DELIVERY_TABLE = "delivery_profiles"

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
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


def _build_dashboard_payload(conn, date_from=None, date_to=None, limit=10):
    """
    Monta o mesmo payload usado no dashboard, com filtros opcionais.
    Reaproveitado pelas rotas de compatibilidade (/metrics, /revenue-series, /transactions).
    """
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
        },
        "chartData": [],
        "recentOrders": [],
        "ordersStatus": {},
        "clientsGrowth": [],
    }

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        # KPIs
        cur.execute(
            f"""
            SELECT COALESCE(SUM(total_amount),0) AS total
            FROM {ORDERS_TABLE}
            WHERE status IN ('delivered','completed')
            """
        )
        payload["kpis"]["totalRevenue"] = float(cur.fetchone()["total"])

        cur.execute(
            f"""
            SELECT COALESCE(AVG(total_amount),0) AS avg_ticket
            FROM {ORDERS_TABLE}
            WHERE status IN ('delivered','completed')
            """
        )
        payload["kpis"]["averageTicket"] = float(cur.fetchone()["avg_ticket"])

        cur.execute(
            f"""
            SELECT COUNT(*)::int AS c
            FROM {ORDERS_TABLE}
            WHERE created_at::date = CURRENT_DATE
            """
        )
        payload["kpis"]["ordersToday"] = int(cur.fetchone()["c"])

        cur.execute(
            f"""
            SELECT COUNT(*)::int AS c
            FROM {CLIENTS_TABLE}
            WHERE created_at::date = CURRENT_DATE
            """
        )
        payload["kpis"]["newClientsToday"] = int(cur.fetchone()["c"])

        cur.execute(
            f"""
            SELECT
              SUM(CASE WHEN status IN ('preparing','on_the_way','in_progress') THEN 1 ELSE 0 END)::int AS in_progress,
              SUM(CASE WHEN status IN ('cancelled','canceled') THEN 1 ELSE 0 END)::int AS canceled
            FROM {ORDERS_TABLE}
            """
        )
        row = cur.fetchone()
        payload["kpis"]["ordersInProgress"] = int(row["in_progress"])
        payload["kpis"]["ordersCanceled"] = int(row["canceled"])

        # Pode não existir em todos os schemas — não falha se faltar
        try:
            cur.execute(
                f"""
                SELECT COUNT(*)::int AS c
                FROM {RESTAURANTS_TABLE}
                WHERE (approved IS FALSE) OR (status = 'pending')
                """
            )
            payload["kpis"]["restaurantsPending"] = int(cur.fetchone()["c"])
        except Exception:
            pass

        try:
            cur.execute(
                f"""
                SELECT COUNT(*)::int AS c
                FROM {DELIVERY_TABLE}
                WHERE active IS TRUE
                """
            )
            payload["kpis"]["activeDeliverymen"] = int(cur.fetchone()["c"])
        except Exception:
            pass

        # Série de receita
        if date_from and date_to:
            cur.execute(
                f"""
                SELECT to_char(d::date,'DD/MM') AS formatted_date,
                       COALESCE(SUM(o.total_amount),0) AS daily_revenue
                FROM generate_series(%s::date, %s::date, '1 day') AS d
                LEFT JOIN {ORDERS_TABLE} o
                  ON o.created_at::date = d::date
                 AND o.status IN ('delivered','completed')
                GROUP BY d
                ORDER BY d
                """,
                (date_from, date_to),
            )
        else:
            cur.execute(
                f"""
                WITH days AS (
                  SELECT generate_series(CURRENT_DATE - INTERVAL '6 day', CURRENT_DATE, INTERVAL '1 day')::date AS d
                )
                SELECT to_char(d,'DD/MM') AS formatted_date,
                       COALESCE((
                         SELECT SUM(o.total_amount)
                         FROM {ORDERS_TABLE} o
                         WHERE o.status IN ('delivered','completed')
                           AND o.created_at::date = d
                       ),0) AS daily_revenue
                FROM days
                ORDER BY d
                """
            )
        payload["chartData"] = [dict(r) for r in cur.fetchall()]
        for it in payload["chartData"]:
            it["daily_revenue"] = float(it["daily_revenue"])

        # Recent orders (with optional from/to/limit)
        params = []
        where = []
        if date_from:
            where.append("created_at::date >= %s")
            params.append(date_from)
        if date_to:
            where.append("created_at::date <= %s")
            params.append(date_to)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        cur.execute(
            f"""
            SELECT id, client_name, restaurant_name, total_amount, status, created_at
            FROM {ORDERS_TABLE}
            {where_sql}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (*params, limit),
        )
        payload["recentOrders"] = [
            {
                "id": str(r["id"]),
                "client_name": r.get("client_name") or "Cliente",
                "restaurant_name": r.get("restaurant_name") or "Restaurante",
                "total_amount": float(r.get("total_amount") or 0),
                "status": r.get("status") or "desconhecido",
                "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
            }
            for r in cur.fetchall()
        ]

        # Distribuição por status
        cur.execute(f"SELECT status, COUNT(*)::int AS c FROM {ORDERS_TABLE} GROUP BY status")
        payload["ordersStatus"] = {(r["status"] or "desconhecido"): int(r["c"]) for r in cur.fetchall()}

        # Crescimento de clientes (últimos 7 dias)
        cur.execute(
            f"""
            WITH days AS (
              SELECT generate_series(CURRENT_DATE - INTERVAL '6 day', CURRENT_DATE, INTERVAL '1 day')::date AS d
            )
            SELECT
              to_char(d, 'DD/MM') AS formatted_date,
              COALESCE((
                SELECT COUNT(*) FROM {CLIENTS_TABLE} c
                WHERE c.created_at::date <= d
              ), 0)::int AS total_clients
            FROM days
            ORDER BY d
            """
        )
        payload["clientsGrowth"] = [dict(r) for r in cur.fetchall()]

    return payload


# -------------------------------------------------------------------
# Auth
# -------------------------------------------------------------------
@admin_bp.route("/login", methods=["POST"])
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

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT user_type FROM users WHERE id = %s", (str(user.id),))
            db_user = cur.fetchone()

        if not db_user or db_user["user_type"] != "admin":
            supabase.auth.sign_out()
            return jsonify({"status": "error", "message": "Acesso permitido apenas a administradores."}), 403

        log_admin_action(user.email, "Login", "Admin login successful", request)

        return jsonify(
            {
                "status": "success",
                "message": "Login de administrador realizado",
                "access_token": response.session.access_token,
                "data": {"user": {"id": user.id, "email": user.email, "user_type": db_user["user_type"]}},
            }
        ), 200
    except AuthApiError:
        return jsonify({"status": "error", "message": "Credenciais inválidas"}), 401
    except Exception as e:
        logger.exception("Erro no admin_login")
        return jsonify({"status": "error", "message": f"Erro inesperado: {str(e)}"}), 500


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


# -------------------------------------------------------------------
# Users
# -------------------------------------------------------------------
@admin_bp.route("/users", methods=["GET"])
@admin_required
def get_all_users():
    filter_user_type = request.args.get("user_type")
    filter_city = request.args.get("city")

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            params = []
            sql = """
                SELECT 
                    u.id, u.email, u.user_type, u.created_at,
                    COALESCE(cp.first_name || ' ' || cp.last_name,
                             rp.restaurant_name,
                             dp.first_name || ' ' || dp.last_name) AS full_name,
                    COALESCE(cp.address_city, rp.address_city, dp.address_city) AS city
                FROM users u
                LEFT JOIN client_profiles cp ON u.id = cp.user_id AND u.user_type = 'client'
                LEFT JOIN restaurant_profiles rp ON u.id = rp.user_id AND u.user_type = 'restaurant'
                LEFT JOIN delivery_profiles dp ON u.id = dp.user_id AND u.user_type = 'delivery'
            """
            where = []
            if filter_user_type and filter_user_type.lower() != "todos":
                where.append("u.user_type = %s")
                params.append(filter_user_type)
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


# -------------------------------------------------------------------
# Restaurants
# -------------------------------------------------------------------
@admin_bp.route("/restaurants", methods=["GET"])
@admin_required
def get_all_restaurants():
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT rp.*, u.created_at
                FROM restaurant_profiles rp
                JOIN users u ON rp.user_id = u.id
                ORDER BY u.created_at DESC;
                """
            )
            rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"status": "success", "data": rows}), 200
    except Exception as e:
        logger.exception("Erro em get_all_restaurants")
        return jsonify({"status": "error", "message": "Erro interno ao buscar restaurantes.", "detail": str(e)}), 500
    finally:
        conn.close()


# -------------------------------------------------------------------
# Dashboard (rota principal)
# -------------------------------------------------------------------
def _is_admin(user_type: str) -> bool:
    return user_type == "admin"


@admin_bp.route("/dashboard", methods=["GET", "OPTIONS"])
def admin_dashboard():
    if request.method == "OPTIONS":
        return jsonify({}), 204

    user_id, user_type, error = get_user_id_from_token(request.headers.get("Authorization"))
    if error:
        return error
    if not _is_admin(user_type):
        return jsonify({"error": "Acesso negado"}), 403

    # Payload padrão (fallback)
    resp = {
        "kpis": {
            "totalRevenue": 0.0,
            "ordersToday": 0,
            "averageTicket": 0.0,
            "newClientsToday": 0,
            "ordersInProgress": 0,
            "ordersCanceled": 0,
            "restaurantsPending": 0,
            "activeDeliverymen": 0,
        },
        "chartData": [],
        "recentOrders": [],
        "ordersStatus": {},
        "clientsGrowth": [],
    }

    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexão com banco"}), 500

        payload = _build_dashboard_payload(conn)
        conn.close()
        return jsonify(payload), 200

    except Exception:
        logger.exception("Erro no /api/admin/dashboard")
        return jsonify(resp), 200


# -------------------------------------------------------------------
# Rotas de compatibilidade com o front antigo
# -------------------------------------------------------------------
@admin_bp.route("/metrics", methods=["GET", "OPTIONS"])
@admin_required
def admin_metrics():
    if request.method == "OPTIONS":
        return jsonify({}), 204
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB connection error"}), 500
    try:
        payload = _build_dashboard_payload(conn, date_from, date_to)
        return jsonify({"status": "success", "data": payload["kpis"]}), 200
    finally:
        conn.close()


@admin_bp.route("/revenue-series", methods=["GET", "OPTIONS"])
@admin_required
def admin_revenue_series():
    if request.method == "OPTIONS":
        return jsonify({}), 204
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB connection error"}), 500
    try:
        payload = _build_dashboard_payload(conn, date_from, date_to)
        return jsonify({"status": "success", "data": payload["chartData"]}), 200
    finally:
        conn.close()


@admin_bp.route("/transactions", methods=["GET", "OPTIONS"])
@admin_required
def admin_transactions():
    if request.method == "OPTIONS":
        return jsonify({}), 204
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    limit = int(request.args.get("limit", 20))
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "DB connection error"}), 500
    try:
        payload = _build_dashboard_payload(conn, date_from, date_to, limit=limit)
        # Mantém o nome que o front espera
        return jsonify({"status": "success", "data": payload["recentOrders"]}), 200
    finally:
        conn.close()
