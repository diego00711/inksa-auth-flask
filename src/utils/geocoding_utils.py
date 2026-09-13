# Arquivo: src/utils/geocoding_utils.py
"""Geocodificação (endereço → coordenada) e reversa (coordenada → endereço).

═══════════════════════════════════════════════════════════════════════════
 NOTA PARA QUANDO HOUVER DEMANDA — trocar o provedor
═══════════════════════════════════════════════════════════════════════════

Hoje isto usa o Nominatim PÚBLICO da OpenStreetMap. É de graça e comunitário,
e vale enquanto o volume for pequeno. Dois motivos para trocar, nesta ordem:

 1. QUALIDADE. A reversa do Nominatim erra o número da casa no Brasil com
    frequência. É por isso que o checkout exige "número e complemento" quando
    o cliente usa a localização atual.
 2. LIMITE. A política deles é ~1 requisição por segundo e proíbe uso
    sistemático pesado. Quando bloquear, os endereços voltam a aparecer como
    coordenada e ninguém é avisado — o sintoma é mudo.

COMO TROCAR (a estrutura já está pronta para isso):
  • Defina a env var GEOCODER_PROVIDER = "google" no Render.
  • Preencha GEOCODER_API_KEY com a chave.
  • Implemente `_reverse_google()` abaixo — a assinatura e o formato de
    retorno já estão definidos, e `reverse_geocode()` despacha sozinho.
  • Nada muda nos apps: eles falam com /api/public/reverse-geocode.

QUAL PROVEDOR: o Google Geocoding é o melhor em endereço brasileiro e o
projeto do Firebase JÁ É um projeto Google Cloud — é habilitar a API no
mesmo lugar. Alternativas sem Google: LocationIQ e Geoapify, que rodam
Nominatim como serviço, com chave e sem risco de bloqueio.

⚠️ A chave NUNCA vai para o app. É por isso que esta chamada mora no
backend: no navegador, a chave vaza no bundle e qualquer um gasta a cota.
"""
import os
import time
import logging
import threading

import requests

# Contato REAL, exigido pela política de uso do Nominatim. Estava
# 'contact@yourdomain.com' — placeholder que os torna livres para bloquear
# a gente sem aviso, porque não há quem avisar.
_USER_AGENT = os.environ.get(
    "GEOCODER_USER_AGENT",
    "InksaDelivery/1.0 (suporte@inksadelivery.com.br)",
)

_PROVIDER = (os.environ.get("GEOCODER_PROVIDER") or "nominatim").strip().lower()

# Cache em memória da reversa. As mesmas coordenadas se repetem MUITO (a
# pessoa mexe no carrinho, volta, tenta de novo), e cada repetição é uma
# requisição a um serviço que nos tolera por gentileza.
_CACHE = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 60 * 60 * 24  # 24h — endereço de um ponto não muda no dia
_CACHE_MAX = 2000


def _cache_get(chave):
    with _CACHE_LOCK:
        item = _CACHE.get(chave)
        if not item:
            return None
        valor, expira = item
        if time.time() > expira:
            _CACHE.pop(chave, None)
            return None
        return valor


def _cache_set(chave, valor):
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()  # simples de propósito: é cache, não verdade
        _CACHE[chave] = (valor, time.time() + _CACHE_TTL)


def _montar_endereco_curto(a):
    """Endereço para o ENTREGADOR ler, não para arquivo.

    Só o que serve pra chegar: rua (+ número quando o serviço acertou o
    prédio), bairro e cidade. Sem estado, CEP e país — o `display_name` cru
    do Nominatim é um parágrafo, e ninguém lê parágrafo de moto na chuva.
    """
    rua = a.get('road') or a.get('pedestrian') or a.get('footway') or a.get('residential')
    bairro = a.get('suburb') or a.get('neighbourhood') or a.get('village')
    cidade = a.get('city') or a.get('town') or a.get('municipality')
    partes = [
        ", ".join(x for x in [rua, a.get('house_number')] if x),
        bairro,
        cidade,
    ]
    return " - ".join(p for p in partes if p)


def _reverse_nominatim(lat, lng):
    r = requests.get(
        "https://nominatim.openstreetmap.org/reverse",
        params={"format": "jsonv2", "addressdetails": 1, "lat": lat, "lon": lng},
        headers={"User-Agent": _USER_AGENT, "Accept-Language": "pt-BR"},
        timeout=8,
    )
    r.raise_for_status()
    j = r.json() or {}
    curto = _montar_endereco_curto(j.get("address") or {})
    return curto or j.get("display_name") or None


def _reverse_google(lat, lng):  # noqa: ARG001
    """Implementar quando GEOCODER_PROVIDER=google. Ver a nota no topo.

    Deve devolver a mesma coisa que _reverse_nominatim: uma string curta e
    legível, ou None. Sugestão: chamar
    https://maps.googleapis.com/maps/api/geocode/json?latlng=..&key=..
    e montar a partir de `address_components` (route, street_number,
    sublocality, administrative_area_level_2).
    """
    raise NotImplementedError(
        "Provedor 'google' ainda não implementado — ver a nota no topo de geocoding_utils.py"
    )


def reverse_geocode(lat, lng):
    """Coordenada → endereço curto. None quando não dá pra resolver.

    Nunca levanta exceção: endereço é conveniência, e derrubar o checkout
    porque um serviço externo piscou seria trocar um problema pequeno por um
    grande. Quem chama decide o que mostrar quando vem None.
    """
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return None

    # Arredonda pra ~11 m no cache: quem anda meio metro não muda de porta,
    # e sem isso cada leitura de GPS vira uma chave nova.
    chave = f"{lat_f:.4f},{lng_f:.4f}"
    em_cache = _cache_get(chave)
    if em_cache is not None:
        return em_cache

    try:
        if _PROVIDER == "google":
            endereco = _reverse_google(lat_f, lng_f)
        else:
            endereco = _reverse_nominatim(lat_f, lng_f)
    except Exception as e:
        logging.warning("Reverse geocode falhou (%s): %s", _PROVIDER, e)
        return None

    if endereco:
        _cache_set(chave, endereco)
    return endereco

def geocode_cached(street, number, neighborhood, city, state, zipcode):
    """Igual a `geocode_address`, mas com cache — para o que vem do app.

    Endereço digitado se repete muito (a pessoa corrige o número, volta,
    salva de novo) e cada repetição é uma requisição ao mesmo serviço que a
    reversa usa, dividindo o mesmo limite. O cache é o que evita gastar a
    cota com a mesma rua três vezes seguidas.
    """
    chave = "fwd:" + "|".join(
        str(x or "").strip().lower()
        for x in (street, number, neighborhood, city, state, zipcode)
    )
    if chave == "fwd:" + "|" * 5:
        return None, None

    em_cache = _cache_get(chave)
    if em_cache is not None:
        return em_cache

    lat, lng = geocode_address(street, number, neighborhood, city, state, zipcode)
    if lat is not None and lng is not None:
        _cache_set(chave, (lat, lng))
    return lat, lng


def _sem_acento(s):
    """'São Paulo' -> 'sao paulo'. Comparar cidade sem isto dá falso negativo."""
    import unicodedata
    return ''.join(
        c for c in unicodedata.normalize('NFD', str(s or '').strip().lower())
        if unicodedata.category(c) != 'Mn'
    )


def _municipio_do_resultado(addr):
    """O Nominatim chama município de nomes diferentes conforme o lugar.

    `city` é o comum, mas em muito município brasileiro vem só `town`,
    `municipality` ou `village`. Olhar só `city` faria a conferência recusar
    endereço bom em cidade pequena — que é justamente pra onde a Inksa quer ir.
    """
    for chave in ('city', 'town', 'municipality', 'village', 'city_district'):
        if addr.get(chave):
            return addr[chave]
    return ''


def _municipio_bate(addr, city, state):
    """O ponto devolvido é da cidade que foi pedida?

    Fail-OPEN de propósito quando o Nominatim não diz o município: recusar sem
    saber derrubaria endereço bom, e a consequência aqui (loja sem coordenada,
    invisível na vitrine) é pior que a do erro que a checagem evita.
    """
    achado = _municipio_do_resultado(addr)
    if not achado:
        return True
    if _sem_acento(achado) != _sem_acento(city):
        return False
    # Cidade igual em estado diferente existe (São Paulo/SP e São Paulo/RS).
    uf = addr.get('ISO3166-2-lvl4') or ''   # ex.: 'BR-SC'
    if state and uf and len(uf) >= 2:
        return _sem_acento(uf[-2:]) == _sem_acento(state)
    return True


def geocode_address(street, number, neighborhood, city, state, zipcode):
    """
    Geocodifica um endereço completo para latitude e longitude usando Nominatim (OpenStreetMap).
    Requer uma chave de API para serviços comerciais como Google Maps/Mapbox em produção.
    
    Args:
        street (str): Nome da rua.
        number (str): Número do endereço.
        neighborhood (str): Bairro.
        city (str): Cidade.
        state (str): Estado (sigla, ex: "SP", "RJ").
        zipcode (str): CEP.
        
    Returns:
        tuple: (latitude, longitude) como floats, ou (None, None) se a geocodificação falhar.
    """
    # "Rua X, 123" — ordem brasileira. Estava "123 Rua X" (ordem americana),
    # que e o mesmo endereco escrito de um jeito que casa pior.
    address_parts = [
        f"{street}, {number}" if (street and number) else (street or number),
        neighborhood,
        city,
        state,
        zipcode
    ]
    # Filtra None e strings vazias para formar o endereço completo
    full_address = ", ".join(filter(None, address_parts)) 
    
    if not full_address:
        logging.info("Geocodificação: Endereço vazio, retornando None, None.")
        return None, None 

    # O Nominatim EXIGE User-Agent identificando quem chama. Estava um
    # placeholder ('contact@yourdomain.com'): na prática, um convite a
    # bloqueio sem aviso, porque não havia a quem avisar.
    headers = {'User-Agent': _USER_AGENT}
    
    # Endpoint da API Nominatim
    nominatim_url = "https://nominatim.openstreetmap.org/search"
    params = {
        'q': full_address,
        'format': 'json',
        'limit': 1,
        # 1, e não 0: é de `address` que sai a cidade usada pra conferir se o
        # ponto devolvido é mesmo da cidade pedida. Ver _municipio_bate().
        'addressdetails': 1,
        # Sem isto, "Centro, Lages, SC" pode casar com um Centro em outro
        # pais. Os apps ja mandavam countrycodes=br; o backend nao mandava.
        'countrycodes': 'br',
    }

    try:
        response = requests.get(nominatim_url, params=params, headers=headers, timeout=8)
        response.raise_for_status() # Lança um erro para status HTTP 4xx/5xx

        results = response.json()

        if results and len(results) > 0:
            lat = float(results[0].get('lat'))
            lon = float(results[0].get('lon'))
            # ⚠️ CONFERE A CIDADE ANTES DE ACEITAR.
            #
            # Sem isto o Nominatim devolve o "mais parecido" que achou no
            # Brasil inteiro, e a gente grava como se fosse. Medido em
            # 13/09/2026: o MESMO texto ("Rua Rodolfo Reis Figueira, 76, São
            # Miguel, Lages") existe duas vezes nos endereços de cliente — um
            # em Lages e outro a 81 km, em outro município. Ponto errado desses
            # não é detalhe: vira entregador dirigindo pra fora da cidade e
            # frete calculado em cima de uma distância que não existe.
            #
            # Comparar CIDADE, e não distância de um centro fixo: a plataforma
            # é multi-cidade de propósito (ver platform_max_delivery_radius).
            # Qualquer raio chumbado em Lages quebraria a expansão.
            if city and not _municipio_bate(results[0].get('address') or {}, city, state):
                logging.warning(
                    "Geocodificação RECUSADA para '%s': resultado caiu em '%s', "
                    "não em '%s'. Coordenada descartada.",
                    full_address, _municipio_do_resultado(results[0].get('address') or {}), city)
                return None, None
            logging.info(f"Geocodificação bem-sucedida para '{full_address}': Lat={lat}, Lon={lon}")
            return lat, lon
        else:
            logging.warning(f"Geocodificação falhou para o endereço: '{full_address}'. Nenhum resultado encontrado.")
            return None, None
    except requests.exceptions.RequestException as e:
        logging.error(f"Erro na requisição HTTP de geocodificação para '{full_address}': {e}")
        return None, None
    except ValueError as e:
        logging.error(f"Erro ao processar resposta JSON da geocodificação para '{full_address}': {e}")
        return None, None

# Teste (opcional, pode ser removido depois)
if __name__ == "__main__":
    print("Testando geocodificação...")
    lat, lon = geocode_address("Rua XV de Novembro", "1000", "Centro", "Lages", "SC", "88501-000")
    if lat and lon:
        print(f"Coordenadas de Lages: Latitude={lat}, Longitude={lon}")
    else:
        print("Geocodificação de Lages falhou.")

    lat, lon = geocode_address("Rua sem nome", "", "", "", "", "")
    if not lat and not lon:
        print("Teste com endereço vazio funcionou como esperado.")