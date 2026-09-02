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

_NOMES_DE_FRETE = ('taxa de entrega', 'frete')


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
