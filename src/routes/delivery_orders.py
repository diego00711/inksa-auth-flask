# inksa-auth-flask/src/routes/delivery_orders.py

import uuid
import traceback
import json
from flask import Blueprint, request, jsonify, g  # Adicionado current_app
import psycopg2
import psycopg2.extras
from datetime import date, timedelta, datetime, time
from decimal import Decimal
from functools import wraps
from flask_cors import cross_origin

# Importa as funções e o cliente supabase do nosso helper centralizado
from ..utils.helpers import get_db_connection, get_user_id_from_token

# Importa a nova função de gamificação
# Certifique-se de que este caminho está correto para o seu gamification_routes.py


# --- Decorator para Segurança (DUPLICADO - Idealmente, centralizar em um módulo de utilitários) ---
def delivery_token_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        conn = None
        try:
            auth_header = request.headers.get("Authorization")
            user_auth_id, user_type, error_response = get_user_id_from_token(
                auth_header
            )

            if error_response:
                return error_response

            if user_type != "delivery":
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Acesso não autorizado. Apenas para entregadores.",
                        }
                    ),
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

            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "SELECT id, user_id FROM delivery_profiles WHERE user_id = %s",
                    (user_auth_id,),
                )
                profile = cur.fetchone()

            if not profile:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Perfil de entregador não encontrado para este usuário",
                        }
                    ),
                    404,
                )

            g.profile_id = str(profile["id"])

            if "profile_id" in kwargs and kwargs["profile_id"] != g.profile_id:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "ID do perfil na URL não corresponde ao token.",
                        }
                    ),
                    403,
                )

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


# --- Encoder JSON Customizado (DUPLICADO - Idealmente, centralizar em um módulo de utilitários) ---
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


# Define o Blueprint para as rotas de pedidos do delivery
delivery_orders_bp = Blueprint("delivery_orders_bp", __name__)

# --- ROTAS DE PEDIDOS ---


@delivery_orders_bp.route("/orders", methods=["GET"])
@delivery_token_required
def get_my_orders():
    profile_id = g.profile_id
    status_filter = request.args.get("status", "all").lower()

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
            base_query = """
                SELECT o.*, 
                       cp.first_name || ' ' || cp.last_name AS client_name,
                       rp.restaurant_name
                FROM orders o
                LEFT JOIN client_profiles cp ON o.client_id = cp.id
                LEFT JOIN restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE o.delivery_id = %s
            """
            params = [profile_id]

            if status_filter != "all":
                base_query += " AND o.status = %s"
                params.append(status_filter.capitalize())

            base_query += " ORDER BY o.created_at DESC"
            cur.execute(base_query, tuple(params))
            orders = cur.fetchall()

            return (
                jsonify(
                    {
                        "status": "success",
                        "data": serialize_data_with_encoder([dict(o) for o in orders]),
                    }
                ),
                200,
            )

    except psycopg2.Error as e:
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


@delivery_orders_bp.route(
    "/orders/<uuid:order_id>", methods=["GET"], endpoint="get_single_order_details"
)
@cross_origin(
    origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000"],
    supports_credentials=True,
    methods=["GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)
@delivery_token_required
def get_order_details(order_id):
    profile_id = g.profile_id
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
            sql_query = """
                SELECT
                    o.id,
                    o.status,
                    rp.address_street || ', ' || rp.address_number || ', ' || rp.address_city || ' - ' || rp.address_neighborhood AS pickup_address, 
                    o.delivery_address,
                    o.total_amount,
                    o.delivery_fee,
                    o.total_amount_items AS subtotal,
                    o.items,
                    o.created_at,
                    CONCAT(cp.first_name, ' ', cp.last_name) AS client_name,
                    cp.phone AS client_phone,
                    rp.restaurant_name AS restaurant_name,
                    rp.phone AS restaurant_phone,
                    rp.address_street AS restaurant_street,
                    rp.address_number AS restaurant_number,
                    rp.address_city AS restaurant_city,
                    rp.address_neighborhood AS restaurant_neighborhood 
                FROM
                    orders o
                LEFT JOIN
                    client_profiles cp ON o.client_id = cp.id
                LEFT JOIN
                    restaurant_profiles rp ON o.restaurant_id = rp.id
                WHERE
                    o.id = %s AND o.delivery_id = %s;
            """
            cur.execute(sql_query, (str(order_id), str(profile_id)))
            order = cur.fetchone()

            if not order:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Pedido não encontrado ou não atribuído a você.",
                        }
                    ),
                    404,
                )

            order_data = dict(order)

            item_ids = [item["menu_item_id"] for item in order_data.get("items", [])]
            if item_ids:
                cur.execute(
                    "SELECT id, name FROM menu_items WHERE id = ANY(%s)", (item_ids,)
                )
                menu_items_map = {
                    str(item["id"]): item["name"] for item in cur.fetchall()
                }

                for item in order_data["items"]:
                    item["name"] = menu_items_map.get(
                        str(item["menu_item_id"]), "Item não encontrado"
                    )

            serialized_order = serialize_data_with_encoder(order_data)

            return jsonify({"status": "success", "data": serialized_order}), 200

    except psycopg2.Error as e:
        traceback.print_exc()
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Erro de banco de dados.",
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
                    "message": "Erro interno ao buscar detalhes do pedido.",
                    "detail": str(e),
                }
            ),
            500,
        )
    finally:
        if conn:
            conn.close()


@delivery_orders_bp.route("/orders/<uuid:order_id>/accept", methods=["POST"])
@cross_origin(
    origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000"],
    supports_credentials=True,
    methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)
@delivery_token_required
def accept_delivery(order_id):
    profile_id = g.profile_id
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
                "SELECT status FROM orders WHERE id = %s AND delivery_id = %s",
                (str(order_id), profile_id),
            )
            order = cur.fetchone()

            if not order:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Pedido não encontrado ou não atribuído a você.",
                        }
                    ),
                    404,
                )

            if order["status"] != "Pendente":  # Ajuste o status conforme seu DB
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": f"Pedido já está em status '{order['status']}' e não pode ser aceito.",
                        }
                    ),
                    400,
                )

            new_status = "Aceito"  # Ajuste o novo status conforme seu DB
            cur.execute(
                "UPDATE orders SET status = %s WHERE id = %s AND delivery_id = %s RETURNING id, status",
                (new_status, str(order_id), profile_id),
            )
            updated_order = cur.fetchone()
            conn.commit()

            if updated_order:
                return (
                    jsonify(
                        {
                            "status": "success",
                            "message": f"Pedido {str(order_id)} aceito com sucesso. Novo status: {updated_order['status']}",
                        }
                    ),
                    200,
                )
            else:
                conn.rollback()
                return (
                    jsonify(
                        {"status": "error", "message": "Falha ao aceitar o pedido."}
                    ),
                    500,
                )

    except psycopg2.Error as e:
        conn.rollback()
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
        conn.rollback()
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


@delivery_orders_bp.route("/orders/<uuid:order_id>/complete", methods=["POST"])
@cross_origin(
    origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000"],
    supports_credentials=True,
    methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)
@delivery_token_required
def complete_delivery(order_id):
    profile_id = g.profile_id
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
            current_status = ["Aceito", "Para Entrega"]
            cur.execute(
                "SELECT status, delivery_fee FROM orders WHERE id = %s AND delivery_id = %s AND status = ANY(%s)",
                (str(order_id), profile_id, current_status),
            )
            order = cur.fetchone()

            if not order:
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Pedido não encontrado, não atribuído a você ou em status inválido para conclusão.",
                        }
                    ),
                    404,
                )

            new_status = "Entregue"
            cur.execute(
                "UPDATE orders SET status = %s WHERE id = %s AND delivery_id = %s RETURNING id, status",
                (new_status, str(order_id), profile_id),
            )
            updated_order = cur.fetchone()

            cur.execute(
                "UPDATE delivery_profiles SET total_deliveries = COALESCE(total_deliveries, 0) + 1 WHERE id = %s",
                (profile_id,),
            )

            # --- NOVO: Chamar a rota interna para adicionar pontos ---
            points_for_delivery = (
                10  # Define quantos pontos a entrega vale. AJUSTE ESTE VALOR.
            )
            event_type = "delivery_completed"

            # Para chamar a função interna, precisamos importá-la de gamification_routes.py
            # Esta importação deve estar no topo do arquivo.
            from .gamification_routes import add_points_for_event

            points_added_successfully = add_points_for_event(
                profile_id=profile_id,
                profile_type="delivery",
                points=points_for_delivery,
                event_type=event_type,
                conn=conn,  # Passa a conexão para que seja parte da mesma transação
                order_id=order_id,  # Passa o order_id para o histórico de pontos
            )

            if not points_added_successfully:
                print(
                    "AVISO: Falha ao adicionar pontos ao entregador. A transação da entrega será revertida."
                )
                conn.rollback()
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Pedido concluído, mas falha ao atribuir pontos.",
                        }
                    ),
                    500,
                )

            conn.commit()  # Commit a transação AGORA, incluindo a atualização do pedido e dos pontos

            if updated_order:
                return (
                    jsonify(
                        {
                            "status": "success",
                            "message": f"Pedido {str(order_id)} concluído com sucesso e pontos atribuídos.",
                        }
                    ),
                    200,
                )
            else:
                conn.rollback()
                return (
                    jsonify(
                        {
                            "status": "error",
                            "message": "Falha ao atualizar o status do pedido",
                        }
                    ),
                    500,
                )

    except psycopg2.Error as e:
        conn.rollback()
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
        conn.rollback()
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
