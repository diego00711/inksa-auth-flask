# -*- coding: utf-8 -*-
# src/routes/gamification_routes.py
import uuid
import traceback
from functools import wraps

from flask import Blueprint, request, jsonify, current_app
from flask_cors import CORS
import psycopg2.extras

from ..utils.helpers import get_user_id_from_token

_CORS_ORIGINS = [
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:5173", "http://127.0.0.1:5173",
    r"https://.*\.vercel\.app",
    "https://admin.inksadelivery.com.br",
    "https://clientes.inksadelivery.com.br",
    "https://restaurantes.inksadelivery.com.br",
    "https://entregadores.inksadelivery.com.br",
]

gamification_bp = Blueprint("gamification", __name__, url_prefix="/gamification")
CORS(gamification_bp, origins=_CORS_ORIGINS, supports_credentials=True)

admin_gamification_bp = Blueprint("admin_gamification", __name__, url_prefix="/admin/gamification")
CORS(admin_gamification_bp, origins=_CORS_ORIGINS, supports_credentials=True)

# ---------- infraestrutura ----------
def _db():
    factory = current_app.config.get("DB_CONN_FACTORY")
    if not factory:
        raise RuntimeError("DB_CONN_FACTORY não configurado no app")
    return factory()

def _ok(data, code=200):
    return jsonify({"status": "success", "data": data}), code

def _err(message="internal_error", code=400, **extra):
    payload = {"status": "error", "message": message}
    payload.update(extra)
    return jsonify(payload), code

def _compute_level(cur, total_points: int):
    cur.execute("SELECT MAX(level_number) AS lvl FROM public.levels WHERE points_required <= %s", (total_points,))
    row = cur.fetchone()
    lvl = int((row["lvl"] or 1))
    cur.execute("SELECT points_required FROM public.levels WHERE level_number = %s", (lvl + 1,))
    nxt = cur.fetchone()
    to_next = (nxt["points_required"] - total_points) if nxt else 0
    return lvl, max(int(to_next), 0)

def internal_required(fn):
    @wraps(fn)
    def _wrap(*a, **kw):
        token = request.headers.get("X-Internal-Token")
        expected = current_app.config.get("GAMIFICATION_INTERNAL_TOKEN")
        if not expected:
            return _err("internal_token_not_configured", 500)
        if token != expected:
            return _err("unauthorized", 403)
        return fn(*a, **kw)
    return _wrap

# ---------- core ----------
def _add_points_event(*, user_id, points: int, event_type: str, description=None, order_id=None):
    if not points:
        return True, {"message": "no-op"}
    conn = _db()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    INSERT INTO public.xp_events (id, user_id, event_type, order_id, points)
                    VALUES (gen_random_uuid(), %s, %s, %s, %s)
                    ON CONFLICT (event_type, order_id) DO NOTHING
                    RETURNING id
                """, (user_id, event_type, order_id, points))
                if cur.fetchone() is None:
                    return True, {"message": "evento_ja_processado"}

                cur.execute("""
                    INSERT INTO public.user_points (user_id, total_points, last_updated)
                    VALUES (%s,%s,NOW())
                    ON CONFLICT (user_id) DO UPDATE
                      SET total_points = public.user_points.total_points + EXCLUDED.total_points,
                          last_updated = NOW()
                    RETURNING total_points
                """, (user_id, points))
                total = int(cur.fetchone()["total_points"])

                cur.execute("""
                    INSERT INTO public.points_history
                      (id, user_id, points_earned, points_type, description, order_id)
                    VALUES (gen_random_uuid(), %s,%s,%s,%s,%s)
                """, (user_id, points, event_type, description or event_type, order_id))

                lvl, to_next = _compute_level(cur, total)
                cur.execute("""
                    UPDATE public.user_points
                       SET current_level=%s, points_to_next_level=%s
                     WHERE user_id=%s
                """, (lvl, to_next, user_id))

                return True, {"user_id": str(user_id), "total_points": total,
                              "current_level": lvl, "points_to_next_level": to_next}
    except Exception as e:
        current_app.logger.exception("gamification._add_points_event failed")
        traceback.print_exc()
        return False, {"error": "db_error", "detail": str(e)}
    finally:
        try: conn.close()
        except Exception: pass

# ---------- rotas existentes ----------
@gamification_bp.post("/add-points-internal")
@internal_required
def add_points_internal_route():
    body = request.get_json(silent=True) or {}
    try:
        user_id = body["user_id"]
        points = int(body["points"])
        event_type = (body.get("event_type") or "pedido").strip()
        order_id = body.get("order_id")
        description = body.get("description")
    except Exception:
        return _err("invalid_body", 422)
    ok, payload = _add_points_event(user_id=user_id, points=points,
                                    event_type=event_type, description=description, order_id=order_id)
    return _ok(payload) if ok else _err(**payload)

@gamification_bp.get("/<user_id>/points-level")
def get_user_points_and_level(user_id):
    conn = _db()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT up.user_id, up.total_points, up.current_level, up.points_to_next_level, l.level_name
                  FROM public.user_points up
             LEFT JOIN public.levels l ON l.level_number = up.current_level
                 WHERE up.user_id = %s
            """, (user_id,))
            row = cur.fetchone()
            if not row:
                return _ok({"user_id": user_id, "total_points": 0, "current_level": 1,
                            "points_to_next_level": 300, "level_name": "Bronze"})
            d = dict(row); d["user_id"] = str(d["user_id"])
            return _ok(d)
    except Exception as e:
        current_app.logger.exception("gamification.points-level failed")
        return _err("db_error", 500, detail=str(e))
    finally:
        try: conn.close()
        except Exception: pass

# ---------- NOVO: endpoints que o front espera ----------
@gamification_bp.get("/overview")
def gamification_overview():
    """
    Compat com front: /api/gamification/overview?scope=restaurant|delivery&period=30d&from=YYYY-MM-DD&to=YYYY-MM-DD
    MVP: calcula totais da tabela user_points; filtros de período são ignorados neste primeiro passo.
    """
    scope = request.args.get("scope")  # restaurant|delivery|client
    conn = _db()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # participantes por tipo (aproximação por join)
            where, params = [], []
            join = """
              LEFT JOIN public.delivery_profiles   dp ON dp.id = up.user_id
              LEFT JOIN public.client_profiles     cp ON cp.id = up.user_id
              LEFT JOIN public.restaurant_profiles rp ON rp.id = up.user_id
            """
            if scope == "restaurant":
                where.append("rp.id IS NOT NULL")
            elif scope == "delivery":
                where.append("dp.id IS NOT NULL")
            elif scope == "client":
                where.append("cp.id IS NOT NULL")
            where_sql = "WHERE " + " AND ".join(where) if where else ""

            cur.execute(f"SELECT COUNT(*)::int AS c FROM public.user_points up {join} {where_sql}", params)
            participants = int(cur.fetchone()["c"])

            cur.execute(f"SELECT COALESCE(SUM(total_points),0) AS xp FROM public.user_points up {join} {where_sql}", params)
            total_xp = int(cur.fetchone()["xp"])

            cur.execute(f"""
                SELECT COALESCE(AVG(current_level),0) AS avg_level
                  FROM public.user_points up {join} {where_sql}
            """, params)
            avg_lvl = float(cur.fetchone()["avg_level"])

            return _ok({
                "participantsActive": participants,
                "xpTotalAcumulado": total_xp,
                "nivelMedio": round(avg_lvl, 2),
                "desafiosAtivos": 0,  # placeholder (não temos tabela de desafios ainda)
            })
    except Exception as e:
        current_app.logger.exception("gamification.overview failed")
        return _err("db_error", 500, detail=str(e))
    finally:
        try: conn.close()
        except Exception: pass

@gamification_bp.get("/leaderboard")
def gamification_leaderboard():
    """
    Compat com front: /api/gamification/leaderboard?scope=restaurant|delivery|client&period=30d&limit=10
    Retorna top por total_points.
    """
    scope = request.args.get("scope")
    limit = min(max(int(request.args.get("limit", 10)), 1), 100)

    conn = _db()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            join = """
              LEFT JOIN public.delivery_profiles   dp ON dp.id = up.user_id
              LEFT JOIN public.client_profiles     cp ON cp.id = up.user_id
              LEFT JOIN public.restaurant_profiles rp ON rp.id = up.user_id
              LEFT JOIN public.levels l ON l.level_number = up.current_level
            """
            where = []
            if scope == "restaurant":
                where.append("rp.id IS NOT NULL")
            elif scope == "delivery":
                where.append("dp.id IS NOT NULL")
            elif scope == "client":
                where.append("cp.id IS NOT NULL")
            where_sql = "WHERE " + " AND ".join(where) if where else ""

            cur.execute(f"""
                SELECT up.user_id, up.total_points, COALESCE(l.level_name,'Bronze') AS level_name,
                       COALESCE(rp.restaurant_name,
                                dp.first_name || ' ' || dp.last_name,
                                cp.first_name || ' ' || cp.last_name,
                                'Anônimo') AS name
                  FROM public.user_points up
                  {join}
                  {where_sql}
              ORDER BY up.total_points DESC
                 LIMIT %s
            """, (limit,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["user_id"] = str(r["user_id"])
            return _ok({"items": rows, "limit": limit})
    except Exception as e:
        current_app.logger.exception("gamification.leaderboard failed")
        return _err("db_error", 500, detail=str(e))
    finally:
        try: conn.close()
        except Exception: pass

# ---------- ranking antigo (mantido) ----------
@gamification_bp.get("/rankings")
def get_global_rankings():
    page  = max(int(request.args.get("page", 1)), 1)
    limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    ftype = request.args.get("type")  # client|delivery|restaurant
    city  = request.args.get("city")
    offset = (page - 1) * limit

    conn = _db()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            sql = """
              SELECT up.user_id, up.total_points, l.level_name,
                     COALESCE(dp.first_name || ' ' || dp.last_name,
                              cp.first_name || ' ' || cp.last_name,
                              rp.restaurant_name, 'Anônimo') AS profile_name,
                     CASE WHEN dp.id IS NOT NULL THEN 'delivery'
                          WHEN cp.id IS NOT NULL THEN 'client'
                          WHEN rp.id IS NOT NULL THEN 'restaurant'
                          ELSE 'unknown' END AS profile_type
              FROM public.user_points up
              LEFT JOIN public.levels l ON up.current_level = l.level_number
              LEFT JOIN public.delivery_profiles   dp ON dp.id = up.user_id
              LEFT JOIN public.client_profiles     cp ON cp.id = up.user_id
              LEFT JOIN public.restaurant_profiles rp ON rp.id = up.user_id
            """
            where, params = [], []
            if ftype in ("client","delivery","restaurant"):
                where.append({"client":"cp.id IS NOT NULL","delivery":"dp.id IS NOT NULL","restaurant":"rp.id IS NOT NULL"}[ftype])
            if city:
                where.append("(dp.city = %s OR cp.city = %s OR rp.city = %s)")
                params += [city, city, city]
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY up.total_points DESC LIMIT %s OFFSET %s"
            params += [limit, offset]
            cur.execute(sql, tuple(params))
            rows = [dict(r) for r in cur.fetchall()]
            return _ok({"items": rows, "page": page, "limit": limit})
    except Exception as e:
        current_app.logger.exception("gamification.rankings failed")
        return _err("db_error", 500, detail=str(e))
    finally:
        try: conn.close()
        except Exception: pass

# retrocompat para import antigo
def add_points_for_event(user_id, profile_type=None, points=0, event_type="pedido",
                         conn=None, order_id=None, description=None):
    ok, _ = _add_points_event(
        user_id=user_id,
        points=int(points or 0),
        event_type=event_type,
        description=description,
        order_id=order_id,
    )
    return ok


# ---------- Admin Gamification ----------

def _admin_required():
    auth = request.headers.get("Authorization")
    uid, utype, err = get_user_id_from_token(auth)
    if err:
        return None, err
    if utype != "admin":
        return None, (_err("unauthorized", 403))
    return uid, None


@admin_gamification_bp.get("")
@admin_gamification_bp.get("/")
def admin_gamification_root():
    """GET /api/admin/gamification — métricas gerais de avaliações e gamificação."""
    _, err = _admin_required()
    if err:
        return err
    partner_type = request.args.get("partner_type")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    conn = _db()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            date_params = []
            date_where = []
            if start_date:
                date_where.append("created_at >= %s")
                date_params.append(start_date)
            if end_date:
                date_where.append("created_at <= %s")
                date_params.append(end_date)
            date_sql = ("WHERE " + " AND ".join(date_where)) if date_where else ""

            def count_reviews(table):
                cur.execute(f"SELECT COUNT(*)::int FROM {table} {date_sql}", date_params)
                return int(cur.fetchone()[0])

            def avg_rating(table):
                cur.execute(f"SELECT COALESCE(AVG(rating), 0) FROM {table} {date_sql}", date_params)
                return float(cur.fetchone()[0])

            rr_count = count_reviews("restaurant_reviews")
            dr_count = count_reviews("delivery_reviews")
            try:
                cr_count = count_reviews("client_reviews")
            except Exception:
                cr_count = 0
                conn.rollback()
            try:
                mr_count = count_reviews("menu_item_reviews")
            except Exception:
                mr_count = 0
                conn.rollback()
            total_reviews = rr_count + dr_count + cr_count + mr_count

            rr_avg = avg_rating("restaurant_reviews")
            dr_avg = avg_rating("delivery_reviews")
            review_counts_nz = [(rr_avg, rr_count), (dr_avg, dr_count)]
            total_weighted = sum(a * c for a, c in review_counts_nz)
            total_count_nz = rr_count + dr_count
            global_avg = (total_weighted / total_count_nz) if total_count_nz else 0.0

            cur.execute("SELECT COUNT(*)::int FROM orders WHERE status = 'delivered' " + (
                "AND created_at >= %s AND created_at <= %s" if (start_date and end_date) else
                ("AND created_at >= %s" if start_date else ("AND created_at <= %s" if end_date else ""))
            ), [p for p in [start_date, end_date] if p])
            total_delivered = int(cur.fetchone()[0]) or 1
            response_rate = round((total_count_nz / total_delivered) * 100, 1)

            cur.execute("SELECT COUNT(*)::int AS c FROM public.user_points")
            participants = int(cur.fetchone()["c"])
            cur.execute("SELECT COALESCE(SUM(total_points), 0)::bigint AS xp FROM public.user_points")
            total_xp = int(cur.fetchone()["xp"])
            cur.execute("SELECT COALESCE(AVG(current_level), 0) AS avg_level FROM public.user_points")
            avg_lvl = round(float(cur.fetchone()["avg_level"]), 2)

            return _ok({
                "total_reviews": total_reviews,
                "average_rating": round(global_avg, 2),
                "response_rate": response_rate,
                "total_participants": participants,
                "total_xp": total_xp,
                "average_level": avg_lvl,
                "active_challenges": 0,
            })
    except Exception as e:
        current_app.logger.exception("admin_gamification.root failed")
        return _err("db_error", 500, detail=str(e))
    finally:
        try:
            conn.close()
        except Exception:
            pass


@admin_gamification_bp.get("/reviews")
def admin_gamification_reviews():
    """GET /api/admin/gamification/reviews — avaliações recentes (restaurant + delivery union)."""
    _, err = _admin_required()
    if err:
        return err
    try:
        limit = min(max(int(request.args.get("limit", 20)), 1), 100)
    except (ValueError, TypeError):
        limit = 20
    partner_type = request.args.get("partner_type")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    date_where = []
    date_params = []
    if start_date:
        date_where.append("created_at >= %s")
        date_params.append(start_date)
    if end_date:
        date_where.append("created_at <= %s")
        date_params.append(end_date)
    date_sql = ("AND " + " AND ".join(date_where)) if date_where else ""

    conn = _db()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            queries = []
            params = []
            if not partner_type or partner_type == "restaurant":
                queries.append(
                    f"SELECT id::text, 'restaurant' AS type, rating, comment, created_at, client_id::text AS reviewer_id FROM restaurant_reviews WHERE 1=1 {date_sql}"
                )
                params += date_params
            if not partner_type or partner_type == "delivery":
                queries.append(
                    f"SELECT id::text, 'delivery' AS type, rating, comment, created_at, client_id::text AS reviewer_id FROM delivery_reviews WHERE 1=1 {date_sql}"
                )
                params += date_params

            if not queries:
                return _ok({"items": []})

            union_sql = " UNION ALL ".join(queries)
            cur.execute(
                f"SELECT * FROM ({union_sql}) AS combined ORDER BY created_at DESC LIMIT %s",
                params + [limit],
            )
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                if r.get("created_at") and hasattr(r["created_at"], "isoformat"):
                    r["created_at"] = r["created_at"].isoformat()
            return _ok({"items": rows})
    except Exception as e:
        current_app.logger.exception("admin_gamification.reviews failed")
        return _err("db_error", 500, detail=str(e))
    finally:
        try:
            conn.close()
        except Exception:
            pass


@admin_gamification_bp.get("/rating-distribution")
def admin_gamification_rating_distribution():
    """GET /api/admin/gamification/rating-distribution — distribuição de notas de 1 a 5."""
    _, err = _admin_required()
    if err:
        return err
    partner_type = request.args.get("partner_type")
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")

    date_where = []
    params = []
    if start_date:
        date_where.append("created_at >= %s")
        params.append(start_date)
    if end_date:
        date_where.append("created_at <= %s")
        params.append(end_date)
    date_sql = ("AND " + " AND ".join(date_where)) if date_where else ""

    table = "delivery_reviews" if partner_type == "delivery" else "restaurant_reviews"

    conn = _db()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                f"""
                SELECT rating, COUNT(*)::int AS count
                FROM {table}
                WHERE rating BETWEEN 1 AND 5 {date_sql}
                GROUP BY rating
                ORDER BY rating
                """,
                params,
            )
            rows = {r["rating"]: r["count"] for r in cur.fetchall()}
            distribution = [{"rating": i, "count": rows.get(i, 0)} for i in range(1, 6)]
            return _ok({"distribution": distribution})
    except Exception as e:
        current_app.logger.exception("admin_gamification.rating-distribution failed")
        return _err("db_error", 500, detail=str(e))
    finally:
        try:
            conn.close()
        except Exception:
            pass


@admin_gamification_bp.get("/overview")
def admin_gamification_overview():
    _, err = _admin_required()
    if err:
        return err
    scope = request.args.get("scope")
    conn = _db()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            join = """
              LEFT JOIN public.delivery_profiles   dp ON dp.id = up.user_id
              LEFT JOIN public.client_profiles     cp ON cp.id = up.user_id
              LEFT JOIN public.restaurant_profiles rp ON rp.id = up.user_id
            """
            where = []
            if scope == "restaurant":
                where.append("rp.id IS NOT NULL")
            elif scope == "delivery":
                where.append("dp.id IS NOT NULL")
            elif scope == "client":
                where.append("cp.id IS NOT NULL")
            where_sql = "WHERE " + " AND ".join(where) if where else ""

            cur.execute(f"SELECT COUNT(*)::int AS c FROM public.user_points up {join} {where_sql}")
            participants = int(cur.fetchone()["c"])

            cur.execute(f"SELECT COALESCE(SUM(total_points),0) AS xp FROM public.user_points up {join} {where_sql}")
            total_xp = int(cur.fetchone()["xp"])

            cur.execute(f"SELECT COALESCE(AVG(current_level),0) AS avg_level FROM public.user_points up {join} {where_sql}")
            avg_lvl = float(cur.fetchone()["avg_level"])

            cur.execute("SELECT COUNT(*)::int AS c FROM public.xp_events WHERE created_at >= NOW() - INTERVAL '30 days'")
            events_30d = int(cur.fetchone()["c"])

            return _ok({
                "participantsActive": participants,
                "xpTotalAcumulado": total_xp,
                "nivelMedio": round(avg_lvl, 2),
                "eventosUltimos30Dias": events_30d,
            })
    except Exception as e:
        current_app.logger.exception("admin_gamification.overview failed")
        return _err("db_error", 500, detail=str(e))
    finally:
        try:
            conn.close()
        except Exception:
            pass


@admin_gamification_bp.get("/leaderboard")
def admin_gamification_leaderboard():
    _, err = _admin_required()
    if err:
        return err
    scope = request.args.get("scope")
    limit = min(max(int(request.args.get("limit", 10)), 1), 100)
    conn = _db()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            join = """
              LEFT JOIN public.delivery_profiles   dp ON dp.id = up.user_id
              LEFT JOIN public.client_profiles     cp ON cp.id = up.user_id
              LEFT JOIN public.restaurant_profiles rp ON rp.id = up.user_id
              LEFT JOIN public.levels l ON l.level_number = up.current_level
            """
            where = []
            if scope == "restaurant":
                where.append("rp.id IS NOT NULL")
            elif scope == "delivery":
                where.append("dp.id IS NOT NULL")
            elif scope == "client":
                where.append("cp.id IS NOT NULL")
            where_sql = "WHERE " + " AND ".join(where) if where else ""
            cur.execute(f"""
                SELECT up.user_id, up.total_points, up.current_level,
                       COALESCE(l.level_name,'Bronze') AS level_name,
                       COALESCE(rp.restaurant_name,
                                dp.first_name || ' ' || dp.last_name,
                                cp.first_name || ' ' || cp.last_name,
                                'Anônimo') AS name,
                       CASE WHEN rp.id IS NOT NULL THEN 'restaurant'
                            WHEN dp.id IS NOT NULL THEN 'delivery'
                            WHEN cp.id IS NOT NULL THEN 'client'
                            ELSE 'unknown' END AS profile_type
                  FROM public.user_points up {join} {where_sql}
              ORDER BY up.total_points DESC
                 LIMIT %s
            """, (limit,))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["user_id"] = str(r["user_id"])
            return _ok({"items": rows, "limit": limit})
    except Exception as e:
        current_app.logger.exception("admin_gamification.leaderboard failed")
        return _err("db_error", 500, detail=str(e))
    finally:
        try:
            conn.close()
        except Exception:
            pass


@admin_gamification_bp.get("/users")
def admin_gamification_users():
    _, err = _admin_required()
    if err:
        return err
    page = max(int(request.args.get("page", 1)), 1)
    limit = min(max(int(request.args.get("limit", 50)), 1), 200)
    offset = (page - 1) * limit
    conn = _db()
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT up.user_id, up.total_points, up.current_level,
                       up.points_to_next_level, up.last_updated,
                       COALESCE(l.level_name,'Bronze') AS level_name,
                       COALESCE(rp.restaurant_name,
                                dp.first_name || ' ' || dp.last_name,
                                cp.first_name || ' ' || cp.last_name,
                                'Anônimo') AS name,
                       CASE WHEN rp.id IS NOT NULL THEN 'restaurant'
                            WHEN dp.id IS NOT NULL THEN 'delivery'
                            WHEN cp.id IS NOT NULL THEN 'client'
                            ELSE 'unknown' END AS profile_type
                  FROM public.user_points up
                  LEFT JOIN public.delivery_profiles   dp ON dp.id = up.user_id
                  LEFT JOIN public.client_profiles     cp ON cp.id = up.user_id
                  LEFT JOIN public.restaurant_profiles rp ON rp.id = up.user_id
                  LEFT JOIN public.levels l ON l.level_number = up.current_level
              ORDER BY up.total_points DESC
                 LIMIT %s OFFSET %s
            """, (limit, offset))
            rows = [dict(r) for r in cur.fetchall()]
            for r in rows:
                r["user_id"] = str(r["user_id"])
                if r.get("last_updated"):
                    r["last_updated"] = r["last_updated"].isoformat()
            cur.execute("SELECT COUNT(*)::int AS c FROM public.user_points")
            total = int(cur.fetchone()["c"])
            return _ok({"items": rows, "total": total, "page": page, "limit": limit})
    except Exception as e:
        current_app.logger.exception("admin_gamification.users failed")
        return _err("db_error", 500, detail=str(e))
    finally:
        try:
            conn.close()
        except Exception:
            pass


@admin_gamification_bp.post("/users/<user_id>/adjust-points")
def admin_gamification_adjust_points(user_id):
    _, err = _admin_required()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    try:
        points = int(body["points"])
        event_type = (body.get("event_type") or "admin_adjustment").strip()
        description = body.get("description", "Ajuste manual pelo admin")
    except Exception:
        return _err("invalid_body", 422)
    ok, payload = _add_points_event(
        user_id=user_id,
        points=points,
        event_type=event_type,
        description=description,
    )
    return _ok(payload) if ok else _err(**payload)
