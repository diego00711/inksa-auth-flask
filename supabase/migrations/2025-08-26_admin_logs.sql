-- Admin Logs table for audit trail

-- Extensions (idempotente)
create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

-- Tabela principal
create table if not exists public.admin_logs (
  id uuid primary key default gen_random_uuid(),
  "timestamp" timestamptz not null default now(),
  admin text not null,
  action text not null,
  details text not null
);

-- Índices para performance
create index if not exists idx_admin_logs_timestamp on public.admin_logs ("timestamp" desc);
create index if not exists idx_admin_logs_admin on public.admin_logs (admin);
create index if not exists idx_admin_logs_action on public.admin_logs (action);
create index if not exists idx_admin_logs_details_trgm on public.admin_logs using gin (details gin_trgm_ops);

-- Segurança
alter table public.admin_logs enable row level security;