# src/routes/categories.py
from flask import Blueprint, jsonify, request
import psycopg2
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token

categories_bp = Blueprint("categories_bp", __name__)


@categories_bp.route("/", methods=["GET"])
def get_categories():
    user_id, user_type, error = get_user_id_from_token(
        request.headers.get("Authorization")
    )
    if error:
        return error
    if user_type != "restaurant":
        return jsonify({"error": "Acesso não autorizado"}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT id, name FROM menu_categories WHERE restaurant_id = %s ORDER BY name ASC",
                (user_id,),
            )
            categories = [dict(row) for row in cur.fetchall()]
            return jsonify({"status": "success", "data": categories}), 200
    except Exception as e:
        print(e)
        return jsonify({"error": "Erro interno ao buscar categorias"}), 500
    finally:
        if conn:
            conn.close()


@categories_bp.route("/", methods=["POST"])
def add_category():
    user_id, user_type, error = get_user_id_from_token(
        request.headers.get("Authorization")
    )
    if error:
        return error
    if user_type != "restaurant":
        return jsonify({"error": "Acesso não autorizado"}), 403

    data = request.get_json()
    if not data or "name" not in data or not data["name"].strip():
        return jsonify({"error": "O nome da categoria é obrigatório."}), 400

    category_name = data["name"].strip()

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "INSERT INTO menu_categories (name, restaurant_id) VALUES (%s, %s) RETURNING id, name",
                (category_name, user_id),
            )
            new_category = dict(cur.fetchone())
            conn.commit()
            return jsonify({"status": "success", "data": new_category}), 201
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": f"A categoria '{category_name}' já existe."}), 409
    except Exception as e:
        conn.rollback()
        print(e)
        return jsonify({"error": "Erro interno ao adicionar categoria"}), 500
    finally:
        if conn:
            conn.close()


@categories_bp.route("/<category_id>", methods=["DELETE", "OPTIONS"])
def delete_category(category_id):
    if request.method == "OPTIONS":
        return "", 200

    user_id, user_type, error = get_user_id_from_token(
        request.headers.get("Authorization")
    )
    if error:
        return error
    if user_type != "restaurant":
        return jsonify({"error": "Acesso não autorizado"}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexão com o banco de dados"}), 500

    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM menu_categories WHERE id = %s AND restaurant_id = %s RETURNING id",
                (category_id, user_id),
            )
            deleted = cur.fetchone()
            if not deleted:
                return (
                    jsonify(
                        {
                            "error": "Categoria não encontrada ou não pertence a este restaurante"
                        }
                    ),
                    404,
                )
            conn.commit()
            return (
                jsonify(
                    {"status": "success", "message": "Categoria excluída com sucesso"}
                ),
                200,
            )
    except Exception as e:
        conn.rollback()
        print(e)
        return jsonify({"error": "Erro interno ao excluir categoria"}), 500
    finally:
        if conn:
            conn.close()
