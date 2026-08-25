# src/routes/coupons_routes.py
# Blueprint: coupons_bp, prefix /api/coupons

import logging
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
import psycopg2
import psycopg2.extras
from ..utils.helpers import get_db_connection, get_user_id_from_token
from ..utils.coupons import evaluate_coupon, contar_usos_do_cliente

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
            if coupon is not None:
                try:
                    uid, utype, err = get_user_id_from_token(request.headers.get('Authorization'))
                    if err is None and utype == 'client':
                        cur.execute("SELECT id FROM client_profiles WHERE user_id = %s", (uid,))
                        perfil = cur.fetchone()
                        if perfil:
                            cliente_id = perfil['id']
                            usos = contar_usos_do_cliente(coupon['id'], perfil['id'], cur)
                except Exception:
                    usos = 0

        # Validação/cálculo centralizado (mesma lógica do fechamento do pedido)
        result = evaluate_coupon(dict(coupon) if coupon else None, order_total, delivery_fee,
                                 restaurant_id=restaurant_id, usos_deste_cliente=usos,
                                 client_id=cliente_id)
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
                r = evaluate_coupon(c, subtotal, delivery_fee,
                                    restaurant_id=restaurant_id,
                                    usos_deste_cliente=usos, client_id=client_id)
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
                       c.restaurant_id, c.paid_by, c.description,
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
                         max_uses_per_client, valid_until)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, code, discount_type, discount_value, min_order_value,
                              max_uses, uses_count, max_uses_per_client, valid_until,
                              is_active, created_at
                """, (code, discount_type, discount_value, min_order_value, max_uses,
                      _limite_por_cliente(data), valid_until))
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
                  is_active, created_at, restaurant_id, paid_by, description"""


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
                             max_uses_per_client, valid_until, description, restaurant_id, paid_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'restaurant')
                        RETURNING {_COUPON_COLS}""",
                    (code, dtype, dvalue, float(data.get('min_order_value') or 0),
                     data.get('max_uses') or None, _limite_por_cliente(data),
                     data.get('valid_until') or None,
                     (data.get('description') or None), rid))
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
