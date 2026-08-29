"""Preço vigente de um item de cardápio.

POR QUE ISTO É UM ARQUIVO SÓ, E NÃO DUAS LINHAS EM CADA LUGAR

O preço de um item é validado no servidor em DOIS caminhos diferentes de
criação de pedido, dentro do payment.py:

  1. o fluxo online, que monta a lista pro provedor de pagamento;
  2. o _validar_itens_e_total, que recalcula o total do lado do servidor.

Esses dois caminhos já divergiram antes nesta base — o comentário do próprio
payment.py registra o caso das opções, que existiam num fluxo e não no outro, e
o resultado foi a loja entregando adicional de graça. Regra de preço que mora em
dois lugares vira duas regras diferentes na primeira alteração.

Então: quem precisa saber quanto um item custa AGORA chama preco_vigente(). Não
existe outra resposta certa em lugar nenhum do sistema.

A CONVENÇÃO DE NOME QUE VALE PRA API DO CLIENTE

No que o app do CLIENTE recebe, `price` significa sempre "o que você paga" —
já com a promoção aplicada — e `original_price` é o valor riscado (ou None).

Isso é deliberado e é o contrário do que parece natural. Se `price` continuasse
sendo o preço cheio e a promoção viesse num campo novo, todo consumidor que já
lê `price` (carrinho, total, "pedir de novo") continuaria cobrando o valor
antigo, o servidor recusaria o pedido pela divergência, e o cliente veria
"Preço dos itens inválido" sem ter feito nada de errado.

No app do PARCEIRO é o oposto e também de propósito: lá `price` é o preço BASE,
que é o que ele edita, e `promo_price` vem separado. Ele está cadastrando, não
comprando.
"""


def preco_vigente(row):
    """Quanto o item custa agora, em reais.

    `row` é qualquer dicionário com as chaves `price` e (opcionalmente)
    `promo_price` — serve tanto pra linha do psycopg2 quanto pro dict do
    supabase-py.

    A promoção só vale se for MENOR que o preço base. Promoção que encarece é
    ignorada em silêncio, e isso protege contra o erro mais provável do
    cadastro: digitar 300 no lugar de 30. Sem esta guarda, o item seria
    vendido por dez vezes o preço e a primeira pessoa a descobrir seria o
    cliente, na tela de pagamento.
    """
    base = _float(row.get('price'))
    promo = row.get('promo_price')
    if promo is None:
        return base
    promo = _float(promo)
    if promo <= 0 or promo >= base:
        return base
    return promo


def em_promocao(row):
    """True quando a promoção do item está valendo de verdade.

    Não basta `promo_price is not None`: uma promoção inerte (maior ou igual ao
    preço base) não pode desenhar selo nem preço riscado na vitrine, senão o
    cliente vê "OFERTA" num item que não está mais barato.
    """
    return preco_vigente(row) < _float(row.get('price'))


def percentual_desconto(row):
    """Desconto em % inteiro, pra desenhar o selo. 0 quando não há promoção."""
    base = _float(row.get('price'))
    if not em_promocao(row) or base <= 0:
        return 0
    return int(round((base - preco_vigente(row)) / base * 100))


def normalizar_promo(promo_bruto, preco_base):
    """Valida o que o parceiro digitou no campo de promoção.

    Devolve o valor a gravar em `promo_price` (float ou None).
    Lança ValueError com mensagem pronta pra mostrar na tela.

    Vazio, None ou 0 significam "tirar a promoção" — é assim que o parceiro
    desliga: limpando o campo. Não existe botão separado de desativar, porque
    campo vazio já é a forma óbvia e um botão a mais seria um estado a mais
    pra sincronizar.
    """
    if promo_bruto is None or promo_bruto == '' or promo_bruto is False:
        return None
    try:
        promo = float(str(promo_bruto).replace(',', '.'))
    except (TypeError, ValueError):
        raise ValueError("Preço promocional inválido.")

    if promo <= 0:
        return None

    base = _float(preco_base)
    if promo >= base:
        raise ValueError(
            "O preço promocional precisa ser MENOR que o preço normal "
            f"(R$ {base:.2f}). Se quiser baixar o preço de vez, altere o preço "
            "normal em vez de criar promoção."
        )
    return round(promo, 2)


def _float(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0
