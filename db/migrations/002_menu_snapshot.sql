-- The sellable menu, shaped for injection into the agent's context.
--
-- Rules encoded here so the prompt never has to:
--   * 86'd and inactive items simply do not appear, so the agent cannot
--     sell what the kitchen does not have.
--   * Every item carries its uuid, which is what the add_item tool takes.
--     The model never invents an id and never free-texts a dish name.
--   * Prices are dollars here (not cents) purely so the model reads them
--     aloud correctly. Cents remain the source of truth everywhere else.

BEGIN;

CREATE VIEW v_sellable_menu AS
SELECT
    mi.restaurant_id,
    c.name AS category,
    c.position AS category_position,
    mi.position AS item_position,
    jsonb_build_object(
        'id', mi.id,
        'name', mi.name,
        'price', round(mi.price_cents / 100.0, 2),
        'aliases', to_jsonb(mi.aliases),
        'tags', to_jsonb(mi.tags),
        'prep_minutes', mi.prep_minutes,
        'description', mi.description,
        'modifier_groups', COALESCE((
            SELECT jsonb_agg(
                jsonb_build_object(
                    'id', mg.id,
                    'name', mg.name,
                    'prompt', mg.prompt,
                    'required', COALESCE(mimg.is_required, mg.min_select > 0),
                    'min', mg.min_select,
                    'max', mg.max_select,
                    'options', COALESCE((
                        SELECT jsonb_agg(
                            jsonb_build_object(
                                'id', m.id,
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

-- Single call the agent session makes on connect.
CREATE FUNCTION menu_snapshot(p_restaurant uuid)
RETURNS jsonb AS $$
    SELECT COALESCE(
        jsonb_agg(cat ORDER BY category_position),
        '[]'::jsonb
    )
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
