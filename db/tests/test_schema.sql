-- Schema guarantees test harness. Run against a fresh migrated database.
--   psql -d opdev -v ON_ERROR_STOP=1 -f db/tests/test_schema.sql
-- Prints PASS lines. Any assertion failure aborts the script.

\set ON_ERROR_STOP on

DO $$
DECLARE
    r_id  uuid;
    cat   uuid;
    item  uuid;
    grp   uuid;
    mod   uuid;
    o_id  uuid;
    o2    uuid;
    oi    uuid;
    n1    integer;
    n2    integer;
    cnt   integer;
    caught boolean;
BEGIN
    -- ---------------------------------------------------------------- fixtures
    INSERT INTO restaurants (slug, name, timezone, tax_bps, transfer_phone_e164)
    -- Deliberately distinct from any seeded number so the harness can run
    -- against a database that already has the pilot tenant loaded.
    VALUES ('test-diner', 'Test Diner', 'America/Chicago', 925, '+16155559990')
    RETURNING id INTO r_id;

    INSERT INTO phone_numbers (restaurant_id, e164) VALUES (r_id, '+16155559991');

    INSERT INTO menu_categories (restaurant_id, name, position)
    VALUES (r_id, 'Mains', 1) RETURNING id INTO cat;

    INSERT INTO menu_items (restaurant_id, category_id, name, price_cents, aliases, prep_minutes)
    VALUES (r_id, cat, 'Nashville Hot Chicken', 1650, ARRAY['hot chicken','the chicken'], 18)
    RETURNING id INTO item;

    INSERT INTO modifier_groups (restaurant_id, name, prompt, min_select, max_select)
    VALUES (r_id, 'Heat Level', 'How spicy would you like that?', 1, 1)
    RETURNING id INTO grp;

    INSERT INTO modifiers (modifier_group_id, name, price_delta_cents)
    VALUES (grp, 'Extra Hot', 0) RETURNING id INTO mod;

    INSERT INTO menu_item_modifier_groups (menu_item_id, modifier_group_id)
    VALUES (item, grp);

    RAISE NOTICE 'PASS  fixtures created';

    -- ------------------------------------------------- 1. happy path lifecycle
    INSERT INTO orders (restaurant_id, channel, order_type, idempotency_key)
    VALUES (r_id, 'phone', 'pickup', 'conv-1:confirm-1')
    RETURNING id INTO o_id;

    INSERT INTO order_items (order_id, menu_item_id, name_snapshot, unit_price_cents, quantity)
    VALUES (o_id, item, 'Nashville Hot Chicken', 1650, 2)
    RETURNING id INTO oi;

    INSERT INTO order_item_modifiers (order_item_id, modifier_id, name_snapshot, price_delta_cents)
    VALUES (oi, mod, 'Extra Hot', 0);

    UPDATE orders SET
        status = 'confirmed',
        business_date = CURRENT_DATE,
        order_number = next_order_number(r_id, CURRENT_DATE),
        subtotal_cents = 3300,
        tax_cents = 305,
        total_cents = 3605
    WHERE id = o_id;

    UPDATE orders SET status = 'fired'     WHERE id = o_id;
    UPDATE orders SET status = 'preparing' WHERE id = o_id;
    UPDATE orders SET status = 'ready'     WHERE id = o_id;
    UPDATE orders SET status = 'completed' WHERE id = o_id;

    SELECT confirmed_at IS NOT NULL AND fired_at IS NOT NULL
           AND ready_at IS NOT NULL AND completed_at IS NOT NULL
      INTO caught FROM orders WHERE id = o_id;
    IF NOT caught THEN RAISE EXCEPTION 'FAIL lifecycle timestamps not stamped'; END IF;
    RAISE NOTICE 'PASS  full lifecycle draft -> completed, timestamps auto-stamped';

    -- ------------------------------------------------------- 2. audit trail
    SELECT count(*) INTO cnt FROM order_events WHERE order_id = o_id;
    IF cnt <> 5 THEN
        RAISE EXCEPTION 'FAIL expected 5 order_events, got %', cnt;
    END IF;
    RAISE NOTICE 'PASS  order_events audit trail auto-populated (% rows)', cnt;

    -- --------------------------------------- 3. illegal transition: skip ahead
    caught := false;
    BEGIN
        INSERT INTO orders (restaurant_id, channel, order_type, idempotency_key)
        VALUES (r_id, 'phone', 'pickup', 'conv-2:confirm-1') RETURNING id INTO o2;
        UPDATE orders SET status = 'fired',
               business_date = CURRENT_DATE,
               order_number = next_order_number(r_id, CURRENT_DATE)
        WHERE id = o2;
    EXCEPTION WHEN check_violation THEN
        caught := true;
    END;
    IF NOT caught THEN RAISE EXCEPTION 'FAIL draft -> fired was allowed'; END IF;
    RAISE NOTICE 'PASS  draft -> fired rejected (must pass through confirmed)';

    -- ------------------------------------ 4. illegal transition: revive a done
    caught := false;
    BEGIN
        UPDATE orders SET status = 'preparing' WHERE id = o_id;
    EXCEPTION WHEN check_violation THEN
        caught := true;
    END;
    IF NOT caught THEN RAISE EXCEPTION 'FAIL completed -> preparing was allowed'; END IF;
    RAISE NOTICE 'PASS  completed -> preparing rejected (terminal state holds)';

    -- ------------------------------------------------- 5. double-fire guard
    caught := false;
    BEGIN
        INSERT INTO orders (restaurant_id, channel, order_type, idempotency_key)
        VALUES (r_id, 'phone', 'pickup', 'conv-1:confirm-1');
    EXCEPTION WHEN unique_violation THEN
        caught := true;
    END;
    IF NOT caught THEN RAISE EXCEPTION 'FAIL duplicate idempotency_key was allowed'; END IF;
    RAISE NOTICE 'PASS  duplicate idempotency_key rejected (no double-fire)';

    -- ---------------------------------------------- 6. order numbers increment
    n1 := next_order_number(r_id, DATE '2026-01-01');
    n2 := next_order_number(r_id, DATE '2026-01-01');
    IF n2 <> n1 + 1 THEN
        RAISE EXCEPTION 'FAIL order numbers not sequential: % then %', n1, n2;
    END IF;
    n1 := next_order_number(r_id, DATE '2026-01-02');
    IF n1 <> 1 THEN RAISE EXCEPTION 'FAIL order number did not reset daily'; END IF;
    RAISE NOTICE 'PASS  order numbers sequential per day and reset at rollover';

    -- ------------------------------------- 7. confirmed order must have number
    caught := false;
    BEGIN
        INSERT INTO orders (restaurant_id, channel, order_type, idempotency_key, status)
        VALUES (r_id, 'phone', 'pickup', 'conv-3:confirm-1', 'confirmed');
    EXCEPTION WHEN check_violation THEN
        caught := true;
    END;
    IF NOT caught THEN RAISE EXCEPTION 'FAIL confirmed order without order_number allowed'; END IF;
    RAISE NOTICE 'PASS  non-draft order without order_number rejected';

    -- ------------------------------------------- 8. dine-in needs a table
    caught := false;
    BEGIN
        INSERT INTO orders (restaurant_id, channel, order_type, idempotency_key)
        VALUES (r_id, 'kiosk', 'dine_in', 'kiosk-1:confirm-1') RETURNING id INTO o2;
        UPDATE orders SET status = 'confirmed',
               business_date = CURRENT_DATE,
               order_number = next_order_number(r_id, CURRENT_DATE)
        WHERE id = o2;
    EXCEPTION WHEN check_violation THEN
        caught := true;
    END;
    IF NOT caught THEN RAISE EXCEPTION 'FAIL dine_in confirmed without table_label'; END IF;
    RAISE NOTICE 'PASS  dine_in confirm without table_label rejected';

    -- --------------------------------------- 9. menu snapshot survives edits
    UPDATE menu_items SET price_cents = 1999, name = 'Nashville Hot Chicken (New)'
    WHERE id = item;
    SELECT count(*) INTO cnt FROM order_items
    WHERE order_id = o_id AND name_snapshot = 'Nashville Hot Chicken'
      AND unit_price_cents = 1650;
    IF cnt <> 1 THEN RAISE EXCEPTION 'FAIL menu edit leaked into historical order'; END IF;
    RAISE NOTICE 'PASS  menu price change did not rewrite a past order';

    -- ---------------------------------------------- 10. bad phone format
    caught := false;
    BEGIN
        INSERT INTO phone_numbers (restaurant_id, e164) VALUES (r_id, '615-555-0111');
    EXCEPTION WHEN check_violation THEN
        caught := true;
    END;
    IF NOT caught THEN RAISE EXCEPTION 'FAIL non-E.164 phone accepted'; END IF;
    RAISE NOTICE 'PASS  non-E.164 phone number rejected';

    -- ------------------------------------------- 11. tenant isolation shape
    SELECT count(*) INTO cnt
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name IN ('menu_items','orders','customers','conversations',
                         'reservations','notifications','menu_categories')
      AND column_name = 'restaurant_id';
    IF cnt <> 7 THEN
        RAISE EXCEPTION 'FAIL not every tenant table carries restaurant_id (got %)', cnt;
    END IF;
    RAISE NOTICE 'PASS  every tenant-scoped table carries restaurant_id';

    -- ------------------------------------------------------------- teardown
    DELETE FROM restaurants WHERE id = r_id;
    SELECT count(*) INTO cnt FROM orders WHERE restaurant_id = r_id;
    IF cnt <> 0 THEN RAISE EXCEPTION 'FAIL cascade delete left orphans'; END IF;
    RAISE NOTICE 'PASS  tenant delete cascades cleanly';

    RAISE NOTICE '--- all schema assertions passed ---';
END $$;
