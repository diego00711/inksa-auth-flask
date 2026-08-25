# -*- coding: utf-8 -*-
# src/routes/referrals_admin.py
"""Painel do admin para o "Indique e ganhe".

Duas coisas: VER o que o programa está custando e trazendo, e MEXER nos números
sem deploy. A segunda existe porque estes valores mudaram três vezes numa tarde
só — campanha que precisa de deploy pra ajustar não é campanha.
"""
import logging
from functools import wraps

import psycopg2.extras
from flask import Blueprint, jsonify, request

from ..utils.helpers import get_db_connection, get_user_id_from_token
from ..utils.platform_settings import invalidate_cache, get_settings

logger = logging.getLogger(__name__)
referrals_admin_bp = Blueprint('referrals_admin', __name__)

# key -> (mínimo, máximo). O teto não é desconfiança: é que um zero a mais num
# campo de texto vira prêmio de R$ 50 por indicação, pago em silêncio a cada
# entrega, e só aparece no fechamento do mês.
_CAMPOS = {
    "referral_enabled":       (0, 1),
    "referral_reward_brl":    (0, 100),
    "referral_min_order_brl": (0, 500),
    "referral_validity_days": (1, 365),
    "referral_monthly_cap":   (0, 1000),
}


def _admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        _uid, user_type, err = get_user_id_from_token(request.headers.get("Authorization"))
        if err:
            return err
        if user_type != "admin":
            return jsonify({"error": "Acesso não autorizado"}), 403
        return fn(*args, **kwargs)
    return wrapper


@referrals_admin_bp.get("")
@referrals_admin_bp.get("/")
@_admin
def painel():
    """Configuração + números + últimas indicações + ranking de quem mais traz."""
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        s = get_settings()
        config = {k: float(s[k]) for k in _CAMPOS}

        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # RESGATADO x EMITIDO é a distinção que importa: cupom emitido é
            # promessa, cupom usado é dinheiro que saiu. Reportar só o emitido
            # superestima o custo e assusta à toa.
            cur.execute("""
                SELECT
                  COUNT(*)                                                  AS total,
                  COUNT(*) FILTER (WHERE r.qualified_at IS NOT NULL)        AS premiadas,
                  COUNT(*) FILTER (WHERE r.qualified_at IS NULL)            AS pendentes,
                  COUNT(*) FILTER (WHERE r.qualified_at IS NOT NULL
                                     AND DATE_TRUNC('month', r.qualified_at)
                                       = DATE_TRUNC('month', NOW()))        AS premiadas_no_mes
                  FROM public.referrals r
            """)
            n = dict(cur.fetchone())

            cur.execute("""
                SELECT
                  COALESCE(SUM(c.discount_value), 0)                                AS emitido,
                  COALESCE(SUM(c.discount_value) FILTER
                    (WHERE COALESCE(c.uses_count,0) > 0), 0)                        AS resgatado,
                  COUNT(*) FILTER (WHERE COALESCE(c.uses_count,0) = 0
                                     AND c.valid_until < now())                     AS vencidos_sem_uso
                  FROM public.coupons c
                  JOIN public.referrals r ON r.reward_coupon_id = c.id
            """)
            dinheiro = dict(cur.fetchone())

            # Quantos indicados VOLTARAM. É o número que diz se a campanha traz
            # cliente ou só traz primeiro pedido — e é o que decide se ela se
            # paga, porque o custo da aquisição só volta do 2º pedido em diante.
            cur.execute("""
                SELECT COUNT(*) FILTER (WHERE p.entregues >= 2) AS voltaram,
                       COUNT(*)                                 AS indicados_com_pedido
                  FROM public.referrals r
                  JOIN LATERAL (
                        SELECT COUNT(*) AS entregues FROM public.orders o
                         WHERE o.client_id = r.referred_id AND o.status = 'delivered'
                  ) p ON TRUE
                 WHERE r.qualified_at IS NOT NULL
            """)
            retencao = dict(cur.fetchone())

            # client_profiles guarda nome e telefone; e-mail vive em users. O
            # telefone é o identificador que o Diego reconhece no dia a dia, e
            # é o que sobra quando a pessoa não preencheu o nome.
            cur.execute("""
                SELECT r.created_at, r.qualified_at, r.code_used,
                       TRIM(CONCAT(ind.first_name, ' ', ind.last_name))   AS indicador,
                       ind.phone                                          AS indicador_fone,
                       TRIM(CONCAT(novo.first_name, ' ', novo.last_name)) AS indicado,
                       novo.phone                                         AS indicado_fone,
                       c.code AS cupom, COALESCE(c.uses_count,0) > 0 AS cupom_usado
                  FROM public.referrals r
                  LEFT JOIN public.client_profiles ind  ON ind.id  = r.referrer_id
                  LEFT JOIN public.client_profiles novo ON novo.id = r.referred_id
                  LEFT JOIN public.coupons c ON c.id = r.reward_coupon_id
                 ORDER BY r.created_at DESC
                 LIMIT 100
            """)
            lista = [{
                "criada_em": r["created_at"].isoformat() if r["created_at"] else None,
                "premiada_em": r["qualified_at"].isoformat() if r["qualified_at"] else None,
                "codigo": r["code_used"],
                "indicador": (r["indicador"] or "").strip() or r["indicador_fone"] or "—",
                "indicado": (r["indicado"] or "").strip() or r["indicado_fone"] or "—",
                "cupom": r["cupom"],
                "cupom_usado": bool(r["cupom_usado"]),
            } for r in cur.fetchall()]

            cur.execute("""
                SELECT TRIM(CONCAT(p.first_name, ' ', p.last_name)) AS nome,
                       p.phone, p.referral_code,
                       COUNT(*) FILTER (WHERE r.qualified_at IS NOT NULL) AS premiadas,
                       COUNT(*)                                            AS total
                  FROM public.referrals r
                  JOIN public.client_profiles p ON p.id = r.referrer_id
                 GROUP BY p.id, p.first_name, p.last_name, p.phone, p.referral_code
                 ORDER BY premiadas DESC, total DESC
                 LIMIT 15
            """)
            ranking = [{
                "nome": (r["nome"] or "").strip() or r["phone"] or "—",
                "codigo": r["referral_code"],
                "premiadas": int(r["premiadas"] or 0),
                "total": int(r["total"] or 0),
            } for r in cur.fetchall()]

        return jsonify({"status": "success", "data": {
            "config": config,
            "numeros": {k: int(v or 0) for k, v in n.items()},
            "dinheiro": {
                "emitido": float(dinheiro["emitido"] or 0),
                "resgatado": float(dinheiro["resgatado"] or 0),
                "vencidos_sem_uso": int(dinheiro["vencidos_sem_uso"] or 0),
            },
            "retencao": {k: int(v or 0) for k, v in retencao.items()},
            "indicacoes": lista,
            "ranking": ranking,
        }}), 200
    except Exception:
        logger.exception("referrals_admin.painel falhou")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try: conn.close()
        except Exception: pass


@referrals_admin_bp.put("")
@referrals_admin_bp.put("/")
@_admin
def salvar_config():
    """Grava os números da campanha. Só as chaves conhecidas, dentro da faixa."""
    body = request.get_json(silent=True) or {}
    mudancas = {}
    for k, (lo, hi) in _CAMPOS.items():
        if k not in body:
            continue
        try:
            v = float(body[k])
        except (TypeError, ValueError):
            return jsonify({"error": f"Valor inválido em {k}"}), 422
        if v < lo or v > hi:
            return jsonify({"error": f"{k} deve ficar entre {lo} e {hi}"}), 422
        mudancas[k] = v
    if not mudancas:
        return jsonify({"error": "Nada para salvar"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn, conn.cursor() as cur:
            for k, v in mudancas.items():
                # Inteiro sai sem ".0": o valor é lido por gente no admin, e
                # "30.0 dias" parece defeito.
                txt = str(int(v)) if float(v).is_integer() else str(v)
                cur.execute("""
                    INSERT INTO public.platform_settings (key, value, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """, (k, txt))
        # Sem isto o admin salva e o app segue com o valor velho por até 60s —
        # e quem testa conclui que não salvou.
        invalidate_cache()
        return jsonify({"status": "success", "data": mudancas}), 200
    except Exception:
        logger.exception("referrals_admin.salvar_config falhou")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try: conn.close()
        except Exception: pass
