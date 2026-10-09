# src/logic/rifa.py
"""
Numeros da campanha (sorteio). Quem gera, quando, e como nao gerar duas vezes.

## ⚠️ NASCE DESLIGADA, E ISSO NAO E DETALHE

`rifa_campanhas.ligada` comeca `false`. Sorteio de premio no Brasil exige
autorizacao previa (Lei 5.768/1971, hoje pela Secretaria de Premios e Apostas
do Ministerio da Fazenda). Enquanto a chave estiver desligada, TODA funcao
daqui devolve zero sem tocar no banco.

O software fica pronto e a campanha nao comeca por acidente. Ligar e um UPDATE
de uma linha, depois que a autorizacao sair.

## A IDEIA CENTRAL: RECONCILIAR, NAO INCREMENTAR

A tentacao e "pedido entregue -> +1 numero". Nao faco assim, por um motivo
concreto desta base: **o gatilho da entrega mora em mais de um lugar**
(orders.py e gamification_routes.py escrevem `status='delivered'`, e a entrega
propria fecha por outro caminho). Incremento disparado de tres lugares vira
premio triplo ou nenhum, e a conta so aparece errada no dia do sorteio.

Entao cada funcao aqui pergunta **"quantos numeros esta pessoa DEVERIA ter?"**
e acerta a diferenca:

    devidos = floor(total_valido / reais_por_numero)
    conceder = devidos - ja_concedidos

Isso e idempotente de graca: chamar duas vezes nao gera nada na segunda. E
conserta sozinho — se um pedido for cancelado, `devidos` cai e a mesma funcao
CANCELA o excedente. Nao preciso de um caminho separado pra estorno.

## O QUE CONTA

- Cliente  : soma de `total_amount_items` dos pedidos ENTREGUES dele.
- Parceiro : soma de `total_amount_items` dos pedidos ENTREGUES da loja.
- Entregador: quantidade de entregas concluidas — e **entrega propria nao
  conta**, porque nelas nao houve entregador Inksa (decisao do Diego).

Frete fora de proposito: `total_amount_items` e o valor dos itens, que e a
mesma base da comissao. Contar frete faria o numero depender da distancia.

## ⚠️ A ARBITRAGEM, QUE NAO E TEORICA

Quem e cliente E dono de loja ganha numero dos DOIS lados girando pedido na
propria loja. O custo e so a comissao (15%), entao cada R$50 girados custam
~R$7,50 e rendem 2 numeros — cerca de R$4 por numero. Com bolo pequeno, isso
compra o premio.

Duas travas aqui:
  1. `_mesmo_dono()` — pedido em que o cliente e o dono da loja nao conta pra
     nenhum dos dois lados.
  2. `teto_numeros_mes` — teto por pessoa por mes. Nasce em 0 (sem teto,
     que e a regra do Diego); existe pra ser ligado sem deploy se aparecer
     abuso.

Nenhuma funcao daqui levanta excecao: numero de sorteio jamais pode derrubar um
pedido que ja aconteceu no mundo real.
"""
import logging
import math

logger = logging.getLogger(__name__)

# Origens que vieram de movimento (pedido/venda/entrega), por oposicao ao
# numero de cadastro. Usadas na reconciliacao.
_ORIGEM_POR_TIPO = {
    'cliente': 'pedido',
    'parceiro': 'venda',
    'entregador': 'entrega',
}


def _campanha(cur):
    """A campanha valendo AGORA, ou None.

    None significa 'nao faca nada' — e o estado normal enquanto a autorizacao
    nao sai. Quem chama nunca precisa testar a chave.
    """
    try:
        cur.execute("""
            SELECT campanha, reais_por_numero, numero_no_cadastro, teto_numeros_mes,
                   nome, premio
              FROM rifa_campanhas
             WHERE ligada = TRUE
               AND (inicio IS NULL OR inicio <= CURRENT_DATE)
               AND (fim    IS NULL OR fim    >= CURRENT_DATE)
             ORDER BY criada_em DESC
             LIMIT 1
        """)
        return cur.fetchone()
    except Exception:
        logger.warning("[RIFA] nao consegui ler a campanha", exc_info=True)
        return None


def _alocar(cur, campanha, quantos):
    """Reserva um bloco de `quantos` numeros e devolve o primeiro.

    ⚠️ O `UPDATE ... RETURNING` TRANCA A LINHA da campanha, entao dois pedidos
    simultaneos nunca recebem o mesmo numero. Fazer `max(numero)+1` teria
    corrida justamente no pico, que e quando a campanha importa.
    """
    if quantos <= 0:
        return None
    cur.execute("""
        UPDATE rifa_campanhas
           SET proximo_numero = proximo_numero + %s
         WHERE campanha = %s
        RETURNING proximo_numero - %s AS inicio
    """, (quantos, campanha, quantos))
    row = cur.fetchone()
    return int(row['inicio']) if row else None


def _mesmo_dono(cur, order_id):
    """True quando quem pediu e dono da loja do pedido.

    E a trava da arbitragem descrita no topo. Compara o `user_id` do perfil de
    cliente com o da loja — e tambem o telefone, porque nada impede duas contas
    de auth para a mesma pessoa.
    """
    try:
        cur.execute("""
            SELECT (cp.user_id = rp.user_id) AS mesmo_user,
                   (NULLIF(TRIM(cp.phone), '') IS NOT NULL
                    AND TRIM(cp.phone) = TRIM(rp.phone)) AS mesmo_telefone
              FROM orders o
              JOIN client_profiles     cp ON cp.id = o.client_id
              JOIN restaurant_profiles rp ON rp.id = o.restaurant_id
             WHERE o.id = %s
        """, (str(order_id),))
        r = cur.fetchone()
        return bool(r and (r['mesmo_user'] or r['mesmo_telefone']))
    except Exception:
        # Na duvida NAO bloqueia: recusar numero de quem tem direito e pior
        # que conceder um a mais, e o teto mensal continua valendo.
        logger.warning("[RIFA] checagem de mesmo dono falhou (%s)", order_id, exc_info=True)
        return False


def esta_excluido(cur, tipo, perfil_id):
    """True quando este perfil não concorre (conta de teste, sócio, etc.).

    ⚠️ O REGULAMENTO PUBLICADO EXCLUI gente, e até 04/10/2026 o código não
    excluía ninguém. Regulamento dizendo uma coisa e sistema fazendo outra é
    exatamente o que invalida um sorteio depois — e seria descoberto no pior
    momento, com o prêmio na mesa.

    A lista mora em `rifa_exclusoes`, com MOTIVO, porque no dia da apuração
    pode ser preciso mostrar por que fulano não estava concorrendo.
    """
    try:
        cur.execute("""
            SELECT 1 FROM rifa_exclusoes WHERE tipo = %s AND perfil_id = %s
        """, (tipo, str(perfil_id)))
        return cur.fetchone() is not None
    except Exception:
        # Na dúvida NÃO exclui: tirar quem tem direito é pior que deixar um a
        # mais, e o admin ainda consegue conferir a lista antes do sorteio.
        logger.warning("[RIFA] checagem de exclusao falhou: %s %s", tipo, perfil_id, exc_info=True)
        return False


def _ja_concedidos(cur, campanha, tipo, perfil_id):
    cur.execute("""
        SELECT COUNT(*) AS n FROM rifa_numeros
         WHERE campanha = %s AND tipo = %s AND perfil_id = %s
           AND origem = %s AND cancelado_em IS NULL
    """, (campanha, tipo, str(perfil_id), _ORIGEM_POR_TIPO[tipo]))
    return int((cur.fetchone() or {'n': 0})['n'])


def _base_valida(cur, tipo, perfil_id):
    """Quanto esta pessoa acumulou de valido — reais, ou entregas.

    'Valido' = entregue e nao arquivado, e sem os pedidos em que o cliente e o
    dono da loja (a arbitragem). Para o entregador, entrega propria fica de
    fora: nela nao houve entregador Inksa.
    """
    sem_arbitragem = """
        AND NOT EXISTS (
            SELECT 1 FROM client_profiles cp2, restaurant_profiles rp2
             WHERE cp2.id = o.client_id AND rp2.id = o.restaurant_id
               AND (cp2.user_id = rp2.user_id
                    OR (NULLIF(TRIM(cp2.phone),'') IS NOT NULL
                        AND TRIM(cp2.phone) = TRIM(rp2.phone))))
    """
    if tipo == 'cliente':
        cur.execute(f"""
            SELECT COALESCE(SUM(o.total_amount_items), 0) AS base
              FROM orders o
             WHERE o.client_id = %s AND o.status = 'delivered'
               AND o.archived_at IS NULL {sem_arbitragem}
        """, (str(perfil_id),))
    elif tipo == 'parceiro':
        cur.execute(f"""
            SELECT COALESCE(SUM(o.total_amount_items), 0) AS base
              FROM orders o
             WHERE o.restaurant_id = %s AND o.status = 'delivered'
               AND o.archived_at IS NULL {sem_arbitragem}
        """, (str(perfil_id),))
    else:  # entregador — conta ENTREGAS, nao reais
        cur.execute("""
            SELECT COUNT(*) AS base
              FROM orders o
              JOIN restaurant_profiles rp ON rp.id = o.restaurant_id
             WHERE o.delivery_id = %s AND o.status = 'delivered'
               AND o.archived_at IS NULL
               AND COALESCE(rp.delivery_type, 'platform') <> 'own'
        """, (str(perfil_id),))
    return float((cur.fetchone() or {'base': 0})['base'] or 0)


def _teto_batido(cur, camp, tipo, perfil_id):
    """True se a pessoa ja atingiu o teto de numeros DESTE mes."""
    teto = int(camp['teto_numeros_mes'] or 0)
    if teto <= 0:
        return False
    cur.execute("""
        SELECT COUNT(*) AS n FROM rifa_numeros
         WHERE campanha = %s AND tipo = %s AND perfil_id = %s
           AND cancelado_em IS NULL
           AND (created_at AT TIME ZONE 'America/Sao_Paulo') >=
               date_trunc('month', NOW() AT TIME ZONE 'America/Sao_Paulo')
    """, (camp['campanha'], tipo, str(perfil_id)))
    return int((cur.fetchone() or {'n': 0})['n']) >= teto


def sincronizar(cur, tipo, perfil_id):
    """Acerta os numeros desta pessoa com a realidade. Devolve o saldo mexido.

    Positivo = concedeu; negativo = cancelou (pedido estornado/cancelado);
    0 = ja estava certo. Chamar de novo nao faz nada — e o ponto.
    """
    camp = _campanha(cur)
    if not camp or tipo not in _ORIGEM_POR_TIPO:
        return 0
    if esta_excluido(cur, tipo, perfil_id):
        return 0
    try:
        base = _base_valida(cur, tipo, perfil_id)
        por = float(camp['reais_por_numero'] or 50)
        devidos = int(base) if tipo == 'entregador' else int(math.floor(base / por)) if por > 0 else 0
        ja = _ja_concedidos(cur, camp['campanha'], tipo, perfil_id)

        if devidos > ja:
            if _teto_batido(cur, camp, tipo, perfil_id):
                logger.info("[RIFA] teto do mes atingido: %s %s", tipo, perfil_id)
                return 0
            faltam = devidos - ja
            inicio = _alocar(cur, camp['campanha'], faltam)
            if inicio is None:
                return 0
            origem = _ORIGEM_POR_TIPO[tipo]
            cur.executemany("""
                INSERT INTO rifa_numeros
                    (campanha, numero, tipo, perfil_id, origem, valor_base, ordinal)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
            """, [(camp['campanha'], inicio + i, tipo, str(perfil_id), origem,
                   None if tipo == 'entregador' else por, ja + i + 1)
                  for i in range(faltam)])
            return faltam

        if devidos < ja:
            # Pedido cancelado/estornado derrubou a base. Cancela o EXCEDENTE,
            # do numero mais alto pro mais baixo — nunca apaga a linha, porque
            # sorteio precisa de trilha.
            sobra = ja - devidos
            cur.execute("""
                UPDATE rifa_numeros
                   SET cancelado_em = NOW(), motivo_cancel = 'pedido cancelado ou estornado'
                 WHERE id IN (
                    SELECT id FROM rifa_numeros
                     WHERE campanha = %s AND tipo = %s AND perfil_id = %s
                       AND origem = %s AND cancelado_em IS NULL
                     ORDER BY numero DESC LIMIT %s)
            """, (camp['campanha'], tipo, str(perfil_id), _ORIGEM_POR_TIPO[tipo], sobra))
            return -sobra

        return 0
    except Exception:
        logger.warning("[RIFA] sincronizar falhou: %s %s", tipo, perfil_id, exc_info=True)
        return 0


def conceder_cadastro(cur, tipo, perfil_id):
    """O numero de boas-vindas. Um por pessoa, por campanha.

    O indice unico parcial no banco garante o 'um so' mesmo se esta funcao for
    chamada duas vezes (cadastro tem retentativa).
    """
    camp = _campanha(cur)
    if not camp or not camp['numero_no_cadastro'] or tipo not in _ORIGEM_POR_TIPO:
        return 0
    if esta_excluido(cur, tipo, perfil_id):
        return 0
    try:
        cur.execute("""
            SELECT 1 FROM rifa_numeros
             WHERE campanha = %s AND tipo = %s AND perfil_id = %s AND origem = 'cadastro'
        """, (camp['campanha'], tipo, str(perfil_id)))
        if cur.fetchone():
            return 0
        n = _alocar(cur, camp['campanha'], 1)
        if n is None:
            return 0
        cur.execute("""
            INSERT INTO rifa_numeros (campanha, numero, tipo, perfil_id, origem)
            VALUES (%s, %s, %s, %s, 'cadastro')
            ON CONFLICT DO NOTHING
        """, (camp['campanha'], n, tipo, str(perfil_id)))
        return 1
    except Exception:
        logger.warning("[RIFA] cadastro falhou: %s %s", tipo, perfil_id, exc_info=True)
        return 0


def distribuir_cadastros(cur):
    """Dá o número de boas-vindas a TODO cadastro que ainda não tem.

    ⚠️ EXISTE PORQUE `conceder_cadastro` SOZINHO NÃO RESOLVE, e eu só percebi
    isso depois de escrever a função (04/10/2026). Ela serve para quem se
    cadastra COM a campanha no ar — mas, no dia em que a campanha liga, já
    existem dezenas de cadastrados, e eles nunca passariam por lá. O resultado
    seria ligar a campanha e as telas aparecerem vazias para todo mundo.

    Esta função é a passagem em massa, e é idempotente: o índice único parcial
    (`origem = 'cadastro'`) recusa a segunda tentativa, então rodar de novo não
    duplica nada e não precisa saber quem já recebeu.

    Roda quando a campanha é LIGADA e, depois disso, periodicamente — assim
    quem se cadastrar amanhã também recebe sem depender de um gancho em três
    telas de cadastro diferentes.

    Devolve quantos números concedeu.
    """
    camp = _campanha(cur)
    if not camp or not camp['numero_no_cadastro']:
        return 0

    tabelas = {
        'cliente': 'client_profiles',
        'parceiro': 'restaurant_profiles',
        'entregador': 'delivery_profiles',
    }
    total = 0
    for tipo, tabela in tabelas.items():
        try:
            # Quem AINDA não tem o número de cadastro, em ordem estável —
            # a ordem decide quais números cada um leva, e ordem instável faria
            # a mesma lista sair diferente a cada execução.
            cur.execute(f"""
                SELECT p.id FROM {tabela} p
                 WHERE NOT EXISTS (
                    SELECT 1 FROM rifa_numeros r
                     WHERE r.campanha = %s AND r.tipo = %s
                       AND r.perfil_id = p.id AND r.origem = 'cadastro')
                   -- Quem o regulamento exclui (conta de teste, sócio) não
                   -- entra nem no número de boas-vindas.
                   AND NOT EXISTS (
                    SELECT 1 FROM rifa_exclusoes e
                     WHERE e.tipo = %s AND e.perfil_id = p.id)
                 ORDER BY p.created_at NULLS LAST, p.id
            """, (camp['campanha'], tipo, tipo))
            faltantes = [r['id'] for r in cur.fetchall()]
            if not faltantes:
                continue

            inicio = _alocar(cur, camp['campanha'], len(faltantes))
            if inicio is None:
                continue
            cur.executemany("""
                INSERT INTO rifa_numeros (campanha, numero, tipo, perfil_id, origem)
                VALUES (%s, %s, %s, %s, 'cadastro')
                ON CONFLICT DO NOTHING
            """, [(camp['campanha'], inicio + i, tipo, str(pid))
                  for i, pid in enumerate(faltantes)])
            total += len(faltantes)
            logger.info("[RIFA] %d numeros de cadastro para %s", len(faltantes), tipo)
        except Exception:
            logger.warning("[RIFA] distribuir_cadastros falhou em %s", tipo, exc_info=True)
    return total


def sincronizar_do_pedido(cur, order_id):
    """UM ponto de entrada para o pedido inteiro — as tres pontas de uma vez.

    Existe para que os varios caminhos que fecham um pedido (entrega pela
    plataforma, entrega propria, cancelamento, estorno) chamem a MESMA linha.
    Regra que entra num caminho so vira buraco nesta base; ja aconteceu.
    """
    camp = _campanha(cur)
    if not camp:
        return {}
    try:
        cur.execute("""
            SELECT client_id, restaurant_id, delivery_id FROM orders WHERE id = %s
        """, (str(order_id),))
        o = cur.fetchone()
        if not o:
            return {}
        r = {}
        if o['client_id']:
            r['cliente'] = sincronizar(cur, 'cliente', o['client_id'])
        if o['restaurant_id']:
            r['parceiro'] = sincronizar(cur, 'parceiro', o['restaurant_id'])
        if o['delivery_id']:
            r['entregador'] = sincronizar(cur, 'entregador', o['delivery_id'])
        return r
    except Exception:
        logger.warning("[RIFA] sincronizar_do_pedido falhou (%s)", order_id, exc_info=True)
        return {}


_TABELA_DO_TIPO = {
    'cliente': 'client_profiles',
    'parceiro': 'restaurant_profiles',
    'entregador': 'delivery_profiles',
}


def estado_publico(cur):
    """O que da pra contar a QUEM AINDA NAO TEM CONTA.

    POR QUE ISSO EXISTE (09/10/2026): a rota /api/client/rifa exigia token.
    Visitante tomava 401, o app engolia o erro em silencio e tratava como
    "campanha desligada", entao o link sumia. Resultado: a rifa — que existe
    pra FAZER a pessoa se cadastrar — so era visivel DEPOIS do cadastro.
    Nos 4 primeiros dias no ar ela trouxe 1 pessoa, e os 83 numeros emitidos
    eram todos retroativos pra quem ja estava cadastrado. Nenhum novo.

    Nao devolve dado de ninguem: so o que ja esta no regulamento publico.
    Numero de ninguem sai daqui — isso continua exigindo token.
    """
    camp = _campanha(cur)
    if not camp:
        return {"ligada": False, "numeros": [], "total": 0}
    return {
        "ligada": True,
        "visitante": True,          # a tela usa isso pra chamar pro cadastro
        "campanha": camp["campanha"],
        "nome": camp.get("nome"),
        "premio": camp.get("premio"),
        "reais_por_numero": float(camp["reais_por_numero"])
                            if camp["reais_por_numero"] is not None else None,
        "numero_no_cadastro": bool(camp["numero_no_cadastro"]),
        "numeros": [],
        "total": 0,
    }


def meus_numeros_por_user(cur, tipo, user_id):
    """Igual a `meus_numeros`, resolvendo o perfil a partir do user_id do token.

    Existe pra que as três rotas (um app cada) fiquem em poucas linhas e
    idênticas entre si — a diferença entre elas é só de qual tabela sai o
    perfil, e isso mora AQUI em vez de repetido três vezes.
    """
    tabela = _TABELA_DO_TIPO.get(tipo)
    if not tabela:
        return {"ligada": False, "numeros": [], "total": 0}
    try:
        cur.execute(f"SELECT id FROM {tabela} WHERE user_id = %s LIMIT 1", (str(user_id),))
        row = cur.fetchone()
        if not row:
            return {"ligada": False, "numeros": [], "total": 0}
        return meus_numeros(cur, tipo, row['id'])
    except Exception:
        logger.warning("[RIFA] meus_numeros_por_user falhou: %s %s", tipo, user_id, exc_info=True)
        return {"ligada": False, "numeros": [], "total": 0}


def meus_numeros(cur, tipo, perfil_id):
    """O que a tela do app mostra: os numeros validos e de onde vieram."""
    camp = _campanha(cur)
    if not camp or tipo not in _ORIGEM_POR_TIPO:
        return {"ligada": False, "numeros": [], "total": 0}
    try:
        cur.execute("""
            SELECT numero, origem, valor_base, created_at
              FROM rifa_numeros
             WHERE campanha = %s AND tipo = %s AND perfil_id = %s
               AND cancelado_em IS NULL
             ORDER BY numero
        """, (camp['campanha'], tipo, str(perfil_id)))
        linhas = [dict(x) for x in cur.fetchall()]
        for l in linhas:
            l['created_at'] = l['created_at'].isoformat() if l['created_at'] else None
            l['valor_base'] = float(l['valor_base']) if l['valor_base'] is not None else None
        return {"ligada": True, "campanha": camp['campanha'],
                "numeros": linhas, "total": len(linhas)}
    except Exception:
        logger.warning("[RIFA] meus_numeros falhou: %s %s", tipo, perfil_id, exc_info=True)
        return {"ligada": False, "numeros": [], "total": 0}
