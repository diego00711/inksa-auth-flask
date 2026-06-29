# src/routes/admin.py
import re
import logging
from functools import wraps

from flask import Blueprint, request, jsonify, current_app
from flask_cors import CORS
import psycopg2
import psycopg2.extras

from gotrue.errors import AuthApiError

from ..utils.helpers import get_db_connection, get_user_id_from_token, supabase, _extract_bearer_token
from ..utils.audit import log_admin_action, log_admin_action_auto

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

def _build_dashboard_payload(conn, date_from=None, date_to=None, limit=10):
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
        conn, f"SELECT COUNT(*)::int FROM {ORDERS_TABLE} WHERE created_at::date = CURRENT_DATE", default=0))
    payload["kpis"]["newClientsToday"] = _safe_int(_fetchval(
        conn, f"SELECT COUNT(*)::int FROM {CLIENTS_TABLE} WHERE created_at::date = CURRENT_DATE", default=0))

    row = _fetchrow(conn, f"""
        SELECT
          SUM(CASE WHEN status IN ('preparing','on_the_way','in_progress') THEN 1 ELSE 0 END)::int AS in_progress,
          SUM(CASE WHEN status IN ('cancelled','canceled') THEN 1 ELSE 0 END)::int AS canceled
        FROM {ORDERS_TABLE}
    """) or {}
    payload["kpis"]["ordersInProgress"] = _safe_int(row.get("in_progress"))
    payload["kpis"]["ordersCanceled"]   = _safe_int(row.get("canceled"))

    payload["kpis"]["restaurantsPending"] = _safe_int(_fetchval(
        conn, f"SELECT COUNT(*)::int FROM {RESTAURANTS_TABLE} WHERE (approved IS FALSE) OR (status='pending')", default=0))
    payload["kpis"]["activeDeliverymen"] = _safe_int(_fetchval(
        conn, f"SELECT COUNT(*)::int FROM {DELIVERY_TABLE} WHERE active IS TRUE", default=0))

    # Série receita
    if date_from and date_to:
        chart_rows = _fetchall(conn, f"""
            SELECT to_char(d::date,'DD/MM') AS formatted_date,
                   COALESCE(SUM(o.total_amount),0) AS daily_revenue
              FROM generate_series(%s::date, %s::date, '1 day') AS d
         LEFT JOIN {ORDERS_TABLE} o
                ON o.created_at::date = d::date AND o.status IN ('delivered','completed')
          GROUP BY d ORDER BY d
        """, (date_from, date_to))
    else:
        chart_rows = _fetchall(conn, f"""
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
              FROM days ORDER BY d
        """)
    for r in chart_rows:
        r["daily_revenue"] = _safe_float(r.get("daily_revenue"))
    payload["chartData"] = chart_rows

    # Recentes
    params, where = [], []
    if date_from:
        where.append("created_at::date >= %s"); params.append(date_from)
    if date_to:
        where.append("created_at::date <= %s"); params.append(date_to)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    recent_rows = _fetchall(conn, f"""
        SELECT id, client_name, restaurant_name, total_amount, status, created_at
          FROM {ORDERS_TABLE}
        {where_sql}
      ORDER BY created_at DESC
         LIMIT %s
    """, (*params, limit))
    payload["recentOrders"] = [{
        "id": str(r.get("id")),
        "client_name": r.get("client_name") or "Cliente",
        "restaurant_name": r.get("restaurant_name") or "Restaurante",
        "total_amount": _safe_float(r.get("total_amount")),
        "status": r.get("status") or "desconhecido",
        "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
    } for r in recent_rows]

    # Status
    status_rows = _fetchall(conn, f"SELECT status, COUNT(*)::int AS c FROM {ORDERS_TABLE} GROUP BY status")
    payload["ordersStatus"] = {(r.get("status") or "desconhecido"): _safe_int(r.get("c")) for r in status_rows}

    # Crescimento clientes
    payload["clientsGrowth"] = _fetchall(conn, f"""
        WITH days AS (
          SELECT generate_series(CURRENT_DATE - INTERVAL '6 day', CURRENT_DATE, INTERVAL '1 day')::date AS d
        )
        SELECT to_char(d,'DD/MM') AS formatted_date,
               COALESCE((SELECT COUNT(*) FROM {CLIENTS_TABLE} c WHERE c.created_at::date <= d),0)::int AS total_clients
          FROM days ORDER BY d
    """)

    return payload

# --------- Auth ---------
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
            "data": {"user": {"id": user.id, "email": user.email, "user_type": db_user["user_type"]}},
        }), 200
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
                    u.id, u.email, u.user_type, u.created_at,
                    COALESCE(cp.first_name || ' ' || cp.last_name,
                             rp.restaurant_name,
                             dp.first_name || ' ' || dp.last_name) AS full_name,
                    COALESCE(cp.address_city, rp.address_city, dp.address_city) AS city
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
                SELECT rp.*, u.created_at
                  FROM restaurant_profiles rp
                  JOIN users u ON rp.user_id = u.id
              ORDER BY u.created_at DESC;
            """)
            rows = [dict(r) for r in cur.fetchall()]
        return jsonify({"status": "success", "data": rows}), 200
    except Exception as e:
        logger.exception("Erro em get_all_restaurants")
        return jsonify({"status": "error", "message": "Erro interno ao buscar restaurantes.", "detail": str(e)}), 500
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

        supabase.auth.admin.update_user_by_id(str(user.id), {"password": new_password})
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

        result = supabase.auth.admin.invite_user_by_email(email)
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


_INCIDENT_RESOLUTIONS = {"pending", "returned", "refunded", "retry", "closed"}

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
                SELECT di.refund_status, di.order_id, o.id_transacao_mp
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
                return jsonify({"status": "error", "message": "Pedido sem transação do Mercado Pago"}), 400

            sdk = current_app.mp_sdk
            if sdk is None:
                return jsonify({"status": "error", "message": "Mercado Pago indisponível"}), 503

            result = sdk.refund().create(payment_id)
            resp = result.get("response", {}) if isinstance(result, dict) else {}
            code = result.get("status", 200) if isinstance(result, dict) else 200
            if code >= 400:
                logger.error("MP recusou reembolso: %s", resp)
                return jsonify({"status": "error", "message": "Mercado Pago recusou o reembolso"}), 400

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
