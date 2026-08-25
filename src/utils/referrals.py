# -*- coding: utf-8 -*-
# src/utils/referrals.py
"""
Indique e ganhe.

    O indicado  -> frete grátis no 1º pedido
    Quem indica -> R$ 5 quando esse pedido é ENTREGUE
    Mínimo de compra pra usar o cupom: R$ 50 de SUBTOTAL (sem frete)

Uma regra só, sem bônus por marco: quanto mais simples de explicar no grupo do
WhatsApp, mais gente indica. E como o app aplica UM cupom por pedido, dez
indicações não viram R$ 50 num pedido — viram dez pedidos de R$ 50 pra cima.

TRÊS DECISÕES QUE SUSTENTAM ISTO:

1. O prêmio nasce na ENTREGA, não no cadastro nem no pedido feito. Prêmio por
   cadastro é ímã de conta falsa; prêmio por pedido feito paga antes de saber
   se o pedido existiu de verdade. Exigir entrega significa endereço real,
   pagamento real e comida recebida — que já é quase toda a defesa antifraude.

2. A defesa contra auto-indicação é ARITMÉTICA, não policial: pra ganhar R$ 5 o
   sujeito precisa comprar R$ 50 de comida. Não fecha a conta dele. Regra que
   depende de detectar CPF/aparelho repetido sempre tem uma brecha; regra que
   depende de o crime dar prejuízo, não.

3. O mínimo conta o SUBTOTAL, sem frete. Contando o total, um pedido de R$ 42
   de comida com R$ 9 de frete "qualificaria", e o desconto cairia justamente
   onde a comida vale pouco — que é onde a comissão não paga o cupom.
"""
import logging
import random
import string

from .helpers import get_db_connection

logger = logging.getLogger(__name__)

def _cfg():
    """Números da campanha, lidos das configurações da plataforma.

    Eram constantes aqui. Mudaram três vezes numa tarde só, e cada mudança
    exigia deploy — o que é o oposto do que uma campanha precisa. Agora saem do
    admin, com cache de 60s como o resto das configurações.

    Fail-safe: qualquer problema cai nos defaults do platform_settings, nunca
    em zero (prêmio zerado silenciosamente é pior que prêmio errado — ninguém
    reclama de não receber o que não sabia que ia receber).
    """
    from .platform_settings import get_settings
    s = get_settings()
    return {
        "ligado": float(s["referral_enabled"]) > 0,
        "valor": float(s["referral_reward_brl"]),
        "minimo": float(s["referral_min_order_brl"]),
        "validade": int(float(s["referral_validity_days"])),
        "teto": int(float(s["referral_monthly_cap"])),
    }

# Sem I, O, 0 e 1: este código é DITADO em grupo de WhatsApp e lido em print.
# "INK-I0O1" gera erro de digitação que o usuário culpa o app, não a fonte.
_ALFABETO = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _sortear(n=6):
    return "".join(random.choice(_ALFABETO) for _ in range(n))


def obter_ou_criar_codigo(cur, client_id):
    """Código de indicação do cliente. Cria na primeira vez que ele abre a tela."""
    cur.execute("SELECT referral_code FROM public.client_profiles WHERE id = %s",
                (str(client_id),))
    row = cur.fetchone()
    if row and row[0]:
        return row[0]

    # A unicidade é do BANCO (coluna UNIQUE); aqui só tentamos de novo se colidir.
    for _ in range(10):
        codigo = "INK" + _sortear()
        try:
            cur.execute("""UPDATE public.client_profiles SET referral_code = %s
                            WHERE id = %s AND referral_code IS NULL
                        RETURNING referral_code""", (codigo, str(client_id)))
            r = cur.fetchone()
            if r:
                return r[0]
            # Já tinha código (corrida com outra aba): devolve o que ficou.
            cur.execute("SELECT referral_code FROM public.client_profiles WHERE id = %s",
                        (str(client_id),))
            r = cur.fetchone()
            if r and r[0]:
                return r[0]
        except Exception:
            cur.connection.rollback()
            continue
    raise RuntimeError("não consegui gerar código de indicação")


def _criar_cupom(cur, dono_id, valor, descricao, validade_dias=None,
                 minimo=None, tipo="fixed"):
    """Cupom PESSOAL da plataforma: código único, um uso só, e com DONO.

    owner_client_id é o que impede o prêmio de vazar. O código chega por push,
    e push é printado e mandado no grupo — sem dono, o primeiro que digitasse
    levava. max_uses=1 garante que só UMA pessoa use, não que seja a certa.

    paid_by='platform' e restaurant_id NULL de propósito: indicação é aquisição
    da Inksa. Se saísse do repasse do parceiro, ele estaria pagando a conta do
    marketing de outro — e descobriria no fechamento do mês.
    """
    c = _cfg()
    if validade_dias is None:
        validade_dias = c["validade"]
    if minimo is None:
        minimo = c["minimo"]
    for _ in range(10):
        code = ("IND" if tipo == "fixed" else "BV") + _sortear(7)
        try:
            cur.execute("""
                INSERT INTO public.coupons
                    (code, discount_type, discount_value, min_order_value,
                     max_uses, max_uses_per_client, valid_until, description,
                     restaurant_id, paid_by, is_active, owner_client_id)
                VALUES (%s, %s, %s, %s, 1, 1, now() + (%s || ' days')::interval,
                        %s, NULL, 'platform', TRUE, %s)
                RETURNING id, code
            """, (code, tipo, valor, minimo, validade_dias, descricao, str(dono_id)))
            return cur.fetchone()
        except Exception:
            cur.connection.rollback()
            continue
    raise RuntimeError("não consegui gerar cupom de indicação")


def aplicar_codigo(cur, client_id, codigo):
    """Vincula o cliente a quem o indicou e devolve o cupom de boas-vindas.

    Só vale pra quem AINDA NÃO comprou: indicação é aquisição, não desconto
    retroativo pra quem já é cliente.
    """
    if not _cfg()["ligado"]:
        return {"ok": False, "erro": "O programa de indicação está pausado no momento."}

    codigo = (codigo or "").strip().upper()
    if not codigo:
        return {"ok": False, "erro": "Informe o código."}

    cur.execute("SELECT id FROM public.client_profiles WHERE upper(referral_code) = %s",
                (codigo,))
    row = cur.fetchone()
    if not row:
        return {"ok": False, "erro": "Código não encontrado."}
    referrer_id = str(row[0])

    if referrer_id == str(client_id):
        return {"ok": False, "erro": "Você não pode usar o seu próprio código."}

    cur.execute("SELECT 1 FROM public.referrals WHERE referred_id = %s", (str(client_id),))
    if cur.fetchone():
        return {"ok": False, "erro": "Você já usou um código de indicação."}

    cur.execute("""SELECT COUNT(*) FROM public.orders
                    WHERE client_id = %s AND status = 'delivered'""", (str(client_id),))
    if cur.fetchone()[0] > 0:
        return {"ok": False, "erro": "O código de indicação vale só antes do primeiro pedido."}

    # Frete grátis: alavanca de maior percepção e menor custo em delivery, e
    # ataca a objeção de quem nunca pediu ("vou pagar frete só pra testar?").
    # Sem mínimo — exigir R$ 50 logo no pedido de estreia derrubaria a conversão
    # justamente onde ela é mais frágil.
    cupom_id, cupom_code = _criar_cupom(
        cur, client_id, 0, "Frete grátis de boas-vindas (indicação)",
        minimo=0, tipo="free_delivery")

    cur.execute("""INSERT INTO public.referrals (referrer_id, referred_id, code_used)
                   VALUES (%s, %s, %s)""", (referrer_id, str(client_id), codigo))

    return {"ok": True, "cupom": cupom_code,
            "mensagem": "Frete grátis no seu primeiro pedido!"}


def qualificar_por_entrega(client_id, order_id):
    """Paga quem indicou este cliente, se este for o 1º pedido ENTREGUE dele.

    Chamada do /complete de orders.py, junto com os pontos. Conexão própria e
    falha silenciosa: prêmio de indicação não pode derrubar a entrega de um
    pedido. Devolve o que foi concedido (ou None) pra quem quiser avisar.
    """
    c = _cfg()
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn, conn.cursor() as cur:
            cur.execute("""SELECT COUNT(*) FROM public.orders
                            WHERE client_id = %s AND status = 'delivered'""", (str(client_id),))
            if cur.fetchone()[0] != 1:
                return None   # não é o primeiro: nada a pagar

            # FOR UPDATE + qualified_at IS NULL: dois /complete simultâneos no
            # mesmo pedido não pagam duas vezes.
            cur.execute("""SELECT id, referrer_id FROM public.referrals
                            WHERE referred_id = %s AND qualified_at IS NULL
                            FOR UPDATE""", (str(client_id),))
            row = cur.fetchone()
            if not row:
                return None
            referral_id, referrer_id = row[0], str(row[1])

            cur.execute("""SELECT COUNT(*) FROM public.referrals
                            WHERE referrer_id = %s AND qualified_at IS NOT NULL
                              AND DATE_TRUNC('month', qualified_at) = DATE_TRUNC('month', NOW())""",
                        (referrer_id,))
            no_mes = cur.fetchone()[0]
            # Programa pausado ou teto batido: a indicação é registrada assim
            # mesmo. Ela ACONTECEU — e marcar isso é o que impede o mesmo
            # indicado voltar a pagar mês que vem ou quando religar.
            if not c["ligado"] or no_mes >= c["teto"]:
                cur.execute("""UPDATE public.referrals
                                  SET qualified_at = now(), qualifying_order_id = %s
                                WHERE id = %s""", (str(order_id), referral_id))
                return {"teto_atingido": True}

            cupom_id, cupom_code = _criar_cupom(
                cur, referrer_id, c["valor"],
                f"Indicação premiada — R$ {c['valor']:.2f}".replace(".", ","))

            cur.execute("""SELECT COUNT(*) FROM public.referrals
                            WHERE referrer_id = %s AND qualified_at IS NOT NULL""",
                        (referrer_id,))
            total = cur.fetchone()[0] + 1

            cur.execute("""UPDATE public.referrals
                              SET qualified_at = now(), qualifying_order_id = %s,
                                  reward_coupon_id = %s
                            WHERE id = %s""",
                        (str(order_id), cupom_id, referral_id))

            return {"referrer_id": referrer_id, "cupom": cupom_code,
                    "valor": c["valor"], "total_indicacoes": total}
    except Exception:
        logger.exception("referrals.qualificar_por_entrega falhou")
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def cupons_do_cliente(cur, client_id):
    """Cupons pessoais deste cliente, do mais novo pro mais velho.

    ISTO EXISTE PORQUE O CÓDIGO NÃO PODE MORAR SÓ NO PUSH. A notificação é
    dispensada, o celular é trocado, e aí a pessoa ganhou um cupom que não tem
    onde procurar — e conclui, com razão, que não recebeu nada.

    Mostra os já usados e os vencidos também: sumir com eles faria parecer que
    o prêmio nunca existiu.
    """
    cur.execute("""
        SELECT code, discount_type, discount_value, min_order_value, valid_until,
               COALESCE(uses_count, 0) >= COALESCE(max_uses, 1) AS usado,
               valid_until IS NOT NULL AND valid_until < now() AS vencido
          FROM public.coupons
         WHERE owner_client_id = %s AND is_active
         ORDER BY created_at DESC
         LIMIT 40
    """, (str(client_id),))
    return [{
        "codigo": r[0],
        "tipo": r[1],
        "valor": float(r[2] or 0),
        "minimo": float(r[3] or 0),
        "vence_em": r[4].isoformat() if r[4] else None,
        "usado": bool(r[5]),
        "vencido": bool(r[6]),
    } for r in cur.fetchall()]


def resumo(cur, client_id):
    """Código, quantas indicações, quanto rendeu e os cupons — tela do cliente."""
    codigo = obter_ou_criar_codigo(cur, client_id)
    cur.execute("""
        SELECT COUNT(*) FILTER (WHERE qualified_at IS NOT NULL)  AS premiadas,
               COUNT(*) FILTER (WHERE qualified_at IS NULL)      AS pendentes,
               COUNT(*) FILTER (WHERE qualified_at IS NOT NULL
                                  AND DATE_TRUNC('month', qualified_at)
                                    = DATE_TRUNC('month', NOW()))AS no_mes
          FROM public.referrals WHERE referrer_id = %s
    """, (str(client_id),))
    premiadas, pendentes, no_mes = cur.fetchone()
    c = _cfg()
    return {
        "codigo": codigo,
        "ligado": c["ligado"],
        "premiadas": int(premiadas or 0),
        "pendentes": int(pendentes or 0),
        "no_mes": int(no_mes or 0),
        "teto_mensal": c["teto"],
        # Soma o que os CUPONS realmente valem, em vez de multiplicar pelo valor
        # de hoje: se o prêmio mudar de R$5 pra R$7, o histórico continua
        # contando o que cada indicação valeu na época.
        "ganho_total": round(sum(
            k["valor"] for k in cupons_do_cliente(cur, client_id)
            if k["tipo"] != "free_delivery"), 2),
        "valor_indicacao": c["valor"],
        "minimo_de_compra": c["minimo"],
        "cupons": cupons_do_cliente(cur, client_id),
    }
