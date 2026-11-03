-- Impede que 0,0 (e lixo equivalente) volte a ser gravado como coordenada.
--
-- POR QUE NO BANCO, E NÃO NO PYTHON:
--
-- A Oishii Sushi, única loja fora de Lages, ficou gravada com latitude 0 e
-- longitude 0 -- um ponto no Golfo da Guiné, a 11 mil km. Com o raio da
-- plataforma em 15 km ela nunca apareceu pra ninguém, nem dentro de Fraiburgo.
-- Ficou assim de 20/10 a 02/11/2026 sem ninguém notar, porque NADA quebrou:
-- a loja existia, abria, tinha 17 itens. Só não aparecia.
--
-- O que deixou passar foi a regra estar escrita como `latitude IS NOT NULL`.
-- Zero não é nulo. Essa mesma linha aparece em DEZ lugares (public_restaurants,
-- orders x2, admin x4, monitor, carrinho_abandonado, restaurant) -- consertar
-- um por um é garantir que o próximo lugar novo nasça errado de novo, que é
-- exatamente o que já aconteceu com outras regras aqui.
--
-- Com a trava no banco, as dez continuam como estão e passam a estar certas,
-- porque o valor ruim não chega mais na tabela.

BEGIN;

-- 1) Normaliza o que já está gravado. 0,0 nunca foi coordenada: é o valor que
--    o front mandava quando o geocode dele falhava.
UPDATE restaurant_profiles
   SET latitude = NULL, longitude = NULL
 WHERE latitude = 0 AND longitude = 0;

UPDATE delivery_profiles
   SET latitude = NULL, longitude = NULL
 WHERE latitude = 0 AND longitude = 0;

-- 2) A trava. Aceita o par nulo (loja ainda sem endereço resolvido) e aceita
--    coordenada de verdade. Recusa 0,0 e qualquer valor fora do globo.
--
--    ⚠️ De propósito NÃO limita ao Brasil: isso é regra de negócio e muda no
--    dia em que a operação mudar. O que não muda nunca é que 0,0 é lixo.
ALTER TABLE restaurant_profiles
  DROP CONSTRAINT IF EXISTS coordenada_utilizavel;
ALTER TABLE restaurant_profiles
  ADD CONSTRAINT coordenada_utilizavel CHECK (
        (latitude IS NULL AND longitude IS NULL)
     OR (latitude IS NOT NULL AND longitude IS NOT NULL
         AND NOT (latitude = 0 AND longitude = 0)
         AND latitude BETWEEN -90 AND 90
         AND longitude BETWEEN -180 AND 180)
  );

ALTER TABLE delivery_profiles
  DROP CONSTRAINT IF EXISTS coordenada_utilizavel;
ALTER TABLE delivery_profiles
  ADD CONSTRAINT coordenada_utilizavel CHECK (
        (latitude IS NULL AND longitude IS NULL)
     OR (latitude IS NOT NULL AND longitude IS NOT NULL
         AND NOT (latitude = 0 AND longitude = 0)
         AND latitude BETWEEN -90 AND 90
         AND longitude BETWEEN -180 AND 180)
  );

COMMIT;
