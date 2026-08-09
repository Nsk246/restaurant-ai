-- =============================================================================
-- 003_short_codes.sql
--
-- The agent-facing menu identified items by uuid. That is wrong for a voice
-- model: a native-audio model has to emit a 36-character random string to
-- order a burger, and speech-to-speech models are unreliable at reproducing
-- long random tokens. The failures look like the agent being stupid.
--
-- Short codes fix three things at once:
--   * reliability, since "smash-burger" survives a model round trip
--   * prompt size, roughly 40% of the snapshot was uuids
--   * readability of logs and transcripts when debugging a call
--
-- The uuids remain the real primary keys. Codes are a stable public handle.
-- =============================================================================

BEGIN;

-- Turn a display name into a short handle: lowercase, hyphenated, trimmed.
CREATE FUNCTION slugify(src text, max_len integer DEFAULT 24)
RETURNS text AS $$
    SELECT left(
        trim(BOTH '-' FROM
            regexp_replace(lower(coalesce(src, '')), '[^a-z0-9]+', '-', 'g')
        ),
        max_len
    );
$$ LANGUAGE sql IMMUTABLE;

-- -----------------------------------------------------------------------------
-- Menu items
-- -----------------------------------------------------------------------------

ALTER TABLE menu_items ADD COLUMN code text;

-- Backfill, disambiguating collisions with a numeric suffix rather than
-- failing. Two dishes can legitimately slugify the same way.
WITH numbered AS (
    SELECT id,
           restaurant_id,
           slugify(name) AS base,
           ROW_NUMBER() OVER (
               PARTITION BY restaurant_id, slugify(name) ORDER BY position, name
           ) AS n
    FROM menu_items
)
UPDATE menu_items mi
SET code = CASE WHEN nu.n = 1 THEN nu.base ELSE nu.base || '-' || nu.n END
FROM numbered nu
WHERE mi.id = nu.id;

ALTER TABLE menu_items
    ALTER COLUMN code SET NOT NULL,
    ADD CONSTRAINT menu_items_code_unique UNIQUE (restaurant_id, code),
    ADD CONSTRAINT menu_items_code_shape CHECK (code ~ '^[a-z0-9][a-z0-9-]*$');

-- -----------------------------------------------------------------------------
-- Modifiers
--
-- Prefixed with the group so an item carrying two groups cannot produce two
-- identical codes, which would make the agent's choice ambiguous.
-- -----------------------------------------------------------------------------

ALTER TABLE modifiers ADD COLUMN code text;

WITH numbered AS (
    SELECT m.id,
           m.modifier_group_id,
           slugify(mg.name, 8) || '-' || slugify(m.name, 14) AS base,
           ROW_NUMBER() OVER (
               PARTITION BY m.modifier_group_id,
                            slugify(mg.name, 8) || '-' || slugify(m.name, 14)
               ORDER BY m.position, m.name
           ) AS n
    FROM modifiers m
    JOIN modifier_groups mg ON mg.id = m.modifier_group_id
)
UPDATE modifiers m
SET code = CASE WHEN nu.n = 1 THEN nu.base ELSE nu.base || '-' || nu.n END
FROM numbered nu
WHERE m.id = nu.id;

ALTER TABLE modifiers
    ALTER COLUMN code SET NOT NULL,
    ADD CONSTRAINT modifiers_code_unique UNIQUE (modifier_group_id, code),
    ADD CONSTRAINT modifiers_code_shape CHECK (code ~ '^[a-z0-9][a-z0-9-]*$');

-- Keep new rows honest without forcing every insert to think about it.
CREATE FUNCTION default_menu_item_code() RETURNS trigger AS $$
BEGIN
    IF NEW.code IS NULL THEN
        NEW.code := slugify(NEW.name);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_menu_items_code
    BEFORE INSERT ON menu_items
    FOR EACH ROW EXECUTE FUNCTION default_menu_item_code();

CREATE FUNCTION default_modifier_code() RETURNS trigger AS $$
DECLARE
    group_name text;
BEGIN
    IF NEW.code IS NULL THEN
        SELECT name INTO group_name FROM modifier_groups WHERE id = NEW.modifier_group_id;
        NEW.code := slugify(group_name, 8) || '-' || slugify(NEW.name, 14);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_modifiers_code
    BEFORE INSERT ON modifiers
    FOR EACH ROW EXECUTE FUNCTION default_modifier_code();

-- -----------------------------------------------------------------------------
-- Snapshot now speaks codes, not uuids
-- -----------------------------------------------------------------------------

DROP FUNCTION IF EXISTS menu_snapshot(uuid);
DROP VIEW IF EXISTS v_sellable_menu;

CREATE VIEW v_sellable_menu AS
SELECT
    mi.restaurant_id,
    c.name AS category,
    c.position AS category_position,
    mi.position AS item_position,
    jsonb_build_object(
        'code', mi.code,
        'name', mi.name,
        'price', round(mi.price_cents / 100.0, 2),
        'aliases', to_jsonb(mi.aliases),
        'tags', to_jsonb(mi.tags),
        'prep_minutes', mi.prep_minutes,
        'description', mi.description,
        'modifier_groups', COALESCE((
            SELECT jsonb_agg(
                jsonb_build_object(
                    'name', mg.name,
                    'prompt', mg.prompt,
                    'required', COALESCE(mimg.is_required, mg.min_select > 0),
                    'min', mg.min_select,
                    'max', mg.max_select,
                    'options', COALESCE((
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'code', m.code,
                                'name', m.name,
                                'price_delta', round(m.price_delta_cents / 100.0, 2)
                            ) ORDER BY m.position
                        )
                        FROM modifiers m
                        WHERE m.modifier_group_id = mg.id AND m.is_available
                    ), '[]'::jsonb)
                ) ORDER BY mimg.position
            )
            FROM menu_item_modifier_groups mimg
            JOIN modifier_groups mg ON mg.id = mimg.modifier_group_id
            WHERE mimg.menu_item_id = mi.id
        ), '[]'::jsonb)
    ) AS item
FROM menu_items mi
JOIN menu_categories c ON c.id = mi.category_id
WHERE mi.is_active
  AND mi.is_available
  AND c.is_active;

CREATE FUNCTION menu_snapshot(p_restaurant uuid)
RETURNS jsonb AS $$
    SELECT COALESCE(jsonb_agg(cat ORDER BY category_position), '[]'::jsonb)
    FROM (
        SELECT
            category_position,
            jsonb_build_object(
                'category', category,
                'items', jsonb_agg(item ORDER BY item_position)
            ) AS cat
        FROM v_sellable_menu
        WHERE restaurant_id = p_restaurant
        GROUP BY category, category_position
    ) grouped;
$$ LANGUAGE sql STABLE;

COMMIT;
