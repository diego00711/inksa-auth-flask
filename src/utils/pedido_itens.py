# src/utils/pedido_itens.py
#
# A LINHA DE FRETE NÃO É UM PRODUTO — E JÁ ENGANOU O SISTEMA TRÊS VEZES.
#
# O checkout acrescenta "Taxa de Entrega" dentro de `orders.items` para fechar
# a conta. Ela ocupa a mesma lista dos produtos de verdade, e por isso todo
# código que percorre os itens precisa saber ignorá-la. Quando esquece:
#
#   • contagem de volumes: 3 sacos viravam 4 itens, e o entregador usa esse
#     número para julgar se a carga cabe na moto;
#   • relatório de vendas: o frete entrava como produto vendido;
#   • API de parceiro: o PDV imprimiria "Taxa de Entrega" na COZINHA, e a soma
#     dos itens não fechava com o subtotal do pedido.
#
# A regra tem duas partes e as duas importam. Só o nome não basta: uma loja de
# material de construção pode vender um produto chamado "frete", e ele
# sumiria dos relatórios. Por isso exige também não ter `menu_item_id` — item
# de catálogo tem id, linha sintética do checkout não tem.

import json

_NOMES_DE_FRETE = ('taxa de entrega', 'frete')


def normalizar(items):
    """`orders.items` como lista, venha ele nos TRÊS formatos que existem.

    O campo aparece como lista, como string JSON e como objeto aninhado
    ({"items": [...]}) dependendo de por onde o pedido entrou — são três
    caminhos de criação distintos, e cada um grava do seu jeito. Qualquer parse
    que assuma um formato só devolve vazio nos outros dois, em silêncio.

    ⚠️ Devolver [] em vez de levantar é deliberado: quem chama isto está
    contando volume, somando venda ou dando baixa de estoque — nenhum deles
    deve derrubar um pedido porque o JSON veio torto.
    """
    if not items:
        return []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    if isinstance(items, dict):
        items = items.get('items') or []
    return items if isinstance(items, list) else []


def produtos_do_pedido(items):
    """Os produtos de verdade de um pedido cru: normaliza E tira o frete.

    É o atalho que quase todo chamador quer — as duas armadilhas deste arquivo
    (formato e linha de frete) resolvidas numa chamada só.
    """
    return apenas_produtos(normalizar(items))


def eh_linha_de_frete(item):
    """A linha é a taxa de entrega sintética (e não um produto do cardápio)?"""
    if not isinstance(item, dict):
        return False
    if item.get('menu_item_id'):
        return False
    nome = str(item.get('title') or item.get('name') or '').strip().lower()
    return nome in _NOMES_DE_FRETE


def apenas_produtos(itens):
    """Os itens do pedido sem a linha de frete."""
    if not isinstance(itens, list):
        return []
    return [i for i in itens if isinstance(i, dict) and not eh_linha_de_frete(i)]
