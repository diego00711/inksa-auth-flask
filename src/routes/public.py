# src/routes/public.py
# Endpoints publicos (sem autenticacao) - leitura de configuracoes que os apps Cliente/Restaurante/Entregador consomem.

import logging
from datetime import datetime, timedelta

import psycopg2.extras
from flask import Blueprint, jsonify, request

from ..utils.helpers import get_db_connection, get_user_id_from_token
from src.extensions import limiter

logger = logging.getLogger(__name__)
public_bp = Blueprint("public_bp", __name__)


@public_bp.get("/support-info")
def public_support_info():
    """Retorna informacoes de contato/suporte da plataforma. Sem autenticacao."""
    conn = get_db_connection()
    if not conn:
        return jsonify({
            "email": "suporte@inksadelivery.com.br",
            "whatsapp": "5549999679697",
            "phone": "(49) 99967-9697",
            "hours": "Seg a Sex, 8h às 18h",
            "platform_name": "Inksa Delivery",
        }), 200
    try:
        keys = ("contact_email", "contact_whatsapp", "contact_phone", "support_hours", "platform_name")
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT key, value FROM platform_settings WHERE key = ANY(%s)", (list(keys),))
            rows = {r["key"]: r["value"] for r in cur.fetchall()}
        return jsonify({
            "email": rows.get("contact_email") or "suporte@inksadelivery.com.br",
            "whatsapp": rows.get("contact_whatsapp") or "5549999679697",
            "phone": rows.get("contact_phone") or "(49) 99967-9697",
            "hours": rows.get("support_hours") or "Seg a Sex, 8h às 18h",
            "platform_name": rows.get("platform_name") or "Inksa Delivery",
        }), 200
    except Exception:
        logger.exception("Erro em public_support_info")
        return jsonify({
            "email": "suporte@inksadelivery.com.br",
            "whatsapp": "5549999679697",
            "phone": "(49) 99967-9697",
            "hours": "Seg a Sex, 8h às 18h",
            "platform_name": "Inksa Delivery",
        }), 200
    finally:
        conn.close()


@public_bp.get("/app-config")
def public_app_config():
    """Config de comportamento dos apps (sem autenticacao).

    Hoje expoe apenas `idle_logout_minutes` (logoff automatico por inatividade
    nos apps Parceiro/Entregador). 0 = recurso desligado. Editavel no admin em
    Configuracoes. Extensivel para outras flags de UX no futuro."""
    default_idle = 60
    conn = get_db_connection()
    if not conn:
        return jsonify({"idle_logout_minutes": default_idle}), 200
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT value FROM platform_settings WHERE key = %s",
                ("idle_logout_minutes",),
            )
            row = cur.fetchone()
        try:
            val = int(float((row["value"] if row else None) or default_idle))
        except (TypeError, ValueError):
            val = default_idle
        # Sanidade: 0 = desligado; senao entre 5 min e 24h.
        if val != 0:
            val = max(5, min(val, 1440))
        return jsonify({"idle_logout_minutes": val}), 200
    except Exception:
        logger.exception("Erro em public_app_config")
        return jsonify({"idle_logout_minutes": default_idle}), 200
    finally:
        conn.close()


@public_bp.get("/social-day")
def public_social_day():
    """
    Status do Dia I (Inksa Social) + valor arrecadado na janela do evento.

    Config em platform_settings (setada na pagina admin "Inksa Social"):
      social_day_date          YYYY-MM-DD
      social_day_start         HH:MM
      social_day_end           HH:MM
      social_day_show_in_apps  'true' | 'false'

    "Arrecadado" = receita real da plataforma na janela (mesma formula do
    dashboard admin): SUM(comissao_plataforma) + SUM(margem_frete) dos pedidos
    delivered/completed criados dentro da janela, no fuso America/Sao_Paulo.

    Sem token: so devolve dados quando show_in_apps = true (e esconde o banner
    24h depois do fim). Com token de ADMIN devolve sempre — a pagina do admin
    usa este mesmo endpoint para o painel ao vivo.
    """
    conn = get_db_connection()
    if not conn:
        return jsonify({"configured": False, "visible": False}), 200
    try:
        keys = ("social_day_date", "social_day_start", "social_day_end",
                "social_day_show_in_apps", "social_nominations_open")
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("SELECT key, value FROM platform_settings WHERE key = ANY(%s)", (list(keys),))
            cfg = {r["key"]: (r["value"] or "").strip() for r in cur.fetchall()}

            # Janela de indicação: quem a cidade acha que deve receber o lucro.
            #
            # VAI EM TODA RESPOSTA, inclusive nas que escondem o banner. O
            # momento natural de abrir indicação é justamente quando o Dia I
            # anterior já passou e o banner se escondeu sozinho — se o campo só
            # viesse na resposta completa, abrir as indicações não mostraria
            # nada em app nenhum. Foi o que aconteceu no primeiro teste.
            nominations_open = (cfg.get("social_nominations_open") or "").lower() == "true"

            date_str = cfg.get("social_day_date") or ""
            if not date_str:
                return jsonify({"configured": False, "visible": False,
                                "nominations_open": nominations_open}), 200

            start_str = cfg.get("social_day_start") or "00:00"
            end_str = cfg.get("social_day_end") or "23:59"
            show_in_apps = (cfg.get("social_day_show_in_apps") or "").lower() == "true"

            # Admin autenticado ve o status mesmo com a exibicao desligada
            is_admin = False
            if request.headers.get("Authorization"):
                try:
                    _uid, utype, err = get_user_id_from_token(request.headers.get("Authorization"))
                    is_admin = (err is None and utype == "admin")
                except Exception:
                    is_admin = False

            # Janela e "agora" no fuso de Sao Paulo (created_at e timestamptz/UTC)
            cur.execute("SELECT (now() AT TIME ZONE 'America/Sao_Paulo') AS now_sp")
            now_sp = cur.fetchone()["now_sp"]
            try:
                win_start = datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %H:%M")
                win_end = datetime.strptime(f"{date_str} {end_str}", "%Y-%m-%d %H:%M")
            except ValueError:
                return jsonify({"configured": False, "visible": False,
                                "nominations_open": nominations_open}), 200
            if win_end < win_start:
                win_end = win_start

            if now_sp < win_start:
                phase = "scheduled"
            elif now_sp <= win_end:
                phase = "live"
            else:
                phase = "ended"

            # Apps mostram o banner ate 24h depois do fim; admin sempre ve
            expired = phase == "ended" and now_sp > (win_end + timedelta(hours=24))
            visible = show_in_apps and not expired
            if not visible and not is_admin:
                return jsonify({"configured": True, "visible": False,
                                "nominations_open": nominations_open}), 200

            raised = 0.0
            orders_count = 0
            commission = 0.0
            margin = 0.0
            if phase != "scheduled":
                cur.execute(
                    """
                    SELECT COALESCE(SUM(comissao_plataforma), 0) AS commission,
                           COALESCE(SUM(margem_frete), 0)        AS margin,
                           COUNT(*)                              AS orders_count
                      FROM orders
                     WHERE status IN ('delivered', 'completed')
                       AND (created_at AT TIME ZONE 'America/Sao_Paulo') >= %s
                       AND (created_at AT TIME ZONE 'America/Sao_Paulo') <= %s
                    """,
                    (win_start, win_end),
                )
                row = cur.fetchone() or {}
                commission = float(row.get("commission") or 0)
                margin = float(row.get("margin") or 0)
                orders_count = int(row.get("orders_count") or 0)
                # margem_frete pode ser negativa em casos residuais; o contador
                # publico nunca mostra valor negativo
                raised = round(max(commission + margin, 0.0), 2)

        payload = {
            "configured": True,
            "visible": visible,
            "phase": phase,
            "date": date_str,
            "start_time": start_str,
            "end_time": end_str,
            "raised": raised,
            "orders_count": orders_count,
            "nominations_open": nominations_open,
        }
        if is_admin:
            payload["breakdown"] = {"commission": round(commission, 2), "margin": round(margin, 2)}
            payload["show_in_apps"] = show_in_apps
        return jsonify(payload), 200
    except Exception:
        logger.exception("Erro em public_social_day")
        return jsonify({"configured": False, "visible": False}), 200
    finally:
        conn.close()


@public_bp.get("/social-day/history")
def public_social_day_history():
    """
    Historico publico dos Dias I ja realizados (prestacao de contas).
    Alimentado pelo admin em Inksa Social -> Prestacao de contas.
    """
    conn = get_db_connection()
    if not conn:
        return jsonify({"events": [], "total_raised": 0}), 200
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                """
                SELECT id, event_date, start_time, end_time, raised, orders_count,
                       destination, proof_url
                  FROM social_day_events
                 ORDER BY event_date DESC, created_at DESC
                """
            )
            rows = cur.fetchall()
        events = [{
            "id": str(r["id"]),
            "date": r["event_date"].isoformat() if r["event_date"] else None,
            "start_time": r["start_time"],
            "end_time": r["end_time"],
            "raised": float(r["raised"] or 0),
            "orders_count": int(r["orders_count"] or 0),
            "destination": r["destination"] or "",
            "proof_url": r["proof_url"] or "",
        } for r in rows]
        total = round(sum(e["raised"] for e in events), 2)
        return jsonify({"events": events, "total_raised": total}), 200
    except Exception:
        logger.exception("Erro em public_social_day_history")
        return jsonify({"events": [], "total_raised": 0}), 200
    finally:
        conn.close()


@public_bp.get("/reverse-geocode")
@limiter.limit("30 per minute")
def reverse_geocode_endpoint():
    """Coordenada → endereço curto, para o checkout do Cliente.

    POR QUE ISTO EXISTE NO BACKEND. Até 16/08/2026 o app chamava o Nominatim
    DIRETO do navegador. Três problemas nisso:

      • A política do Nominatim exige um User-Agent identificando quem chama,
        e o navegador não deixa definir User-Agent num fetch. Ou seja: a gente
        usava um serviço comunitário sem se identificar, sujeito a bloqueio
        por origem — e o sintoma do bloqueio é MUDO (o endereço volta a
        aparecer como coordenada).
      • Sem cache: a mesma pessoa mexendo no carrinho disparava a mesma
        consulta várias vezes.
      • E, no dia em que virar serviço pago, a chave estaria no bundle do app,
        visível pra qualquer um gastar a cota.

    Aqui resolve os três de uma vez, e trocar de provedor não exige AAB novo.
    Ver a nota no topo de src/utils/geocoding_utils.py.

    Devolve 200 com endereco=null quando não dá pra resolver — o app cai no
    "Minha localização (lat, lng)". Nunca 500: endereço é conveniência, e
    derrubar o checkout por causa de um serviço externo seria trocar um
    problema pequeno por um grande.
    """
    from ..utils.geocoding_utils import reverse_geocode

    lat = request.args.get("lat")
    lng = request.args.get("lng") or request.args.get("lon")
    if lat in (None, "") or lng in (None, ""):
        return jsonify({"status": "error", "error": "lat e lng são obrigatórios"}), 400

    endereco = reverse_geocode(lat, lng)
    return jsonify({"status": "success", "data": {"endereco": endereco}}), 200


@public_bp.get("/geocode")
@limiter.limit("30 per minute")
def geocode_endpoint():
    """Endereço → coordenada, para centralizar o mapa no cadastro de endereço.

    Mesma razão da rota acima: era chamada direta do navegador ao Nominatim,
    sem User-Agent e sem cache. Divide o mesmo limite e vai trocar de provedor
    junto — ver a nota no topo de src/utils/geocoding_utils.py.

    Devolve lat/lng nulos quando não acha, e o cliente continua podendo
    marcar o ponto na mão no mapa.
    """
    from ..utils.geocoding_utils import geocode_cached

    lat, lng = geocode_cached(
        request.args.get("street"),
        request.args.get("number"),
        request.args.get("neighborhood"),
        request.args.get("city"),
        request.args.get("state"),
        request.args.get("zipcode"),
    )
    return jsonify({"status": "success", "data": {"lat": lat, "lng": lng}}), 200


# ---------------------------------------------------------------------------
# Números públicos — o contador do site institucional
# ---------------------------------------------------------------------------

# Abaixo deste total, o site NÃO mostra a seção. Não é maquiagem: é a diferença
# entre não falar de número ainda e falar um número que trabalha contra a
# Inksa. Um dono de restaurante que abre o site e lê "34 pessoas" fecha a aba —
# o mesmo número, daqui a alguns meses, vende sozinho.
#
# Fica em platform_settings (site_numeros_minimo) pra ser mudado sem deploy.
# Zero = mostrar sempre, seja qual for o número.
_MINIMO_PADRAO = 300


@public_bp.get("/numeros")
def public_numeros():
    """Quantas pessoas já estão dentro da Inksa. Sem autenticação.

    Conta CADASTROS, não sessões: é o único número que a gente pode afirmar
    sem asterisco. Cliente, entregador e parceiro somam no total porque os
    três são gente que criou conta aqui.

    `publicar` é a decisão, não o dado. O site respeita: se vier false, a
    seção inteira some da página em vez de mostrar número pequeno.
    """
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "success", "publicar": False}), 200
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT
                  (SELECT count(*)::int FROM client_profiles)   AS clientes,
                  (SELECT count(*)::int FROM delivery_profiles) AS entregadores,
                  (SELECT count(*)::int FROM restaurant_profiles
                    WHERE COALESCE(approved,false) AND COALESCE(active,false)) AS parceiros,
                  (SELECT count(*)::int FROM orders
                    WHERE status = 'delivered')                 AS entregues
            """)
            r = cur.fetchone() or {}

        clientes = int(r.get("clientes") or 0)
        entregadores = int(r.get("entregadores") or 0)
        parceiros = int(r.get("parceiros") or 0)
        total = clientes + entregadores + parceiros

        try:
            from ..utils.platform_settings import get_settings
            minimo = int(float(get_settings().get("site_numeros_minimo") or _MINIMO_PADRAO))
        except Exception:
            minimo = _MINIMO_PADRAO

        return jsonify({
            "status": "success",
            "publicar": total >= minimo,
            "total": total,
            "clientes": clientes,
            "entregadores": entregadores,
            "parceiros": parceiros,
            "entregues": int(r.get("entregues") or 0),
        }), 200
    except Exception:
        logger.exception("Erro ao contar números públicos")
        # Falha silenciosa: o site simplesmente não mostra a seção. Melhor um
        # site sem contador que um contador escrito "0".
        return jsonify({"status": "success", "publicar": False}), 200
    finally:
        try: conn.close()
        except Exception: pass


# ---------------------------------------------------------------------------
# Prestação de contas — para onde foi o dinheiro dos pedidos
# ---------------------------------------------------------------------------

# Mesma lógica do contador: existe, calcula sozinho, e só publica quando tem o
# que mostrar. Abaixo disto a porcentagem é ruído — com 3 pedidos, um pedido
# grande sozinho entorta o gráfico inteiro.
_MIN_PEDIDOS_PADRAO = 30


@public_bp.get("/transparencia")
def public_transparencia():
    """O split real de tudo que já foi entregue. Sem autenticação.

    A conta que o site mostra hoje como EXEMPLO (um pedido de R$100) vira aqui
    a conta somada de verdade. É a única página do site em que a Inksa mostra
    número próprio, então ela tem duas obrigações:

    1. FECHAR. As três fatias somam exatamente o que o cliente pagou, porque a
       fatia da Inksa é calculada por diferença (total − parceiro − entregador)
       e não por outra coluna. Se algum dia um campo novo entrar no meio do
       cálculo, essa conta continua fechando em vez de sobrar um resto que
       ninguém explica. Prestação de contas que não bate é pior que nenhuma.

    2. NÃO SE CONFUNDIR COM CAIXA. Isto é para onde foi o dinheiro DOS PEDIDOS,
       não o extrato da Inksa. Em pedido pago em dinheiro o cliente paga o
       entregador na mão e a comissão vira dívida — o dinheiro nunca passa pela
       conta da empresa. Chamar isso de receita seria mentira contábil.

    Do que fica com a Inksa ainda saem gateway, servidor e imposto. A página
    diz isso com todas as letras; esconder inverteria o sentido de existir dela.
    """
    conn = get_db_connection()
    if not conn:
        return jsonify({"status": "success", "publicar": False}), 200
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("""
                SELECT count(*)::int                                   AS pedidos,
                       COALESCE(SUM(total_amount), 0)                  AS movimentado,
                       COALESCE(SUM(valor_repassado_restaurante), 0)   AS parceiros,
                       COALESCE(SUM(valor_repassado_entregador), 0)    AS entregadores
                  FROM orders
                 WHERE status = 'delivered'
            """)
            o = cur.fetchone() or {}

            # Dia I: o que já foi doado. Vem da mesma tabela que alimenta a
            # página pública do evento, então os dois números não divergem.
            cur.execute("SELECT COALESCE(SUM(raised), 0) AS doado FROM social_day_events")
            d = cur.fetchone() or {}

        pedidos = int(o.get("pedidos") or 0)
        movimentado = float(o.get("movimentado") or 0)
        parceiros = float(o.get("parceiros") or 0)
        entregadores = float(o.get("entregadores") or 0)
        # Por diferença, nunca por outra coluna — ver a obrigação 1 acima.
        inksa = max(0.0, movimentado - parceiros - entregadores)

        try:
            from ..utils.platform_settings import get_settings
            minimo = int(float(get_settings().get("site_transparencia_minimo")
                               or _MIN_PEDIDOS_PADRAO))
        except Exception:
            minimo = _MIN_PEDIDOS_PADRAO

        def pct(v):
            return round(100.0 * v / movimentado, 1) if movimentado > 0 else 0.0

        return jsonify({
            "status": "success",
            "publicar": pedidos >= minimo and movimentado > 0,
            "pedidos": pedidos,
            "movimentado": round(movimentado, 2),
            "parceiros": round(parceiros, 2),
            "entregadores": round(entregadores, 2),
            "inksa": round(inksa, 2),
            "pct_parceiros": pct(parceiros),
            "pct_entregadores": pct(entregadores),
            "pct_inksa": pct(inksa),
            "doado": round(float(d.get("doado") or 0), 2),
        }), 200
    except Exception:
        logger.exception("Erro ao montar a prestação de contas")
        return jsonify({"status": "success", "publicar": False}), 200
    finally:
        try: conn.close()
        except Exception: pass
