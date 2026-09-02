# src/routes/parceiro_api.py
#
# API PÚBLICA DE PARCEIRO — o que o PDV/ERP da loja consome.
#
# ── POR QUE ELA EXISTE ─────────────────────────────────────────────────────
# O mercado brasileiro funciona ao contrário do que parece: o iFood não
# escreve um conector para cada PDV. Ele publica UMA API, e cada fabricante de
# PDV constrói em cima. Todo sistema de restaurante que se preze já tem essa
# encanação pronta — receber pedido de app de delivery não é capacidade nova
# para eles.
#
# Então a Inksa expõe o contrato e cada PDV novo custa ZERO linha de código
# nosso: só conversa e homologação. O contrário — a gente empurrar o pedido
# para dentro do sistema deles — exigiria fila, retentativa, guardar
# credencial de terceiro e um adaptador por fabricante.
#
# ── POR QUE É DE PROPÓSITO PEQUENA ─────────────────────────────────────────
# Contrato público não se desfaz: assim que um PDV integrar, mudar formato
# quebra a operação de uma loja no meio do movimento. Então a v1 expõe só o
# que uma COZINHA precisa, e nada além:
#
#   • ler pedidos          • aceitar   • começar preparo   • marcar pronto
#   • ler e enviar cardápio
#
# O que ficou FORA, e por quê:
#   - Cancelar pedido. Cancelamento de pedido pago dispara estorno ao cliente,
#     e essa lógica mora na rota do app. Reimplementar aqui criaria dois
#     caminhos para devolver dinheiro — o tipo de divergência que já custou
#     caro neste projeto. Cancelar continua pelo app até dar para reusar.
#   - "Saiu para entrega" e "Entregue". Passam por CÓDIGO (retirada/entrega),
#     que é o que prova que a entrega aconteceu. Um endpoint de API que pulasse
#     o código deixaria qualquer integração fechar pedido sem entregar.
#
# ── AUTENTICAÇÃO ───────────────────────────────────────────────────────────
# Bearer token por loja, guardado como hash. Ver partner_api_tokens.

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import psycopg2.extras
from flask import Blueprint, jsonify, request

from ..extensions import limiter
from ..utils.catalogo import importar_itens
from ..utils.helpers import get_db_connection

logger = logging.getLogger(__name__)

parceiro_api_bp = Blueprint('parceiro_api_bp', __name__)

VERSAO = '1.0'
_MAX_ITENS_POR_LOTE = 2000
_MAX_PEDIDOS_POR_PAGINA = 100

# Só o que uma cozinha faz. Ver o cabeçalho para o que ficou de fora.
_TRANSICOES_PERMITIDAS = {
    'aceitar':  ('pending', 'accepted'),
    'preparar': ('accepted', 'preparing'),
    'pronto':   ('preparing', 'ready'),
}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def gerar_token(ambiente='live'):
    """Devolve (token_em_texto, hash, prefixo).

    O texto só existe aqui e na resposta da criação — nunca é gravado. Perdeu,
    gera outro. Guardar para poder "mostrar de novo" transformaria a tabela
    num alvo que hoje ela não é.
    """
    corpo = secrets.token_urlsafe(32)
    token = f"ink_{ambiente}_{corpo}"
    return token, _hash(token), token[:16]


def _erro(codigo, mensagem, http=400, **extra):
    """Erro com CÓDIGO estável.

    Quem integra trata `codigo` no código-fonte dele. Se a gente só mandasse
    mensagem em português, qualquer melhoria de texto quebraria o tratamento
    do outro lado.
    """
    corpo = {"erro": codigo, "mensagem": mensagem}
    corpo.update(extra)
    return jsonify(corpo), http


def exige_token(f):
    """Resolve o Bearer token para UMA loja e injeta `loja` no handler."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        cabecalho = request.headers.get('Authorization', '')
        if not cabecalho.startswith('Bearer '):
            return _erro('sem_credencial',
                         'Envie o cabeçalho: Authorization: Bearer <seu token>', 401)

        token = cabecalho[7:].strip()
        if not token:
            return _erro('sem_credencial', 'Token vazio.', 401)

        conn = None
        try:
            conn = get_db_connection()
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT t.id AS token_id, t.ambiente, t.restaurant_id,
                           r.restaurant_name, r.user_id, r.active
                      FROM partner_api_tokens t
                      JOIN restaurant_profiles r ON r.id = t.restaurant_id
                     WHERE t.token_hash = %s AND t.revogado_em IS NULL
                """, (_hash(token),))
                linha = cur.fetchone()

                if not linha:
                    # Mesma resposta para token inexistente e revogado: dizer
                    # qual é dos dois entrega informação a quem está tentando.
                    return _erro('credencial_invalida',
                                 'Token inválido ou revogado.', 401)

                # Best-effort: saber que a integração está viva vale muito no
                # suporte ("o PDV parou de buscar às 14h"), mas não pode
                # derrubar a chamada se a escrita falhar.
                try:
                    cur.execute("UPDATE partner_api_tokens SET ultimo_uso_em = NOW() WHERE id = %s",
                                (linha['token_id'],))
                    conn.commit()
                except Exception:
                    conn.rollback()
                    logger.warning('Falha ao gravar ultimo_uso_em', exc_info=True)

                loja = dict(linha)
        except Exception:
            logger.exception('Erro autenticando token de parceiro')
            return _erro('erro_interno', 'Falha ao validar credencial.', 500)
        finally:
            if conn:
                conn.close()

        return f(*args, loja=loja, **kwargs)
    return wrapper


def _pedido_publico(linha):
    """Formato ESTÁVEL do pedido. Mudar campo aqui quebra integração alheia.

    Nomes em português porque quem integra é fabricante de PDV brasileiro; e
    valores numéricos como número, não string, para ninguém precisar adivinhar
    separador decimal.
    """
    itens = linha.get('items')
    # `orders.items` chega como array, string JSON ou objeto {items:[...]}
    # dependendo do caminho que criou o pedido. Quem integra não pode herdar
    # essa bagunça: normaliza aqui.
    if isinstance(itens, dict):
        itens = itens.get('items') or []
    if isinstance(itens, str):
        import json
        try:
            itens = json.loads(itens)
        except ValueError:
            itens = []
    if not isinstance(itens, list):
        itens = []

    def num(v):
        try:
            return round(float(v), 2)
        except (TypeError, ValueError):
            return 0.0

    return {
        "numero": linha.get('numero'),
        "id": str(linha.get('id')),
        "status": linha.get('status'),
        "criado_em": linha['created_at'].isoformat() if linha.get('created_at') else None,
        "atualizado_em": linha['updated_at'].isoformat() if linha.get('updated_at') else None,
        "pagamento": {
            "forma": linha.get('payment_method'),
            "pago": linha.get('status_pagamento') == 'approved',
        },
        "valores": {
            "itens": num(linha.get('total_amount', 0)) - num(linha.get('delivery_fee', 0)),
            "entrega": num(linha.get('delivery_fee')),
            "total": num(linha.get('total_amount')),
        },
        "entrega": {
            "tipo": linha.get('delivery_type') or 'plataforma',
            "endereco": linha.get('delivery_address'),
        },
        "cliente": {
            "nome": linha.get('cliente_nome'),
            "telefone": linha.get('cliente_telefone'),
        },
        "itens": [
            {
                "nome": i.get('title') or i.get('name'),
                "quantidade": i.get('quantity') or i.get('qty') or 1,
                "preco_unitario": num(i.get('unit_price') if i.get('unit_price') is not None
                                      else i.get('price')),
                "observacao": i.get('notes') or i.get('observacao') or None,
            }
            for i in itens if isinstance(i, dict)
        ],
        "observacoes": linha.get('notes'),
    }


_SELECT_PEDIDO = """
    SELECT o.id, o.numero, o.status, o.status_pagamento, o.created_at, o.updated_at,
           o.total_amount, o.delivery_fee, o.payment_method, o.delivery_address,
           o.items, o.notes,
           r.delivery_type,
           c.first_name || COALESCE(' ' || c.last_name, '') AS cliente_nome,
           c.phone AS cliente_telefone
      FROM orders o
      JOIN restaurant_profiles r ON r.id = o.restaurant_id
      LEFT JOIN client_profiles c ON c.id = o.client_id
     WHERE o.restaurant_id = %s
"""


# ───────────────────────────────────────────────────────────────────────────
# Diagnóstico
# ───────────────────────────────────────────────────────────────────────────

@parceiro_api_bp.get('/status')
@limiter.limit("60/minute")
@exige_token
def status(loja):
    """Primeira chamada de quem está integrando: o token funciona e é de quem?

    Existe porque sem ela o integrador descobre que errou a credencial só
    quando um pedido não chega — e aí o problema já é da loja, no movimento.
    """
    return jsonify({
        "ok": True,
        "versao": VERSAO,
        "ambiente": loja['ambiente'],
        "loja": {"id": str(loja['restaurant_id']),
                 "nome": loja['restaurant_name'],
                 "aberta": bool(loja['active'])},
        "servidor_em": datetime.now(timezone.utc).isoformat(),
    }), 200


# ───────────────────────────────────────────────────────────────────────────
# Pedidos
# ───────────────────────────────────────────────────────────────────────────

@parceiro_api_bp.get('/pedidos')
@limiter.limit("120/minute")
@exige_token
def listar_pedidos(loja):
    """Pedidos alterados desde um instante. É assim que o PDV busca novidade.

    `desde` usa `updated_at`, não `created_at`: o PDV precisa reagir também a
    um pedido antigo que mudou de status. Filtrando por criação, um pedido
    cancelado depois nunca chegaria ao sistema da loja.

    Sem `desde`, devolve as últimas 24h — para o integrador conseguir a
    primeira resposta sem ler documentação nenhuma.
    """
    desde_txt = (request.args.get('desde') or '').strip()
    if desde_txt:
        try:
            desde = datetime.fromisoformat(desde_txt.replace('Z', '+00:00'))
        except ValueError:
            return _erro('parametro_invalido',
                         "Use ISO 8601 em 'desde'. Ex: 2026-09-02T14:30:00Z")
    else:
        desde = datetime.now(timezone.utc) - timedelta(hours=24)

    try:
        limite = min(int(request.args.get('limite', 50)), _MAX_PEDIDOS_POR_PAGINA)
    except ValueError:
        limite = 50

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                _SELECT_PEDIDO + """
                   AND o.updated_at > %s
                   AND o.status <> 'awaiting_payment'
                   AND o.archived_at IS NULL
                 ORDER BY o.updated_at ASC
                 LIMIT %s
                """, (loja['restaurant_id'], desde, limite))
            pedidos = [_pedido_publico(dict(l)) for l in cur.fetchall()]
    except Exception:
        logger.exception('Erro listando pedidos na API de parceiro')
        return _erro('erro_interno', 'Falha ao buscar pedidos.', 500)
    finally:
        if conn:
            conn.close()

    # `proximo_desde` evita que o integrador tenha que calcular a janela — e,
    # mais importante, evita o furo clássico de usar o relógio DELE, que pode
    # estar adiantado e pular pedido.
    proximo = pedidos[-1]['atualizado_em'] if pedidos else desde.isoformat()
    return jsonify({"pedidos": pedidos, "quantidade": len(pedidos),
                    "proximo_desde": proximo}), 200


@parceiro_api_bp.get('/pedidos/<int:numero>')
@limiter.limit("120/minute")
@exige_token
def obter_pedido(numero, loja):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(_SELECT_PEDIDO + " AND o.numero = %s",
                        (loja['restaurant_id'], numero))
            linha = cur.fetchone()
    except Exception:
        logger.exception('Erro obtendo pedido na API de parceiro')
        return _erro('erro_interno', 'Falha ao buscar pedido.', 500)
    finally:
        if conn:
            conn.close()

    if not linha:
        return _erro('nao_encontrado', f'Pedido #{numero} não existe nesta loja.', 404)
    return jsonify(_pedido_publico(dict(linha))), 200


def _mudar_status(numero, loja, acao):
    de, para = _TRANSICOES_PERMITIDAS[acao]
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""SELECT id, status FROM orders
                            WHERE restaurant_id = %s AND numero = %s
                            FOR UPDATE""",
                        (loja['restaurant_id'], numero))
            pedido = cur.fetchone()
            if not pedido:
                return _erro('nao_encontrado', f'Pedido #{numero} não existe nesta loja.', 404)

            atual = (pedido['status'] or '').strip()

            # Repetir a mesma ação devolve OK, não erro. PDV reenvia por
            # timeout de rede o tempo todo; tratar repetição como falha faria
            # o sistema da loja mostrar erro de um pedido que já está certo.
            if atual == para:
                return jsonify({"ok": True, "numero": numero, "status": atual,
                                "observacao": "O pedido já estava neste status."}), 200

            if atual != de:
                return _erro('transicao_invalida',
                             f"Pedido #{numero} está em '{atual}'. "
                             f"'{acao}' só vale quando está em '{de}'.",
                             409, status_atual=atual)

            campos, valores = "status = %s, updated_at = NOW()", [para]

            if acao == 'aceitar':
                minutos = (request.get_json(silent=True) or {}).get('tempo_preparo_min')
                try:
                    minutos = int(minutos) if minutos is not None else None
                except (TypeError, ValueError):
                    minutos = None
                campos += ", accepted_at = NOW()"
                if minutos and 0 < minutos <= 240:
                    campos += ", estimated_prep_time = %s"
                    valores.append(minutos)

            valores.append(pedido['id'])
            cur.execute(f"UPDATE orders SET {campos} WHERE id = %s", valores)
            conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        logger.exception('Erro mudando status na API de parceiro')
        return _erro('erro_interno', 'Falha ao atualizar o pedido.', 500)
    finally:
        if conn:
            conn.close()

    return jsonify({"ok": True, "numero": numero, "status": para}), 200


@parceiro_api_bp.post('/pedidos/<int:numero>/aceitar')
@limiter.limit("120/minute")
@exige_token
def aceitar(numero, loja):
    """Aceita o pedido. Corpo opcional: {"tempo_preparo_min": 25}"""
    return _mudar_status(numero, loja, 'aceitar')


@parceiro_api_bp.post('/pedidos/<int:numero>/preparar')
@limiter.limit("120/minute")
@exige_token
def preparar(numero, loja):
    return _mudar_status(numero, loja, 'preparar')


@parceiro_api_bp.post('/pedidos/<int:numero>/pronto')
@limiter.limit("120/minute")
@exige_token
def pronto(numero, loja):
    """Marca como pronto. É o que libera o pedido para o entregador."""
    return _mudar_status(numero, loja, 'pronto')


# ───────────────────────────────────────────────────────────────────────────
# Cardápio
# ───────────────────────────────────────────────────────────────────────────

@parceiro_api_bp.get('/cardapio')
@limiter.limit("30/minute")
@exige_token
def ler_cardapio(loja):
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""SELECT name, description, price, promo_price, category,
                                  ean, stock, is_available
                             FROM menu_items WHERE restaurant_id = %s
                            ORDER BY category, name""",
                        (loja['restaurant_id'],))
            itens = [{
                "nome": i['name'],
                "descricao": i['description'],
                "preco": float(i['price']) if i['price'] is not None else None,
                "preco_promocional": float(i['promo_price']) if i.get('promo_price') is not None else None,
                "categoria": i['category'],
                "ean": i['ean'],
                "estoque": i['stock'],
                "disponivel": bool(i['is_available']),
            } for i in cur.fetchall()]
    except Exception:
        logger.exception('Erro lendo cardápio na API de parceiro')
        return _erro('erro_interno', 'Falha ao ler o cardápio.', 500)
    finally:
        if conn:
            conn.close()

    return jsonify({"itens": itens, "quantidade": len(itens)}), 200


@parceiro_api_bp.post('/cardapio')
@limiter.limit("10/minute")
@exige_token
def enviar_cardapio(loja):
    """Cria ou atualiza itens. Reenviar o mesmo cardápio é seguro.

    Usa exatamente a mesma função da importação por planilha do app
    (utils/catalogo.importar_itens) — reconcilia por EAN, senão por nome. Sem
    isso, sincronizar duas vezes duplicaria o cardápio inteiro.

    `teste: true` valida sem gravar nada. É o que o integrador roda primeiro.
    """
    corpo = request.get_json(silent=True) or {}
    itens = corpo.get('itens') or corpo.get('items') or []

    if not isinstance(itens, list) or not itens:
        return _erro('sem_itens', "Envie 'itens': [{nome, preco, ...}]")
    if len(itens) > _MAX_ITENS_POR_LOTE:
        return _erro('lote_grande',
                     f'Máximo {_MAX_ITENS_POR_LOTE} itens por chamada. Divida em partes.')

    # Aceita os nomes em português da nossa API e os em inglês do banco: o
    # integrador não deveria ter que descobrir isso na tentativa e erro.
    traduzidos = [{
        'name':        i.get('nome') or i.get('name'),
        'price':       i.get('preco') if i.get('preco') is not None else i.get('price'),
        'category':    i.get('categoria') or i.get('category'),
        'description': i.get('descricao') or i.get('description'),
        'ean':         i.get('ean'),
        'stock':       i.get('estoque') if i.get('estoque') is not None else i.get('stock'),
    } for i in itens if isinstance(i, dict)]

    teste = bool(corpo.get('teste') or corpo.get('dry_run'))

    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            criados, atualizados, ignorados = importar_itens(
                cur, loja['user_id'], loja['restaurant_id'], traduzidos, dry_run=teste)
            if not teste:
                conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        logger.exception('Erro importando cardápio na API de parceiro')
        return _erro('erro_interno', 'Falha ao gravar o cardápio.', 500)
    finally:
        if conn:
            conn.close()

    return jsonify({
        "ok": True, "teste": teste, "recebidos": len(traduzidos),
        "criados": criados, "atualizados": atualizados,
        "ignorados": ignorados[:50], "total_ignorados": len(ignorados),
    }), 200


# ───────────────────────────────────────────────────────────────────────────
# GERÊNCIA DAS CREDENCIAIS — usada pelo APP do parceiro, não pelo PDV.
#
# Blueprint separado de propósito: aqui a autenticação é o login normal do
# parceiro (JWT). Se estas rotas aceitassem o próprio token da API, quem
# roubasse um token poderia criar outros e sobreviver à revogação.
# ───────────────────────────────────────────────────────────────────────────

from ..utils.helpers import get_user_id_from_token  # noqa: E402

parceiro_credenciais_bp = Blueprint('parceiro_credenciais_bp', __name__)


def _loja_do_login():
    """(restaurant_id, erro). Resolve o parceiro logado para a loja dele."""
    user_id, user_type, erro = get_user_id_from_token(request.headers.get('Authorization'))
    if erro:
        return None, erro
    if user_type != 'restaurant':
        return None, _erro('sem_permissao', 'Apenas parceiros acessam credenciais.', 403)

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT id FROM restaurant_profiles WHERE user_id = %s", (user_id,))
            linha = cur.fetchone()
            if not linha:
                return None, _erro('sem_loja', 'Perfil de parceiro não encontrado.', 404)
            return linha['id'], None
    finally:
        conn.close()


@parceiro_credenciais_bp.get('')
@parceiro_credenciais_bp.get('/')
def listar_credenciais():
    restaurant_id, erro = _loja_do_login()
    if erro:
        return erro

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""SELECT id, nome, prefixo, ambiente, criado_em,
                                  ultimo_uso_em, revogado_em
                             FROM partner_api_tokens
                            WHERE restaurant_id = %s
                            ORDER BY criado_em DESC""", (restaurant_id,))
            itens = [{
                "id": str(l['id']), "nome": l['nome'],
                # Nunca o token inteiro: só o suficiente para o parceiro saber
                # qual é qual quando tiver mais de uma integração.
                "prefixo": l['prefixo'] + '…',
                "ambiente": l['ambiente'],
                "criado_em": l['criado_em'].isoformat() if l['criado_em'] else None,
                "ultimo_uso_em": l['ultimo_uso_em'].isoformat() if l['ultimo_uso_em'] else None,
                "revogada": l['revogado_em'] is not None,
            } for l in cur.fetchall()]
    finally:
        conn.close()

    return jsonify({"credenciais": itens}), 200


@parceiro_credenciais_bp.post('')
@parceiro_credenciais_bp.post('/')
@limiter.limit("10/hour")
def criar_credencial():
    """Cria uma credencial. O token aparece UMA vez, nesta resposta."""
    restaurant_id, erro = _loja_do_login()
    if erro:
        return erro

    corpo = request.get_json(silent=True) or {}
    nome = (str(corpo.get('nome') or '').strip() or 'Integração')[:60]
    ambiente = 'sandbox' if corpo.get('ambiente') == 'sandbox' else 'live'

    token, hash_, prefixo = gerar_token(ambiente)

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""INSERT INTO partner_api_tokens
                             (restaurant_id, nome, token_hash, prefixo, ambiente)
                           VALUES (%s,%s,%s,%s,%s) RETURNING id, criado_em""",
                        (restaurant_id, nome, hash_, prefixo, ambiente))
            nova = cur.fetchone()
            conn.commit()
    except Exception:
        conn.rollback()
        logger.exception('Erro criando credencial de parceiro')
        return _erro('erro_interno', 'Não foi possível criar a credencial.', 500)
    finally:
        conn.close()

    return jsonify({
        "id": str(nova['id']), "nome": nome, "ambiente": ambiente,
        "token": token,
        "aviso": "Guarde agora: este token não será mostrado de novo.",
    }), 201


@parceiro_credenciais_bp.delete('/<uuid:credencial_id>')
def revogar_credencial(credencial_id):
    """Revoga. Não apaga: o histórico de quando foi criada e usada continua."""
    restaurant_id, erro = _loja_do_login()
    if erro:
        return erro

    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""UPDATE partner_api_tokens SET revogado_em = NOW()
                            WHERE id = %s AND restaurant_id = %s AND revogado_em IS NULL
                        RETURNING id""", (str(credencial_id), restaurant_id))
            achou = cur.fetchone()
            conn.commit()
    finally:
        conn.close()

    if not achou:
        return _erro('nao_encontrado', 'Credencial não encontrada ou já revogada.', 404)
    return jsonify({"ok": True}), 200
