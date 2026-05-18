-- Executar no Supabase SQL Editor
ALTER TABLE public.client_profiles ADD COLUMN IF NOT EXISTS fcm_token TEXT;
ALTER TABLE public.restaurant_profiles ADD COLUMN IF NOT EXISTS fcm_token TEXT;
ALTER TABLE public.delivery_profiles ADD COLUMN IF NOT EXISTS fcm_token TEXT;
