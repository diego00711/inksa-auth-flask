# src/routes/public.py
# Endpoints publicos (sem autenticacao) - leitura de configuracoes que os apps Cliente/Restaurante/Entregador consomem.

import logging
from datetime import datetime, timedelta

import psycopg2.extras
from flask import Blueprint, jsonify, request

from ..utils.helpers import get_db_connection, get_user_id_from_token

logger = logging.getLogger(__name__)
public_bp = Blueprint("public_bp", __name__)


@public_bp.get("/support-info")
def public_support_info():
    """Retorna informacoes de contato/suporte da plataforma. Sem autenticacao."""
    conn = get_db_connection()
    if not conn:
        return jsonify({
            "email": "suporte@inksadelivery.com.br",
            "whatsapp": "5549999679697",
            "phone": "(49) 99967-9697",
            "hours": "Seg a Sex, 8h às 18h",
            "platform_name": "Inksa Delivery",
        }), 200
    try:
        keys = ("contact_email", "contact_whatsapp", "contact_phone", "support_hours", "platform_name")
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT key, value FROM platform_settings WHERE key = ANY(%s)", (list(keys),))
            rows = {r["key"]: r["value"] for r in cur.fetchall()}
        return jsonify({
            "email": rows.get("contact_email") or "suporte@inksadelivery.com.br",
            "whatsapp": rows.get("contact_whatsapp") or "5549999679697",
            "phone": rows.get("contact_phone") or "(49) 99967-9697",
            "hours": rows.get("support_hours") or "Seg a Sex, 8h às 18h",
            "platform_name": rows.get("platform_name") or "Inksa Delivery",
        }), 200
    except Exception:
        logger.exception("Erro em public_support_info")
        return jsonify({
            "email": "suporte@inksadelivery.com.br",
            "whatsapp": "5549999679697",
            "phone": "(49) 99967-9697",
            "hours": "Seg a Sex, 8h às 18h",
            "platform_name": "Inksa Delivery",
        }), 200
    finally:
        conn.close()


@public_bp.get("/app-config")
def public_app_config():
    """Config de comportamento dos apps (sem autenticacao).

    Hoje expoe apenas `idle_logout_minutes` (logoff automatico por inatividade
    nos apps Parceiro/Entregador). 0 = recurso desligado. Editavel no admin em
    Configuracoes. Extensivel para outras flags de UX no futuro."""
    default_idle = 60
    conn = get_db_connection()
    if not conn:
        return jsonify({"idle_logout_minutes": default_idle}), 200
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT value FROM platform_settings WHERE key = %s",
                ("idle_logout_minutes",),
            )
            row = cur.fetchone()
        try:
            val = int(float((row["value"] if row else None) or default_idle))
        except (TypeError, ValueError):
            val = default_idle
        # Sanidade: 0 = desligado; senao entre 5 min e 24h.
        if val != 0:
            val = max(5, min(val, 1440))
        return jsonify({"idle_logout_minutes": val}), 200
    except Exception:
        logger.exception("Erro em public_app_config")
        return jsonify({"idle_logout_minutes": default_idle}), 200
    finally:
        conn.close()


@public_bp.get("/social-day")
def public_social_day():
    """
    Status do Dia I (Inksa Social) + valor arrecadado na janela do evento.

    Config em platform_settings (setada na pagina admin "Inksa Social"):
      social_day_date          YYYY-MM-DD
      social_day_start         HH:MM
      social_day_end           HH:MM
      social_day_show_in_apps  'true' | 'false'

    "Arrecadado" = receita real da plataforma na janela (mesma formula do
    dashboard admin): SUM(comissao_plataforma) + SUM(margem_frete) dos pedidos
    delivered/completed criados dentro da janela, no fuso America/Sao_Paulo.

    Sem token: so devolve dados quando show_in_apps = true (e esconde o banner
    24h depois do fim). Com token de ADMIN devolve sempre — a pagina do admin
    usa este mesmo endpoint para o painel ao vivo.
    """
    conn = get_db_connection()
    if not conn:
        return jsonify({"configured": False, "visible": False}), 200
    try:
        keys = ("social_day_date", "social_day_start", "social_day_end", "social_day_show_in_apps")
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT key, value FROM platform_settings WHERE key = ANY(%s)", (list(keys),))
            cfg = {r["key"]: (r["value"] or "").strip() for r in cur.fetchall()}

            date_str = cfg.get("social_day_date") or ""
            if not date_str:
                return jsonify({"configured": False, "visible": False}), 200

            start_str = cfg.get("social_day_start") or "00:00"
            end_str = cfg.get("social_day_end") or "23:59"
            show_in_apps = (cfg.get("social_day_show_in_apps") or "").lower() == "true"

            # Admin autenticado ve o status mesmo com a exibicao desligada
            is_admin = False
            if request.headers.get("Authorization"):
                try:
                    _uid, utype, err = get_user_id_from_token(request.headers.get("Authorization"))
                    is_admin = (err is None and utype == "admin")
                except Exception:
                    is_admin = False

            # Janela e "agora" no fuso de Sao Paulo (created_at e timestamptz/UTC)
            cur.execute("SELECT (now() AT TIME ZONE 'America/Sao_Paulo') AS now_sp")
            now_sp = cur.fetchone()["now_sp"]
            try:
                win_start = datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %H:%M")
                win_end = datetime.strptime(f"{date_str} {end_str}", "%Y-%m-%d %H:%M")
            except ValueError:
                return jsonify({"configured": False, "visible": False}), 200
            if win_end < win_start:
                win_end = win_start

            if now_sp < win_start:
                phase = "scheduled"
            elif now_sp <= win_end:
                phase = "live"
            else:
                phase = "ended"

            # Apps mostram o banner ate 24h depois do fim; admin sempre ve
            expired = phase == "ended" and now_sp > (win_end + timedelta(hours=24))
            visible = show_in_apps and not expired
            if not visible and not is_admin:
                return jsonify({"configured": True, "visible": False}), 200

            raised = 0.0
            orders_count = 0
            commission = 0.0
            margin = 0.0
            if phase != "scheduled":
                cur.execute(
                    """
                    SELECT COALESCE(SUM(comissao_plataforma), 0) AS commission,
                           COALESCE(SUM(margem_frete), 0)        AS margin,
                           COUNT(*)                              AS orders_count
                      FROM orders
                     WHERE status IN ('delivered', 'completed')
                       AND (created_at AT TIME ZONE 'America/Sao_Paulo') >= %s
                       AND (created_at AT TIME ZONE 'America/Sao_Paulo') <= %s
                    """,
                    (win_start, win_end),
                )
                row = cur.fetchone() or {}
                commission = float(row.get("commission") or 0)
                margin = float(row.get("margin") or 0)
                orders_count = int(row.get("orders_count") or 0)
                # margem_frete pode ser negativa em casos residuais; o contador
                # publico nunca mostra valor negativo
                raised = round(max(commission + margin, 0.0), 2)

        payload = {
            "configured": True,
            "visible": visible,
            "phase": phase,
            "date": date_str,
            "start_time": start_str,
            "end_time": end_str,
            "raised": raised,
            "orders_count": orders_count,
        }
        if is_admin:
            payload["breakdown"] = {"commission": round(commission, 2), "margin": round(margin, 2)}
            payload["show_in_apps"] = show_in_apps
        return jsonify(payload), 200
    except Exception:
        logger.exception("Erro em public_social_day")
        return jsonify({"configured": False, "visible": False}), 200
    finally:
        conn.close()


@public_bp.get("/social-day/history")
def public_social_day_history():
    """
    Historico publico dos Dias I ja realizados (prestacao de contas).
    Alimentado pelo admin em Inksa Social -> Prestacao de contas.
    """
    conn = get_db_connection()
    if not conn:
        return jsonify({"events": [], "total_raised": 0}), 200
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT id, event_date, start_time, end_time, raised, orders_count,
                       destination, proof_url
                  FROM social_day_events
                 ORDER BY event_date DESC, created_at DESC
                """
            )
            rows = cur.fetchall()
        events = [{
            "id": str(r["id"]),
            "date": r["event_date"].isoformat() if r["event_date"] else None,
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "raised": float(r["raised"] or 0),
            "orders_count": int(r["orders_count"] or 0),
            "destination": r["destination"] or "",
            "proof_url": r["proof_url"] or "",
        } for r in rows]
        total = round(sum(e["raised"] for e in events), 2)
        return jsonify({"events": events, "total_raised": total}), 200
    except Exception:
        logger.exception("Erro em public_social_day_history")
        return jsonify({"events": [], "total_raised": 0}), 200
    finally:
        conn.close()
