CREATE TABLE IF NOT EXISTS public.delivery_tracking (
    order_id UUID PRIMARY KEY,
    latitude DECIMAL(10, 8) NOT NULL,
    longitude DECIMAL(11, 8) NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
