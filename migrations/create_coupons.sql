CREATE TABLE IF NOT EXISTS public.coupons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(50) UNIQUE NOT NULL,
    discount_type VARCHAR(20) NOT NULL CHECK (discount_type IN ('percentage', 'fixed', 'free_delivery')),
    discount_value DECIMAL(10,2) NOT NULL,
    min_order_value DECIMAL(10,2) DEFAULT 0,
    max_uses INTEGER DEFAULT NULL,
    uses_count INTEGER DEFAULT 0,
    valid_until TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Cupons de exemplo
INSERT INTO public.coupons (code, discount_type, discount_value, min_order_value, max_uses, valid_until, is_active)
VALUES
    ('INKSA10', 'percentage', 10, 0, 1000, NOW() + INTERVAL '90 days', TRUE),
    ('BEMVINDO', 'fixed', 5, 20, 500, NOW() + INTERVAL '90 days', TRUE),
    ('FRETE0', 'free_delivery', 0, 0, 200, NOW() + INTERVAL '90 days', TRUE)
ON CONFLICT (code) DO NOTHING;
