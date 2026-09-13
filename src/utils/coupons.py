# src/utils/coupons.py
"""
Lógica ÚNICA de validação/cálculo de cupom, compartilhada entre:
  - /api/coupons/validate  (preview do cliente no carrinho)
  - payment.py             (aplicação real no fechamento do pedido)

Antes cada lugar tinha a sua cópia e elas divergiam: o payment.py lia a coluna
errada (min_order_amount, que não existe -> mínimo nunca valia), não checava
validade nem limite de usos, e não tratava frete grátis. Centralizar garante
que o desconto mostrado ao cliente seja EXATAMENTE o desconto cobrado.

Regras (tudo configurável pelo admin, tabela `coupons`):
  - is_active = true
  - valid_until >= agora (se preenchido)
  - uses_count < max_uses (se preenchido)          <- teto GLOBAL
  - usos do cliente < max_uses_per_client (idem)   <- teto POR PESSOA
  - subtotal >= min_order_value (se preenchido)
  - desconto por tipo:
      percentage    -> subtotal * value/100        (limitado ao subtotal)
      fixed         -> min(value, subtotal)
      free_delivery -> delivery_fee (isenta o frete)
"""
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def _parse_dt(v):
    """Aceita datetime (psycopg2) ou string ISO (supabase). Retorna aware UTC."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def contar_usos_do_cliente(coupon_id, client_id, cur=None):
    """Quantas vezes ESTE cliente já usou ESTE cupom.

    Passe `cur` quando já houver um cursor aberto (evita segunda conexão);
    sem ele, abre a sua própria.

    Devolve 0 quando não dá pra saber — sem cliente identificado, ou se a
    consulta falhar. Fail-open de propósito: quem realmente barra é o
    fechamento do pedido, onde o cliente SEMPRE está autenticado. No preview do
    carrinho, na pior das hipóteses a pessoa vê "cupom válido" e leva a recusa
    no fechamento; o contrário — recusar cupom legítimo porque o banco piscou —
    é pior, porque ela desiste do pedido.
    """
    if not coupon_id or not client_id:
        return 0

    sql = ("SELECT COUNT(*) FROM public.coupon_redemptions "
           "WHERE coupon_id = %s AND client_id = %s")
    args = (str(coupon_id), str(client_id))

    if cur is not None:
        try:
            cur.execute(sql, args)
            row = cur.fetchone()
            return int((row[0] if row else 0) or 0)
        except Exception as exc:
            logger.warning("Falha ao contar usos do cupom %s pelo cliente %s: %s",
                           coupon_id, client_id, exc)
            return 0

    from .helpers import get_db_connection
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return 0
        with conn.cursor() as c:
            c.execute(sql, args)
            row = c.fetchone()
            return int((row[0] if row else 0) or 0)
    except Exception as exc:
        logger.warning("Falha ao contar usos do cupom %s pelo cliente %s: %s",
                       coupon_id, client_id, exc)
        return 0
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def reserva_do_cliente(coupon_id, client_id, cur=None):
    """Quando expira a reserva deste cliente neste cupom relâmpago?

    Devolve o `expires_at` (datetime) se existir reserva, ou None se não existir
    — e None TAMBÉM quando a consulta falha.

    ⚠️ Aqui o None é FAIL-CLOSED de propósito, ao contrário do
    `contar_usos_do_cliente` logo acima. O raciocínio é outro: um cupom
    relâmpago SEM reserva não é um cupom legítimo sendo recusado por azar — ele
    simplesmente não foi ativado. E o cliente tocou no banner segundos atrás, do
    lado do app: repetir o toque é barato. Errar pro outro lado seria distribuir
    desconto de campanha pra quem nunca entrou nela.
    """
    if not coupon_id or not client_id:
        return None

    sql = ("SELECT expires_at FROM public.coupon_reservations "
           "WHERE coupon_id = %s AND client_id = %s")
    args = (str(coupon_id), str(client_id))

    def _le(c):
        c.execute(sql, args)
        row = c.fetchone()
        if not row:
            return None
        return row[0] if not isinstance(row, dict) else row.get('expires_at')

    if cur is not None:
        try:
            return _le(cur)
        except Exception as exc:
            logger.warning("Falha ao ler reserva do cupom %s p/ cliente %s: %s",
                           coupon_id, client_id, exc)
            return None

    from .helpers import get_db_connection
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return None
        with conn.cursor() as c:
            return _le(c)
    except Exception as exc:
        logger.warning("Falha ao ler reserva do cupom %s p/ cliente %s: %s",
                       coupon_id, client_id, exc)
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def precos_para_o_cupom(coupon, itens_do_pedido):
    """Preço do item que ESTE cupom cobre, se ele estiver no carrinho.

    Devolve {menu_item_id: preco_unitario} ou {} — o formato que
    `evaluate_coupon(itens_do_carrinho=...)` espera.

    POR QUE ESTA FUNÇÃO EXISTE, EM VEZ DE USAR OS PREÇOS JÁ CALCULADOS

    No fluxo online o cupom é avaliado ANTES do laço que precifica os itens
    (payment.py: cupom por volta da linha 874, itens a partir da 1020).
    Reordenar aquilo pra ter os preços na mão mexeria no caminho que grava
    pedido de verdade, pra ganhar o que uma consulta resolve.

    Então: só busca quando o cupom É de item (`menu_item_id`), e busca SÓ
    aquele item. Cupom comum não paga nada por isto.

    O preço vem do BANCO, nunca do que o app mandou — o app manda o preço que
    ele acha, e cupom que confia nisso é cupom que o cliente escolhe o valor.
    Usa `preco_vigente`, o mesmo resolvedor do fechamento, pra respeitar
    promoção da loja.
    """
    alvo = (coupon or {}).get('menu_item_id')
    if not alvo:
        return {}

    # O item está mesmo no carrinho? Sem isso, uma consulta por nada.
    recebidos = [str((i or {}).get('menu_item_id') or '') for i in (itens_do_pedido or [])]
    tem_no_carrinho = any(r == str(alvo) for r in recebidos)
    if not tem_no_carrinho:
        # LOGA O QUE CHEGOU, não só "não achei".
        #
        # Em 13/09/2026 o Diego estava com o item DA OFERTA no carrinho e mesmo
        # assim levava "Adicione o item da oferta ao carrinho". Daqui não dava
        # pra saber se o app mandou id errado, id nenhum, ou outro formato —
        # e sem isso a investigação vira adivinhação sobre o aparelho dele.
        logger.info("oferta de item: alvo=%s nao esta no carrinho; recebidos=%r",
                    alvo, recebidos)
        return {}

    try:
        from .helpers import supabase as _sb
        from .precos import preco_vigente
        r = (_sb.table('menu_items')
               .select('price, promo_price')
               .eq('id', str(alvo)).limit(1).execute())
        if not r.data:
            # Mesmo motivo do log acima: daqui pra frente as duas causas de {}
            # levam à MESMA mensagem na tela ("adicione o item ao carrinho"),
            # e sem distinguir uma da outra a investigação não anda.
            logger.info("oferta de item: menu_items nao devolveu linha pro id %s", alvo)
            return {}
        preco = float(preco_vigente(r.data[0]))
        logger.info("oferta de item: alvo=%s no carrinho, preco do banco=%.2f", alvo, preco)
        return {str(alvo): preco}
    except Exception:
        logger.warning("Não deu pra ler o preço do item %s da oferta", alvo, exc_info=True)
        # Devolve vazio: o evaluate recusa a oferta. É o lado certo pra errar —
        # aceitar sem saber o preço é o que fazia a plataforma pagar por item
        # que nem estava no carrinho.
        return {}


def evaluate_coupon(coupon, subtotal, delivery_fee=0.0, now=None, restaurant_id=None,
                    usos_deste_cliente=0, client_id=None, reserva_expira_em=None,
                    itens_do_carrinho=None):
    """Valida um cupom já buscado (dict da linha) e calcula o desconto.

    `coupon` pode vir do psycopg2 (DictCursor) ou do supabase (select '*') —
    ambos têm as mesmas chaves. Retorna dict:
      { valid, discount_amount, discount_type, message,
        paid_by, restaurant_discount, platform_discount }
    discount_amount é 0.0 quando inválido.

    `restaurant_id` = loja do pedido. Cupom com restaurant_id preenchido é
    EXCLUSIVO daquela loja; cupom com restaurant_id NULL é da plataforma e vale
    em qualquer uma. Sem essa checagem, o cupom que o parceiro A criou daria
    desconto num pedido do parceiro B — e sairia do repasse do B.
    """
    now = now or datetime.now(timezone.utc)
    subtotal = _to_float(subtotal)
    delivery_fee = _to_float(delivery_fee)

    if not coupon:
        return {"valid": False, "discount_amount": 0.0, "discount_type": None,
                "message": "Cupom não encontrado"}

    disc_type = (coupon.get('discount_type') or '').lower()

    if not coupon.get('is_active'):
        return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                "message": "Este cupom não está ativo"}

    dono = coupon.get('restaurant_id')
    if dono and restaurant_id and str(dono) != str(restaurant_id):
        return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                "message": "Este cupom vale só em outra loja"}

    # Cupom PESSOAL (prêmio de indicação): só o dono usa.
    #
    # O código chega por notificação, e notificação é printada e mandada no
    # grupo. Sem esta trava, quem visse o código usaria primeiro — o max_uses=1
    # garante que só UMA pessoa use, não que seja a pessoa certa.
    #
    # Sem client_id (chamada sem login), cupom pessoal é RECUSADO: na dúvida
    # sobre quem está pedindo, não se entrega dinheiro de outro.
    pessoal = coupon.get('owner_client_id')
    if pessoal and (not client_id or str(pessoal) != str(client_id)):
        return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                "message": "Este cupom é pessoal e foi emitido para outra conta"}

    vu = _parse_dt(coupon.get('valid_until'))
    if vu and vu < now:
        return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                "message": "Este cupom expirou"}

    max_uses = coupon.get('max_uses')
    uses_count = int(coupon.get('uses_count') or 0)
    if max_uses is not None and uses_count >= int(max_uses):
        return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                "message": "Este cupom atingiu o limite de usos"}

    # Teto POR PESSOA. Sem ele, o mesmo cliente podia usar o cupom todo dia e
    # esgotar sozinho o teto global — numa promoção de captação, é o cliente
    # antigo consumindo o orçamento feito pra trazer gente nova.
    por_cliente = coupon.get('max_uses_per_client')
    if por_cliente is not None:
        try:
            limite = int(por_cliente)
        except (TypeError, ValueError):
            limite = 0
        if limite > 0 and int(usos_deste_cliente or 0) >= limite:
            msg = ("Você já usou este cupom" if limite == 1
                   else f"Você já usou este cupom {limite} vezes")
            return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                    "message": msg}

    # OFERTA RELÂMPAGO: além da janela da campanha, o cliente tem o relógio DELE,
    # que começou quando ele tocou no banner. Só vale dentro dos dois.
    #
    # `reserva_minutos` preenchido é o que marca o cupom como relâmpago. Cupom
    # comum não tem, e nem passa por aqui — nada do que já existe muda.
    #
    # A mensagem separa os três casos de propósito. O Diego escolheu NÃO mostrar
    # contador na tela; então a recusa é o único momento em que o cliente
    # descobre o que houve, e "cupom inválido" seco aqui viraria reclamação.
    if coupon.get('reserva_minutos'):
        if not client_id:
            return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                    "message": "Entre na sua conta para usar esta oferta relâmpago"}
        if reserva_expira_em is None:
            return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                    "message": "Toque na oferta relâmpago no app para ativá-la"}
        expira = _parse_dt(reserva_expira_em)
        if expira and expira < now:
            return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                    "message": "Sua oferta relâmpago expirou"}

    min_val = _to_float(coupon.get('min_order_value'))
    if subtotal < min_val:
        return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                "message": f"Pedido mínimo para este cupom é R$ {min_val:.2f}"}

    # ─── OFERTA PRESA A UM ITEM ──────────────────────────────────────────────
    #
    # "Lanche a R$ 9,99" não é desconto no pedido, é PREÇO daquele lanche. Com
    # `menu_item_id` preenchido, `discount_value` é o preço ALVO do item, e o
    # desconto é a diferença — sobre UMA unidade.
    #
    # Por que uma unidade: sem isso, 10 X-Bacon sairiam a R$ 9,99 cada. A oferta
    # é pra trazer o cliente, não pra abastecer a rua.
    #
    # `itens_do_carrinho` = {menu_item_id: preco_unitario}. Sem ele não dá pra
    # saber se o item está no carrinho, e a oferta é RECUSADA — este é o único
    # caminho em que um dado ausente não pode virar "vale": era assim que a
    # Coca de R$ 6 saía de graça.
    item_alvo = coupon.get('menu_item_id')
    if item_alvo:
        if not itens_do_carrinho:
            return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                    "message": "Adicione o item da oferta ao carrinho para usá-la"}
        preco_no_carrinho = None
        for k, v in (itens_do_carrinho or {}).items():
            if str(k) == str(item_alvo):
                preco_no_carrinho = _to_float(v)
                break
        if preco_no_carrinho is None:
            return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                    "message": "Esta oferta é de um item específico — adicione ele ao carrinho"}

        preco_alvo = _to_float(coupon.get('discount_value'))
        discount = round(max(0.0, preco_no_carrinho - preco_alvo), 2)
        if discount <= 0:
            # O item já custa igual ou menos que o preço da oferta (promoção da
            # loja, por exemplo). Não há o que descontar, e fingir que há faria
            # a plataforma pagar por nada.
            return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                    "message": "Este item já está por um preço igual ou melhor"}

        paid_by = (coupon.get('paid_by') or 'platform').lower()
        r_disc, p_disc = (discount, 0.0) if paid_by == 'restaurant' else (0.0, discount)
        return {"valid": True, "discount_amount": discount,
                "discount_type": "item_price", "message": "Oferta aplicada!",
                "paid_by": paid_by, "exclusivo": bool(coupon.get('reserva_minutos')),
                "menu_item_id": str(item_alvo),
                "restaurant_discount": round(r_disc, 2),
                "platform_discount": round(p_disc, 2)}

    value = _to_float(coupon.get('discount_value'))
    if disc_type == 'percentage':
        discount = min(round(subtotal * value / 100.0, 2), subtotal)
    elif disc_type == 'fixed':
        discount = round(min(value, subtotal), 2)
    elif disc_type == 'free_delivery':
        discount = round(delivery_fee, 2)
    else:
        return {"valid": False, "discount_amount": 0.0, "discount_type": disc_type,
                "message": "Tipo de cupom inválido"}

    discount = max(0.0, discount)

    # QUEM PAGA. 'restaurant' = sai do repasse do parceiro (cupom criado por
    # ele); 'platform' = sai da comissão da Inksa (campanha da plataforma).
    # Sem essa separação o desconto saía sempre da comissão — e um cupom maior
    # que a comissão fazia a plataforma fechar NEGATIVO naquele pedido.
    paid_by = (coupon.get('paid_by') or 'platform').lower()
    if paid_by == 'restaurant':
        restaurant_discount, platform_discount = discount, 0.0
    else:
        restaurant_discount, platform_discount = 0.0, discount

    # NÃO EMPILHA COM NADA. Hoje só a oferta relâmpago é exclusiva.
    #
    # Sem isto, o cliente somava a oferta relâmpago com o desconto do Clube no
    # mesmo pedido — `total_discount = backend_discount + club_discount` em
    # payment.py — e as DUAS pontas saem da plataforma. Um lanche de R$ 9,99
    # numa campanha de captação vira R$ 8,99 com 10% de Diamante, e a Inksa
    # paga os dois.
    #
    # Quem respeita esta bandeira é quem monta o total (payment.py, nos dois
    # caminhos de pedido). Aqui ela só é hasteada.
    exclusivo = bool(coupon.get('reserva_minutos'))

    return {"valid": True, "discount_amount": discount,
            "discount_type": disc_type, "message": "Cupom válido!",
            "paid_by": paid_by, "exclusivo": exclusivo,
            "restaurant_discount": round(restaurant_discount, 2),
            "platform_discount": round(platform_discount, 2)}


def consume_coupon(coupon_id, client_id=None, order_id=None):
    """Registra o uso: incrementa uses_count E grava quem usou.

    O registro por cliente é o que faz `max_uses_per_client` valer — sem ele o
    limite por pessoa seria inverificável, porque o pedido não guarda o cupom.
    Passar client_id é o que liga a trava; sem ele só o contador global sobe.

    Best-effort: falha não deve derrubar o pedido. Usa conexão psycopg2 própria.
    """
    from .helpers import get_db_connection
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.coupons SET uses_count = COALESCE(uses_count, 0) + 1 WHERE id = %s",
                (str(coupon_id),),
            )
            if client_id:
                # ON CONFLICT: se o fluxo de pagamento reprocessar o mesmo
                # pedido (retry de webhook, duplo clique), não queima o direito
                # da pessoa duas vezes.
                cur.execute(
                    "INSERT INTO public.coupon_redemptions (coupon_id, client_id, order_id) "
                    "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (str(coupon_id), str(client_id), str(order_id) if order_id else None),
                )
        conn.commit()
    except Exception as exc:
        logger.warning("Falha ao incrementar uses_count do cupom %s: %s", coupon_id, exc)
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
