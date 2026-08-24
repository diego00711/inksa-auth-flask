# -*- coding: utf-8 -*-
# src/routes/referrals_routes.py
"""Indique e ganhe — endpoints do cliente.

A regra de negócio toda mora em utils/referrals.py. Aqui só entra autenticação,
resolução do perfil e transporte.
"""
import logging

from flask import Blueprint, jsonify, request

from ..utils.helpers import get_db_connection, get_user_id_from_token
from ..utils.referrals import resumo, aplicar_codigo

logger = logging.getLogger(__name__)
referrals_bp = Blueprint('referrals', __name__)


def _perfil_do_cliente(cur, auth_uid):
    cur.execute("SELECT id FROM public.client_profiles WHERE user_id = %s", (auth_uid,))
    row = cur.fetchone()
    return str(row[0]) if row else None


@referrals_bp.route('/meu', methods=['GET'])
def meu_codigo():
    """Código do cliente + quantas indicações já renderam."""
    auth_uid, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'client':
        return jsonify({"error": "Indicação é do cliente."}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn, conn.cursor() as cur:
            client_id = _perfil_do_cliente(cur, auth_uid)
            if not client_id:
                return jsonify({"error": "Perfil não encontrado"}), 404
            return jsonify({"status": "success", "data": resumo(cur, client_id)}), 200
    except Exception:
        logger.exception("referrals.meu_codigo falhou")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try: conn.close()
        except Exception: pass


@referrals_bp.route('/aplicar', methods=['POST'])
def aplicar():
    """Cliente informa o código de quem o indicou. Body: {code}.

    Devolve o cupom de frete grátis pra ele usar no primeiro pedido. Erro de
    regra volta 200 com ok=false: a tela precisa explicar o motivo ("você já
    usou um código") em vez de mostrar "erro" genérico.
    """
    auth_uid, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'client':
        return jsonify({"error": "Indicação é do cliente."}), 403

    codigo = (request.get_json(silent=True) or {}).get('code')
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn, conn.cursor() as cur:
            client_id = _perfil_do_cliente(cur, auth_uid)
            if not client_id:
                return jsonify({"error": "Perfil não encontrado"}), 404
            return jsonify(aplicar_codigo(cur, client_id, codigo)), 200
    except Exception:
        logger.exception("referrals.aplicar falhou")
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try: conn.close()
        except Exception: pass
