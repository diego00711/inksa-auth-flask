# src/utils/cep_correios.py
"""Confere o endereço contra os Correios ANTES de ele virar destino de entrega.

POR QUE ISTO EXISTE

Em 13/09/2026 a Me Mimei Confeitaria recebeu o primeiro pedido real da loja e o
entregador foi parar longe dali. O cadastro dizia:

    Rua Trinta e Um de Março, 126 — Guarujá — CEP 88521-000

E o CEP 88521-000 cobre os números **493 a 1564**. O número 126 fica no outro
trecho da mesma rua: bairro São Sebastião, CEP 88520-335. O geocodificador fez
o que foi mandado — pôs o pino no trecho do Guarujá — e ninguém tinha como
saber que o endereço era impossível, porque nada conferia.

Erro de endereço não aparece no cadastro. Aparece no entregador rodando à toa,
com o cliente esperando e a loja achando que o sistema é ruim.

⚠️ ISTO AVISA, NÃO BLOQUEIA. A base dos Correios tem buraco (loteamento novo,
rua sem faixa cadastrada, complemento vazio), e travar o cadastro de um parceiro
por causa disso seria trocar um problema raro por um comum. Quem decide é gente:
a mensagem vai pra tela e pro log.
"""
import logging
import re

import requests

logger = logging.getLogger(__name__)

_VIACEP = "https://viacep.com.br/ws/{}/json/"
_TIMEOUT = 6          # cadastro é interativo: melhor não conferir que travar a tela
_cache = {}           # cep -> dict dos Correios (ou None). Vive no processo.


def _so_digitos(s):
    return re.sub(r"\D", "", str(s or ""))


def consulta_cep(cep):
    """Dados dos Correios pro CEP, ou None. Nunca levanta exceção."""
    cep = _so_digitos(cep)
    if len(cep) != 8:
        return None
    if cep in _cache:
        return _cache[cep]
    try:
        r = requests.get(_VIACEP.format(cep), timeout=_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        d = None if d.get("erro") else d
    except Exception as e:
        # Correios fora do ar não pode derrubar o salvamento de um cadastro.
        logger.info("ViaCEP indisponível para %s: %s", cep, e)
        return None
    _cache[cep] = d
    return d


def _faixa_de_numeros(complemento):
    """'de 0493/494 a 1563/1564' -> (493, 1564); 'até 491/492' -> (0, 492).

    O campo `complemento` dos Correios é texto livre e nem sempre traz faixa
    ('Lado par', 'Km 12', vazio). Quando não dá pra ler número nenhum, devolve
    None e a conferência de número simplesmente não acontece.
    """
    if not complemento:
        return None
    nums = [int(n) for n in re.findall(r"\d+", complemento)]
    if not nums:
        return None
    c = complemento.strip().lower()
    if c.startswith(("ate", "até")):
        return (0, max(nums))
    if "ao fim" in c:
        return (min(nums), 10 ** 9)
    return (min(nums), max(nums))


def _sem_acento(s):
    import unicodedata
    return "".join(
        ch for ch in unicodedata.normalize("NFD", str(s or "").strip().lower())
        if unicodedata.category(ch) != "Mn"
    )


def confere_endereco(cep, numero=None, bairro=None):
    """Devolve lista de avisos (vazia = nada estranho encontrado).

    Cada aviso é uma frase pronta pra mostrar pra pessoa que está cadastrando —
    não um código de erro. Quem lê é o parceiro ou o admin, não um programa.
    """
    dados = consulta_cep(cep)
    if dados is None:
        return []   # sem base pra comparar: cala. Ver o ⚠️ no topo do arquivo.

    avisos = []

    faixa = _faixa_de_numeros(dados.get("complemento"))
    n = _so_digitos(numero)
    if faixa and n:
        n = int(n)
        if not (faixa[0] <= n <= faixa[1]):
            avisos.append(
                "O número {} não pertence ao CEP {}: esse CEP cobre os números "
                "{} a {}. Confira o número ou o CEP.".format(
                    n, dados.get("cep") or cep, faixa[0], faixa[1])
            )

    b_correios = (dados.get("bairro") or "").strip()
    if bairro and b_correios and _sem_acento(bairro) != _sem_acento(b_correios):
        avisos.append(
            "Nos Correios o CEP {} é do bairro \"{}\", e o cadastro diz \"{}\".".format(
                dados.get("cep") or cep, b_correios, bairro)
        )

    if avisos:
        logger.warning("Endereço suspeito (CEP %s, nº %s): %s", cep, numero, " | ".join(avisos))
    return avisos
