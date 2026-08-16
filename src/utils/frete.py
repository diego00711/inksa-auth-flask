# src/utils/frete.py
"""A conta do frete da plataforma, num lugar só.

Dois leitores, e é por isso que ela mora aqui:

  1. a COTAÇÃO, no carrinho (`delivery_calculator`) — o número que o cliente vê;
  2. a VALIDAÇÃO, no fechamento (`payment.py`) — o número que vale.

Precisam dar exatamente o mesmo resultado. Enquanto a conta existia só na
cotação, o fechamento aceitava o `delivery_fee` que o app mandasse: dava pra
fechar pedido com frete zero, e quem pagava a conta era o entregador, que
recebe o frete menos a taxa de administração. Ele só descobriria depois de ter
rodado.

Duas contas separadas divergem — é questão de tempo. Uma só não tem como.
"""
from .carga import frete_da_carga


def _num(v, default=0.0):
    try:
        return float(str(v).replace(',', '.'))
    except (TypeError, ValueError):
        return default


def calcular_frete(distancia_km, peso_kg=0, settings=None):
    """Frete da plataforma em R$, e o texto que explica a conta.

    Devolve (valor, metodo). O `metodo` é o que aparece pro cliente e no log —
    frete sem explicação vira reclamação.

    A classe do veículo vem do PESO do pedido, não de quem vai entregar: no
    momento da cotação não existe entregador. Ver carga.frete_da_carga.
    """
    settings = settings or {}
    base = _num(settings.get('fixed_delivery_fee'), 5.0)
    por_km = _num(settings.get('per_km_delivery_fee'), 1.5)
    livre = _num(settings.get('free_delivery_threshold_km'), 1.0)
    dist = max(0.0, _num(distancia_km, 0.0))

    fixo_carga, km_carga, _chave, rotulo = frete_da_carga(peso_kg, settings)
    km_efetivo = km_carga if km_carga is not None else por_km

    valor = base + fixo_carga
    if dist > livre:
        valor += (dist - livre) * km_efetivo
        metodo = f"Taxa base R$ {base:.2f} + R$ {km_efetivo:.2f}/km extra"
    else:
        metodo = f"Taxa base R$ {base:.2f} (dentro do limite gratuito)"

    if fixo_carga or km_carga is not None:
        metodo += f" + carga de {rotulo} (R$ {fixo_carga:.2f})"

    return round(valor, 2), metodo
