# inksa-auth-flask/src/routes/gamification_routes.py

import uuid
import traceback
import json
from flask import Blueprint, request, jsonify, g, current_app
import psycopg2
import psycopg2.extras
from datetime import date, timedelta, datetime, time
from decimal import Decimal
from functools import wraps

# <<< CORREÇÃO: Removido o import do cross_origin, pois já é tratado globalmente >>>
# from flask_cors import cross_origin

# Importa as funções e o cliente supabase do nosso helper centralizado
from ..utils.helpers import get_db_connection, get_user_id_from_token


# --- Decorators e Encoders ---
class CustomJSONEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, (datetime, date, timedelta, time)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)


def serialize_data_with_encoder(data):
    return json.loads(json.dumps(data, cls=CustomJSONEncoder))


def gamification_token_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Este decorador já está correto, sem necessidade de alterações.
        conn = None
        try:
            auth_header = request.headers.get("Authorization")
            user_auth_id, user_type, error_response = get_user_id_from_token(
                auth_header
            )
            if error_response:
                return error_response
            if user_type not in ["client", "delivery", "restaurant", "admin"]:
                return (
                    jsonify({"status": "error", "message": "Acesso não autorizado"}),
                    403,
                )
            conn = get_db_connection()
            if not conn:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Erro de conexão com o banco de dados",
                        }
                    ),
                    500,
                )
            g.user_id = user_auth_id
            g.user_type = user_type
            return f(*args, **kwargs)
        except psycopg2.Error as e:
            traceback.print_exc()
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Erro de banco de dados",
                        "detail": str(e),
                    }
                ),
                500,
            )
        except Exception as e:
            traceback.print_exc()
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Erro interno do servidor",
                        "detail": str(e),
                    }
                ),
                500,
            )
        finally:
            if conn:
                conn.close()

    return decorated_function


gamification_bp = Blueprint("gamification_bp", __name__)


# --- FUNÇÃO: add_points_for_event ---
# (código sem alterações)
def add_points_for_event(
    user_id, profile_type, points, event_type, conn=None, order_id=None
):
    should_close_conn = False
    if conn is None:
        conn = get_db_connection()
        if not conn:
            current_app.logger.error(
                "Não foi possível obter conexão com o BD para adicionar pontos."
            )
            return False
        should_close_conn = True
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                INSERT INTO user_points (user_id, total_points, last_updated) VALUES (%s, %s, NOW())
                ON CONFLICT (user_id) DO UPDATE SET total_points = user_points.total_points + EXCLUDED.total_points, last_updated = NOW()
                RETURNING total_points;
            """,
                (user_id, points),
            )
            updated_total_points = cur.fetchone()["total_points"]
            cur.execute(
                """
                INSERT INTO points_history (user_id, points_earned, points_type, description, order_id, created_at)
                VALUES (%s, %s, %s, %s, %s, NOW());
            """,
                (user_id, points, event_type, f"Pontos por {event_type}", order_id),
            )
            cur.execute(
                """
                WITH level_info AS (SELECT MAX(level_number) as new_level FROM levels WHERE points_required <= %s)
                UPDATE user_points SET 
                    current_level = COALESCE((SELECT new_level FROM level_info), 1),
                    points_to_next_level = COALESCE((SELECT points_required FROM levels WHERE level_number = (SELECT new_level FROM level_info) + 1) - %s, 0)
                WHERE user_id = %s
            """,
                (updated_total_points, updated_total_points, user_id),
            )
            if should_close_conn:
                conn.commit()
            return True
    except psycopg2.Error as e:
        if should_close_conn:
            conn.rollback()
        current_app.logger.error(f"Erro de DB ao adicionar pontos: {str(e)}")
        traceback.print_exc()
        return False
    except Exception as e:
        if should_close_conn:
            conn.rollback()
        current_app.logger.error(f"Erro ao adicionar pontos: {str(e)}")
        traceback.print_exc()
        return False
    finally:
        if should_close_conn and conn:
            conn.close()


# --- ROTAS DE GAMIFICAÇÃO ---


@gamification_bp.route("/add-points-internal", methods=["POST"])
def add_points_internal_route():
    data = request.get_json()
    user_id, profile_type, points, event_type = (
        data.get("user_id"),
        data.get("profile_type"),
        data.get("points"),
        data.get("event_type"),
    )
    order_id = data.get("order_id", None)
    if not all([user_id, profile_type, points, event_type]):
        return (
            jsonify({"status": "error", "message": "Campos obrigatórios ausentes"}),
            400,
        )
    success = add_points_for_event(
        user_id, profile_type, points, event_type, order_id=order_id
    )
    if success:
        return (
            jsonify(
                {"status": "success", "message": "Pontos adicionados com sucesso."}
            ),
            200,
        )
    else:
        return (
            jsonify({"status": "error", "message": "Falha ao adicionar pontos."}),
            500,
        )


@gamification_bp.route("/<string:user_id>/points-level", methods=["GET"])
@gamification_token_required
def get_user_points_and_level(user_id):
    if g.user_type != "admin" and user_id != g.user_id:
        return jsonify({"status": "error", "message": "Acesso não autorizado"}), 403

    conn = get_db_connection()
    if not conn:
        return (
            jsonify(
                {"status": "error", "message": "Erro de conexão com o banco de dados"}
            ),
            500,
        )

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # <<< CORREÇÃO APLICADA AQUI: Removida a coluna 'l.description' que não existe >>>
            cur.execute(
                """
                SELECT 
                    up.total_points,
                    up.current_level,
                    up.points_to_next_level,
                    l.level_name
                FROM user_points up
                LEFT JOIN levels l ON up.current_level = l.level_number
                WHERE up.user_id = %s
            """,
                (user_id,),
            )

            result = cur.fetchone()

            if not result:
                cur.execute(
                    """
                    INSERT INTO user_points (user_id, total_points, current_level, points_to_next_level)
                    VALUES (%s, 0, 1, 100)
                    RETURNING total_points, current_level, points_to_next_level
                """,
                    (user_id,),
                )
                result = cur.fetchone()
                conn.commit()

                cur.execute("SELECT level_name FROM levels WHERE level_number = 1")
                level_info = cur.fetchone() or {"level_name": "Iniciante"}
                result_dict = dict(result)
                result_dict.update(level_info)
                result = result_dict

            return (
                jsonify(
                    {
                        "status": "success",
                        "data": serialize_data_with_encoder(dict(result)),
                    }
                ),
                200,
            )

    except psycopg2.Error as e:
        traceback.print_exc()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro de banco de dados",
                    "detail": str(e),
                }
            ),
            500,
        )
    except Exception as e:
        traceback.print_exc()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro interno do servidor",
                    "detail": str(e),
                }
            ),
            500,
        )
    finally:
        if conn:
            conn.close()


@gamification_bp.route("/<string:user_id>/badges", methods=["GET"])
@gamification_token_required
def get_user_badges(user_id):
    if g.user_type != "admin" and user_id != g.user_id:
        return jsonify({"status": "error", "message": "Acesso não autorizado"}), 403

    conn = get_db_connection()
    if not conn:
        return (
            jsonify(
                {"status": "error", "message": "Erro de conexão com o banco de dados"}
            ),
            500,
        )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT ub.id, ub.badge_id, ub.earned_at, b.name, b.description, b.icon_url, b.points_reward 
                FROM user_badges ub JOIN badges b ON ub.badge_id = b.id
                WHERE ub.user_id = %s ORDER BY ub.earned_at DESC
            """,
                (user_id,),
            )
            badges = cur.fetchall()
            return (
                jsonify(
                    {
                        "status": "success",
                        "data": {
                            "userId": user_id,
                            "profileType": g.user_type,
                            "badges": serialize_data_with_encoder(
                                [dict(b) for b in badges]
                            ),
                        },
                    }
                ),
                200,
            )
    except psycopg2.Error as e:
        traceback.print_exc()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro de banco de dados",
                    "detail": str(e),
                }
            ),
            500,
        )
    except Exception as e:
        traceback.print_exc()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro interno do servidor",
                    "detail": str(e),
                }
            ),
            500,
        )
    finally:
        if conn:
            conn.close()


@gamification_bp.route("/rankings", methods=["GET"])
@gamification_token_required
def get_global_rankings():
    conn = get_db_connection()
    if not conn:
        return (
            jsonify(
                {"status": "error", "message": "Erro de conexão com o banco de dados"}
            ),
            500,
        )
    try:
        filter_type = request.args.get("type")
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            sql_query = """
                SELECT up.user_id, up.total_points, l.level_name,
                    COALESCE(dp.first_name || ' ' || dp.last_name, cp.first_name || ' ' || cp.last_name, rp.restaurant_name, 'Anônimo') AS profile_name,
                    CASE
                        WHEN dp.user_id IS NOT NULL THEN 'delivery'
                        WHEN cp.user_id IS NOT NULL THEN 'client'
                        WHEN rp.id IS NOT NULL THEN 'restaurant'
                        ELSE 'unknown'
                    END AS profile_type
                FROM user_points up
                LEFT JOIN levels l ON up.current_level = l.level_number
                LEFT JOIN delivery_profiles dp ON up.user_id = dp.user_id
                LEFT JOIN client_profiles cp ON up.user_id = cp.user_id
                LEFT JOIN restaurant_profiles rp ON up.user_id = rp.id
            """
            params = []
            if filter_type in ["client", "delivery", "restaurant"]:
                if filter_type == "restaurant":
                    sql_query += " WHERE rp.id IS NOT NULL"
                else:
                    profile_table_alias = {"client": "cp", "delivery": "dp"}[
                        filter_type
                    ]
                    sql_query += f" WHERE {profile_table_alias}.user_id IS NOT NULL"
            sql_query += " ORDER BY up.total_points DESC LIMIT 100"
            cur.execute(sql_query, tuple(params))
            rankings = cur.fetchall()
            return (
                jsonify(
                    {
                        "status": "success",
                        "data": serialize_data_with_encoder(
                            [dict(r) for r in rankings]
                        ),
                    }
                ),
                200,
            )
    except psycopg2.Error as e:
        traceback.print_exc()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro de banco de dados",
                    "detail": str(e),
                }
            ),
            500,
        )
    except Exception as e:
        traceback.print_exc()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro interno do servidor",
                    "detail": str(e),
                }
            ),
            500,
        )
    finally:
        if conn:
            conn.close()


# --- Rotas de Admin ---
@gamification_bp.route("/levels", methods=["GET", "POST"])
@gamification_token_required
def manage_levels():
    if g.user_type != "admin":
        return jsonify({"status": "error", "message": "Acesso não autorizado"}), 403
    conn = get_db_connection()
    if not conn:
        return (
            jsonify(
                {"status": "error", "message": "Erro de conexão com o banco de dados"}
            ),
            500,
        )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if request.method == "GET":
                cur.execute("SELECT * FROM levels ORDER BY points_required ASC")
                levels = cur.fetchall()
                return (
                    jsonify(
                        {
                            "status": "success",
                            "data": serialize_data_with_encoder(
                                [dict(l) for l in levels]
                            ),
                        }
                    ),
                    200,
                )
            elif request.method == "POST":
                data = request.get_json()
                required = ["level_number", "level_name", "points_required"]
                if not all(k in data for k in required):
                    return (
                        jsonify(
                            {
                                "status": "error",
                                "message": "Campos obrigatórios ausentes",
                            }
                        ),
                        400,
                    )
                cur.execute(
                    "INSERT INTO levels (level_number, level_name, points_required) VALUES (%s, %s, %s) RETURNING *",
                    (data["level_number"], data["level_name"], data["points_required"]),
                )
                new_level = cur.fetchone()
                conn.commit()
                return (
                    jsonify(
                        {
                            "status": "success",
                            "message": "Nível criado com sucesso",
                            "data": serialize_data_with_encoder(dict(new_level)),
                        }
                    ),
                    201,
                )
    except psycopg2.Error as e:
        conn.rollback()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro de banco de dados",
                    "detail": str(e),
                }
            ),
            500,
        )
    finally:
        if conn:
            conn.close()


@gamification_bp.route("/badges", methods=["GET", "POST"])
@gamification_token_required
def manage_badges():
    if g.user_type != "admin":
        return jsonify({"status": "error", "message": "Acesso não autorizado"}), 403
    conn = get_db_connection()
    if not conn:
        return (
            jsonify(
                {"status": "error", "message": "Erro de conexão com o banco de dados"}
            ),
            500,
        )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if request.method == "GET":
                cur.execute("SELECT * FROM badges ORDER BY name ASC")
                badges = cur.fetchall()
                return (
                    jsonify(
                        {
                            "status": "success",
                            "data": serialize_data_with_encoder(
                                [dict(b) for b in badges]
                            ),
                        }
                    ),
                    200,
                )
            elif request.method == "POST":
                data = request.get_json()
                required = ["name", "description", "icon_url", "points_reward"]
                if not all(k in data for k in required):
                    return (
                        jsonify(
                            {
                                "status": "error",
                                "message": "Campos obrigatórios ausentes",
                            }
                        ),
                        400,
                    )
                cur.execute(
                    "INSERT INTO badges (name, description, icon_url, points_reward) VALUES (%s, %s, %s, %s) RETURNING *",
                    (
                        data["name"],
                        data["description"],
                        data["icon_url"],
                        data["points_reward"],
                    ),
                )
                new_badge = cur.fetchone()
                conn.commit()
                return (
                    jsonify(
                        {
                            "status": "success",
                            "message": "Emblema criado com sucesso",
                            "data": serialize_data_with_encoder(dict(new_badge)),
                        }
                    ),
                    201,
                )
    except psycopg2.Error as e:
        conn.rollback()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro de banco de dados",
                    "detail": str(e),
                }
            ),
            500,
        )
    finally:
        if conn:
            conn.close()
