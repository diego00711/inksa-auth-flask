# src/routes/coupons_routes.py
# Blueprint: coupons_bp, prefix /api/coupons

import logging
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token
from ..utils.coupons import (evaluate_coupon, contar_usos_do_cliente, reserva_do_cliente,
                             precos_para_o_cupom)

logger = logging.getLogger(__name__)

coupons_bp = Blueprint('coupons', __name__)


def _texto_do_cupom(cupom, loja):
    """Título e corpo do push. Fala do BENEFÍCIO, não do cupom.

    "cupom criado" não diz nada pro cliente; "R$ 10 off na Higas Japan" diz.
    E o nome da loja no título é o que faz ele reconhecer que é um lugar
    onde ele já comeu — sem isso vira propaganda de desconhecido.
    """
    tipo = (cupom.get('discount_type') or '').lower()
    try:
        valor = float(cupom.get('discount_value') or 0)
    except (TypeError, ValueError):
        valor = 0.0

    if tipo == 'free_delivery':
        beneficio = 'frete grátis'
    elif tipo == 'percentage':
        beneficio = f'{valor:.0f}% de desconto'.replace('.0%', '%')
    else:
        beneficio = f'R$ {valor:.2f} de desconto'.replace('.', ',')

    minimo = ''
    try:
        mv = float(cupom.get('min_order_value') or 0)
        if mv > 0:
            minimo = f' em pedidos acima de R$ {mv:.2f}'.replace('.', ',')
    except (TypeError, ValueError):
        pass

    return (
        f'{beneficio} na {loja}',
        f'Use o código {cupom.get("code")}{minimo}. Aproveitar agora?',
    )


def _anunciar_cupom(cupom, restaurant_id):
    """Avisa por push os clientes que JÁ PEDIRAM nesta loja.

    Três travas, e cada uma existe por um motivo:

    1. SÓ QUEM JÁ PEDIU AQUI. Push de loja desconhecida é propaganda; push da
       hamburgueria onde a pessoa já comeu é serviço. Essa diferença decide
       se ela mantém ou silencia o app.
    2. TETO DIÁRIO (push_campaign_daily_cap, padrão 1). O concorrente manda 6
       em 96 minutos — funciona com milhões de usuários, onde perder uma
       fatia é aceitável. Com 19 clientes, cada um que silencia é 5% da base,
       e quem desliga não religa.
    3. UMA VEZ POR CUPOM. Índice único no banco: reenviar o mesmo cupom é
       impossível, mesmo que a rota seja chamada duas vezes.

    Nunca lança: falha aqui não pode desfazer a criação do cupom.
    """
    from ..services.notification_service import send_campaign

    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT restaurant_name FROM restaurant_profiles WHERE id = %s",
                        (str(restaurant_id),))
            row = cur.fetchone()
            loja = (row and row['restaurant_name']) or 'sua loja favorita'

            try:
                cur.execute("SELECT value FROM platform_settings WHERE key = 'push_campaign_daily_cap'")
                r = cur.fetchone()
                teto = int(str(r['value']).strip()) if r else 1
            except Exception:
                teto = 1
            if teto <= 0:
                return  # campanhas desligadas

            campanha = f"coupon:{cupom.get('id')}"
            cur.execute("""
                SELECT cp.id, cp.fcm_token
                  FROM client_profiles cp
                 WHERE NULLIF(TRIM(cp.fcm_token), '') IS NOT NULL
                   -- já pediu NESTA loja
                   AND EXISTS (SELECT 1 FROM orders o
                                WHERE o.client_id = cp.id
                                  AND o.restaurant_id = %s
                                  AND o.status NOT IN ('cancelled','canceled','awaiting_payment'))
                   -- não estourou o teto de campanhas de hoje
                   AND (SELECT COUNT(*) FROM push_campaign_log l
                         WHERE l.client_id = cp.id
                           AND (l.sent_at AT TIME ZONE 'America/Sao_Paulo')::date
                               = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date) < %s
                   -- ainda não recebeu ESTE cupom
                   AND NOT EXISTS (SELECT 1 FROM push_campaign_log l2
                                    WHERE l2.client_id = cp.id AND l2.campanha = %s)
            """, (str(restaurant_id), teto, campanha))
            destinos = [(r['id'], r['fcm_token']) for r in cur.fetchall()]

            if not destinos:
                logger.info("Cupom %s: nenhum cliente elegível pra push", cupom.get('code'))
                return

            titulo, corpo = _texto_do_cupom(cupom, loja)
            res = send_campaign(destinos, titulo, corpo, {
                'type': 'coupon', 'coupon_code': cupom.get('code'),
                'restaurant_id': str(restaurant_id), 'url': '/',
            })

            # Registra só quem REALMENTE recebeu — senão o teto do dia seria
            # consumido por envio que falhou.
            invalidos = set(res.get('invalidos') or [])
            enviados = [cid for cid, _ in destinos if cid not in invalidos]
            if enviados:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO push_campaign_log (client_id, campanha, tipo) VALUES %s "
                    "ON CONFLICT (client_id, campanha) DO NOTHING",
                    [(cid, campanha, 'coupon') for cid in enviados])
            # Token que o FCM recusou = app desinstalado. Limpa pra base não
            # encher de lixo e pro contador de "clientes com push" ser honesto.
            if invalidos:
                cur.execute("UPDATE client_profiles SET fcm_token = NULL WHERE id = ANY(%s::uuid[])",
                            ([str(c) for c in invalidos],))
            conn.commit()
            logger.info("Cupom %s anunciado: %d enviados de %d elegíveis",
                        cupom.get('code'), res.get('enviados', 0), len(destinos))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _so_digitado(data):
    """Cupom que NAO aparece no app: so vale pra quem digitar o codigo.

    Serve pra campanha de fora (radio, panfleto, parceria). Escondido, cada uso
    e prova de que aquele canal trouxe o pedido — se aparecesse na vitrine,
    qualquer cliente pegaria e a conta deixaria de medir coisa alguma.

    NAO e o mesmo que is_active = false: ali o cupom nao vale pra ninguem.
    Aqui ele vale normal, so nao e oferecido.
    """
    v = data.get('somente_digitado')
    if isinstance(v, str):
        return v.strip().lower() in ('1', 'true', 'yes', 'on', 'sim')
    return bool(v)


def _limite_por_cliente(data):
    """Lê max_uses_per_client do corpo, tolerando o que os apps mandam.

    Aceita número (1, 2, "1"), booleano (o checkbox "só 1 por cliente" manda
    true) e vazio. Vazio/0/false = NULL = sem limite por pessoa, que é o
    comportamento de sempre — cupom antigo não muda de regra sozinho.

    Teto de 100 pra um dedo escorregado no formulário não virar um número
    absurdo gravado no banco.
    """
    v = data.get('max_uses_per_client')
    if v is True:
        return 1
    if v in (None, '', False):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return min(n, 100) if n > 0 else None


def _table_exists(cur) -> bool:
    """Verifica se a tabela coupons existe."""
    try:
        cur.execute("""
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'coupons'
        """)
        return cur.fetchone() is not None
    except Exception:
        return False


@coupons_bp.route('/validate', methods=['POST'])
def validate_coupon():
    """
    POST /api/coupons/validate
    Body: { "code": str, "order_total": float }
    Retorna: { valid, discount_type, discount_value, discount_amount, message }
    """
    conn = None
    try:
        data = request.get_json(silent=True) or {}
        code = str(data.get('code', '')).strip().upper()
        try:
            order_total = float(data.get('order_total', 0))
        except (ValueError, TypeError):
            order_total = 0.0
        try:
            delivery_fee = float(data.get('delivery_fee', 0))
        except (ValueError, TypeError):
            delivery_fee = 0.0
        # Loja do carrinho — usada pra filtrar cupom de outra loja.
        restaurant_id = data.get('restaurant_id') or None

        if not code:
            return jsonify({"valid": False, "message": "Codigo do cupom e obrigatorio"}), 400

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexao com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if not _table_exists(cur):
                logger.warning("Tabela coupons nao existe. Execute create_coupons.sql")
                return jsonify({"valid": False, "message": "Sistema de cupons nao configurado. Execute create_coupons.sql"}), 200

            # A loja do carrinho decide quais cupons servem: os DELA e os da
            # plataforma (restaurant_id NULL). O cupom de outra loja nem é
            # buscado — senão o cliente veria "cupom válido" e o desconto seria
            # recusado no fechamento.
            cur.execute("""
                SELECT id, code, discount_type, discount_value, min_order_value,
                       max_uses, uses_count, max_uses_per_client, valid_until,
                       is_active, restaurant_id, paid_by, owner_client_id
                FROM public.coupons
                WHERE UPPER(code) = %s
                  AND (restaurant_id IS NULL OR restaurant_id = %s)
                ORDER BY restaurant_id NULLS LAST
                LIMIT 1
            """, (code, restaurant_id))
            coupon = cur.fetchone()

            # Quem já usou este cupom não pode descobrir só no fechamento: se o
            # carrinho disser "válido" e o pedido recusar, a pessoa acha que o
            # app quebrou. O token é opcional aqui — sem ele, conta 0 e a trava
            # real continua sendo a do fechamento.
            usos = 0
            cliente_id = None
            reserva = None   # oferta relâmpago: quando a reserva DESTE cliente expira
            if coupon is not None:
                try:
                    uid, utype, err = get_user_id_from_token(request.headers.get('Authorization'))
                    if err is None and utype == 'client':
                        cur.execute("SELECT id FROM client_profiles WHERE user_id = %s", (uid,))
                        perfil = cur.fetchone()
                        if perfil:
                            cliente_id = perfil['id']
                            usos = contar_usos_do_cliente(coupon['id'], perfil['id'], cur)
                            reserva = reserva_do_cliente(coupon['id'], perfil['id'], cur)
                except Exception:
                    usos = 0

        # Validação/cálculo centralizado (mesma lógica do fechamento do pedido)
        result = evaluate_coupon(dict(coupon) if coupon else None, order_total, delivery_fee,
                                 restaurant_id=restaurant_id, usos_deste_cliente=usos,
                                 client_id=cliente_id, reserva_expira_em=reserva,
                                 # O preview precisa da MESMA conta do fechamento,
                                 # senão o carrinho diz "vale" e o pedido recusa.
                                 itens_do_carrinho=precos_para_o_cupom(
                                     coupon, (request.get_json(silent=True) or {}).get('itens') or []))
        if not result["valid"]:
            return jsonify({"valid": False, "message": result["message"]}), 200

        return jsonify({
            "valid": True,
            "coupon_id": str(coupon['id']),
            "code": coupon['code'],
            "discount_type": result["discount_type"],
            "discount_value": float(coupon['discount_value']),
            "discount_amount": result["discount_amount"],
            "message": "Cupom valido!"
        }), 200

    except Exception as e:
        logger.error(f"Erro em validate_coupon: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()


@coupons_bp.route('/disponiveis', methods=['GET'])
def cupons_disponiveis():
    """Cupons que ESTE cliente pode usar NESTE carrinho, com o desconto de cada um.

    POR QUE EXISTE: só entra UM cupom por pedido, mas o cliente não tinha como
    saber disso nem o que tinha na mão. O convidado ganha frete grátis, chega
    numa loja que está com promoção própria, e escolhia às cegas — ou nem sabia
    que tinha escolha, porque o cupom dele só existia dentro de uma notificação.

    Devolve tudo já avaliado contra este carrinho (evaluate_coupon, a mesma
    função do checkout), ordenado pelo que economiza mais. Assim a tela mostra
    a decisão em vez de esconder o conflito.

    Query: restaurant_id, subtotal, delivery_fee.
    """
    auth_uid, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'client':
        return jsonify({"status": "success", "data": []}), 200

    restaurant_id = request.args.get('restaurant_id') or None
    try:
        subtotal = float(request.args.get('subtotal') or 0)
        delivery_fee = float(request.args.get('delivery_fee') or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "subtotal/delivery_fee inválidos"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM public.client_profiles WHERE user_id = %s", (auth_uid,))
            perfil = cur.fetchone()
            if not perfil:
                return jsonify({"status": "success", "data": []}), 200
            client_id = str(perfil['id'])

            # Três origens: o cupom PESSOAL dele, o da LOJA do carrinho e o de
            # CAMPANHA da plataforma. Cupom pessoal de outra pessoa nem é lido.
            cur.execute("""
                SELECT id, code, discount_type, discount_value, min_order_value,
                       max_uses, uses_count, max_uses_per_client, valid_until,
                       is_active, restaurant_id, paid_by, owner_client_id, description
                  FROM public.coupons
                 WHERE is_active
                   -- Cupom de campanha externa não entra na lista do carrinho:
                   -- ele só vale digitado. Aparecer aqui daria o desconto a
                   -- quem nunca ouviu o anúncio, e o uso deixaria de medir o
                   -- canal. O /validate continua aceitando normalmente.
                   AND NOT somente_digitado
                   AND (valid_until IS NULL OR valid_until >= now())
                   AND (COALESCE(uses_count,0) < COALESCE(max_uses, 2147483647))
                   AND (owner_client_id = %s
                        OR (owner_client_id IS NULL
                            AND (restaurant_id IS NULL OR restaurant_id = %s)))
                 ORDER BY created_at DESC
                 LIMIT 50
            """, (client_id, restaurant_id))
            candidatos = [dict(r) for r in cur.fetchall()]

            saida = []
            for c in candidatos:
                usos = contar_usos_do_cliente(c['id'], client_id, cur)
                # Relâmpago sem reserva viva cai como inválido logo abaixo e some
                # da lista — é o comportamento certo: oferta que ele não ativou
                # não deve aparecer como disponível no carrinho.
                r = evaluate_coupon(c, subtotal, delivery_fee,
                                    restaurant_id=restaurant_id,
                                    usos_deste_cliente=usos, client_id=client_id,
                                    reserva_expira_em=reserva_do_cliente(c['id'], client_id, cur),
                                    # Esta rota é GET e não recebe os itens do
                                    # carrinho — e não precisa: oferta relâmpago
                                    # nasce `somente_digitado = TRUE`, então ela
                                    # é filtrada da consulta lá em cima e nunca
                                    # chega aqui. Lista vazia deixa a recusa
                                    # explícita se um dia chegar.
                                    itens_do_carrinho={})
                if not r["valid"] or r["discount_amount"] <= 0:
                    continue
                saida.append({
                    "codigo": c["code"],
                    "desconto": round(float(r["discount_amount"]), 2),
                    "tipo": c["discount_type"],
                    "minimo": float(c["min_order_value"] or 0),
                    "vence_em": c["valid_until"].isoformat() if c["valid_until"] else None,
                    "meu": c["owner_client_id"] is not None,
                    "da_loja": c["restaurant_id"] is not None,
                    "descricao": c["description"],
                })

        # Melhor primeiro: a tela precisa poder dizer qual compensa mais sem
        # fazer o cliente calcular.
        saida.sort(key=lambda x: x["desconto"], reverse=True)
        return jsonify({"status": "success", "data": saida}), 200
    except Exception:
        logger.exception("cupons_disponiveis falhou")
        # Falha aqui não pode travar o carrinho: sem a lista, o campo de código
        # continua funcionando como sempre funcionou.
        return jsonify({"status": "success", "data": []}), 200
    finally:
        try: conn.close()
        except Exception: pass


@coupons_bp.route('/admin', methods=['GET'])
def list_coupons():
    """
    GET /api/coupons/admin
    Lista todos os cupons. Requer autenticacao admin.
    """
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'admin':
            return jsonify({"error": "Acesso restrito a administradores"}), 403

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexao com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if not _table_exists(cur):
                return jsonify({"error": "Tabela coupons nao existe. Execute create_coupons.sql"}), 503

            cur.execute("""
                SELECT c.id, c.code, c.discount_type, c.discount_value, c.min_order_value,
                       c.max_uses, c.uses_count, c.valid_until, c.is_active, c.created_at,
                       c.restaurant_id, c.paid_by, c.description, c.somente_digitado,
                       rp.restaurant_name
                FROM public.coupons c
                LEFT JOIN public.restaurant_profiles rp ON rp.id = c.restaurant_id
                ORDER BY c.created_at DESC
            """)
            rows = cur.fetchall()

        result = []
        for row in rows:
            r = dict(row)
            r['id'] = str(r['id'])
            if r.get('valid_until'):
                r['valid_until'] = r['valid_until'].isoformat()
            if r.get('created_at'):
                r['created_at'] = r['created_at'].isoformat()
            r['discount_value'] = float(r['discount_value'])
            r['min_order_value'] = float(r['min_order_value'] or 0)
            if r.get('restaurant_id'):
                r['restaurant_id'] = str(r['restaurant_id'])
            # Rótulo pronto pro admin não ter que deduzir de quem é o cupom.
            r['owner_label'] = r.get('restaurant_name') or 'Inksa (plataforma)'
            result.append(r)

        return jsonify({"coupons": result, "total": len(result)}), 200

    except Exception as e:
        logger.error(f"Erro em list_coupons: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()


@coupons_bp.route('/admin', methods=['POST'])
def create_coupon():
    """
    POST /api/coupons/admin
    Body: { code, discount_type, discount_value, min_order_value, max_uses, valid_until }
    Requer autenticacao admin.
    """
    conn = None
    try:
        user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
        if error:
            return error
        if user_type != 'admin':
            return jsonify({"error": "Acesso restrito a administradores"}), 403

        data = request.get_json(silent=True) or {}
        code = str(data.get('code', '')).strip().upper()
        discount_type = str(data.get('discount_type', '')).strip().lower()
        try:
            discount_value = float(data['discount_value'])
        except (KeyError, ValueError, TypeError):
            return jsonify({"error": "discount_value invalido ou ausente"}), 400

        if not code:
            return jsonify({"error": "code e obrigatorio"}), 400
        if discount_type not in ('percentage', 'fixed', 'free_delivery'):
            return jsonify({"error": "discount_type invalido. Use: percentage, fixed, free_delivery"}), 400

        min_order_value = float(data.get('min_order_value', 0) or 0)
        max_uses = data.get('max_uses')
        valid_until = data.get('valid_until')

        conn = get_db_connection()
        if not conn:
            return jsonify({"error": "Erro de conexao com o banco de dados"}), 500

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if not _table_exists(cur):
                return jsonify({"error": "Tabela coupons nao existe. Execute create_coupons.sql"}), 503

            try:
                cur.execute("""
                    INSERT INTO public.coupons
                        (code, discount_type, discount_value, min_order_value, max_uses,
                         max_uses_per_client, valid_until, somente_digitado)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, code, discount_type, discount_value, min_order_value,
                              max_uses, uses_count, max_uses_per_client, valid_until,
                              is_active, created_at, somente_digitado
                """, (code, discount_type, discount_value, min_order_value, max_uses,
                      _limite_por_cliente(data), valid_until, _so_digitado(data)))
                new_coupon = dict(cur.fetchone())
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                return jsonify({"error": f"Cupom com codigo '{code}' ja existe"}), 409

        new_coupon['id'] = str(new_coupon['id'])
        if new_coupon.get('valid_until'):
            new_coupon['valid_until'] = new_coupon['valid_until'].isoformat()
        if new_coupon.get('created_at'):
            new_coupon['created_at'] = new_coupon['created_at'].isoformat()
        new_coupon['discount_value'] = float(new_coupon['discount_value'])
        new_coupon['min_order_value'] = float(new_coupon['min_order_value'] or 0)

        return jsonify({"success": True, "coupon": new_coupon}), 201

    except Exception as e:
        logger.error(f"Erro em create_coupon: {e}", exc_info=True)
        if conn:
            conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# CUPONS DO PARCEIRO
# O parceiro cria cupom SÓ da própria loja, e o desconto sai do repasse DELE
# (paid_by='restaurant'). As duas coisas são forçadas no servidor: nada que vem
# no body decide dono nem quem paga — senão daria pra criar cupom em nome de
# outra loja, ou jogar a conta na comissão da Inksa.
# ─────────────────────────────────────────────────────────────────────────────

_COUPON_COLS = """id, code, discount_type, discount_value, min_order_value,
                  max_uses, uses_count, max_uses_per_client, valid_until,
                  is_active, created_at, restaurant_id, paid_by, description,
                  somente_digitado"""


def _own_restaurant_id(cur, user_id):
    """id do restaurant_profiles do parceiro logado (None se não achar)."""
    cur.execute("SELECT id FROM public.restaurant_profiles WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    return row[0] if row else None


def _partner_ctx():
    """(user_id, erro_response). Só parceiro passa."""
    user_id, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return None, error
    if user_type != 'restaurant':
        return None, (jsonify({"error": "Acesso restrito a parceiros"}), 403)
    return user_id, None


def _max_discount_pct():
    """Teto de desconto que o parceiro pode criar (configurável no admin).
    Evita que ele se afunde sem querer com um '90% OFF' digitado errado."""
    try:
        from ..utils.platform_settings import get_settings
        return float(get_settings().get("coupon_max_discount_pct") or 30)
    except Exception:
        return 30.0


def _serialize(row):
    r = dict(row)
    r['id'] = str(r['id'])
    if r.get('restaurant_id'):
        r['restaurant_id'] = str(r['restaurant_id'])
    for k in ('valid_until', 'created_at'):
        if r.get(k) and hasattr(r[k], 'isoformat'):
            r[k] = r[k].isoformat()
    if r.get('discount_value') is not None:
        r['discount_value'] = float(r['discount_value'])
    r['min_order_value'] = float(r.get('min_order_value') or 0)
    return r


@coupons_bp.route('/mine', methods=['GET'])
def list_my_coupons():
    """GET /api/coupons/mine — cupons da loja do parceiro logado."""
    user_id, err = _partner_ctx()
    if err:
        return err
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexao com o banco de dados"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            if not _table_exists(cur):
                return jsonify({"coupons": [], "total": 0}), 200
            rid = _own_restaurant_id(cur, user_id)
            if not rid:
                return jsonify({"error": "Perfil de parceiro nao encontrado"}), 404
            cur.execute(
                f"SELECT {_COUPON_COLS} FROM public.coupons "
                "WHERE restaurant_id = %s ORDER BY created_at DESC", (rid,))
            rows = [_serialize(r) for r in cur.fetchall()]
        return jsonify({"coupons": rows, "total": len(rows),
                        "max_discount_pct": _max_discount_pct()}), 200
    except Exception as e:
        logger.error(f"Erro em list_my_coupons: {e}", exc_info=True)
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        conn.close()


@coupons_bp.route('/mine', methods=['POST'])
def create_my_coupon():
    """POST /api/coupons/mine — parceiro cria cupom da própria loja."""
    user_id, err = _partner_ctx()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    code = str(data.get('code', '')).strip().upper()
    dtype = str(data.get('discount_type', '')).strip().lower()
    try:
        dvalue = float(data['discount_value'])
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "Valor do desconto invalido"}), 400

    if not code or len(code) < 3:
        return jsonify({"error": "O codigo precisa ter ao menos 3 letras"}), 400
    if dtype not in ('percentage', 'fixed', 'free_delivery'):
        return jsonify({"error": "Tipo de desconto invalido"}), 400
    if dtype != 'free_delivery' and dvalue <= 0:
        return jsonify({"error": "O desconto precisa ser maior que zero"}), 400

    teto = _max_discount_pct()
    if dtype == 'percentage' and dvalue > teto:
        return jsonify({"error": f"O desconto maximo permitido e {teto:.0f}%"}), 400

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexao com o banco de dados"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            rid = _own_restaurant_id(cur, user_id)
            if not rid:
                return jsonify({"error": "Perfil de parceiro nao encontrado"}), 404
            try:
                cur.execute(
                    f"""INSERT INTO public.coupons
                            (code, discount_type, discount_value, min_order_value, max_uses,
                             max_uses_per_client, valid_until, description, restaurant_id,
                             somente_digitado, paid_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'restaurant')
                        RETURNING {_COUPON_COLS}""",
                    (code, dtype, dvalue, float(data.get('min_order_value') or 0),
                     data.get('max_uses') or None, _limite_por_cliente(data),
                     data.get('valid_until') or None,
                     (data.get('description') or None), rid, _so_digitado(data)))
                novo = _serialize(cur.fetchone())
                conn.commit()
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                return jsonify({"error": f"Voce ja tem um cupom com o codigo {code}"}), 409
        # Avisa quem já é cliente DESTA loja. Fora da transação de propósito:
        # falha de push jamais pode desfazer a criação do cupom.
        try:
            _anunciar_cupom(novo, rid)
        except Exception:
            logger.warning("Falha ao anunciar cupom por push", exc_info=True)

        return jsonify({"success": True, "coupon": novo}), 201
    except Exception as e:
        logger.error(f"Erro em create_my_coupon: {e}", exc_info=True)
        conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        conn.close()


@coupons_bp.route('/mine/<coupon_id>', methods=['PUT'])
def update_my_coupon(coupon_id):
    """PUT /api/coupons/mine/<id> — edita cupom da própria loja.
    O WHERE inclui restaurant_id: o parceiro não alcança cupom de outra loja
    nem da plataforma, mesmo mandando o id certo."""
    user_id, err = _partner_ctx()
    if err:
        return err
    data = request.get_json(silent=True) or {}
    campos, valores = [], []
    if 'is_active' in data:
        campos.append("is_active = %s"); valores.append(bool(data['is_active']))
    if 'somente_digitado' in data:
        campos.append("somente_digitado = %s"); valores.append(_so_digitado(data))
    if 'min_order_value' in data:
        campos.append("min_order_value = %s"); valores.append(float(data.get('min_order_value') or 0))
    if 'max_uses' in data:
        campos.append("max_uses = %s"); valores.append(data.get('max_uses') or None)
    if 'max_uses_per_client' in data:
        campos.append("max_uses_per_client = %s"); valores.append(_limite_por_cliente(data))
    if 'valid_until' in data:
        campos.append("valid_until = %s"); valores.append(data.get('valid_until') or None)
    if 'description' in data:
        campos.append("description = %s"); valores.append(data.get('description') or None)
    if 'discount_value' in data:
        try:
            dv = float(data['discount_value'])
        except (ValueError, TypeError):
            return jsonify({"error": "Valor do desconto invalido"}), 400
        teto = _max_discount_pct()
        if str(data.get('discount_type', '')).lower() == 'percentage' and dv > teto:
            return jsonify({"error": f"O desconto maximo permitido e {teto:.0f}%"}), 400
        campos.append("discount_value = %s"); valores.append(dv)
    if not campos:
        return jsonify({"error": "Nada para atualizar"}), 400
    campos.append("updated_at = NOW()")

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexao com o banco de dados"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            rid = _own_restaurant_id(cur, user_id)
            if not rid:
                return jsonify({"error": "Perfil de parceiro nao encontrado"}), 404
            cur.execute(
                f"UPDATE public.coupons SET {', '.join(campos)} "
                f"WHERE id = %s AND restaurant_id = %s RETURNING {_COUPON_COLS}",
                (*valores, coupon_id, rid))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Cupom nao encontrado"}), 404
            conn.commit()
        return jsonify({"success": True, "coupon": _serialize(row)}), 200
    except Exception as e:
        logger.error(f"Erro em update_my_coupon: {e}", exc_info=True)
        conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        conn.close()


@coupons_bp.route('/mine/<coupon_id>', methods=['DELETE'])
def delete_my_coupon(coupon_id):
    """DELETE /api/coupons/mine/<id> — apaga cupom da própria loja."""
    user_id, err = _partner_ctx()
    if err:
        return err
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexao com o banco de dados"}), 500
    try:
        with conn.cursor() as cur:
            rid = _own_restaurant_id(cur, user_id)
            if not rid:
                return jsonify({"error": "Perfil de parceiro nao encontrado"}), 404
            cur.execute("DELETE FROM public.coupons WHERE id = %s AND restaurant_id = %s",
                        (coupon_id, rid))
            apagou = cur.rowcount
            conn.commit()
        if not apagou:
            return jsonify({"error": "Cupom nao encontrado"}), 404
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.error(f"Erro em delete_my_coupon: {e}", exc_info=True)
        conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        conn.close()


# ─── Admin: editar e excluir (faltavam — errou o valor, tinha que ir no banco) ─

@coupons_bp.route('/admin/<coupon_id>', methods=['PUT', 'PATCH'])
def admin_update_coupon(coupon_id):
    # PATCH também: o admin já chamava PATCH pra ativar/desativar e caía em 404.
    _, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'admin':
        return jsonify({"error": "Acesso restrito a administradores"}), 403

    data = request.get_json(silent=True) or {}
    campos, valores = [], []
    for campo in ('discount_type', 'description'):
        if campo in data:
            campos.append(f"{campo} = %s"); valores.append(data[campo] or None)
    for campo in ('discount_value', 'min_order_value'):
        if campo in data:
            try:
                valores.append(float(data[campo] or 0))
            except (ValueError, TypeError):
                return jsonify({"error": f"{campo} invalido"}), 400
            campos.append(f"{campo} = %s")
    if 'max_uses' in data:
        campos.append("max_uses = %s"); valores.append(data.get('max_uses') or None)
    if 'max_uses_per_client' in data:
        campos.append("max_uses_per_client = %s"); valores.append(_limite_por_cliente(data))
    if 'valid_until' in data:
        campos.append("valid_until = %s"); valores.append(data.get('valid_until') or None)
    if 'is_active' in data:
        campos.append("is_active = %s"); valores.append(bool(data['is_active']))
    if 'somente_digitado' in data:
        campos.append("somente_digitado = %s"); valores.append(_so_digitado(data))
    if 'paid_by' in data and data['paid_by'] in ('platform', 'restaurant'):
        campos.append("paid_by = %s"); valores.append(data['paid_by'])
    if not campos:
        return jsonify({"error": "Nada para atualizar"}), 400
    campos.append("updated_at = NOW()")

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexao com o banco de dados"}), 500
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(f"UPDATE public.coupons SET {', '.join(campos)} "
                        f"WHERE id = %s RETURNING {_COUPON_COLS}", (*valores, coupon_id))
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Cupom nao encontrado"}), 404
            conn.commit()
        return jsonify({"success": True, "coupon": _serialize(row)}), 200
    except Exception as e:
        logger.error(f"Erro em admin_update_coupon: {e}", exc_info=True)
        conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        conn.close()


@coupons_bp.route('/admin/<coupon_id>', methods=['DELETE'])
def admin_delete_coupon(coupon_id):
    _, user_type, error = get_user_id_from_token(request.headers.get('Authorization'))
    if error:
        return error
    if user_type != 'admin':
        return jsonify({"error": "Acesso restrito a administradores"}), 403
    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "Erro de conexao com o banco de dados"}), 500
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM public.coupons WHERE id = %s", (coupon_id,))
            apagou = cur.rowcount
            conn.commit()
        if not apagou:
            return jsonify({"error": "Cupom nao encontrado"}), 404
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.error(f"Erro em admin_delete_coupon: {e}", exc_info=True)
        conn.rollback()
        return jsonify({"error": "Erro interno do servidor"}), 500
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════
# OFERTA RELÂMPAGO
# ═══════════════════════════════════════════════════════════════════════════

@coupons_bp.route('/relampago/<uuid:banner_id>/reservar', methods=['POST', 'OPTIONS'])
def reservar_relampago(banner_id):
    """O cliente tocou no banner: reserva a oferta pra ele e devolve o código.

    A reserva é o que faz o relógio existir. São DOIS tempos, e o cupom só vale
    dentro dos dois:
      1. a JANELA da campanha — `banners.starts_at/ends_at`, igual pra todos
      2. a RESERVA deste cliente — `reserva_minutos` contados deste toque

    ⚠️ UMA POR CLIENTE, SEM RENOVAR. Se ele deixar os minutos passarem, acabou —
    tocar de novo devolve a reserva vencida, não uma nova. Sem isso o relógio
    seria enfeite: bastava tocar outra vez pra ganhar mais tempo, e "relâmpago"
    viraria "promoção comum com contador". Quem garante isso é a UNIQUE
    (coupon_id, client_id) no banco, não este código.
    """
    if request.method == 'OPTIONS':
        return jsonify({}), 204

    uid, utype, err = get_user_id_from_token(request.headers.get('Authorization'))
    if err:
        return err
    if utype != 'client':
        return jsonify({"error": "Apenas clientes reservam ofertas"}), 403

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM client_profiles WHERE user_id = %s", (uid,))
            perfil = cur.fetchone()
            if not perfil:
                return jsonify({"error": "Perfil de cliente não encontrado"}), 404
            client_id = perfil['id']

            # Banner + cupom numa consulta só. A janela da campanha é conferida
            # AQUI: banner fora do ar não reserva nada, mesmo que alguém chame a
            # rota direto.
            cur.execute("""
                SELECT b.id AS banner_id, b.restaurant_id,
                       (b.is_active IS TRUE
                        AND (b.starts_at IS NULL OR b.starts_at <= NOW())
                        AND (b.ends_at   IS NULL OR b.ends_at   >= NOW())) AS no_ar,
                       c.id AS coupon_id, c.code, c.reserva_minutos,
                       c.is_active AS cupom_ativo, c.max_uses, c.uses_count,
                       c.menu_item_id, r.slug
                  FROM banners b
                  LEFT JOIN coupons c            ON c.id = b.coupon_id
                  LEFT JOIN restaurant_profiles r ON r.id = b.restaurant_id
                 WHERE b.id = %s
            """, (str(banner_id),))
            b = cur.fetchone()

            if not b:
                return jsonify({"error": "Oferta não encontrada"}), 404
            if not b['coupon_id']:
                return jsonify({"error": "Este banner não tem oferta"}), 409
            if not b['no_ar']:
                return jsonify({"error": "Esta oferta já saiu do ar"}), 409
            if not b['cupom_ativo']:
                return jsonify({"error": "Esta oferta não está mais ativa"}), 409

            minutos = int(b['reserva_minutos'] or 0)
            if minutos <= 0:
                # Banner com cupom comum: nada a reservar, só entrega o código.
                return jsonify({"status": "success", "data": {
                    "codigo": b['code'], "slug": b['slug'],
                    "restaurant_id": str(b['restaurant_id']) if b['restaurant_id'] else None,
                    "expira_em": None,
                }}), 200

            # Esgotou antes de ele chegar. Vale conferir ANTES de criar reserva:
            # reservar o que não existe mais só adiaria a recusa pro fechamento.
            if b['max_uses'] is not None and int(b['uses_count'] or 0) >= int(b['max_uses']):
                return jsonify({"error": "Esta oferta acabou"}), 409

            # ON CONFLICT DO NOTHING + leitura: quem já tinha reserva recebe a
            # DELE, viva ou vencida. Nunca renova — ver o aviso lá em cima.
            cur.execute("""
                INSERT INTO coupon_reservations (coupon_id, client_id, banner_id, expires_at)
                VALUES (%s, %s, %s, NOW() + make_interval(mins => %s))
                ON CONFLICT (coupon_id, client_id) DO NOTHING
            """, (b['coupon_id'], client_id, b['banner_id'], minutos))
            criou_agora = cur.rowcount > 0

            cur.execute("""SELECT expires_at, expires_at > NOW() AS viva
                             FROM coupon_reservations
                            WHERE coupon_id = %s AND client_id = %s""",
                        (b['coupon_id'], client_id))
            reserva = cur.fetchone()

            # Contabiliza o toque no mesmo lugar que já conta clique de banner.
            cur.execute("UPDATE banners SET click_count = COALESCE(click_count,0) + 1 "
                        "WHERE id = %s", (b['banner_id'],))
            conn.commit()

        if not reserva['viva']:
            return jsonify({"error": "Sua oferta relâmpago expirou",
                            "expirou": True}), 409

        return jsonify({"status": "success", "data": {
            "codigo": b['code'],
            "slug": b['slug'],
            # Pra onde a tela leva: com item, abre o produto; sem, cai na loja.
            "menu_item_id": str(b['menu_item_id']) if b['menu_item_id'] else None,
            "restaurant_id": str(b['restaurant_id']) if b['restaurant_id'] else None,
            "expira_em": reserva['expires_at'].isoformat(),
            "primeira_vez": criou_agora,
        }}), 200

    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("Erro ao reservar oferta relâmpago do banner %s", banner_id)
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


# Quanto tempo sem dar sinal de vida conta como "app fechado".
# O app do cliente carimba last_seen a cada batida (client.py:60). 15 min é
# folgado de propósito: quem fechou o app há 5 minutos ainda está por perto e
# receber push do que ele acabou de ver na tela é irritante.
_MINUTOS_PRA_CONSIDERAR_FECHADO = 15


def _publico_do_relampago(cur, banner, publico, so_app_fechado, campanha, quem_disparou=None):
    """Monta a lista de (client_id, fcm_token) que vai receber o push.

    `publico` diz QUEM entra:
      'ja_pediram' — quem já pediu NESTA loja. Lista morna, melhor conversão,
                     mas não traz gente nova.
      'no_raio'    — quem está dentro do raio de entrega da loja. ⚠️ depende de
                     `client_profiles.latitude/longitude`, que só começou a ser
                     preenchido em 12/09/2026 — cliente antigo não tem, e por
                     isso NÃO entra. Hoje isso é quase ninguém.
      'todos'      — todo cliente com notificação ligada. É o que serve pra
                     TRAZER USUÁRIO, que é o propósito da oferta relâmpago.

    As travas que sobrevivem em qualquer público:
      - token de notificação existe
      - não recebeu ESTA rodada ainda (push_campaign_log, índice único)
      - app fechado, se pedido
    """
    condicoes = ["NULLIF(TRIM(cp.fcm_token), '') IS NOT NULL",
                 "NOT EXISTS (SELECT 1 FROM push_campaign_log l "
                 "             WHERE l.client_id = cp.id AND l.campanha = %s)"]
    args = [campanha]

    if so_app_fechado:
        # last_seen NULL = nunca abriu depois do recurso existir: conta como
        # fechado. Fail-open aqui só custa um push a mais pra quem está no app.
        condicoes.append("(cp.last_seen IS NULL OR cp.last_seen < NOW() - make_interval(mins => %s))")
        args.append(_MINUTOS_PRA_CONSIDERAR_FECHADO)

    if publico == 'so_eu':
        # TESTE EM SI MESMO. Manda só pro perfil de CLIENTE de quem está
        # disparando, e ignora tudo que filtraria um envio de verdade: app
        # fechado, teto do dia e "já recebeu esta rodada".
        #
        # Sem isso não dá pra testar, e o motivo é irônico: quem está no admin
        # acabou de usar o app, então o filtro de "app fechado" exclui
        # justamente a pessoa que quer ver a notificação chegar.
        cur.execute("SELECT id, fcm_token FROM client_profiles "
                    " WHERE user_id = %s AND NULLIF(TRIM(fcm_token),'') IS NOT NULL",
                    (str(quem_disparou),))
        r = cur.fetchone()
        return [(r['id'], r['fcm_token'])] if r else []

    if publico == 'ja_pediram':
        condicoes.append(
            "EXISTS (SELECT 1 FROM orders o WHERE o.client_id = cp.id "
            "         AND o.restaurant_id = %s "
            "         AND o.status NOT IN ('cancelled','canceled','awaiting_payment'))")
        args.append(str(banner['restaurant_id']))

    elif publico == 'no_raio':
        condicoes.append("cp.latitude IS NOT NULL AND cp.longitude IS NOT NULL")
        # earth_distance/ll_to_earth, NÃO PostGIS: este banco tem `cube` e
        # `earthdistance`, e PostGIS não está instalado. É a mesma função que o
        # filtro de raio do banner já usa (banners.py:116).
        condicoes.append(
            "earth_distance(ll_to_earth(cp.latitude, cp.longitude), "
            "               ll_to_earth(%s, %s)) <= %s * 1000.0")
        args += [banner['loja_lat'], banner['loja_lng'], banner['raio_km']]

    cur.execute("SELECT cp.id, cp.fcm_token FROM client_profiles cp WHERE "
                + " AND ".join(condicoes), tuple(args))
    return [(r['id'], r['fcm_token']) for r in cur.fetchall()]


@coupons_bp.route('/relampago/<uuid:banner_id>/disparar', methods=['POST', 'OPTIONS'])
def disparar_relampago(banner_id):
    """Manda o push da oferta relâmpago. Só admin.

    Body: {
      "publico": "todos" | "ja_pediram" | "no_raio",
      "rodada":  "inicio" | "ultima_chamada",
      "so_app_fechado": true
    }

    DUAS RODADAS, DUAS CHAVES DE CAMPANHA. A chave é
    `relampago:<banner>:<rodada>`, e o índice único de `push_campaign_log` é por
    chave — então a "última chamada" alcança inclusive quem já recebeu o
    primeiro aviso. Se as duas dividissem a mesma chave, a última chamada não
    sairia pra ninguém, que é justamente o oposto do pedido.

    ⚠️ O TETO DIÁRIO VALE NA PRIMEIRA RODADA E NÃO VALE NA ÚLTIMA CHAMADA.
    Isso é escolha, não descuido: o teto existe pra impedir bombardeio de
    campanhas DIFERENTES no mesmo dia, e a última chamada é o fim da MESMA
    campanha que a pessoa já recebeu. O limite real continua sendo dois pushes
    por oferta e por pessoa, garantido pelas duas chaves.

    "App fechado" = sem bater o heartbeat há 15 min. Quem está com o app aberto
    já vê o banner na tela; mandar push do que ele está olhando é o tipo de
    coisa que faz desinstalar.
    """
    if request.method == 'OPTIONS':
        return jsonify({}), 204

    uid, utype, err = get_user_id_from_token(request.headers.get('Authorization'))
    if err:
        return err
    if utype != 'admin':
        return jsonify({"error": "Apenas admin dispara campanha"}), 403

    corpo = request.get_json(silent=True) or {}
    publico = (corpo.get('publico') or 'todos').strip().lower()
    if publico not in ('todos', 'ja_pediram', 'no_raio', 'so_eu'):
        return jsonify({"error": "publico inválido"}), 400
    rodada = (corpo.get('rodada') or 'inicio').strip().lower()
    if rodada not in ('inicio', 'ultima_chamada'):
        return jsonify({"error": "rodada inválida"}), 400
    so_app_fechado = corpo.get('so_app_fechado', True) is not False

    # QUANTAS PESSOAS AVISAR DESTA VEZ.
    #
    # Existe pra não prometer 50 lanches quando só há 10. O `max_uses` já impede
    # o 11º de USAR, mas aí o estrago já foi feito: 40 pessoas recebem um convite
    # e levam "esta oferta acabou" na cara. Avisar de 10 em 10 e repetir é o que
    # transforma um teto num ritmo.
    #
    # 1 = mandar pra UMA pessoa. Serve pra testar em si mesmo antes de soltar.
    # Vazio/0 = sem limite (todo mundo que se encaixar no público).
    try:
        quantos = int(corpo.get('quantos') or 0)
    except (TypeError, ValueError):
        quantos = 0
    quantos = max(0, quantos)

    from ..services.notification_service import send_campaign

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    invalidos = set()
    res = {}
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT b.id, b.title, b.restaurant_id,
                       (b.is_active IS TRUE
                        AND (b.starts_at IS NULL OR b.starts_at <= NOW())
                        AND (b.ends_at   IS NULL OR b.ends_at   >= NOW())) AS no_ar,
                       b.ends_at,
                       c.id AS coupon_id, c.code, c.discount_type, c.discount_value,
                       c.max_uses, c.uses_count,
                       r.restaurant_name, r.latitude AS loja_lat, r.longitude AS loja_lng,
                       COALESCE(r.own_delivery_radius_km, 10) AS raio_km
                  FROM banners b
                  LEFT JOIN coupons c             ON c.id = b.coupon_id
                  LEFT JOIN restaurant_profiles r ON r.id = b.restaurant_id
                 WHERE b.id = %s
            """, (str(banner_id),))
            b = cur.fetchone()

            if not b:
                return jsonify({"error": "Banner não encontrado"}), 404
            if not b['coupon_id']:
                return jsonify({"error": "Este banner não tem cupom — nada a anunciar"}), 409
            if not b['no_ar']:
                return jsonify({"error": "Banner fora do ar. Ative a campanha antes de disparar."}), 409
            if publico == 'no_raio' and (b['loja_lat'] is None or b['loja_lng'] is None):
                return jsonify({"error": "A loja não tem coordenada — não dá pra calcular raio"}), 409

            campanha = "relampago:%s:%s" % (banner_id, rodada)
            destinos = _publico_do_relampago(cur, b, publico, so_app_fechado, campanha, uid)

            # Corta DEPOIS de montar a lista: quem sobrar continua elegível e
            # entra no próximo disparo, porque o `push_campaign_log` só registra
            # quem recebeu de verdade. Assim "mandar de 10 em 10" funciona
            # apertando o mesmo botão de novo.
            sobraram = 0
            if quantos and len(destinos) > quantos:
                sobraram = len(destinos) - quantos
                destinos = destinos[:quantos]

            # Teto diário: só na primeira rodada (ver o aviso no topo).
            # No teste em si mesmo o teto do dia não vale — ele existe pra
            # proteger o CLIENTE de bombardeio, e aqui o cliente é você.
            if rodada == 'inicio' and destinos and publico != 'so_eu':
                try:
                    cur.execute("SELECT value FROM platform_settings "
                                "WHERE key = 'push_campaign_daily_cap'")
                    r = cur.fetchone()
                    teto = int(str(r['value']).strip()) if r else 1
                except Exception:
                    teto = 1
                if teto <= 0:
                    return jsonify({"error": "Campanhas por push estão desligadas "
                                             "(push_campaign_daily_cap = 0)"}), 409
                ids = [str(c) for c, _ in destinos]
                cur.execute("""
                    SELECT client_id FROM push_campaign_log
                     WHERE client_id = ANY(%s::uuid[])
                       AND (sent_at AT TIME ZONE 'America/Sao_Paulo')::date
                           = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
                     GROUP BY client_id HAVING COUNT(*) >= %s
                """, (ids, teto))
                estourados = set(str(r['client_id']) for r in cur.fetchall())
                destinos = [(c, t) for c, t in destinos if str(c) not in estourados]

            if not destinos:
                return jsonify({"status": "success", "data": {
                    "enviados": 0, "elegiveis": 0,
                    "aviso": "Ninguém elegível: ou já receberam esta rodada, "
                             "ou estão com o app aberto, ou não têm notificação ligada."
                }}), 200

            loja = b['restaurant_name'] or 'uma loja perto de você'
            restam = None
            if b['max_uses'] is not None:
                restam = max(0, int(b['max_uses']) - int(b['uses_count'] or 0))

            if rodada == 'ultima_chamada':
                titulo = "Última chance na %s" % loja
                cauda = ("Restam %d." % restam) if restam else "Está acabando."
                corpo_push = "%s — %s Toque e garanta." % (
                    b['title'] or 'Oferta relâmpago', cauda)
            else:
                titulo = "Oferta relâmpago na %s" % loja
                corpo_push = "%s. Toque pra ativar a sua." % (
                    b['title'] or 'Desconto por tempo limitado')

            res = send_campaign(destinos, titulo, corpo_push, {
                'type': 'relampago', 'banner_id': str(banner_id),
                'coupon_code': b['code'], 'url': '/',
            })

            invalidos = set(res.get('invalidos') or [])
            enviados = [cid for cid, _ in destinos if cid not in invalidos]
            if publico == 'so_eu':
                # Teste não entra no histórico: senão a segunda tentativa
                # voltaria "ninguém elegível" e pareceria que o envio parou
                # de funcionar.
                enviados = []
            if enviados:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO push_campaign_log (client_id, campanha, tipo) VALUES %s "
                    "ON CONFLICT (client_id, campanha) DO NOTHING",
                    [(cid, campanha, 'relampago') for cid in enviados])
            if invalidos:
                # Token recusado pelo FCM = app desinstalado. Limpa, senão a
                # base de tokens só engorda com lixo e o contador de "clientes
                # com push" mente.
                cur.execute("UPDATE client_profiles SET fcm_token = NULL "
                            "WHERE id = ANY(%s::uuid[])", ([str(c) for c in invalidos],))
            conn.commit()
            total_elegiveis = len(destinos)

        return jsonify({"status": "success", "data": {
            "enviados": res.get('enviados', 0),
            "elegiveis": total_elegiveis,
            "falhas": res.get('falhas', 0),
            "tokens_limpos": len(invalidos),
            "publico": publico, "rodada": rodada,
            "sobraram": sobraram,
        }}), 200

    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("Erro ao disparar oferta relâmpago do banner %s", banner_id)
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass


@coupons_bp.route('/relampago/<uuid:banner_id>/criar', methods=['POST', 'OPTIONS'])
def criar_relampago(banner_id):
    """Cria o cupom da oferta relâmpago e amarra no banner. Só admin.

    Body: {
      "restaurant_id":  uuid da loja,
      "discount_type":  "fixed" | "percentage" | "free_delivery",
      "discount_value": número,
      "reserva_minutos": 5,
      "max_uses":       quantas ofertas existem no total (opcional = ilimitado),
      "min_order_value": opcional,
      "code":           opcional — sem ele, geramos um
    }

    UM ENDPOINT SÓ, DE PROPÓSITO. Cupom e banner nascem juntos ou não nascem:
    banner relâmpago sem cupom é uma arte que promete desconto e não entrega, e
    cupom relâmpago sem banner é um cupom que ninguém consegue ativar (a reserva
    só acontece pelo toque no banner). Separar em dois passos deixaria os dois
    estados meio-feitos possíveis.

    DECISÕES QUE O ENDPOINT TOMA SOZINHO, e por quê:
    - paid_by = 'platform'  — a Inksa absorve. A oferta existe pra TRAZER
      usuário novo; cobrar isso do parceiro seria fazê-lo pagar pela captação
      da plataforma.
    - max_uses_per_client = 1 — é o "1 por cliente" do pedido.
    - valid_until = banner.ends_at — o cupom morre junto com a campanha. Sem
      isso sobraria um cupom vivo depois do banner sair do ar, e alguém que
      guardou o código continuaria usando.
    - somente_digitado = TRUE — a oferta NÃO aparece na lista de cupons da loja.
      Ela se ativa pelo banner, e só. Se aparecesse ali, qualquer cliente
      pegaria sem passar pela campanha e o relógio não significaria nada.
    """
    if request.method == 'OPTIONS':
        return jsonify({}), 204

    uid, utype, err = get_user_id_from_token(request.headers.get('Authorization'))
    if err:
        return err
    if utype != 'admin':
        return jsonify({"error": "Apenas admin cria oferta relâmpago"}), 403

    d = request.get_json(silent=True) or {}
    restaurant_id = (d.get('restaurant_id') or '').strip() or None
    if not restaurant_id:
        return jsonify({"error": "Escolha a loja da oferta"}), 400

    tipo = (d.get('discount_type') or 'fixed').strip().lower()
    if tipo not in ('fixed', 'percentage', 'free_delivery'):
        return jsonify({"error": "Tipo de desconto inválido"}), 400
    try:
        valor = float(d.get('discount_value') or 0)
    except (TypeError, ValueError):
        valor = 0.0
    if tipo != 'free_delivery' and valor <= 0:
        return jsonify({"error": "O desconto precisa ser maior que zero"}), 400
    if tipo == 'percentage' and valor > 100:
        return jsonify({"error": "Desconto em % não pode passar de 100"}), 400

    try:
        minutos = int(d.get('reserva_minutos') or 5)
    except (TypeError, ValueError):
        minutos = 5
    # Piso de 1 min: abaixo disso o cliente não consegue nem montar o carrinho,
    # e a oferta viraria pegadinha. Teto de 60 pra não virar cupom comum.
    minutos = max(1, min(minutos, 60))

    try:
        max_uses = int(d['max_uses']) if d.get('max_uses') not in (None, '') else None
    except (TypeError, ValueError):
        max_uses = None
    try:
        minimo = float(d.get('min_order_value') or 0)
    except (TypeError, ValueError):
        minimo = 0.0

    codigo = (d.get('code') or '').strip().upper()
    item_id = (d.get('menu_item_id') or '').strip() or None

    conn = get_db_connection()
    if not conn:
        return jsonify({"error": "DB indisponível"}), 503
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id, coupon_id, ends_at FROM banners WHERE id = %s",
                        (str(banner_id),))
            b = cur.fetchone()
            if not b:
                return jsonify({"error": "Banner não encontrado"}), 404
            if b['coupon_id']:
                return jsonify({"error": "Este banner já tem uma oferta. "
                                         "Apague a antiga antes de criar outra."}), 409

            cur.execute("SELECT restaurant_name FROM restaurant_profiles WHERE id = %s",
                        (restaurant_id,))
            loja = cur.fetchone()
            if not loja:
                return jsonify({"error": "Loja não encontrada"}), 404

            if not codigo:
                # Código legível, derivado do nome da loja. O cliente não precisa
                # digitar (o banner arma sozinho), mas ele APARECE no carrinho —
                # e "RELAMPAGO-MISTER" explica de onde veio o desconto melhor
                # que um punhado de letras aleatórias.
                import re as _re
                base = _re.sub(r'[^A-Z0-9]', '', (loja['restaurant_name'] or 'LOJA').upper())[:10]
                codigo = f"RELAMPAGO{base}"

            # Item: com ele, `discount_value` deixa de ser desconto e passa a
            # ser o PREÇO ALVO do item ("X-Bacon a R$ 9,99"). Sem ele, o cupom
            # desconta o pedido inteiro — e é assim que a Coca saía de graça.
            if item_id:
                cur.execute("SELECT id, name, price FROM menu_items "
                            "WHERE id = %s AND restaurant_id = %s",
                            (item_id, restaurant_id))
                it = cur.fetchone()
                if not it:
                    return jsonify({"error": "Item não encontrado nessa loja"}), 404
                if valor >= float(it['price'] or 0):
                    return jsonify({"error": f"O preço da oferta (R$ {valor:.2f}) tem que ser "
                                             f"MENOR que o do item (R$ {float(it['price']):.2f})"}), 400
                tipo = 'item_price'
                descricao = f"Relâmpago — {it['name']} por R$ {valor:.2f}"
            else:
                descricao = f"Oferta relâmpago — {loja['restaurant_name']}"

            cur.execute("""
                INSERT INTO coupons
                    (code, discount_type, discount_value, min_order_value,
                     max_uses, uses_count, max_uses_per_client, valid_until,
                     is_active, restaurant_id, paid_by, description,
                     somente_digitado, reserva_minutos, menu_item_id)
                VALUES (%s, %s, %s, %s, %s, 0, 1, %s, TRUE, %s, 'platform', %s, TRUE, %s, %s)
                RETURNING id, code
            """, (codigo, tipo, valor, minimo, max_uses, b['ends_at'],
                  restaurant_id, descricao, minutos, item_id))
            cupom = cur.fetchone()

            cur.execute("UPDATE banners SET coupon_id = %s, restaurant_id = %s, "
                        "menu_item_id = %s, updated_at = NOW() WHERE id = %s",
                        (cupom['id'], restaurant_id, item_id, str(banner_id)))
            conn.commit()

        return jsonify({"status": "success", "data": {
            "coupon_id": str(cupom['id']),
            "code": cupom['code'],
            "reserva_minutos": minutos,
            "loja": loja['restaurant_name'],
        }}), 201

    except psycopg2.errors.UniqueViolation:
        try:
            conn.rollback()
        except Exception:
            pass
        return jsonify({"error": f"Já existe um cupom com o código {codigo}. "
                                 "Escolha outro."}), 409
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.exception("Erro ao criar oferta relâmpago no banner %s", banner_id)
        return jsonify({"error": "Erro interno"}), 500
    finally:
        try:
            conn.close()
        except Exception:
            pass
