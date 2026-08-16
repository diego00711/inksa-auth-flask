# src/utils/carga.py
"""Peso do pedido e qual veículo dá conta dele.

Nasceu quando o catálogo deixou de ser só comida. Pet shop, mercado e
agropecuária vendem coisas que simplesmente NÃO cabem numa moto: dois sacos
de ração de 30 kg são 60 kg. Até aqui o pedido era oferecido igualmente ao
entregador de bicicleta — ele aceitava, ia até a loja e não levava, com o
cliente já cobrado.

A regra mora num lugar só de propósito: ela vai ser consultada no despacho
(quem pode ver o pedido), no checkout (dá pra entregar isso?) e mais tarde
no frete (carro custa mais que moto). Três leitores da mesma regra é como
ela começa a divergir.
"""
import logging

logger = logging.getLogger(__name__)

# O cadastro do entregador e as configurações usam nomes diferentes pro mesmo
# veículo ('bicicleta' no perfil, 'bike' na chave de raio já existente).
# Normalizar aqui evita que uma comparação silenciosamente não bata.
_APELIDOS = {
    'bike': 'bike', 'bicicleta': 'bike', 'bicicletas': 'bike',
    'moto': 'moto', 'motocicleta': 'moto', 'motoca': 'moto',
    'carro': 'carro', 'automovel': 'carro', 'automóvel': 'carro',
    'utilitario': 'utilitario', 'utilitário': 'utilitario',
    'van': 'utilitario', 'caminhonete': 'utilitario', 'pickup': 'utilitario',
    # LEGADO EM INGLÊS. O CHECK do banco sempre aceitou 'motorcycle' e 'car',
    # mas eles não estavam aqui: normalizavam pra None e o filtro de carga, que
    # é fail-closed pra veículo desconhecido, deixava o entregador sem receber
    # NADA — online e calado. Um valor que o banco aceita e o código não
    # entende é uma armadilha esperando alguém cair.
    'motorcycle': 'moto', 'car': 'carro',
}

# Ordem crescente de capacidade — usada pra dizer o veículo MÍNIMO necessário.
_ORDEM = ('bike', 'moto', 'carro', 'utilitario')

_PADRAO_KG = {'bike': 8.0, 'moto': 20.0, 'carro': 80.0, 'utilitario': 300.0}

_ROTULO = {
    'bike': 'bicicleta',
    'moto': 'moto',
    'carro': 'carro',
    'utilitario': 'utilitário',
}


def normalizar_veiculo(valor):
    """'Bicicleta' → 'bike'. Devolve None quando não reconhece."""
    if not valor:
        return None
    return _APELIDOS.get(str(valor).strip().lower())


def capacidades(settings=None):
    """Capacidade em kg de cada veículo, do admin, com padrão de segurança."""
    settings = settings or {}
    fora = {}
    for chave in _ORDEM:
        bruto = settings.get(f'capacidade_kg_{chave}')
        try:
            valor = float(str(bruto).replace(',', '.')) if bruto not in (None, '') else None
        except (TypeError, ValueError):
            valor = None
        # Configuração inválida NÃO vira capacidade infinita: cai no padrão.
        fora[chave] = valor if (valor and valor > 0) else _PADRAO_KG[chave]
    return fora


def peso_dos_itens(itens, pesos_por_id=None):
    """Soma peso_kg * quantidade. Item sem peso conhecido conta 0.

    Tolera os três formatos que `orders.items` assume (lista, string JSON e
    objeto aninhado) — a mesma bagunça que o resto do código já contorna.
    """
    import json

    if not itens:
        return 0.0
    if isinstance(itens, str):
        try:
            itens = json.loads(itens)
        except (json.JSONDecodeError, TypeError):
            return 0.0
    if isinstance(itens, dict):
        itens = itens.get('items') or itens.get('itens') or []
    if not isinstance(itens, list):
        return 0.0

    pesos_por_id = pesos_por_id or {}
    total = 0.0
    for item in itens:
        if not isinstance(item, dict):
            continue
        try:
            qtd = float(item.get('quantity') or item.get('quantidade') or 1)
        except (TypeError, ValueError):
            qtd = 1.0
        peso = item.get('peso_kg')
        if peso in (None, ''):
            peso = pesos_por_id.get(str(item.get('menu_item_id') or item.get('id') or ''))
        try:
            peso = float(str(peso).replace(',', '.')) if peso not in (None, '') else 0.0
        except (TypeError, ValueError):
            peso = 0.0
        total += max(peso, 0.0) * max(qtd, 0.0)
    return round(total, 3)


def buscar_pesos(cur, itens):
    """Lê peso_kg do catálogo pros itens do pedido.

    O peso viaja no item quando o app manda, mas não dá pra confiar no que
    vem do cliente — preço a gente já valida no servidor, peso idem.
    """
    import json

    if isinstance(itens, str):
        try:
            itens = json.loads(itens)
        except (json.JSONDecodeError, TypeError):
            return {}
    if isinstance(itens, dict):
        itens = itens.get('items') or itens.get('itens') or []
    if not isinstance(itens, list):
        return {}

    ids = []
    for item in itens:
        if isinstance(item, dict):
            mid = item.get('menu_item_id') or item.get('id')
            if mid:
                ids.append(str(mid))
    if not ids:
        return {}
    try:
        cur.execute("SELECT id, peso_kg FROM menu_items WHERE id = ANY(%s::uuid[])", (ids,))
        # Índice, não nome: assim funciona com cursor comum E com DictCursor.
        # Com r['peso_kg'] esta função exigia DictCursor sem dizer — quem
        # chamasse com cursor comum levava TypeError, que o except abaixo
        # engolia, e o pedido seguia como 0 kg. Aconteceu: o adicional de
        # frete por carga nasceu sem funcionar por causa disso.
        return {str(r[0]): r[1] for r in cur.fetchall() if r[1] is not None}
    except Exception:
        logger.warning("Não consegui ler peso dos itens — pedido segue como 0 kg", exc_info=True)
        return {}


def peso_do_pedido(cur, itens):
    """Peso total do pedido, com os pesos vindos do catálogo (autoritativo)."""
    return peso_dos_itens(itens, buscar_pesos(cur, itens))


def veiculo_comporta(vehicle_type, peso_kg, settings=None):
    """O veículo do entregador dá conta desse peso?

    FAIL-CLOSED em duas situações, e as duas são de propósito:
      • veículo não reconhecido → False. Melhor não ofertar do que ofertar
        uma carga que ninguém sabe se cabe.
      • peso desconhecido/zero → True. Pedido sem peso informado é o caso
        normal de comida; travar isso pararia a operação inteira.
    """
    try:
        peso = float(peso_kg or 0)
    except (TypeError, ValueError):
        peso = 0.0
    if peso <= 0:
        return True
    chave = normalizar_veiculo(vehicle_type)
    if not chave:
        return False
    return peso <= capacidades(settings)[chave]


def veiculo_minimo(peso_kg, settings=None):
    """Menor veículo que leva esse peso. None se nem o maior dá conta.

    Devolve (chave, rótulo) — o rótulo é o que aparece pro cliente.
    """
    try:
        peso = float(peso_kg or 0)
    except (TypeError, ValueError):
        peso = 0.0
    if peso <= 0:
        return ('bike', _ROTULO['bike'])
    caps = capacidades(settings)
    for chave in _ORDEM:
        if peso <= caps[chave]:
            return (chave, _ROTULO[chave])
    return None


# Adicional de frete por CLASSE EXIGIDA. Bike e moto não têm adicional: são a
# referência, o que `fixed_delivery_fee` + `per_km_delivery_fee` já cobrem.
_PADRAO_FRETE = {
    'carro':       {'fixo': 8.0,  'km': 2.50},
    'utilitario':  {'fixo': 25.0, 'km': 3.50},
}


def frete_da_carga(peso_kg, settings=None):
    """Como o peso do pedido muda o frete.

    ⚠️ COBRA-SE PELO QUE O PEDIDO EXIGE, NÃO PELO VEÍCULO DE QUEM ACEITA.
    Isso não é preferência, é imposição do fluxo: o frete é cotado no
    checkout, ANTES de existir entregador. Se dependesse do veículo de quem
    aceita, o preço mudaria depois que o cliente já viu — ou o carro receberia
    o mesmo que a moto por carregar 60 kg.

    Devolve (fixo_extra, preco_km, chave_do_veiculo, rótulo):
      • fixo_extra  — soma à taxa base. Paga o TRABALHO de carregar, que a
                      conta por km não cobre: 60 kg de ração são 10-15 min a
                      mais de esforço, iguais a 1 km ou a 10.
      • preco_km    — SUBSTITUI o per_km normal. Paga o CUSTO de rodar: carro
                      faz ~10 km/L contra ~35 km/L da moto.
      • None em preco_km = usa o per_km normal (bike/moto).

    Peso 0 (item sem peso cadastrado) cai em bike: sem adicional. É o mesmo
    fail-open da trava de carga, e a razão de o peso precisar ser obrigatório
    nos segmentos que vendem coisa pesada — senão 60 kg passam por 0 kg e o
    adicional nunca dispara.
    """
    settings = settings or {}
    alvo = veiculo_minimo(peso_kg, settings)
    if not alvo:
        # Nem o maior veículo leva: quem chama decide o que fazer (a trava de
        # carga recusa o pedido). Aqui devolve o teto, pra não cobrar barato.
        alvo = ('utilitario', _ROTULO['utilitario'])
    chave, rotulo = alvo

    padrao = _PADRAO_FRETE.get(chave)
    if not padrao:
        return (0.0, None, chave, rotulo)

    def _num(nome, default):
        bruto = settings.get(nome)
        try:
            v = float(str(bruto).replace(',', '.')) if bruto not in (None, '') else None
        except (TypeError, ValueError):
            v = None
        # Configuração inválida NÃO vira frete zero: cai no padrão.
        return v if (v is not None and v >= 0) else default

    fixo = _num(f'frete_adicional_{chave}', padrao['fixo'])
    km = _num(f'frete_km_{chave}', padrao['km'])
    return (fixo, km, chave, rotulo)
