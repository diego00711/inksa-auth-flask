# inksa-auth-flask

Backend principal do Inksa Delivery — Flask + Supabase + APScheduler, hospedado no Render.

---

## Keep-Alive — Manter o Servidor Acordado

O Render no plano gratuito hiberna o serviço após **15 minutos de inatividade**. O sistema de keep-alive já está integrado em 3 camadas:

### Camada 1 — APScheduler (Backend)
Job automático dentro do próprio servidor que pinga todos os serviços Inksa a cada **10 minutos**:
- `https://inksa-auth-flask-dev.onrender.com/api/health`
- `https://clientes.inksadelivery.com.br`
- `https://entregadores.inksadelivery.com.br`
- `https://restaurante.inksadelivery.com.br`
- `https://admin.inksadelivery.com.br`

Para adicionar URLs extras, configure a variável de ambiente:
```
KEEP_ALIVE_EXTRA_URLS=https://exemplo1.com,https://exemplo2.com
```

### Camada 2 — Service Worker (Frontends)
Cada app frontend (clientes, entregadores, restaurantes, admin) possui um Service Worker que pinga `GET /api/health` a cada 10 minutos enquanto o usuário mantém o app aberto no navegador.

### Camada 3 — UptimeRobot (Externo — recomendado)

Configure um monitor gratuito no UptimeRobot para garantir pings mesmo quando nenhum usuário está online:

1. Acesse [https://uptimerobot.com](https://uptimerobot.com) e crie uma conta gratuita
2. Clique em **"Add New Monitor"**
3. Preencha:
   - **Monitor Type:** `HTTP(s)`
   - **Friendly Name:** `Inksa Backend`
   - **URL:** `https://inksa-auth-flask-dev.onrender.com/api/health`
   - **Monitoring Interval:** `Every 5 minutes`
4. Clique em **"Create Monitor"**

O UptimeRobot pinga a cada 5 minutos — suficiente para manter o Render acordado 24/7 no plano gratuito.

---

## Endpoint de Health

```
GET /api/health
```

Resposta:
```json
{
  "status": "ok",
  "timestamp": "2026-05-24T10:30:00.000000",
  "service": "inksa-auth-flask",
  "version": "1.0.0",
  "database": "connected",
  "mercado_pago": "configured"
}
```

Outros endpoints de status:
- `GET /health` — status simples com timestamp
- `GET /healthz` — `{"status": "ok"}`
- `GET /api/healthz` — `{"status": "ok"}`

---

## Variáveis de Ambiente (Render)

| Variável | Descrição |
|---|---|
| `SUPABASE_URL` | URL do projeto Supabase |
| `SUPABASE_SERVICE_ROLE_KEY` | Chave service role do Supabase |
| `MERCADO_PAGO_ACCESS_TOKEN` | Token do Mercado Pago |
| `JWT_SECRET` | Secret para tokens JWT |
| `DATABASE_URL` | URL direta do banco PostgreSQL |
| `KEEP_ALIVE_URL` | URL principal de keep-alive (padrão: próprio /api/health) |
| `KEEP_ALIVE_EXTRA_URLS` | URLs adicionais separadas por vírgula |
| `DISABLE_SCHEDULER` | Definir como `true` para desativar o scheduler |
| `SENTRY_DSN` | DSN do Sentry para monitoramento de erros |

---

## Desenvolvimento Local

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env      # configure as variáveis
python -m src.main
```
