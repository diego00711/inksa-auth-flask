# -*- coding: utf-8 -*-
# src/routes/club_routes.py — Clube Inksa: níveis/benefícios configuráveis (3 audiências)
import logging
import unicodedata
from flask import Blueprint, request, jsonify
from flask_cors import CORS
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token
from ..utils.club import fetch_levels, level_for_activity, next_level, monthly_activity

logger = logging.getLogger(__name__)

club_bp = Blueprint('club', __name__)

_CORS_ORIGINS = [
    "http://localhost:3000", "http://127.0.0.1:3000",
    "http://localhost:5173", "http://127.0.0.1:5173",
    r"https://.*\.vercel\.app",
    "https://admin.inksadelivery.com.br",
    "https://clientes.inksadelivery.com.br",
    "https://restaurantes.inksadelivery.com.br",
    "https://entregadores.inksadelivery.com.br",
]
CORS(club_bp, origins=_CORS_ORIGINS, supports_credentials=True)

_VALID_AUDIENCES = ("client", "delivery", "restaurant")
_PROFILE_TABLE = {"client": "client_profiles", "delivery": "delivery_profiles", "restaurant": "restaurant_profiles"}


def _slug(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower().strip()
    return s.replace(" ", "_")


def _render_benefits(benefits):
    """Transforma o jsonb de benefícios numa lista de textos legíveis (para os apps)."""
    b = benefits or {}
    out = []
    if b.get("free_delivery_always"):
        out.append("Frete grátis em todos os pedidos")
    elif b.get("free_delivery_from_nth"):
        out.append(f"Frete grátis a partir do {int(b['free_delivery_from_nth'])}º pedido do mês")
    if b.get("subtotal_discount_pct"):
        out.append(f"{_fmt(b['subtotal_discount_pct'])}% de desconto no subtotal")
    if b.get("per_delivery_bonus"):
        out.append(f"Bônus de R$ {float(b['per_delivery_bonus']):.2f} por entrega")
    if b.get("freight_keep_extra_pct"):
        out.append(f"Fica com {_fmt(b['freight_keep_extra_pct'])}% a mais do frete")
    # Sai do PRÓPRIO dado, não de um texto escrito à mão em `extra`. Se a frase
    # fosse fixa, ela continuaria na tela depois de alguém tirar o benefício no
    # admin — prometendo pagamento rápido que não acontece mais. Aqui a frase e
    # a regra são a mesma coisa: sumiu o campo, sumiu a promessa.
    if b.get("payout_express_days"):
        _d = int(float(b["payout_express_days"]))
        out.append(f"Recebe em {_d} dia{'s' if _d > 1 else ''}, sem esperar a sexta")
    if b.get("priority"):
        out.append("Prioridade na fila de pedidos")
    if b.get("featured_listing"):
        out.append("Destaque na listagem para os clientes")
    for extra in (b.get("extra") or []):
        if extra:
            out.append(str(extra))
    return out


def _fmt(n):
    try:
        f = float(n)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return str(n)


def _to_view(lvl, nxt):
    """Formata um nível do banco no formato que os apps esperam."""
    return {
        "level": _slug(lvl["name"]),
        "label": lvl["name"],
        "name": lvl["name"],
        "emoji": lvl.get("emoji") or "🏅",
        "color": lvl.get("color"),
        "level_order": lvl["level_order"],
        "min_orders": int(lvl["min_activity"]),
        "max_orders": (int(nxt["min_activity"]) - 1) if nxt else None,
        "benefits": _render_benefits(lvl.get("benefits")),
        "benefits_raw": lvl.get("benefits") or {},
    }


def _resolve_profile(cur, audience, auth_uid):
    cur.execute(f"SELECT id FROM public.{_PROFILE_TABLE[audience]} WHERE user_id = %s", (auth_uid,))
    row = cur.fetchone()
    return str(row["id"]) if row else None


# ─── Público: tabela de níveis por audiência ────────────────────────────────
@club_bp.route('/levels', methods=['GET'])
def get_levels():
    """GET /api/club/levels?audience=client|delivery|restaurant (default client)."""
    audience = (request.args.get("audience") or "client").lower()
    if audience not in _VALID_AUDIENCES:
        audience = "client"
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            levels = fetch_levels(cur, audience)
            views = []
            for i, lvl in enumerate(levels):
                nxt = levels[i + 1] if i + 1 < len(levels) else None
                views.append(_to_view(lvl, nxt))
        return jsonify({"status": "success", "data": views}), 200
    except Exception:
        logger.exception("club.get_levels failed")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try: conn.close()
        except Exception: pass


# ─── Status do usuário logado (qualquer audiência) ──────────────────────────
@club_bp.route('/status', methods=['GET'])
def get_club_status():
    """GET /api/club/status — status do clube do usuário autenticado (client/delivery/restaurant)."""
    auth_uid, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type not in _VALID_AUDIENCES:
        return jsonify({"error": "Tipo de usuário sem clube"}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            profile_id = _resolve_profile(cur, user_type, auth_uid)
            if not profile_id:
                return jsonify({"error": "Perfil não encontrado"}), 404

            levels = fetch_levels(cur, user_type)
            activity = monthly_activity(cur, user_type, profile_id)
            current = level_for_activity(levels, activity)
            nxt = next_level(levels, current)

            current_view = _to_view(current, nxt) if current else None
            next_view = None
            if nxt:
                after = next_level(levels, nxt)
                next_view = _to_view(nxt, after)

            to_next = (int(nxt["min_activity"]) - activity) if nxt else 0
            to_next = max(0, to_next)

            unit = "entrega" if user_type == "delivery" else "pedido"
            if nxt:
                plural = "s" if to_next != 1 else ""
                motivation = f"Faltam {to_next} {unit}{plural} para você ser {nxt['name']}! {nxt.get('emoji','')}"
            else:
                motivation = "Você atingiu o nível máximo! 💎 Aproveite todos os benefícios."

            # Pedidos recentes do mês (só cliente usa na tela; barato pros demais)
            col = {"client": "client_id", "delivery": "delivery_id", "restaurant": "restaurant_id"}[user_type]
            cur.execute(f"""
                SELECT id, created_at, total_amount
                  FROM orders
                 WHERE {col} = %s
                   AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())
              ORDER BY created_at DESC LIMIT 10
            """, (profile_id,))
            recent = []
            for r in cur.fetchall():
                recent.append({
                    "id": str(r["id"]),
                    "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                    "total_amount": float(r["total_amount"]) if r["total_amount"] is not None else 0.0,
                })

        return jsonify({"status": "success", "data": {
            "audience": user_type,
            "current_level": current_view,
            "next_level": next_view,
            "orders_this_month": activity,
            "orders_to_next_level": to_next,
            "recent_orders": recent,
            "motivation": motivation,
        }}), 200
    except Exception:
        logger.exception("club.get_club_status failed")
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        try: conn.close()
        except Exception: pass


# ─── Admin: CRUD dos níveis/benefícios ──────────────────────────────────────
def _admin_or_err():
    _, user_type, err = get_user_id_from_token(request.headers.get("Authorization"))
    if err:
        return err
    if user_type != "admin":
        return jsonify({"status": "error", "message": "Acesso restrito a administradores"}), 403
    return None


@club_bp.route('/admin/levels', methods=['GET'])
def admin_list_levels():
    """GET /api/club/admin/levels?audience=... — lista para o admin (com benefits_raw)."""
    err = _admin_or_err()
    if err:
        return err
    audience = (request.args.get("audience") or "").lower()
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if audience in _VALID_AUDIENCES:
                cur.execute("""SELECT id, audience, level_order, name, emoji, color, min_activity,
                                      benefits, is_active
                                 FROM public.club_levels WHERE audience = %s
                             ORDER BY level_order ASC""", (audience,))
            else:
                cur.execute("""SELECT id, audience, level_order, name, emoji, color, min_activity,
                                      benefits, is_active
                                 FROM public.club_levels ORDER BY audience, level_order ASC""")
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d["id"] = str(d["id"])
                rows.append(d)
        return jsonify({"status": "success", "data": rows}), 200
    except Exception:
        logger.exception("club.admin_list_levels failed")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try: conn.close()
        except Exception: pass


@club_bp.route('/admin/levels', methods=['POST'])
def admin_create_level():
    err = _admin_or_err()
    if err:
        return err
    b = request.get_json(silent=True) or {}
    audience = (b.get("audience") or "").lower()
    if audience not in _VALID_AUDIENCES:
        return jsonify({"error": "audience inválido"}), 422
    try:
        level_order = int(b["level_order"])
        min_activity = int(b.get("min_activity", 0))
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "level_order e min_activity devem ser inteiros"}), 422
    name = (b.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name é obrigatório"}), 422

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                INSERT INTO public.club_levels
                    (audience, level_order, name, emoji, color, min_activity, benefits, is_active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (audience, level_order, name, b.get("emoji") or "🏅", b.get("color") or "#cd7f32",
                  min_activity, psycopg2.extras.Json(b.get("benefits") or {}), bool(b.get("is_active", True))))
            new_id = str(cur.fetchone()["id"])
        return jsonify({"status": "success", "data": {"id": new_id}}), 201
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Já existe um nível com essa ordem para essa audiência"}), 409
    except Exception:
        logger.exception("club.admin_create_level failed")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try: conn.close()
        except Exception: pass


@club_bp.route('/admin/levels/<level_id>', methods=['PUT'])
def admin_update_level(level_id):
    err = _admin_or_err()
    if err:
        return err
    b = request.get_json(silent=True) or {}
    fields, params = [], []
    for col in ("name", "emoji", "color"):
        if col in b:
            fields.append(f"{col} = %s"); params.append(b[col])
    if "min_activity" in b:
        try:
            params.append(int(b["min_activity"])); fields.append("min_activity = %s")
        except (ValueError, TypeError):
            return jsonify({"error": "min_activity inválido"}), 422
    if "level_order" in b:
        try:
            params.append(int(b["level_order"])); fields.append("level_order = %s")
        except (ValueError, TypeError):
            return jsonify({"error": "level_order inválido"}), 422
    if "benefits" in b:
        fields.append("benefits = %s"); params.append(psycopg2.extras.Json(b["benefits"] or {}))
    if "is_active" in b:
        fields.append("is_active = %s"); params.append(bool(b["is_active"]))
    if not fields:
        return jsonify({"error": "Nada para atualizar"}), 422
    fields.append("updated_at = NOW()")
    params.append(level_id)

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(f"UPDATE public.club_levels SET {', '.join(fields)} WHERE id = %s RETURNING id", params)
            if not cur.fetchone():
                return jsonify({"error": "Nível não encontrado"}), 404
        return jsonify({"status": "success", "message": "Nível atualizado"}), 200
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": "Já existe um nível com essa ordem para essa audiência"}), 409
    except Exception:
        logger.exception("club.admin_update_level failed")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try: conn.close()
        except Exception: pass


@club_bp.route('/admin/levels/<level_id>', methods=['DELETE'])
def admin_delete_level(level_id):
    err = _admin_or_err()
    if err:
        return err
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("DELETE FROM public.club_levels WHERE id = %s RETURNING id", (level_id,))
            if not cur.fetchone():
                return jsonify({"error": "Nível não encontrado"}), 404
        return jsonify({"status": "success", "message": "Nível removido"}), 200
    except Exception:
        logger.exception("club.admin_delete_level failed")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try: conn.close()
        except Exception: pass
