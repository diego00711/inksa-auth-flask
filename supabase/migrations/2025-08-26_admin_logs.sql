-- Admin Logs schema for Inksa
-- This migration creates the admin_logs table and supporting indexes.
-- Notes:
-- - Keep RLS enabled. Backend uses the Service Role key and bypasses RLS.
-- - Do NOT expose the Service Role key in frontend environments.

-- Extensions
create extension if not exists pgcrypto;  -- for gen_random_uuid()
create extension if not exists pg_trgm;   -- for ILIKE performance on details

-- Table
create table if not exists public.admin_logs (
  id uuid primary key default gen_random_uuid(),
  "timestamp" timestamptz not null default now(),
  admin text not null,
  action text not null,
  details text not null
);

-- Indexes
create index if not exists idx_admin_logs_timestamp on public.admin_logs ("timestamp");
create index if not exists idx_admin_logs_admin on public.admin_logs (admin);
create index if not exists idx_admin_logs_action on public.admin_logs (action);
create index if not exists idx_admin_logs_details_trgm on public.admin_logs using gin (details gin_trgm_ops);

-- Security
alter table public.admin_logs enable row level security;