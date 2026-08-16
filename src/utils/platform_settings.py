"""
Leitura de configurações da plataforma (tabela platform_settings) com cache em memória.

A tabela tem schema chave/valor (TEXT). Esta camada interpreta os tipos
para o restante do backend e mantém um cache simples por TTL para evitar
ir ao Postgres a cada cálculo de frete/repasse.

Uso:
    from src.utils.platform_settings import get_settings, get_decimal, invalidate_cache
    s = get_settings()
    fixed_fee = s["fixed_delivery_fee"]  # Decimal
"""
import logging
import os
import threading
import time
from decimal import Decimal, InvalidOperation

from .helpers import get_db_connection

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = int(os.environ.get("PLATFORM_SETTINGS_TTL", "60"))
_cache: dict | None = None
_cache_expires_at: float = 0.0
_lock = threading.Lock()


# Valores tipados que o restante do código consome.
# Defaults batem com os valores semeados na migration.
_DEFAULTS: dict[str, Decimal] = {
    "fixed_delivery_fee":         Decimal("3.00"),
    "per_km_delivery_fee":        Decimal("1.50"),
    "free_delivery_threshold_km": Decimal("2.00"),
    "commission_rate":            Decimal("0.10"),  # 0..1, não 10
    "delivery_base_fee":          Decimal("5.00"),  # legado (modelo antigo de repasse)
    "delivery_per_km_fee":        Decimal("1.00"),  # legado (modelo antigo de repasse)
    # Taxa de administração retida pela plataforma sobre o frete (0..1).
    # O entregador recebe o frete integral MENOS esta taxa. Editável no admin
    # (campo "Taxa de administração sobre o frete", key financial_delivery_commission).
    "financial_delivery_commission": Decimal("0.15"),
    # Raio de atendimento em km (chave já existente e editável no admin em
    # "Raio máximo de entrega"). O cliente só vê restaurantes dentro deste raio
    # dele, e o entregador só vê pedidos cujo restaurante está dentro deste raio
    # dele. É o que separa as cidades sem misturar tudo (cada um vê o que é da
    # sua região).
    "platform_max_delivery_radius": Decimal("15"),
    # Raio POR TIPO DE VEÍCULO (km). Bike alcança menos que moto/carro. Se um
    # deles ficar 0/vazio, cai no platform_max_delivery_radius acima. 'outro'
    # sempre usa o raio global. Editável no admin.
    "delivery_radius_bike_km":  Decimal("2"),
    "delivery_radius_moto_km":  Decimal("8"),
    "delivery_radius_carro_km": Decimal("10"),
    # Utilitário alcança mais: é quem faz a entrega grande, que compensa rodar.
    "delivery_radius_utilitario_km": Decimal("15"),
    # Motor de atribuição de pedidos. dispatch_assign_enabled: 0 = broadcast
    # (todos no raio veem, padrão atual), 1 = atribuição (oferta ao mais
    # próximo com timeout). offer_seconds: tempo da oferta. decline_cooldown_min:
    # minutos sem receber ofertas após RECUSAR.
    "dispatch_assign_enabled":      Decimal("1"),
    "dispatch_offer_seconds":       Decimal("30"),
    "dispatch_decline_cooldown_min": Decimal("15"),
    # PESOS da escolha do entregador (só ordenam quem JÁ passou nos filtros de
    # raio/cooldown/cadastro). Somam 100 por convenção, mas qualquer proporção
    # funciona — o que vale é o peso relativo. Editáveis no admin.
    #   distance = perto do restaurante (eficiência: cliente espera menos)
    #   idle     = há quanto tempo está sem entregar (justiça: quem está parado)
    #   rating   = nota das avaliações (qualidade do serviço)
    #   balance  = poucas entregas hoje (espalha a renda entre os entregadores)
    # Deixar distance=100 e o resto 0 reproduz o comportamento antigo
    # ("sempre o mais próximo").
    "dispatch_weight_distance":     Decimal("50"),
    "dispatch_weight_idle":         Decimal("20"),
    "dispatch_weight_rating":       Decimal("15"),
    "dispatch_weight_balance":      Decimal("15"),
    # Referências de normalização:
    # idle_target_minutes: parado por este tempo já vale nota máxima em "idle".
    # daily_target: nº de entregas no dia em que o bônus de "balance" zera.
    "dispatch_idle_target_minutes": Decimal("60"),
    "dispatch_daily_target":        Decimal("10"),
    # Nota atribuída a quem ainda NÃO tem avaliação. Sem isso o novato entraria
    # com nota 0 e nunca receberia pedido pra ser avaliado.
    "dispatch_default_rating":      Decimal("4"),
    # FRETE POR CLASSE DE VEÍCULO. Cobra-se pelo que o PEDIDO exige (peso), não
    # pelo veículo de quem aceita — o frete é cotado no checkout, antes de
    # existir entregador. O 'fixo' soma à taxa base e paga o trabalho de
    # carregar; o 'km' SUBSTITUI o per_km e paga o custo de rodar (carro faz
    # ~10 km/L contra ~35 da moto). Bike e moto não têm adicional: são a
    # referência que fixed_delivery_fee/per_km_delivery_fee já cobrem.
    "frete_adicional_carro":       Decimal("8.00"),
    "frete_km_carro":              Decimal("2.50"),
    "frete_adicional_utilitario":  Decimal("25.00"),
    "frete_km_utilitario":         Decimal("3.50"),
    # Teto de desconto (%) que o PARCEIRO pode criar no cupom dele. Trava de
    # segurança: o desconto sai do repasse dele, então um "90" digitado por
    # engano viraria prejuízo. Não limita os cupons criados pela Inksa.
    "coupon_max_discount_pct":      Decimal("30"),
    # Logoff automático por inatividade (minutos) nos apps Parceiro e Entregador.
    # 0/vazio = desliga o recurso. Editável no admin.
    "idle_logout_minutes":          Decimal("60"),
}


def _to_decimal(raw, default: Decimal) -> Decimal:
    if raw is None or raw == "":
        return default
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError):
        return default


def _normalize(rows: list[tuple[str, str]]) -> dict[str, Decimal]:
    """Converte rows (key, value) text→Decimal aplicando regras por campo."""
    raw = {k: v for k, v in rows}
    out: dict[str, Decimal] = dict(_DEFAULTS)

    # Campos em R$ ou km (números diretos)
    for k in ("fixed_delivery_fee", "per_km_delivery_fee", "free_delivery_threshold_km",
              "delivery_base_fee", "delivery_per_km_fee", "platform_max_delivery_radius",
              "delivery_radius_bike_km", "delivery_radius_moto_km", "delivery_radius_carro_km",
              "delivery_radius_utilitario_km",
              "dispatch_assign_enabled", "dispatch_offer_seconds", "dispatch_decline_cooldown_min",
              "dispatch_weight_distance", "dispatch_weight_idle", "dispatch_weight_rating",
              "dispatch_weight_balance", "dispatch_idle_target_minutes", "dispatch_daily_target",
              "dispatch_default_rating",
              "frete_adicional_carro", "frete_km_carro",
              "frete_adicional_utilitario", "frete_km_utilitario",
              "idle_logout_minutes"):
        out[k] = _to_decimal(raw.get(k), _DEFAULTS[k])

    # commission_rate é guardado como percentual humano (10 = 10%);
    # converte para fração 0..1 que o resto do código usa.
    raw_comm = raw.get("commission_rate")
    if raw_comm is not None and raw_comm != "":
        try:
            v = Decimal(str(raw_comm))
            # Heurística: valor > 1 significa que está em "percent humano" (ex: 10 = 10%).
            out["commission_rate"] = (v / Decimal("100")) if v > Decimal("1") else v
        except (InvalidOperation, TypeError):
            pass

    # financial_delivery_commission = taxa de administração sobre o frete.
    # Guardada SEMPRE como percentual humano (15 = 15%); converte p/ fração 0..1
    # e limita ao intervalo válido. Campo rotulado "(%)" no admin, sem heurística
    # ambígua (0,5 = 0,5%, não 50%).
    raw_adm = raw.get("financial_delivery_commission")
    if raw_adm is not None and raw_adm != "":
        try:
            v = Decimal(str(raw_adm)) / Decimal("100")
            if v < 0:
                v = Decimal("0")
            elif v > 1:
                v = Decimal("1")
            out["financial_delivery_commission"] = v
        except (InvalidOperation, TypeError):
            pass

    return out


def _load_from_db() -> dict[str, Decimal]:
    conn = get_db_connection()
    if not conn:
        logger.warning("platform_settings: DB indisponível, usando defaults")
        return dict(_DEFAULTS)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key, value FROM platform_settings WHERE key = ANY(%s)",
                (list(_DEFAULTS.keys()),),
            )
            rows = cur.fetchall()
        return _normalize(rows)
    except Exception:
        logger.exception("platform_settings: falha ao ler do DB, usando defaults")
        return dict(_DEFAULTS)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_settings() -> dict[str, Decimal]:
    """Retorna dict tipado com todos os settings de plataforma (cache TTL)."""
    global _cache, _cache_expires_at
    now = time.time()
    if _cache is not None and now < _cache_expires_at:
        return _cache
    with _lock:
        # double-check inside lock
        if _cache is not None and now < _cache_expires_at:
            return _cache
        _cache = _load_from_db()
        _cache_expires_at = now + _CACHE_TTL_SECONDS
        return _cache


def get_decimal(key: str) -> Decimal:
    """Atalho para um campo específico (com fallback no default)."""
    return get_settings().get(key, _DEFAULTS.get(key, Decimal("0")))


def invalidate_cache() -> None:
    """Força a próxima leitura a buscar no DB. Chamar após PUT /settings."""
    global _cache, _cache_expires_at
    with _lock:
        _cache = None
        _cache_expires_at = 0.0


# ---------------------------------------------------------------------------
# Domain helpers
# ---------------------------------------------------------------------------

def calculate_courier_payout(delivery_distance_km, delivery_fee=None) -> Decimal:
    """
    Calcula quanto o entregador recebe por uma entrega.

    Modelo atual: o entregador recebe o FRETE INTEGRAL menos a taxa de
    administração retida pela plataforma (financial_delivery_commission,
    editável no admin):

        repasse = delivery_fee * (1 - taxa_administracao)

    Assim a margem da plataforma sobre o frete é sempre uma fração POSITIVA do
    frete (delivery_fee * taxa), nunca negativa, em qualquer distância. Isto
    substituiu o modelo antigo (delivery_base_fee + delivery_per_km_fee * km),
    que descasava do frete cobrado do cliente e gerava subsídio em entregas
    curtas.

    `delivery_distance_km` é mantido na assinatura por compatibilidade com os
    chamadores, mas não é mais usado aqui (o frete cobrado, passado em
    `delivery_fee`, já embute a distância).
    """
    s = get_settings()
    try:
        fee = Decimal(str(delivery_fee)) if delivery_fee is not None else None
    except (InvalidOperation, TypeError):
        fee = None

    if fee is None or fee < 0:
        return Decimal("0.00")

    admin_rate = s["financial_delivery_commission"]  # fração 0..1
    payout = fee * (Decimal("1") - admin_rate)
    if payout < 0:
        payout = Decimal("0")
    return payout.quantize(Decimal("0.01"))


def founding_commission_factor(restaurant_id) -> Decimal:
    """Fator multiplicador da comissão para a campanha "Parceiro Fundador".

    Retorna o fator promocional (ex.: 0.5 = metade) quando o restaurante está
    marcado como `fundador` E a campanha ainda está no prazo (data fixa global
    `founding_partner_until`, comparada no fuso America/Sao_Paulo). Caso
    contrário retorna 1 (comissão cheia).

    Fail-safe: sem restaurante, sem data definida, campanha expirada ou qualquer
    erro → 1 (nunca dá desconto por engano).
    """
    if not restaurant_id:
        return Decimal("1")
    conn = get_db_connection()
    if not conn:
        return Decimal("1")
    try:
        with conn.cursor() as cur:
            # A janela é POR PARCEIRO (fundador_ate, carimbada na marcação).
            # A data global `founding_partner_until` fica só como retaguarda
            # pra quem foi marcado antes de a coluna existir.
            cur.execute(
                """SELECT COALESCE(fundador, false),
                          fundador_ate,
                          (fundador_ate IS NOT NULL
                           AND (now() AT TIME ZONE 'America/Sao_Paulo')::date <= fundador_ate)
                     FROM restaurant_profiles WHERE id = %s""",
                (str(restaurant_id),),
            )
            row = cur.fetchone()
            if not row or not row[0]:
                return Decimal("1")

            cur.execute(
                "SELECT key, value FROM platform_settings WHERE key = ANY(%s)",
                (["founding_partner_until", "founding_partner_factor"],),
            )
            cfg = {k: v for k, v in cur.fetchall()}

            if row[1] is not None:
                # Tem janela própria: ela manda, e ponto.
                if not row[2]:
                    return Decimal("1")  # janela do parceiro encerrada
            else:
                # Sem janela própria (marcado antes da migration): cai na data
                # global. Sem ela, sem promo — fail-safe.
                until = (cfg.get("founding_partner_until") or "").strip()
                if not until:
                    return Decimal("1")
                cur.execute(
                    "SELECT (now() AT TIME ZONE 'America/Sao_Paulo')::date <= %s::date",
                    (until,),
                )
                if not cur.fetchone()[0]:
                    return Decimal("1")  # campanha já encerrada

        factor = _to_decimal(cfg.get("founding_partner_factor"), Decimal("0.5"))
        if factor < 0:
            factor = Decimal("0")
        elif factor > 1:
            factor = Decimal("1")
        return factor
    except Exception:
        logger.exception("founding_commission_factor falhou — cobrando comissão cheia")
        return Decimal("1")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def calculate_platform_commission(subtotal, restaurant_id=None) -> Decimal:
    """Calcula a comissão da plataforma sobre o subtotal do pedido.

    Se `restaurant_id` for de um Parceiro Fundador dentro do prazo da campanha,
    aplica o fator promocional (metade da comissão)."""
    try:
        sub = Decimal(str(subtotal))
    except (InvalidOperation, TypeError):
        return Decimal("0.00")
    rate = get_settings()["commission_rate"]
    if restaurant_id:
        rate = rate * founding_commission_factor(restaurant_id)
    return (sub * rate).quantize(Decimal("0.01"))
