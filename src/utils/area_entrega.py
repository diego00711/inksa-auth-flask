# src/utils/area_entrega.py
"""O endereço do cliente está dentro da área que a loja atende?

Nasceu de dois pedidos reais criados em 13 e 15/08/2026: restaurante em São
Paulo, entrega em Nova Iguaçu (RJ), **331,74 km**, frete de **R$ 501,10** — com
o raio da plataforma configurado em 50 km. Um deles chegou a gerar cobrança PIX
de R$ 553,10; o outro reservava R$ 481,06 para um entregador que teria que
cruzar dois estados.

Por que passou: a trava de raio existia só para `delivery_type = 'own'`. No
`'platform'` o cálculo era `taxa base + km × preço`, sem teto e sem checagem de
área — 331 km viravam R$ 501 com naturalidade.

A regra fica aqui, num lugar só, porque tem TRÊS leitores: a calculadora de
frete (que responde ao carrinho), e os dois caminhos que inserem pedido
(orders.py e payment.py). Trava em um só é trava furada: a calculadora é
conselho, não barreira — quem grava o pedido é quem precisa recusar.
"""
import logging
from math import radians, cos, sin, asin, sqrt

logger = logging.getLogger(__name__)


def distancia_km(lat1, lon1, lat2, lon2):
    """Haversine em km. None se faltar qualquer coordenada."""
    try:
        lat1, lon1, lat2, lon2 = float(lat1), float(lon1), float(lat2), float(lon2)
    except (TypeError, ValueError):
        return None
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 6371.0 * 2 * asin(sqrt(a))


def raio_da_loja(delivery_type, own_delivery_radius_km, settings=None):
    """Raio que vale para esta loja, em km.

    Loja de ENTREGA PRÓPRIA cobra taxa fixa em qualquer distância, então o raio
    dela é uma trava de dinheiro — vale o MENOR entre o dela e o da plataforma.
    Quem não configurou segue no raio da plataforma.
    """
    # Importa o módulo de settings só quando PRECISA ler do banco. Importar
    # sempre arrastava a cadeia inteira (helpers → jwt → psycopg2) mesmo com as
    # settings já em mãos, e deixava esta regra impossível de testar sozinha.
    if settings is None:
        from .platform_settings import get_settings
        settings = get_settings()
    try:
        raio_plataforma = float(settings["platform_max_delivery_radius"])
    except (KeyError, TypeError, ValueError):
        raio_plataforma = 15.0

    if str(delivery_type or 'platform') == 'own' and own_delivery_radius_km:
        try:
            proprio = float(own_delivery_radius_km)
            if proprio > 0:
                return min(proprio, raio_plataforma)
        except (TypeError, ValueError):
            pass
    return raio_plataforma


def verificar_area(rest_lat, rest_lng, cli_lat, cli_lng,
                   delivery_type='platform', own_delivery_radius_km=None,
                   settings=None, nome_loja=None):
    """Devolve (ok, dados).

    `ok=False` vem com uma mensagem pronta pro cliente, dizendo o raio e a
    distância — o "fora da área" seco faz a pessoa achar que é bug do app.

    FAIL-OPEN quando falta coordenada, e isso é deliberado: restaurante sem
    geocodificar já é tratado em outro lugar (cobra taxa base e sinaliza), e
    recusar aqui derrubaria o carrinho de lojas que só estão com cadastro
    incompleto. O que esta função existe pra impedir é a distância ABSURDA,
    que só é mensurável quando há as duas pontas.
    """
    raio = raio_da_loja(delivery_type, own_delivery_radius_km, settings)
    dist = distancia_km(rest_lat, rest_lng, cli_lat, cli_lng)

    if dist is None:
        return True, {"distancia_km": None, "raio_km": raio, "motivo": "sem_coordenadas"}

    if dist > raio:
        loja = nome_loja or "Esta loja"
        return False, {
            "distancia_km": round(dist, 2),
            "raio_km": raio,
            "error": "fora_da_area",
            "message": (
                f"{loja} entrega até {raio:.0f} km e o endereço escolhido está a "
                f"{dist:.1f} km. Escolha um endereço mais perto ou outra loja."
            ),
        }

    return True, {"distancia_km": round(dist, 2), "raio_km": raio}
