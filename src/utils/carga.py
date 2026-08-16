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
        return {str(r['id']): r['peso_kg'] for r in cur.fetchall() if r['peso_kg'] is not None}
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
