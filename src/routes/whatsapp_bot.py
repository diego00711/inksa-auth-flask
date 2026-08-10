# src/routes/whatsapp_bot.py
"""
Bot do WhatsApp do Inksa (Cloud API da Meta).

Responde três coisas, que foi o que o Diego pediu:
  1. link do app pra quem quer pedir
  2. status do pedido, buscando pelo telefone de quem mandou a mensagem
  3. menu (virar parceiro / virar entregador / falar com atendente)

Desenho SEM estado de conversa de propósito: o bot lê a mensagem e decide.
Máquina de estado por usuário daria muito mais caminho pra dar errado (sessão
perdida, worker diferente no Render, pessoa sumindo no meio) do que valor —
com 3 opções, casar por palavra-chave resolve e nunca "trava" ninguém.
"""
import logging
import os

from flask import Blueprint, request, jsonify

from ..utils.helpers import get_db_connection
from ..utils import whatsapp as wa

logger = logging.getLogger(__name__)

whatsapp_bp = Blueprint("whatsapp", __name__)

APP_URL = "https://clientes.inksadelivery.com.br"

STATUS_LEGIVEL = {
    "pending": "aguardando a loja aceitar",
    "accepted": "aceito pela loja",
    "preparing": "em preparo",
    "ready": "pronto, aguardando entregador",
    "accepted_by_delivery": "entregador a caminho da loja",
    "delivering": "saiu para entrega",
    "delivered": "entregue",
    "cancelled": "cancelado",
    "delivery_failed": "não foi possível entregar",
    "awaiting_payment": "aguardando o pagamento",
}

MENU = (
    "Oi! Aqui é o *Inksa Delivery* 🛵\n\n"
    "Digite o número da opção:\n\n"
    "*1* — Fazer um pedido\n"
    "*2* — Ver meu pedido\n"
    "*3* — Quero cadastrar minha loja\n"
    "*4* — Quero ser entregador\n"
    "*5* — Falar com uma pessoa"
)


def _texto(msg):
    """Extrai o texto de qualquer tipo de mensagem que interessa."""
    if msg.get("type") == "text":
        return (msg.get("text") or {}).get("body") or ""
    # botão/lista: usa o título como se a pessoa tivesse digitado
    for tipo in ("button", "interactive"):
        bloco = msg.get(tipo) or {}
        if tipo == "interactive":
            bloco = bloco.get("button_reply") or bloco.get("list_reply") or {}
        t = bloco.get("text") or bloco.get("title")
        if t:
            return t
    return ""


def _resposta_para(corpo: str, telefone: str) -> str:
    """A decisão do bot. Função pura — dá pra testar sem Meta e sem banco."""
    t = (corpo or "").strip().lower()

    def tem(*palavras):
        return any(p in t for p in palavras)

    # STATUS vem antes de PEDIR de propósito: as duas intenções compartilham a
    # palavra "pedido" ("quero fazer um pedido" x "cadê meu pedido"), e a de
    # status é a mais específica. Invertendo a ordem, quem pergunta do pedido
    # dele receberia o link do app — que é o pior erro possível aqui.
    if t in ("2", "2.") or tem("meu pedido", "onde esta", "onde está", "onde ta",
                               "status", "rastrear", "cadê", "cade", "chegou",
                               "demorando", "atras"):
        return _status_do_pedido(telefone)

    if t in ("1", "1.") or tem("pedir", "pedido", "cardapio", "cardápio",
                               "comprar", "fome", "lanche", "delivery"):
        return (
            "Pra pedir é rapidinho 👇\n\n"
            f"{APP_URL}\n\n"
            "Abre direto no celular, sem precisar instalar nada. "
            "Dá pra pagar em PIX, cartão ou dinheiro na entrega."
        )

    if t in ("3", "3.") or tem("parceiro", "minha loja", "cadastrar loja", "restaurante", "vender"):
        return (
            "Que bom ter você com a gente! 🧡\n\n"
            "Pra cadastrar sua loja é só acessar:\n"
            "https://parceiros.inksadelivery.com.br\n\n"
            "Se preferir, me manda o *nome da loja* e o *bairro* que a gente "
            "te chama pra conversar."
        )

    if t in ("4", "4.") or tem("entregador", "motoboy", "entregas", "trabalhar"):
        return (
            "Boa! Estamos montando o time de entregadores em Lages 🛵\n\n"
            "Cadastro aqui:\n"
            "https://entregadores.inksadelivery.com.br\n\n"
            "Você precisa de veículo próprio (moto, carro ou bike) e CNH quando "
            "for moto ou carro."
        )

    if t in ("5", "5.") or tem("atendente", "humano", "pessoa", "falar com", "reclama", "problema"):
        return (
            "Beleza, já estou chamando alguém da equipe 🙋\n\n"
            "Me conta o que aconteceu enquanto isso — se for sobre um pedido, "
            "manda o número dele que agiliza."
        )

    # Primeira mensagem, "oi", ou qualquer coisa que não casou
    return MENU


def _status_do_pedido(telefone: str) -> str:
    """Último pedido de quem mandou a mensagem, achado pelo telefone."""
    chave = wa.chave_de_busca(telefone)
    if not chave:
        return "Não consegui identificar seu número. Você pode acompanhar em " + APP_URL

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            raise RuntimeError("sem conexão")
        import psycopg2.extras
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # Compara pelos últimos 8 dígitos dos dois lados: o cadastro tem
            # telefone com máscara, com/sem o 9, e o WhatsApp manda com o 55.
            cur.execute(
                """
                SELECT o.status, o.total_amount, o.created_at, o.delivery_code,
                       COALESCE(rp.restaurant_name, 'a loja') AS loja
                  FROM orders o
                  JOIN client_profiles cp ON cp.id = o.client_id
                  LEFT JOIN restaurant_profiles rp ON rp.id = o.restaurant_id
                 WHERE RIGHT(regexp_replace(COALESCE(cp.phone,''), '\\D', '', 'g'), 8) = %s
                 ORDER BY o.created_at DESC
                 LIMIT 1
                """,
                (chave,),
            )
            row = cur.fetchone()
    except Exception as e:
        logger.error("Falha ao buscar pedido no WhatsApp: %s", e, exc_info=True)
        return ("Tive um problema pra consultar agora 😕 Dá uma olhada em "
                f"{APP_URL} — lá o status aparece em tempo real.")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    if not row:
        return (
            "Não achei nenhum pedido com este número 🤔\n\n"
            "Se você pediu com outro telefone, dá pra ver tudo em:\n" + APP_URL
        )

    situacao = STATUS_LEGIVEL.get(row["status"], row["status"])
    linhas = [
        f"Seu último pedido em *{row['loja']}*:",
        f"Situação: *{situacao}*",
        f"Valor: R$ {float(row['total_amount'] or 0):.2f}".replace(".", ","),
    ]
    # O código só serve enquanto o pedido está a caminho.
    if row["status"] in ("delivering", "accepted_by_delivery") and row.get("delivery_code"):
        linhas.append(f"\n🔑 Código de entrega: *{row['delivery_code']}*")
        linhas.append("Mostre esse código a quem entregar.")
    linhas.append(f"\nAcompanhe em tempo real: {APP_URL}")
    return "\n".join(linhas)


def _ja_respondido(message_id: str, telefone: str, nome: str, corpo: str, resposta: str) -> bool:
    """Grava o evento e diz se ele JÁ tinha sido tratado.

    A Meta reenvia o mesmo evento quando o webhook demora ou falha. Sem esta
    trava a pessoa receberia a mesma resposta 2 ou 3 vezes.
    """
    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            return False
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO whatsapp_events
                       (message_id, from_phone, profile_name, body, reply_sent)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (message_id) DO NOTHING""",
                (message_id, telefone, nome, corpo, resposta),
            )
            novo = cur.rowcount > 0
        conn.commit()
        return not novo
    except Exception as e:
        logger.warning("Não deu pra registrar o evento do WhatsApp: %s", e)
        return False
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


@whatsapp_bp.route("/webhook", methods=["GET"])
def verificar_webhook():
    """Verificação da Meta ao cadastrar a URL: devolve o hub.challenge."""
    esperado = os.environ.get("WHATSAPP_VERIFY_TOKEN")
    modo = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    desafio = request.args.get("hub.challenge")
    if modo == "subscribe" and esperado and token == esperado:
        logger.info("✅ Webhook do WhatsApp verificado pela Meta.")
        return desafio or "", 200
    logger.warning("⛔ Verificação do webhook do WhatsApp recusada.")
    return "forbidden", 403


@whatsapp_bp.route("/webhook", methods=["POST"])
def receber_mensagem():
    """Recebe as mensagens. SEMPRE devolve 200 depois de validar a assinatura:
    erro nosso não pode fazer a Meta reenviar em loop."""
    if not wa.verificar_assinatura(request):
        return jsonify({"status": "forbidden"}), 403

    try:
        dados = request.get_json(silent=True) or {}
        for entrada in dados.get("entry", []):
            for mudanca in entrada.get("changes", []):
                valor = mudanca.get("value") or {}
                contatos = {c.get("wa_id"): (c.get("profile") or {}).get("name")
                            for c in valor.get("contacts", [])}
                for msg in valor.get("messages", []):
                    telefone = msg.get("from")
                    corpo = _texto(msg)
                    resposta = _resposta_para(corpo, telefone)
                    if _ja_respondido(msg.get("id"), telefone,
                                      contatos.get(telefone), corpo, resposta):
                        logger.info("Evento repetido do WhatsApp ignorado: %s", msg.get("id"))
                        continue
                    wa.enviar_texto(telefone, resposta)
    except Exception as e:
        logger.error("Erro ao processar webhook do WhatsApp: %s", e, exc_info=True)

    return jsonify({"status": "ok"}), 200
