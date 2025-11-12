# -*- coding: utf-8 -*-
# inksa-auth-flask/src/routes/gamification_routes.py
#
# Gamificação – MVP sólido, seguro e idempotente.
# Rotas já existentes:
#   POST /add-points-internal              -> credita XP (somente serviço)
#   GET  /<user_id>/points-level           -> saldo/nível
#   GET  /rankings                         -> ranking com paginação
#
# Novas rotas (para casar com o front do Admin):
#   GET  /overview                         -> resumo por scope
#   GET  /leaderboard                      -> ranking por scope
#
# Exporte DOIS blueprints:
#   - gamification_bp            (para /api/gamification)
#   - admin_gamification_bp      (para /api/admin/gamification)  << novo
#
# Requer:
#   - app.config["DB_CONN_FACTORY"] -> callable -> psycopg2.connect(...)
#   - app.config["GAMIFICATION_INTERNAL_TOKEN"] -> segredo do endpoint interno
#
# Tabelas:
#   public.levels(level_number, level_name, points_required)
#   public.user_points(user_id, total_points, current_level, points_to_next_level, last_updated)
#   public.points_history(id, user_id, points_earned, points_type, description, order_id, created_at)
#   public.xp_events(id, user_id, event_type, order_id, points, created_at)  UNIQUE(event_type, order_id)

import uuid
import traceback
from functools import wraps
from flask import Blueprint, request, jsonify, current_app
import psycopg2.extras

# Blueprints:
gamification_bp = Blueprint("gamification", __name__)
admin_gamification_bp = Blueprint("admin_gamification", __name__)

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
    cur.execute(
        "SELECT MAX(level_number) AS lvl FROM public.levels WHERE points_required <= %s",
        (total_points,),
    )
    row = cur.fetchone()
    lvl = int(row["lvl"] or 1)
    cur.execute(
        "SELECT points_required FROM public.levels WHERE level_number = %s",
        (lvl + 1,),
    )
    nxt = cur.fetchone()
    to_next = (nxt["points_required"] - total_points) if nxt else 0
    return lvl, max(int(to_next), 0)

def _scope_to_type(scope: str):
    """Mapeia scope dos componentes do Admin para nossos perfis."""
    s = (scope or "").strip().lower()
    if s in ("customer", "client", "clientes"):
        return "client"
    if s in ("delivery", "entregador", "courier"):
        return "delivery"
    if s in ("restaurant", "restaurante", "partner"):
        return "restaurant"
    return None

# ---------- proteção do endpoint interno ----------
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
    """Fluxo idempotente – tudo na MESMA transação."""
    if not points:
        return True, {"message": "no-op"}

    conn = _db()
    try:
        with conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # 1) ledger para idempotência
                cur.execute(
                    """
                    INSERT INTO public.xp_events (id, user_id, event_type, order_id, points)
                    VALUES (%s,%s,%s,%s,%s)
                    ON CONFLICT (event_type, order_id) DO NOTHING
                    RETURNING id
                    """,
                    (uuid.uuid4(), user_id, event_type, order_id, points),
                )
                if cur.fetchone() is None:
                    return True, {"message": "evento_ja_processado"}

                # 2) carteira (soma)
                cur.execute(
                    """
                    INSERT INTO public.user_points (user_id, total_points, last_updated)
                    VALUES (%s,%s,NOW())
                    ON CONFLICT (user_id) DO UPDATE
                      SET total_points = public.user_points.total_points + EXCLUDED.total_points,
                          last_updated = NOW()
                    RETURNING total_points
                    """,
                    (user_id, points),
                )
                total = int(cur.fetchone()["total_points"])

                # 3) histórico
                cur.execute(
                    """
                    INSERT INTO public.points_history
                      (id, user_id, points_earned, points_type, description, order_id)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    """,
                    (uuid.uuid4(), user_id, points, event_type, description or event_type, order_id),
                )

                # 4) nível
                lvl, to_next = _compute_level(cur, total)
                cur.execute(
                    """
                    UPDATE public.user_points
                       SET current_level=%s, points_to_next_level=%s
                     WHERE user_id=%s
                    """,
                    (lvl, to_next, user_id),
                )

                return True, {
                    "user_id": str(user_id),
                    "total_points": total,
                    "current_level": lvl,
                    "points_to_next_level": to_next,
                }
    except Exception as e:
        current_app.logger.exception("gamification._add_points_event failed")
        traceback.print_exc()
        return False, {"error": "db_error", "detail": str(e)}
    finally:
        try:
            conn.close()
        except Exception:
            pass

# ---------- rotas básicas (ambos blueprints) ----------
def _bind_basic_routes(bp: Blueprint):
    @bp.post("/add-points-internal")
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

        ok, payload = _add_points_event(
            user_id=user_id,
            points=points,
            event_type=event_type,
            description=description,
            order_id=order_id,
        )
        return _ok(payload) if ok else _err(**payload)

    @bp.get("/<user_id>/points-level")
    def get_user_points_and_level(user_id):
        conn = _db()
        try:
            with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    """
                    SELECT up.user_id, up.total_points, up.current_level, up.points_to_next_level,
                           l.level_name
                      FROM public.user_points up
                 LEFT JOIN public.levels l ON l.level_number = up.current_level
                     WHERE up.user_id = %s
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
                if not row:
                    return _ok({
                        "user_id": user_id,
                        "total_points": 0,
                        "current_level": 1,
                        "points_to_next_level": 300,
                        "level_name": "Bronze",
                    })
                data = dict(row)
                data["user_id"] = str(data["user_id"])
                return _ok(data)
        except Exception as e:
            current_app.logger.exception("gamification.points-level failed")
            return _err("db_error", 500, detail=str(e))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @bp.get("/rankings")
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
                    where.append({
                        "client": "cp.id IS NOT NULL",
                        "delivery": "dp.id IS NOT NULL",
                        "restaurant": "rp.id IS NOT NULL"
                    }[ftype])
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
            try:
                conn.close()
            except Exception:
                pass

# ---------- rotas "admin" (overview/leaderboard) ----------
def _bind_admin_routes(bp: Blueprint):
    @bp.get("/overview")
    def overview():
        """
        Resumo simples para cards do Admin.
        Query:
          - scope = customer|delivery|restaurant
          - from, to (opcional, YYYY-MM-DD)  -> filtra pontos por período (points_history)
        Retorna:
          { participants_active, xp_total_acumulado, nivel_medio }
        """
        scope = request.args.get("scope")
        ftype = _scope_to_type(scope)
        date_from = request.args.get("from")
        date_to   = request.args.get("to")

        conn = _db()
        try:
            with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # filtro por tipo
                type_where = ""
                if ftype == "client":
                    type_where = "cp.id IS NOT NULL"
                elif ftype == "delivery":
                    type_where = "dp.id IS NOT NULL"
                elif ftype == "restaurant":
                    type_where = "rp.id IS NOT NULL"

                # participantes ativos (tem registro em user_points)
                cur.execute(
                    f"""
                    SELECT COUNT(*)::int AS c
                    FROM public.user_points up
                    LEFT JOIN public.client_profiles cp ON cp.id = up.user_id
                    LEFT JOIN public.delivery_profiles dp ON dp.id = up.user_id
                    LEFT JOIN public.restaurant_profiles rp ON rp.id = up.user_id
                    {('WHERE ' + type_where) if type_where else ''}
                    """
                )
                participants = int(cur.fetchone()["c"] or 0)

                # xp acumulado no período (history)
                params, where = [], []
                if date_from:
                    where.append("ph.created_at::date >= %s")
                    params.append(date_from)
                if date_to:
                    where.append("ph.created_at::date <= %s")
                    params.append(date_to)
                if ftype:
                    where.append({
                        "client": "cp.id IS NOT NULL",
                        "delivery": "dp.id IS NOT NULL",
                        "restaurant": "rp.id IS NOT NULL",
                    }[ftype])
                where_sql = ("WHERE " + " AND ".join(where)) if where else ""

                cur.execute(
                    f"""
                    SELECT COALESCE(SUM(ph.points_earned),0) AS xp
                    FROM public.points_history ph
                    LEFT JOIN public.client_profiles cp ON cp.id = ph.user_id
                    LEFT JOIN public.delivery_profiles dp ON dp.id = ph.user_id
                    LEFT JOIN public.restaurant_profiles rp ON rp.id = ph.user_id
                    {where_sql}
                    """,
                    tuple(params),
                )
                xp_total = float(cur.fetchone()["xp"] or 0)

                # nível médio (com base no snapshot atual)
                cur.execute(
                    f"""
                    SELECT COALESCE(AVG(up.current_level),0) AS lvl
                    FROM public.user_points up
                    LEFT JOIN public.client_profiles cp ON cp.id = up.user_id
                    LEFT JOIN public.delivery_profiles dp ON dp.id = up.user_id
                    LEFT JOIN public.restaurant_profiles rp ON rp.id = up.user_id
                    {('WHERE ' + type_where) if type_where else ''}
                    """
                )
                lvl_avg = float(cur.fetchone()["lvl"] or 0)

                return _ok({
                    "participants_active": participants,
                    "xp_total_acumulado": xp_total,
                    "nivel_medio": lvl_avg,
                    # pode adicionar "desafios_ativos" depois; por ora 0
                    "desafios_ativos": 0,
                })
        except Exception as e:
            current_app.logger.exception("gamification.overview failed")
            return _err("db_error", 500, detail=str(e))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @bp.get("/leaderboard")
    def leaderboard():
        """
        Ranking usado pelo Admin.
        Query:
          - scope = customer|delivery|restaurant
          - limit (default 10)
        """
        scope = request.args.get("scope")
        limit = max(1, min(int(request.args.get("limit", 10)), 100))
        ftype = _scope_to_type(scope)

        # Reutiliza a mesma lógica do /rankings
        # aqui filtrando por tipo e limit.
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
                    where.append({
                        "client": "cp.id IS NOT NULL",
                        "delivery": "dp.id IS NOT NULL",
                        "restaurant": "rp.id IS NOT NULL"
                    }[ftype])
                if where:
                    sql += " WHERE " + " AND ".join(where)
                sql += " ORDER BY up.total_points DESC LIMIT %s"
                params.append(limit)

                cur.execute(sql, tuple(params))
                rows = [dict(r) for r in cur.fetchall()]
                return _ok({"items": rows, "limit": limit})
        except Exception as e:
            current_app.logger.exception("gamification.leaderboard failed")
            return _err("db_error", 500, detail=str(e))
        finally:
            try:
                conn.close()
            except Exception:
                pass

# ligar as rotas aos dois blueprints
_bind_basic_routes(gamification_bp)
_bind_basic_routes(admin_gamification_bp)
_bind_admin_routes(admin_gamification_bp)

# --- retrocompat: manter assinatura esperada por delivery_orders.py ---
def add_points_for_event(user_id, profile_type=None, points=0, event_type="pedido",
                         conn=None, order_id=None, description=None):
    """
    Wrapper compatível com a importação antiga:
      from .gamification_routes import add_points_for_event

    Ignora profile_type/conn (não são necessários no MVP).
    Retorna True/False como antes.
    """
    ok, _ = _add_points_event(
        user_id=user_id,
        points=int(points or 0),
        event_type=event_type,
        description=description,
        order_id=order_id,
    )
    return ok
