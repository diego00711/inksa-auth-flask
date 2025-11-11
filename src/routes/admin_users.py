import traceback
from datetime import datetime, timedelta
from functools import wraps

from flask import Blueprint, jsonify, request
from flask_cors import CORS
import psycopg2.extras

from ..utils.audit import log_admin_action_auto
from ..utils.helpers import get_db_connection, get_user_id_from_token


ALLOWED_ORIGINS = [
    "https://inksa-admin-v0-q4yqjmgnt-inksas-projects.vercel.app",
    "https://admin.inksadelivery.com.br",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


admin_users_bp = Blueprint("admin_users_bp", __name__)
legacy_admin_users_bp = Blueprint("legacy_admin_users_bp", __name__)

for _bp in (admin_users_bp, legacy_admin_users_bp):
    CORS(_bp, origins=ALLOWED_ORIGINS, supports_credentials=True)


DISPLAY_NAME_SQL = """
    COALESCE(
        CASE
            WHEN u.user_type = 'client' THEN COALESCE(cp.first_name, '') || ' ' || COALESCE(cp.last_name, '')
            WHEN u.user_type = 'restaurant' THEN rp.restaurant_name
            WHEN u.user_type = 'delivery' THEN COALESCE(dp.first_name, '') || ' ' || COALESCE(dp.last_name, '')
            ELSE u.email
        END,
        ''
    )
"""

PROFILE_JOINS_SQL = """
    LEFT JOIN client_profiles cp ON u.id = cp.user_id AND u.user_type = 'client'
    LEFT JOIN restaurant_profiles rp ON u.id = rp.user_id AND u.user_type = 'restaurant'
    LEFT JOIN delivery_profiles dp ON u.id = dp.user_id AND u.user_type = 'delivery'
"""


def admin_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        user_id, user_type, error_response = get_user_id_from_token(auth_header)

        if error_response:
            return error_response

        if user_type != "admin":
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Acesso não autorizado. Rota exclusiva para administradores.",
                    }
                ),
                403,
            )

        return func(*args, **kwargs)

    return wrapper


def get_user_status(user_data):
    full_name = (user_data or {}).get("full_name", "")
    return "active" if full_name and full_name.strip() else "inactive"


@admin_users_bp.route("/", methods=["GET"], strict_slashes=False)
@admin_required
def list_users():
    page = max(1, int(request.args.get("page", 1)))
    page_size = min(100, max(1, int(request.args.get("page_size", 20))))
    query = request.args.get("query", "").strip()
    status_filter = request.args.get("status", "all").lower()
    role_filter = request.args.get("role", "").strip()
    sort_param = request.args.get("sort", "created_at:desc").strip()

    if ":" in sort_param:
        sort_field, sort_direction = sort_param.split(":", 1)
        sort_direction = sort_direction.upper()
        if sort_direction not in ["ASC", "DESC"]:
            sort_direction = "DESC"
    else:
        sort_field = sort_param
        sort_direction = "DESC"

    allowed_sort_fields = ["created_at", "email", "user_type", "full_name"]
    if sort_field not in allowed_sort_fields:
        sort_field = "created_at"

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            base_query = f"""
                SELECT
                    u.id,
                    u.email,
                    u.user_type,
                    u.created_at,
                    {DISPLAY_NAME_SQL} AS full_name,
                    COALESCE(cp.address_city, rp.address_city, dp.address_city) AS city,
                    COALESCE(cp.phone, rp.phone, dp.phone) AS phone
                FROM users u
                {PROFILE_JOINS_SQL}
            """

            where_clauses = []
            params = []

            if role_filter:
                where_clauses.append("u.user_type = %s")
                params.append(role_filter)

            if query:
                where_clauses.append(
                    f"(u.email ILIKE %s OR {DISPLAY_NAME_SQL} ILIKE %s)"
                )
                like_value = f"%{query}%"
                params.extend([like_value, like_value])

            where_sql = ""
            if where_clauses:
                where_sql = " WHERE " + " AND ".join(where_clauses)

            count_query = (
                "SELECT COUNT(DISTINCT u.id) AS total "
                "FROM users u "
                f"{PROFILE_JOINS_SQL} "
                f"{where_sql}"
            )
            cur.execute(count_query, tuple(params))
            total_count = cur.fetchone()["total"]

            offset = (page - 1) * page_size
            final_query = (
                f"{base_query} {where_sql} ORDER BY {sort_field} {sort_direction} LIMIT %s OFFSET %s"
            )
            cur.execute(final_query, tuple(params + [page_size, offset]))
            rows = cur.fetchall()

        users = [dict(row) for row in rows]
        filtered_users = []
        for user in users:
            user["status"] = get_user_status(user)
            if status_filter == "all" or user["status"] == status_filter:
                filtered_users.append(user)

        if status_filter != "all":
            total_count = len(filtered_users)

        response = {
            "items": filtered_users,
            "total": total_count,
            "page": page,
            "page_size": page_size,
        }
        return jsonify(response), 200

    except Exception as exc:
        traceback.print_exc()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro interno ao buscar usuários.",
                    "detail": str(exc),
                }
            ),
            500,
        )
    finally:
        if conn:
            conn.close()


@admin_users_bp.route("/<uuid:user_id>", methods=["GET"], strict_slashes=False)
@admin_required
def get_user_detail(user_id):
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    detail_sql = f"""
        SELECT
            u.id,
            u.email,
            u.user_type,
            u.created_at,
            {DISPLAY_NAME_SQL} AS full_name,
            COALESCE(cp.address_city, rp.address_city, dp.address_city) AS city,
            COALESCE(cp.phone, rp.phone, dp.phone) AS phone,
            cp.first_name,
            cp.last_name,
            cp.cpf,
            cp.address_street,
            cp.address_number,
            cp.address_neighborhood,
            cp.address_city AS client_city,
            cp.address_state,
            cp.address_zipcode,
            rp.restaurant_name,
            rp.business_name,
            rp.cnpj,
            rp.address_street AS rest_address_street,
            rp.address_number AS rest_address_number,
            rp.address_neighborhood AS rest_address_neighborhood,
            rp.address_city AS rest_address_city,
            rp.address_state AS rest_address_state,
            rp.address_zipcode AS rest_address_zipcode,
            dp.first_name AS delivery_first_name,
            dp.last_name AS delivery_last_name,
            dp.cpf AS delivery_cpf,
            dp.birth_date,
            dp.vehicle_type
        FROM users u
        {PROFILE_JOINS_SQL}
        WHERE u.id = %s
    """

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(detail_sql, (str(user_id),))
            row = cur.fetchone()

        if not row:
            return jsonify({"status": "error", "message": "Usuário não encontrado"}), 404

        user = dict(row)
        user["status"] = get_user_status(user)
        return jsonify({"status": "success", "data": user}), 200

    except Exception as exc:
        traceback.print_exc()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro interno ao buscar usuário.",
                    "detail": str(exc),
                }
            ),
            500,
        )
    finally:
        if conn:
            conn.close()


@admin_users_bp.route("/summary", methods=["GET"], strict_slashes=False)
@admin_required
def get_users_summary():
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    summary_sql = f"""
        SELECT
            u.user_type,
            COUNT(*) AS total,
            COUNT(*) FILTER (
                WHERE CASE
                    WHEN u.user_type = 'admin' THEN TRUE
                    ELSE {DISPLAY_NAME_SQL} <> ''
                END
            ) AS active,
            COUNT(*) FILTER (
                WHERE CASE
                    WHEN u.user_type = 'admin' THEN FALSE
                    ELSE {DISPLAY_NAME_SQL} = ''
                END
            ) AS inactive,
            COUNT(*) FILTER (WHERE u.created_at >= NOW() - INTERVAL '7 days') AS last_7_days,
            COUNT(*) FILTER (WHERE u.created_at >= NOW() - INTERVAL '30 days') AS last_30_days
        FROM users u
        {PROFILE_JOINS_SQL}
        GROUP BY u.user_type
        ORDER BY u.user_type
    """

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(summary_sql)
            rows = cur.fetchall()

            summary = []
            totals = {
                "total_users": 0,
                "active_users": 0,
                "inactive_users": 0,
                "new_last_7_days": 0,
                "new_last_30_days": 0,
            }

            for row in rows:
                row_dict = dict(row)
                summary.append(
                    {
                        "user_type": row_dict["user_type"],
                        "total": int(row_dict["total"] or 0),
                        "active": int(row_dict["active"] or 0),
                        "inactive": int(row_dict["inactive"] or 0),
                        "last_7_days": int(row_dict["last_7_days"] or 0),
                        "last_30_days": int(row_dict["last_30_days"] or 0),
                    }
                )

                totals["total_users"] += int(row_dict["total"] or 0)
                totals["active_users"] += int(row_dict["active"] or 0)
                totals["inactive_users"] += int(row_dict["inactive"] or 0)
                totals["new_last_7_days"] += int(row_dict["last_7_days"] or 0)
                totals["new_last_30_days"] += int(row_dict["last_30_days"] or 0)

            try:
                recent_limit = int(request.args.get("recent_limit", 10))
            except (TypeError, ValueError):
                recent_limit = 10
            recent_limit = max(1, min(recent_limit, 50))

            recent_sql = f"""
                SELECT
                    u.id,
                    u.email,
                    u.user_type,
                    u.created_at,
                    {DISPLAY_NAME_SQL} AS display_name
                FROM users u
                {PROFILE_JOINS_SQL}
                ORDER BY u.created_at DESC
                LIMIT %s
            """

            cur.execute(recent_sql, (recent_limit,))
            recent_rows = cur.fetchall()

        recent_signups = [
            {
                "id": str(row["id"]),
                "email": row["email"],
                "user_type": row["user_type"],
                "created_at": row["created_at"].isoformat()
                if isinstance(row["created_at"], datetime)
                else row["created_at"],
                "display_name": row["display_name"],
            }
            for row in recent_rows
        ]

        payload = {
            "summary": summary,
            "totals": totals,
            "recent_signups": recent_signups,
        }
        return jsonify({"status": "success", "data": payload}), 200

    except Exception as exc:
        traceback.print_exc()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro interno ao gerar métricas de usuários.",
                    "detail": str(exc),
                }
            ),
            500,
        )
    finally:
        if conn:
            conn.close()


@admin_users_bp.route("/signups-trend", methods=["GET"], strict_slashes=False)
@admin_required
def get_users_signups_trend():
    try:
        days = int(request.args.get("days", 30))
    except (TypeError, ValueError):
        days = 30

    days = max(1, min(days, 90))
    start_date = datetime.utcnow().date() - timedelta(days=days - 1)
    end_date = datetime.utcnow().date()

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    trend_sql = """
        SELECT
            created_at::date AS day,
            user_type,
            COUNT(*) AS total
        FROM users
        WHERE created_at >= %s
        GROUP BY 1, 2
        ORDER BY day ASC
    """

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(trend_sql, (start_date,))
            rows = cur.fetchall()

        data_by_day = {}
        for row in rows:
            day = row["day"]
            data_by_day.setdefault(day, {"total": 0, "by_type": {}})
            count = int(row["total"])
            data_by_day[day]["total"] += count
            data_by_day[day]["by_type"][row["user_type"]] = count

        series = []
        current = start_date
        while current <= end_date:
            day_data = data_by_day.get(current, {"total": 0, "by_type": {}})
            series.append(
                {
                    "date": current.isoformat(),
                    "total": day_data["total"],
                    "by_type": day_data["by_type"],
                }
            )
            current += timedelta(days=1)

        payload = {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "days": days,
            "series": series,
        }
        return jsonify({"status": "success", "data": payload}), 200

    except Exception as exc:
        traceback.print_exc()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro interno ao montar série de cadastros.",
                    "detail": str(exc),
                }
            ),
            500,
        )
    finally:
        if conn:
            conn.close()


@admin_users_bp.route("/<uuid:user_id>", methods=["PATCH"], strict_slashes=False)
@admin_required
def update_user(user_id):
    data = request.get_json()
    if not data:
        return (
            jsonify({"status": "error", "message": "Nenhum dado enviado para atualização."}),
            400,
        )

    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "error", "message": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id, user_type, email FROM users WHERE id = %s", (str(user_id),))
            user = cur.fetchone()

            if not user:
                return jsonify({"status": "error", "message": "Usuário não encontrado"}), 404

            updates = []
            params = []
            update_details = []

            if "user_type" in data:
                new_user_type = data["user_type"]
                valid_types = ["client", "restaurant", "delivery", "admin"]

                if new_user_type not in valid_types:
                    return (
                        jsonify(
                            {
                                "status": "error",
                                "message": "Tipo de usuário inválido. Deve ser um de: "
                                + ", ".join(valid_types),
                            }
                        ),
                        400,
                    )

                updates.append("user_type = %s")
                params.append(new_user_type)
                update_details.append(f"user_type: {user['user_type']} -> {new_user_type}")

            if "status" in data:
                new_status = data["status"]
                if new_status not in ["active", "inactive"]:
                    return (
                        jsonify(
                            {
                                "status": "error",
                                "message": "Status inválido. Deve ser 'active' ou 'inactive'.",
                            }
                        ),
                        400,
                    )
                update_details.append(f"status: -> {new_status}")

            if updates:
                params.append(str(user_id))
                update_query = f"UPDATE users SET {', '.join(updates)} WHERE id = %s"
                cur.execute(update_query, tuple(params))

                if cur.rowcount == 0:
                    conn.rollback()
                    return jsonify({"status": "error", "message": "Falha ao atualizar usuário"}), 500

                conn.commit()

            if update_details:
                log_admin_action_auto(
                    "UpdateUser",
                    f"Updated user {user['email']} (ID: {user_id}): {', '.join(update_details)}",
                )

        return jsonify({"status": "success", "message": "Usuário atualizado com sucesso."}), 200

    except Exception as exc:
        if conn:
            conn.rollback()
        traceback.print_exc()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro interno ao atualizar usuário.",
                    "detail": str(exc),
                }
            ),
            500,
        )
    finally:
        if conn:
            conn.close()


def _register_legacy_routes():
    legacy_admin_users_bp.add_url_rule(
        "/",
        endpoint="legacy_list_users",
        view_func=list_users,
        methods=["GET"],
        strict_slashes=False,
    )
    legacy_admin_users_bp.add_url_rule(
        "/summary",
        endpoint="legacy_users_summary",
        view_func=get_users_summary,
        methods=["GET"],
        strict_slashes=False,
    )
    legacy_admin_users_bp.add_url_rule(
        "/signups-trend",
        endpoint="legacy_users_signups_trend",
        view_func=get_users_signups_trend,
        methods=["GET"],
        strict_slashes=False,
    )
    legacy_admin_users_bp.add_url_rule(
        "/<uuid:user_id>",
        endpoint="legacy_get_user_detail",
        view_func=get_user_detail,
        methods=["GET"],
        strict_slashes=False,
    )
    legacy_admin_users_bp.add_url_rule(
        "/<uuid:user_id>",
        endpoint="legacy_update_user",
        view_func=update_user,
        methods=["PATCH"],
        strict_slashes=False,
    )


_register_legacy_routes()


__all__ = ("admin_users_bp", "legacy_admin_users_bp")

