# src/config.py

"""
Ficheiro central de configurações para o ecossistema Inksa Delivery.
Mova para cá todas as "regras de negócio" que podem mudar com o tempo.
"""

# =================================================
# Configurações de Taxa de Entrega da Plataforma
# =================================================
# A taxa base cobrada em todas as entregas.
FIXED_DELIVERY_FEE = 6.00

# O custo adicional por cada quilómetro rodado.
PER_KM_DELIVERY_FEE = 2.50

# A distância (em KM) abaixo da qual o custo adicional não é aplicado.
# Neste caso, até 3km, o cliente paga apenas a taxa fixa.
FREE_DELIVERY_THRESHOLD_KM = 5.0


# =================================================
# Configurações de Comissão da Plataforma
# =================================================
# A percentagem de comissão que a plataforma retém sobre o valor dos itens.
# 0.15 representa 15%.
PLATFORM_COMMISSION_RATE = 0.15