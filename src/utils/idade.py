# src/utils/idade.py
"""Pedido que exige maioridade — quem decide, e por quê num lugar só.

Nasceu quando a Inksa passou a ter loja de bebida no catálogo. Vender álcool
a menor de 18 é CRIME (ECA, art. 243), não infração administrativa, e
responde tanto a loja quanto a plataforma que intermediou.

O desenho é o mesmo da carga (utils/carga.py), de propósito:

  • a marca vive no CATÁLOGO (`menu_items.age_restricted`), que é do parceiro;
  • ela é CONGELADA no pedido (`orders.age_restricted`) no fechamento, lida do
    catálogo — NUNCA do que o app mandou.

O segundo ponto é o que faz a trava valer alguma coisa. Se o servidor
acreditasse no payload, bastaria um POST com `age_restricted: false` pra
sumir com o aviso e com a instrução ao entregador. É a mesma lição que o
frete e o peso já custaram caro aqui.

⚠️ ESTA TRAVA NÃO VERIFICA IDADE — ela AVISA e INSTRUI. Quem confere o
documento é o entregador, na porta. O software não tem como saber a idade de
quem abre a porta; o que ele pode fazer é (a) obrigar o cliente a declarar
antes de pagar, (b) marcar o pedido e (c) botar a instrução na frente de
quem entrega. Prometer mais que isso seria falso.
"""
import logging

logger = logging.getLogger(__name__)

# Texto único, para os três apps mostrarem a MESMA coisa. Divergir aqui vira
# cliente alegando que "no app dizia outra coisa".
AVISO_CLIENTE = (
    "Este pedido tem item com venda proibida para menores de 18 anos. "
    "Ao continuar, você declara ser maior de idade e concorda em apresentar "
    "documento com foto ao entregador."
)
INSTRUCAO_ENTREGADOR = (
    "PEDIDO COM ITEM PARA MAIORES DE 18. Confira documento com foto antes de "
    "entregar. Se quem receber for menor de idade ou recusar apresentar "
    "documento, NÃO entregue e registre uma ocorrência."
)


def _lista_de_itens(itens):
    """Aceita os três formatos que `orders.items` assume por aí."""
    import json

    if isinstance(itens, str):
        try:
            itens = json.loads(itens)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(itens, dict):
        itens = itens.get('items') or itens.get('itens') or []
    return itens if isinstance(itens, list) else []


def ids_dos_itens(itens):
    """IDs de catálogo presentes no pedido."""
    ids = []
    for item in _lista_de_itens(itens):
        if isinstance(item, dict):
            mid = item.get('menu_item_id') or item.get('id')
            if mid:
                ids.append(str(mid))
    return ids


def pedido_restrito(cur, itens):
    """O pedido tem ao menos um item que exige maioridade?

    Lê do catálogo com o cursor recebido. Índice numérico em vez de nome de
    coluna: assim funciona com cursor comum E com DictCursor — a mesma
    armadilha que já fez o adicional de carga nascer sem funcionar.

    Fail-CLOSED ao contrário do peso: se não der pra apurar, devolve False.
    Parece contraditório, mas não é — marcar um pedido comum como restrito
    faria o entregador pedir documento pra quem comprou pizza, e o aviso
    perderia o sentido por excesso. O caminho seguro aqui é a marca no
    catálogo estar certa, e ela é explícita.
    """
    ids = ids_dos_itens(itens)
    if not ids:
        return False
    try:
        cur.execute(
            "SELECT 1 FROM menu_items "
            " WHERE id = ANY(%s::uuid[]) AND COALESCE(age_restricted, false) "
            " LIMIT 1",
            (ids,),
        )
        return cur.fetchone() is not None
    except Exception:
        logger.warning("Não consegui apurar restrição de idade do pedido", exc_info=True)
        return False
