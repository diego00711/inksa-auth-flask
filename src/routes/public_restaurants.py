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


# ─── vitrine de cupons ───────────────────────────────────────────────────────
# O cupom do parceiro só serve pra trazer pedido se o cliente souber que ele
# existe. Estas funções montam a "etiqueta de promoção" que aparece no card da
# loja e a lista de cupons da página dela.

# Ticket de referência usado só pra ORDENAR cupons entre si (qual vira a
# etiqueta do card). Não entra em nenhum cálculo de cobrança.
_TICKET_REFERENCIA = 50.0


def _rotulo_cupom(c, delivery_fee=0):
    tipo = c.get("discount_type")
    valor = float(c.get("discount_value") or 0)
    if tipo == "free_delivery":
        return "Frete grátis"
    if tipo == "percentage":
        return f"{valor:.0f}% OFF"
    return f"R$ {valor:.2f}".replace(".", ",") + " OFF"


def _peso_cupom(c, delivery_fee=0):
    """Quanto o cupom valeria num pedido de referência — só pra escolher qual
    aparece no card quando a loja tem mais de um."""
    tipo = c.get("discount_type")
    valor = float(c.get("discount_value") or 0)
    if tipo == "percentage":
        return _TICKET_REFERENCIA * valor / 100.0
    if tipo == "free_delivery":
        return float(delivery_fee or 0) or 6.0
    return valor


def _buscar_cupons_ativos(cur, restaurant_ids):
    """{restaurant_id: [cupons ativos]} para as lojas passadas.

    Uma consulta só pra lista inteira — um SELECT por card faria N+1 na home,
    que é a tela mais acessada do app.
    """
    if not restaurant_ids:
        return {}
    cur.execute(
        """
        SELECT id, restaurant_id, code, discount_type, discount_value,
               min_order_value, description
          FROM public.coupons
         -- ::uuid[] é obrigatório: a lista chega como texto e "uuid = text"
         -- não existe no Postgres — sem o cast a home inteira devolvia 500.
         WHERE restaurant_id = ANY(%s::uuid[])
           AND is_active IS TRUE
           AND (valid_until IS NULL OR valid_until >= NOW())
           AND (max_uses IS NULL OR COALESCE(uses_count, 0) < max_uses)
         ORDER BY created_at DESC
        """,
        (list(restaurant_ids),),
    )
    por_loja = {}
    for row in cur.fetchall():
        c = dict(row)
        c["id"] = str(c["id"])
        rid = str(c.pop("restaurant_id"))
        c["discount_value"] = float(c["discount_value"] or 0)
        c["min_order_value"] = float(c["min_order_value"] or 0)
        c["label"] = _rotulo_cupom(c)
        por_loja.setdefault(rid, []).append(c)
    return por_loja


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
    state    = (request.args.get("state")    or "").strip().upper()  # UF escolhida
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
                     "COALESCE(u.is_active, TRUE) = TRUE",
                     # SEM COORDENADAS A LOJA NÃO APARECE. Sem lat/lng não dá pra
                     # calcular frete nem distância, e ela escapava do filtro de
                     # raio — uma loja de outra cidade aparecia pra qualquer
                     # cliente. Fica escondida até geocodificar o endereço.
                     "rp.latitude IS NOT NULL", "rp.longitude IS NOT NULL",
                     # LOJA SEM NENHUM ITEM NO CARDÁPIO NÃO APARECE.
                     #
                     # Mesma lógica das coordenadas, um passo adiante: a gente
                     # já escondia o que quebra o FRETE, e deixava passar o que
                     # quebra a VENDA. Loja aberta e vazia é pior que loja
                     # fechada — o cliente entra, não acha nada pra pedir, e
                     # não volta. Aconteceu no primeiro dia do parceiro novo
                     # (18/08): loja aberta, zero itens, ninguém avisado.
                     #
                     # Escondida, não bloqueada: o parceiro segue mexendo no
                     # app à vontade e reaparece sozinho no minuto em que
                     # cadastrar o primeiro item. Bloquear seria receber quem
                     # está chegando com um "não".
                     """EXISTS (SELECT 1 FROM menu_items mi
                                 WHERE mi.restaurant_id = rp.id
                                   AND COALESCE(mi.is_available, TRUE) = TRUE)"""]
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
            # cliente, só mostra lojas dentro do raio da plataforma — é o que
            # separa as cidades (o cliente vê só o que dá pra atender).
            # Quando o cliente ESCOLHE uma cidade no seletor, ignora o raio (ele
            # quer ver aquela cidade, mesmo estando longe/em outro lugar).
            if has_coords and not city and not state:
                from ..utils.platform_settings import get_settings as _get_settings
                radius_km = float(_get_settings()["platform_max_delivery_radius"])
                # A loja de ENTREGA PRÓPRIA pode encurtar esse raio: ela cobra
                # taxa fixa em qualquer distância, então um pedido a 30 km sairia
                # do bolso dela. Quem não configurou (NULL) segue no raio da
                # plataforma — o de menor valor entre os dois é o que vale.
                # O CASE amarra o raio próprio ao delivery_type='own': se a loja
                # voltar pra entrega da plataforma, um valor antigo na coluna
                # não fica limitando ela em silêncio.
                where.append(
                    "earth_distance(ll_to_earth(rp.latitude, rp.longitude), ll_to_earth(%s, %s)) <= "
                    "LEAST(%s, COALESCE(CASE WHEN rp.delivery_type = 'own' "
                    "THEN rp.own_delivery_radius_km END, %s) * 1000.0)"
                )
                params += [user_lat, user_lon, radius_km * 1000.0, radius_km]

            if category:
                where.append("(rp.category ILIKE %s OR rp.cuisine_type ILIKE %s)")
                params += [f"%{category}%", f"%{category}%"]

            if search:
                where.append("rp.restaurant_name ILIKE %s")
                params.append(f"%{search}%")

            if city:
                where.append("rp.address_city ILIKE %s")
                params.append(city)

            # UF sozinha (sem cidade) = "me mostra o estado inteiro". Compara
            # normalizado porque no cadastro isso é texto livre: a base tem
            # 'SP' e 'Sp' hoje, e sem UPPER/TRIM a loja sumiria do filtro.
            if state:
                where.append("UPPER(TRIM(rp.address_state)) = %s")
                params.append(state)

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
                    COALESCE(rp.segment, 'restaurante')        AS segment,
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

            # Etiqueta de promoção no card: o melhor cupom ativo da loja.
            cupons_por_loja = _buscar_cupons_ativos(cur, [str(r["id"]) for r in rows])
            for r in rows:
                lista = cupons_por_loja.get(str(r["id"])) or []
                r["has_coupon"] = bool(lista)
                r["promo_label"] = (
                    max(lista, key=lambda c: _peso_cupom(c, r.get("delivery_fee")))["label"]
                    if lista else None
                )
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

@public_restaurants_bp.get("/states")
def list_states():
    """UFs que têm loja vendendo, com quantas cidades cada uma.

    Primeiro degrau do seletor (Estado → Cidade). Sem ele, ao abrir a segunda
    praça a lista de cidades vira uma rolagem sem fim e o cliente de Lages tem
    que caçar Lages no meio de tudo.

    A UF é normalizada aqui (UPPER + TRIM) porque no cadastro ela é texto
    livre: hoje a base tem 'SP' e 'Sp' convivendo. Sem normalizar, o mesmo
    estado apareceria duas vezes no seletor.

    Loja SEM UF preenchida não some do app — ela simplesmente não entra neste
    seletor. Recusar cadastro incompleto aqui esconderia loja que funciona.
    """
    conn = None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT UPPER(TRIM(address_state)) AS uf,
                       COUNT(DISTINCT LOWER(TRIM(address_city))) AS cidades
                  FROM restaurant_profiles
                 WHERE COALESCE(approved, TRUE) = TRUE
                   AND COALESCE(active, TRUE) = TRUE
                   AND NULLIF(TRIM(address_state), '') IS NOT NULL
                   AND NULLIF(TRIM(address_city), '')  IS NOT NULL
                 GROUP BY 1
                 ORDER BY 1
            """)
            estados = [{"uf": r[0], "cidades": int(r[1])} for r in cur.fetchall()]
        return jsonify(estados), 200
    except Exception as e:
        logger.error(f"Erro em list_states: {e}")
        return jsonify([]), 200  # fail-soft: sem estados, o seletor cai direto em cidades
    finally:
        if conn:
            _close(conn)


@public_restaurants_bp.get("/cities")
def list_cities():
    """Cidades com loja vendendo. `?state=SC` restringe à UF (segundo degrau).

    Sem o parâmetro devolve tudo — mantém funcionando qualquer versão do app
    que ainda não conheça o seletor de estado.
    """
    uf = (request.args.get("state") or "").strip().upper()
    conn = None
    try:
        conn = _get_conn()
        with conn.cursor() as cur:
            sql = """
                SELECT DISTINCT NULLIF(TRIM(address_city), '') AS city
                  FROM restaurant_profiles
                 WHERE COALESCE(approved, TRUE) = TRUE
                   AND COALESCE(active, TRUE) = TRUE
                   AND NULLIF(TRIM(address_city), '') IS NOT NULL
            """
            params = []
            if uf:
                sql += " AND UPPER(TRIM(address_state)) = %s"
                params.append(uf)
            sql += " ORDER BY city"
            cur.execute(sql, params)
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

            data = _serialize(dict(row))
            # Cupons desta loja: o cliente vê o código na própria página, sem
            # precisar ter visto a divulgação do parceiro em outro lugar.
            cupons = _buscar_cupons_ativos(cur, [str(restaurant_id)]).get(str(restaurant_id), [])
            data["coupons"] = cupons
            data["has_coupon"] = bool(cupons)
            data["promo_label"] = (
                max(cupons, key=lambda c: _peso_cupom(c, data.get("delivery_fee")))["label"]
                if cupons else None
            )

        return jsonify({"status": "success", "data": data}), 200

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
