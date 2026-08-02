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
    city     = (request.args.get("city")     or "").strip()  # filtra por cidade escolhida
    user_lat = request.args.get("user_lat", type=float)
    user_lon = request.args.get("user_lon", type=float)
    # Paginação: cap padrão evita payload ilimitado conforme o catálogo cresce
    limit  = request.args.get("limit",  default=50, type=int)
    offset = request.args.get("offset", default=0,  type=int)
    limit  = max(1, min(limit, 100))
    offset = max(0, offset)

    conn = None
    try:
        conn = _get_conn()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:

            has_coords = bool(user_lat and user_lon)
            dist_expr = (
                "ROUND((earth_distance(ll_to_earth(rp.latitude, rp.longitude),"
                " ll_to_earth(%s, %s)) / 1000)::numeric, 2)"
                if has_coords else "NULL"
            )

            # Só restaurantes aprovados/ativos (e cujo usuário não foi
            # desativado pelo admin) aparecem publicamente.
            where = ["COALESCE(rp.approved, TRUE) = TRUE", "COALESCE(rp.active, TRUE) = TRUE",
                     "COALESCE(u.is_active, TRUE) = TRUE"]
            params = []

            # Clube: destaque = restaurantes cujo volume do mês alcança um nível
            # com featured_listing. threshold = menor min_activity desses níveis.
            cur.execute("""
                SELECT MIN(min_activity) AS t FROM public.club_levels
                 WHERE audience = 'restaurant' AND is_active
                   AND COALESCE((benefits->>'featured_listing')::boolean, false) = true
            """)
            _ft = cur.fetchone()
            featured_threshold = _ft['t'] if _ft and _ft['t'] is not None else None

            if has_coords:
                # params for dist_expr come first in SELECT
                params += [user_lat, user_lon]
            # params do CASE de destaque (logo após o dist_expr, ainda no SELECT)
            params += [featured_threshold, featured_threshold]

            # Filtro por RAIO de atendimento: quando temos as coordenadas do
            # cliente, só mostra restaurantes dentro de service_radius_km dele —
            # é o que separa as cidades (cliente vê só o que dá pra atender).
            # Restaurantes sem coordenadas continuam aparecendo (fail-open) pra
            # não sumirem por falta de geocode.
            # Quando o cliente ESCOLHE uma cidade no seletor, ignora o raio (ele
            # quer ver aquela cidade, mesmo estando longe/em outro lugar).
            if has_coords and not city:
                from ..utils.platform_settings import get_settings as _get_settings
                radius_km = float(_get_settings()["platform_max_delivery_radius"])
                where.append(
                    "(rp.latitude IS NULL OR rp.longitude IS NULL OR "
                    "earth_distance(ll_to_earth(rp.latitude, rp.longitude), ll_to_earth(%s, %s)) <= %s)"
                )
                params += [user_lat, user_lon, radius_km * 1000.0]

            if category:
                where.append("(rp.category ILIKE %s OR rp.cuisine_type ILIKE %s)")
                params += [f"%{category}%", f"%{category}%"]

            if search:
                where.append("rp.restaurant_name ILIKE %s")
                params.append(f"%{search}%")

            if city:
                where.append("rp.address_city ILIKE %s")
                params.append(city)

            where_sql  = "WHERE " + " AND ".join(where)
            _base_order = "distance_km ASC NULLS LAST" if has_coords else "rp.restaurant_name"
            order_sql  = f"ORDER BY is_featured DESC, {_base_order}"

            # Busca limit+1 para saber se há próxima página sem um COUNT extra
            params += [limit + 1, offset]

            cur.execute(
                f"""
                SELECT
                    rp.id,
                    rp.restaurant_name,
                    COALESCE(rp.trade_name, rp.business_name)  AS trade_name,
                    rp.logo_url,
                    NULL AS cover_url,
                    COALESCE(rp.cuisine_type, rp.category)     AS cuisine_type,
                    rp.category,
                    rp.is_open,
                    COALESCE((SELECT ROUND(AVG(r.rating)::numeric, 1) FROM restaurant_reviews r WHERE r.restaurant_id = rp.id), 0)                     AS rating,
                    COALESCE(rp.delivery_fee, 0)                AS delivery_fee,
                    COALESCE(rp.minimum_order, 0)                AS minimum_order,
                    rp.delivery_time                            AS delivery_time,
                    rp.delivery_type,
                    {dist_expr}                                 AS distance_km,
                    CASE WHEN %s::int IS NOT NULL AND (
                            SELECT COUNT(*) FROM orders o
                             WHERE o.restaurant_id = rp.id AND o.status = 'delivered'
                               AND DATE_TRUNC('month', o.created_at) = DATE_TRUNC('month', NOW())
                         ) >= %s::int THEN 1 ELSE 0 END           AS is_featured
                FROM restaurant_profiles rp
                LEFT JOIN users u ON u.id = rp.user_id
                {where_sql}
                {order_sql}
                LIMIT %s OFFSET %s
                """,
                params,
            )

            rows = [_serialize(dict(r)) for r in cur.fetchall()]
            has_more = len(rows) > limit
            rows = rows[:limit]
        return jsonify({
            "status": "success",
            "data": rows,
            "has_more": has_more,
            "limit": limit,
            "offset": offset,
        }), 200

    except Exception as e:
        logger.exception("Erro ao listar restaurantes: %s", e)
        return jsonify({"error": "Erro ao buscar restaurantes"}), 500
    finally:
        if conn:
            _close(conn)


# ─── GET /api/restaurants/cities ────────────────────────────────────────────
# Cidades que TÊM restaurante aprovado/ativo — alimenta o seletor de cidade do
# cliente (ele filtra os restaurantes pela cidade escolhida).

@public_restaurants_bp.get("/cities")
def list_cities():
    conn = None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT NULLIF(TRIM(address_city), '') AS city
                  FROM restaurant_profiles
                 WHERE COALESCE(approved, TRUE) = TRUE
                   AND COALESCE(active, TRUE) = TRUE
                   AND NULLIF(TRIM(address_city), '') IS NOT NULL
                 ORDER BY city
            """)
            cities = [r[0] for r in cur.fetchall()]
        return jsonify(cities), 200
    except Exception as e:
        logger.error(f"Erro em list_cities: {e}")
        return jsonify([]), 200  # fail-soft: sem cidades, o seletor só não abre
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
                    rp.id,
                    rp.restaurant_name,
                    COALESCE(rp.trade_name, rp.business_name)     AS trade_name,
                    rp.logo_url,
                    NULL AS cover_url,
                    rp.description,
                    COALESCE(rp.cuisine_type, rp.category)        AS cuisine_type,
                    CONCAT_WS(', ',
                        NULLIF(TRIM(COALESCE(rp.address_street,       '')), ''),
                        NULLIF(TRIM(COALESCE(rp.address_number,       '')), ''),
                        NULLIF(TRIM(COALESCE(rp.address_neighborhood, '')), ''),
                        NULLIF(TRIM(COALESCE(rp.address_city,         '')), ''),
                        NULLIF(TRIM(COALESCE(rp.address_state,        '')), '')
                    )                                             AS address,
                    rp.is_open,
                    COALESCE((SELECT ROUND(AVG(r.rating)::numeric, 1) FROM restaurant_reviews r WHERE r.restaurant_id = rp.id), 0)                        AS rating,
                    COALESCE(rp.delivery_fee, 0)                  AS delivery_fee,
                    COALESCE(rp.minimum_order, 0)                 AS minimum_order,
                    rp.delivery_time                              AS delivery_time,
                    rp.phone,
                    rp.category,
                    rp.delivery_type,
                    COALESCE(rp.accepts_cash, TRUE) AS accepts_cash,
                    rp.latitude,
                    rp.longitude
                FROM restaurant_profiles rp
                LEFT JOIN users u ON u.id = rp.user_id
                WHERE rp.id = %s
                  AND COALESCE(rp.approved, TRUE) = TRUE
                  AND COALESCE(rp.active, TRUE) = TRUE
                  AND COALESCE(u.is_active, TRUE) = TRUE
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

            # Validate restaurant exists and is accessible (aprovado/ativo)
            cur.execute(
                """
                SELECT rp.id
                FROM restaurant_profiles rp
                LEFT JOIN users u ON u.id = rp.user_id
                WHERE rp.id = %s
                  AND COALESCE(rp.approved, TRUE) = TRUE
                  AND COALESCE(rp.active, TRUE) = TRUE
                  AND COALESCE(u.is_active, TRUE) = TRUE
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
