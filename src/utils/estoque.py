# src/utils/estoque.py
#
# BAIXA DE ESTOQUE QUANDO O PEDIDO FECHA.
#
# ## O PROBLEMA QUE ISTO RESOLVE
#
# O estoque do parceiro chega por sincronização: o ERP manda o catálogo com
# `estoque`, e `utils/catalogo.py` traduz isso em `is_available = estoque > 0`.
# Funciona — item zerado para de vender. Mas o número só mexia quando o ERP
# mandava de novo, e no meio de duas sincronizações ele mente.
#
# Numa farmácia com uma caixa do remédio e sincronização de hora em hora, dois
# clientes compram a mesma caixa e o segundo descobre na entrega. Restaurante
# não sofre disso porque marca "esgotado" na mão; quem tem cinco mil itens não
# marca nada na mão.
#
# Aqui a conta desce na hora do pedido. O ERP continua sendo o dono do número —
# a próxima sincronização sobrescreve tudo. Isto só cobre a janela entre elas.
#
# ## AS QUATRO REGRAS QUE NÃO PODEM SER QUEBRADAS
#
# 1. `stock IS NULL` NÃO É TOCADO. Restaurante não controla estoque, e todo
#    item de restaurante tem stock nulo. Sem esta cláusula, a primeira venda
#    de qualquer pizzaria transformaria NULL em número e ligaria um controle
#    de estoque que ninguém pediu — e que zeraria o cardápio em uma semana.
#
# 2. NUNCA LIGA `is_available` NA BAIXA. Zerar o estoque desliga o item; ter
#    estoque NÃO o religa. O parceiro pode ter marcado esgotado na mão (faltou
#    na prateleira, embalagem violada), e a venda de um item qualquer não pode
#    desfazer essa decisão dele.
#
# 3. NUNCA FICA NEGATIVO (`GREATEST(..., 0)`). Estoque negativo não significa
#    nada e vaza pra tela do parceiro como defeito.
#
# 4. NÃO DERRUBA O PEDIDO. O cliente já pagou. Se a escrituração falhar, o
#    pedido vale e o erro vai pro log — a próxima sincronização do ERP corrige
#    o número de qualquer jeito. É por isso que toda função aqui engole exceção
#    e abre a própria conexão, em vez de participar da transação do pedido.

import logging

import psycopg2.extras

from .helpers import get_db_connection
from .pedido_itens import produtos_do_pedido

logger = logging.getLogger(__name__)


def _por_item(itens_crus):
    """{menu_item_id: quantidade} a partir de `orders.items` cru.

    Soma as repetições de propósito: o mesmo produto pode aparecer em duas
    linhas do carrinho (tamanhos, observações diferentes). Dar baixa linha a
    linha faria dois UPDATEs no mesmo id dentro da mesma consulta — e num
    `UPDATE ... FROM (VALUES ...)` o Postgres aplica só UM deles, silenciosamente.
    Agregar antes é o que impede a baixa de sair pela metade.
    """
    somas = {}
    for it in produtos_do_pedido(itens_crus):
        mid = it.get('menu_item_id')
        if not mid:
            continue
        # ⚠️ NADA DE `a or b or 1` AQUI. Quantidade ZERO é falsa em Python,
        # então o `or` pularia pro padrão 1 e daria baixa de uma unidade numa
        # linha que pede nenhuma — justo a linha que o `qtd <= 0` abaixo existe
        # pra ignorar. É a mesma armadilha do `erro.message || 'reserva'`:
        # coalescer por falsidade quando o zero é um valor legítimo.
        bruto = it.get('quantity')
        if bruto is None:
            bruto = it.get('quantidade')
        if bruto is None:
            bruto = 1
        try:
            qtd = int(bruto)
        except (TypeError, ValueError):
            qtd = 1
        if qtd <= 0:
            continue
        chave = str(mid)
        somas[chave] = somas.get(chave, 0) + qtd
    return somas


_SQL_BAIXA = """
UPDATE menu_items m
   SET stock = GREATEST(m.stock - v.qtd, 0),
       -- Desliga ao zerar; NUNCA liga. Ver regra 2 no topo do arquivo.
       is_available = CASE WHEN GREATEST(m.stock - v.qtd, 0) = 0
                           THEN FALSE ELSE m.is_available END,
       updated_at = NOW()
  FROM (VALUES %s) AS v(id, qtd)
 WHERE m.id = v.id::uuid
   AND m.stock IS NOT NULL
RETURNING m.id, m.name, m.stock, m.is_available
"""

_SQL_DEVOLUCAO = """
UPDATE menu_items m
   SET stock = m.stock + v.qtd,
       -- Religa SÓ quem estava zerado. Item que já tinha saldo e está
       -- desligado foi o parceiro que desligou na mão — devolver unidade de um
       -- pedido cancelado não é motivo pra desfazer a decisão dele.
       is_available = CASE WHEN m.stock = 0 THEN TRUE ELSE m.is_available END,
       updated_at = NOW()
  FROM (VALUES %s) AS v(id, qtd)
 WHERE m.id = v.id::uuid
   AND m.stock IS NOT NULL
RETURNING m.id, m.name, m.stock, m.is_available
"""


def _aplicar(sql, itens_crus, order_id, verbo):
    somas = _por_item(itens_crus)
    if not somas:
        return []

    conn = None
    try:
        conn = get_db_connection()
        if not conn:
            logger.warning("estoque: sem banco pra %s do pedido %s", verbo, order_id)
            return []
        with conn, conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            linhas = psycopg2.extras.execute_values(
                cur, sql, list(somas.items()), fetch=True
            )
        mexidos = [dict(r) for r in linhas]
        if mexidos:
            # Log por item, e em INFO: quando um parceiro reclamar que o
            # produto sumiu da vitrine, esta linha é a resposta. Só sai pra
            # quem REALMENTE controla estoque — restaurante não polui o log.
            for r in mexidos:
                logger.info("estoque %s: pedido %s, item %s (%s) -> %s%s",
                            verbo, order_id, r['id'], r['name'], r['stock'],
                            "" if r['is_available'] else " [saiu da vitrine]")
        return mexidos
    except Exception:
        # Regra 4: o pedido vale mesmo se isto falhar.
        logger.exception("estoque: falhei na %s do pedido %s (pedido segue valendo)",
                         verbo, order_id)
        return []
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def baixar(itens_crus, order_id=None):
    """Tira do estoque o que este pedido levou. Só mexe em quem controla estoque.

    ⚠️ NÃO É IDEMPOTENTE, e não dá pra ser sem guardar um registro por pedido.
    Chamar duas vezes pro mesmo pedido tira em dobro. Por isso só é chamada na
    CRIAÇÃO do pedido, que acontece uma vez por caminho — e são três caminhos
    (orders.py e duas funções de payment.py), cada um chamando uma vez.
    """
    return _aplicar(_SQL_BAIXA, itens_crus, order_id, "baixa")


def devolver(itens_crus, order_id=None):
    """Devolve ao estoque o que um pedido cancelado não vai levar.

    Sem isto, cada cancelamento comeria estoque pra sempre — e o parceiro veria
    o item sumir da vitrine por uma venda que nunca aconteceu.

    ⚠️ ASSIMETRIA CONHECIDA, medida em 21/09/2026. A baixa trava no zero
    (`GREATEST`), a devolução não sabe que travou. Item com 2 em estoque num
    pedido de 5 desce pra 0 e volta pra 5 — infla 3 unidades fantasma.

    Fica assim de propósito: corrigir exigiria guardar quanto foi realmente
    tirado de cada item por pedido, ou seja, uma tabela de movimentação e um
    ciclo de vida pra ela. Caro demais pro tamanho do erro, que só acontece
    quando alguém pede MAIS do que existe (anomalia por si só) e que a próxima
    sincronização do ERP apaga — o dono do número é o ERP, não a Inksa.

    Se um dia houver estoque sem ERP por trás (a Inksa como fonte da verdade),
    esta conversa muda e a tabela de movimentação passa a valer a pena.
    """
    return _aplicar(_SQL_DEVOLUCAO, itens_crus, order_id, "devolucao")
