"""
Public restaurant endpoints — no authentication required.
Registered at /api/restaurants (plural).

Assumes restaurant_profiles has boolean columns `approved` and `active`.
If those columns don't exist yet, add them via migration:
    ALTER TABLE restaurant_profiles
      ADD COLUMN IF NOT EXISTS approved BOOLEAN DEFAULT TRUE,
      ADD COLUMN IF NOT EXISTS active   BOOLEAN DEFAULT TRUE;
"""

import logging
import traceback
from datetime import datetime, date, time

import psycopg2
import psycopg2.extras
from flask import Blueprint, request, jsonify

from ..utils.helpers import get_db_connection

logger = logging.getLogger(__name__)

public_restaurants_bp = Blueprint("public_restaurants", __name__)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _serialize(obj):
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    return obj


def _get_conn():
    conn = get_db_connection()
    if not conn:
        raise RuntimeError("Erro de conexão com o banco de dados")
    return conn


def _close(conn):
    try:
        conn.close()
    except Exception:
        pass


# ─── GET /api/restaurants/ ───────────────────────────────────────────────────

@public_restaurants_bp.get("/")
@public_restaurants_bp.get("")
def list_restaurants():
    """
    Lista restaurantes aprovados e ativos.

    Query params
    ------------
    category  : filtra por category ou cuisine_type (ILIKE)
    search    : busca em restaurant_name (ILIKE)
    user_lat  : latitude do usuário (para calcular distância)
    user_lon  : longitude do usuário
    """
    category = (request.args.get("category") or "").strip()
    search   = (request.args.get("search")   or "").strip()
    user_lat = request.args.get("user_lat", type=float)
    user_lon = request.args.get("user_lon", type=float)

    conn = None
    try:
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:

            has_coords = bool(user_lat and user_lon)
            dist_expr = (
                "ROUND((earth_distance(ll_to_earth(latitude, longitude),"
                " ll_to_earth(%s, %s)) / 1000)::numeric, 2)"
                if has_coords else "NULL"
            )

            where = [
                "COALESCE(approved, TRUE) = TRUE",
                "COALESCE(active,   TRUE) = TRUE",
            ]
            params = []

            if has_coords:
                # params for dist_expr come first in SELECT
                params += [user_lat, user_lon]

            if category:
                where.append("(category ILIKE %s OR cuisine_type ILIKE %s)")
                params += [f"%{category}%", f"%{category}%"]

            if search:
                where.append("restaurant_name ILIKE %s")
                params.append(f"%{search}%")

            where_sql  = "WHERE " + " AND ".join(where)
            order_sql  = "ORDER BY distance_km ASC NULLS LAST" if has_coords else "ORDER BY restaurant_name"

            cur.execute(
                f"""
                SELECT
                    id,
                    restaurant_name,
                    COALESCE(trade_name, business_name)  AS trade_name,
                    logo_url,
                    NULL AS cover_url,
                    COALESCE(cuisine_type, category)     AS cuisine_type,
                    category,
                    is_open,
                    COALESCE(rating, 0)                  AS avg_rating,
                    COALESCE(delivery_fee, 0)            AS delivery_fee,
                    COALESCE(minimum_order, 0)           AS min_order_value,
                    delivery_time                        AS estimated_time,
                    delivery_type,
                    {dist_expr}                          AS distance_km
                FROM restaurant_profiles
                {where_sql}
                {order_sql}
                """,
                params if params else None,
            )

            rows = [_serialize(dict(r)) for r in cur.fetchall()]
        return jsonify({"status": "success", "data": rows}), 200

    except Exception as e:
        logger.exception("Erro ao listar restaurantes: %s", e)
        return jsonify({"error": "Erro ao buscar restaurantes"}), 500
    finally:
        if conn:
            _close(conn)


# ─── GET /api/restaurants/<restaurant_id> ───────────────────────────────────

@public_restaurants_bp.get("/<uuid:restaurant_id>")
def get_restaurant(restaurant_id):
    """
    Retorna os dados públicos de um restaurante aprovado e ativo.

    404  se não encontrado, não aprovado ou inativo.
    """
    conn = None
    try:
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT
                    id,
                    restaurant_name,
                    COALESCE(trade_name, business_name)           AS trade_name,
                    logo_url,
                    NULL AS cover_url,
                    description,
                    COALESCE(cuisine_type, category)              AS cuisine_type,
                    CONCAT_WS(', ',
                        NULLIF(TRIM(COALESCE(address_street,       '')), ''),
                        NULLIF(TRIM(COALESCE(address_number,       '')), ''),
                        NULLIF(TRIM(COALESCE(address_neighborhood, '')), ''),
                        NULLIF(TRIM(COALESCE(address_city,         '')), ''),
                        NULLIF(TRIM(COALESCE(address_state,        '')), '')
                    )                                             AS address,
                    is_open,
                    COALESCE(rating, 0)                           AS avg_rating,
                    COALESCE(delivery_fee, 0)                     AS delivery_fee,
                    COALESCE(minimum_order, 0)                    AS min_order_value,
                    delivery_time                                 AS estimated_time,
                    phone,
                    category,
                    delivery_type,
                    latitude,
                    longitude
                FROM restaurant_profiles
                WHERE id = %s
                  AND COALESCE(approved, TRUE) = TRUE
                  AND COALESCE(active,   TRUE) = TRUE
                """,
                (str(restaurant_id),),
            )
            row = cur.fetchone()

        if not row:
            return jsonify({"error": "Restaurante não encontrado"}), 404

        return jsonify({"status": "success", "data": _serialize(dict(row))}), 200

    except Exception as e:
        logger.exception("Erro ao buscar restaurante %s: %s", restaurant_id, e)
        return jsonify({"error": "Erro ao buscar restaurante"}), 500
    finally:
        if conn:
            _close(conn)


# ─── GET /api/restaurants/<restaurant_id>/menu ──────────────────────────────

@public_restaurants_bp.get("/<uuid:restaurant_id>/menu")
def get_restaurant_menu(restaurant_id):
    """
    Retorna o cardápio de um restaurante agrupado por categoria.

    Resposta
    --------
    {
      "status": "success",
      "categories": [
        {
          "name": "Hambúrgueres",
          "items": [
            { "id": "...", "name": "...", "description": "...",
              "price": 29.90, "image_url": "...", "available": true }
          ]
        }
      ]
    }
    """
    conn = None
    try:
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:

            # Validate restaurant exists and is accessible
            cur.execute(
                """
                SELECT id FROM restaurant_profiles
                WHERE id = %s
                  AND COALESCE(approved, TRUE) = TRUE
                  AND COALESCE(active,   TRUE) = TRUE
                """,
                (str(restaurant_id),),
            )
            if not cur.fetchone():
                return jsonify({"error": "Restaurante não encontrado"}), 404

            cur.execute(
                """
                SELECT
                    id,
                    name,
                    description,
                    price,
                    COALESCE(category, 'Outros') AS category,
                    is_available                 AS available,
                    image_url
                FROM menu_items
                WHERE restaurant_id = %s
                  AND is_available = TRUE
                ORDER BY category, name
                """,
                (str(restaurant_id),),
            )
            items = [_serialize(dict(r)) for r in cur.fetchall()]

        # Group by category in Python — preserves insertion order (Python 3.7+)
        grouped: dict[str, list] = {}
        for item in items:
            cat = item.pop("category") or "Outros"
            grouped.setdefault(cat, []).append(item)

        categories = [{"name": cat, "items": itms} for cat, itms in grouped.items()]
        return jsonify({"status": "success", "categories": categories}), 200

    except Exception as e:
        logger.exception("Erro ao buscar cardápio do restaurante %s: %s", restaurant_id, e)
        return jsonify({"error": "Erro ao buscar cardápio"}), 500
    finally:
        if conn:
            _close(conn)
