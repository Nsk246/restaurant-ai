-- =============================================================================
-- 001_init.sql
-- Restaurant AI platform, core schema.
--
-- Conventions:
--   * Money is always integer cents. Never floats.
--   * Every tenant-scoped table carries restaurant_id NOT NULL.
--   * Order line items snapshot name and price at time of order, so editing
--     the menu later never rewrites history.
--   * Order status transitions are enforced by a trigger, not by convention.
-- =============================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "citext";

-- -----------------------------------------------------------------------------
-- Enums
-- -----------------------------------------------------------------------------

CREATE TYPE channel AS ENUM ('phone', 'kiosk');

CREATE TYPE order_type AS ENUM ('pickup', 'delivery', 'dine_in');

CREATE TYPE order_status AS ENUM (
    'draft',        -- agent is still building it, never visible to kitchen
    'confirmed',    -- customer said yes to the readback
    'fired',        -- handed to the kitchen sink, ticket exists
    'preparing',
    'ready',
    'completed',
    'cancelled'
);

CREATE TYPE reservation_status AS ENUM (
    'requested', 'confirmed', 'seated', 'completed', 'cancelled', 'no_show'
);

CREATE TYPE conversation_outcome AS ENUM (
    'order_placed', 'reservation_made', 'info_only',
    'transferred', 'abandoned', 'error'
);

CREATE TYPE notification_channel AS ENUM ('sms', 'email');

CREATE TYPE notification_status AS ENUM ('queued', 'sent', 'delivered', 'failed');

-- -----------------------------------------------------------------------------
-- Shared trigger: maintain updated_at
-- -----------------------------------------------------------------------------

CREATE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- Tenants
-- -----------------------------------------------------------------------------

CREATE TABLE restaurants (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            citext NOT NULL UNIQUE,
    name            text NOT NULL,
    timezone        text NOT NULL DEFAULT 'America/Chicago',
    address_line1   text,
    address_line2   text,
    city            text,
    region          text,
    postal_code     text,
    country         char(2) NOT NULL DEFAULT 'US',

    -- Where the agent warm-transfers a caller who needs a human.
    transfer_phone_e164 text,

    -- Sales tax applied to orders, in basis points. 925 = 9.25%.
    tax_bps         integer NOT NULL DEFAULT 0 CHECK (tax_bps >= 0 AND tax_bps <= 10000),

    -- Per-tenant agent config: greeting text, voice id, persona notes,
    -- upsell policy, max party size, delivery radius, and so on.
    agent_config    jsonb NOT NULL DEFAULT '{}'::jsonb,

    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_restaurants_updated
    BEFORE UPDATE ON restaurants
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Inbound Twilio number resolves the tenant. This is the tenant router.
CREATE TABLE phone_numbers (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    e164            text NOT NULL UNIQUE CHECK (e164 ~ '^\+[1-9][0-9]{7,14}$'),
    provider        text NOT NULL DEFAULT 'twilio',
    provider_sid    text,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_phone_numbers_restaurant ON phone_numbers(restaurant_id);

-- Weekly recurring service hours. day_of_week: 0 = Sunday.
-- Overnight windows are expressed with closes_at < opens_at.
CREATE TABLE service_hours (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    day_of_week     smallint NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    opens_at        time NOT NULL,
    closes_at       time NOT NULL,
    -- 'dining', 'pickup', 'delivery'. Lets pickup close before the dining room.
    service         text NOT NULL DEFAULT 'dining',
    UNIQUE (restaurant_id, day_of_week, service, opens_at)
);

CREATE INDEX idx_service_hours_restaurant ON service_hours(restaurant_id);

-- One-off closures and holiday hours. Beats special-casing in the prompt.
CREATE TABLE service_exceptions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    on_date         date NOT NULL,
    is_closed       boolean NOT NULL DEFAULT true,
    opens_at        time,
    closes_at       time,
    note            text,
    UNIQUE (restaurant_id, on_date)
);

-- -----------------------------------------------------------------------------
-- Menu
-- -----------------------------------------------------------------------------

CREATE TABLE menu_categories (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    name            text NOT NULL,
    description     text,
    position        integer NOT NULL DEFAULT 0,
    is_active       boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (restaurant_id, name)
);

CREATE TRIGGER trg_menu_categories_updated
    BEFORE UPDATE ON menu_categories
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE menu_items (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    category_id     uuid NOT NULL REFERENCES menu_categories(id) ON DELETE RESTRICT,

    name            text NOT NULL,
    description     text,
    price_cents     integer NOT NULL CHECK (price_cents >= 0),

    -- Spoken aliases the agent should accept. Callers say "the wings",
    -- the menu says "Nashville Hot Wings".
    aliases         text[] NOT NULL DEFAULT '{}',

    -- 'vegan', 'gluten_free', 'contains_nuts', 'spicy'. Drives allergen answers.
    tags            text[] NOT NULL DEFAULT '{}',

    prep_minutes    integer NOT NULL DEFAULT 15 CHECK (prep_minutes >= 0),
    calories        integer,
    position        integer NOT NULL DEFAULT 0,

    -- is_active  = on the menu at all.
    -- is_available = in stock right now. This is the 86 button on the KDS.
    is_active       boolean NOT NULL DEFAULT true,
    is_available    boolean NOT NULL DEFAULT true,
    unavailable_until timestamptz,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (restaurant_id, name)
);

CREATE TRIGGER trg_menu_items_updated
    BEFORE UPDATE ON menu_items
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_menu_items_restaurant ON menu_items(restaurant_id);
CREATE INDEX idx_menu_items_category ON menu_items(category_id);
-- Hot path: build the sellable menu snapshot injected into the agent context.
CREATE INDEX idx_menu_items_sellable
    ON menu_items(restaurant_id, position)
    WHERE is_active AND is_available;

CREATE TABLE modifier_groups (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    name            text NOT NULL,
    -- Question the agent asks: "What temperature for the steak?"
    prompt          text,
    min_select      integer NOT NULL DEFAULT 0 CHECK (min_select >= 0),
    max_select      integer NOT NULL DEFAULT 1 CHECK (max_select >= 1),
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (restaurant_id, name),
    CHECK (max_select >= min_select)
);

CREATE TABLE modifiers (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    modifier_group_id uuid NOT NULL REFERENCES modifier_groups(id) ON DELETE CASCADE,
    name              text NOT NULL,
    price_delta_cents integer NOT NULL DEFAULT 0,
    position          integer NOT NULL DEFAULT 0,
    is_available      boolean NOT NULL DEFAULT true,
    UNIQUE (modifier_group_id, name)
);

CREATE INDEX idx_modifiers_group ON modifiers(modifier_group_id);

CREATE TABLE menu_item_modifier_groups (
    menu_item_id      uuid NOT NULL REFERENCES menu_items(id) ON DELETE CASCADE,
    modifier_group_id uuid NOT NULL REFERENCES modifier_groups(id) ON DELETE CASCADE,
    position          integer NOT NULL DEFAULT 0,
    -- Overrides the group default for this specific item.
    is_required       boolean,
    PRIMARY KEY (menu_item_id, modifier_group_id)
);

-- -----------------------------------------------------------------------------
-- Customers
-- -----------------------------------------------------------------------------

CREATE TABLE customers (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    phone_e164      text CHECK (phone_e164 ~ '^\+[1-9][0-9]{7,14}$'),
    name            text,
    email           citext,

    -- "Allergic to shellfish", "always orders the #3". Agent reads this on
    -- recognising a returning caller.
    notes           text,
    tags            text[] NOT NULL DEFAULT '{}',

    -- TCPA hygiene. A caller who dials in has consented to a transactional
    -- reply, but log it explicitly rather than assuming.
    sms_consent_at  timestamptz,
    sms_opted_out   boolean NOT NULL DEFAULT false,

    order_count     integer NOT NULL DEFAULT 0,
    last_seen_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (restaurant_id, phone_e164)
);

CREATE TRIGGER trg_customers_updated
    BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_customers_restaurant ON customers(restaurant_id);

-- -----------------------------------------------------------------------------
-- Conversations (one per call or kiosk session)
-- -----------------------------------------------------------------------------

CREATE TABLE conversations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    customer_id     uuid REFERENCES customers(id) ON DELETE SET NULL,
    channel         channel NOT NULL,

    -- Twilio CallSid for phone, kiosk device id for walk-ins.
    external_id     text,
    from_e164       text,

    started_at      timestamptz NOT NULL DEFAULT now(),
    ended_at        timestamptz,
    outcome         conversation_outcome,

    -- Turn-by-turn: [{role, text, ts, tool_calls, latency_ms}]
    transcript      jsonb NOT NULL DEFAULT '[]'::jsonb,

    -- Latency instrumentation. This is how we know if we regressed.
    turn_count          integer NOT NULL DEFAULT 0,
    p50_response_ms     integer,
    p95_response_ms     integer,

    recording_url       text,
    recording_disclosed boolean NOT NULL DEFAULT false,
    transferred_to_human boolean NOT NULL DEFAULT false,
    error_detail        text,

    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (restaurant_id, external_id)
);

CREATE INDEX idx_conversations_restaurant_started
    ON conversations(restaurant_id, started_at DESC);
CREATE INDEX idx_conversations_customer ON conversations(customer_id);

-- -----------------------------------------------------------------------------
-- Orders
-- -----------------------------------------------------------------------------

CREATE TABLE orders (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    customer_id     uuid REFERENCES customers(id) ON DELETE SET NULL,
    conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL,

    -- Short human-readable number the kitchen and customer both use.
    -- Resets daily per restaurant. Null until the order leaves draft.
    order_number    integer,
    business_date   date,

    channel         channel NOT NULL,
    order_type      order_type NOT NULL,
    status          order_status NOT NULL DEFAULT 'draft',

    -- The anti-double-fire guarantee. The agent generates this once per
    -- confirmation attempt; a retry with the same key is a no-op insert.
    idempotency_key text NOT NULL,

    subtotal_cents  integer NOT NULL DEFAULT 0 CHECK (subtotal_cents >= 0),
    tax_cents       integer NOT NULL DEFAULT 0 CHECK (tax_cents >= 0),
    tip_cents       integer NOT NULL DEFAULT 0 CHECK (tip_cents >= 0),
    total_cents     integer NOT NULL DEFAULT 0 CHECK (total_cents >= 0),

    -- Payment happens through a link, never over voice. No PAN ever lands here.
    payment_status  text NOT NULL DEFAULT 'unpaid',
    payment_link_url text,
    payment_ref     text,

    scheduled_for   timestamptz,
    quoted_minutes  integer,
    table_label     text,
    delivery_address jsonb,
    customer_note   text,

    confirmed_at    timestamptz,
    fired_at        timestamptz,
    ready_at        timestamptz,
    completed_at    timestamptz,
    cancelled_at    timestamptz,
    cancel_reason   text,

    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    UNIQUE (restaurant_id, idempotency_key),
    UNIQUE (restaurant_id, business_date, order_number),

    -- A draft has no number; anything past draft must have one.
    CONSTRAINT order_number_matches_status CHECK (
        (status = 'draft' AND order_number IS NULL AND business_date IS NULL)
        OR (status <> 'draft' AND order_number IS NOT NULL AND business_date IS NOT NULL)
    ),
    CONSTRAINT dine_in_has_table CHECK (
        order_type <> 'dine_in' OR status = 'draft' OR table_label IS NOT NULL
    ),
    CONSTRAINT delivery_has_address CHECK (
        order_type <> 'delivery' OR status = 'draft' OR delivery_address IS NOT NULL
    )
);

CREATE TRIGGER trg_orders_updated
    BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_orders_restaurant_created
    ON orders(restaurant_id, created_at DESC);
CREATE INDEX idx_orders_customer ON orders(customer_id);
-- Hot path: the kitchen display polls or subscribes on this.
CREATE INDEX idx_orders_kds
    ON orders(restaurant_id, status, fired_at)
    WHERE status IN ('fired', 'preparing', 'ready');

CREATE TABLE order_items (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id          uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    -- Kept for analytics, but the snapshot columns are the source of truth.
    menu_item_id      uuid REFERENCES menu_items(id) ON DELETE SET NULL,

    name_snapshot     text NOT NULL,
    unit_price_cents  integer NOT NULL CHECK (unit_price_cents >= 0),
    quantity          integer NOT NULL DEFAULT 1 CHECK (quantity > 0),

    -- Free text the kitchen reads, never parsed back into structure.
    special_instructions text,
    position          integer NOT NULL DEFAULT 0,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_order_items_order ON order_items(order_id);

CREATE TABLE order_item_modifiers (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    order_item_id     uuid NOT NULL REFERENCES order_items(id) ON DELETE CASCADE,
    modifier_id       uuid REFERENCES modifiers(id) ON DELETE SET NULL,
    name_snapshot     text NOT NULL,
    price_delta_cents integer NOT NULL DEFAULT 0
);

CREATE INDEX idx_order_item_modifiers_item ON order_item_modifiers(order_item_id);

-- Append-only audit trail. Every status change lands here automatically.
CREATE TABLE order_events (
    id              bigserial PRIMARY KEY,
    order_id        uuid NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    from_status     order_status,
    to_status       order_status NOT NULL,
    actor           text NOT NULL DEFAULT 'system',
    detail          jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_order_events_order ON order_events(order_id, created_at);

-- -----------------------------------------------------------------------------
-- Order state machine, enforced in the database
-- -----------------------------------------------------------------------------

-- Validation and timestamp stamping. Must be BEFORE so it can mutate NEW and
-- abort ahead of the CHECK constraints.
CREATE FUNCTION enforce_order_transition() RETURNS trigger AS $$
DECLARE
    ok boolean;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.status = OLD.status THEN
            RETURN NEW;
        END IF;

        ok := CASE OLD.status
            WHEN 'draft'     THEN NEW.status IN ('confirmed', 'cancelled')
            WHEN 'confirmed' THEN NEW.status IN ('fired', 'cancelled')
            WHEN 'fired'     THEN NEW.status IN ('preparing', 'ready', 'cancelled')
            WHEN 'preparing' THEN NEW.status IN ('ready', 'cancelled')
            WHEN 'ready'     THEN NEW.status IN ('completed', 'cancelled')
            ELSE false  -- completed and cancelled are terminal
        END;

        IF NOT ok THEN
            RAISE EXCEPTION
                'illegal order transition % -> % for order %',
                OLD.status, NEW.status, OLD.id
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;

    -- Stamp the timestamp for the state we just entered.
    CASE NEW.status
        WHEN 'confirmed' THEN NEW.confirmed_at := COALESCE(NEW.confirmed_at, now());
        WHEN 'fired'     THEN NEW.fired_at     := COALESCE(NEW.fired_at, now());
        WHEN 'ready'     THEN NEW.ready_at     := COALESCE(NEW.ready_at, now());
        WHEN 'completed' THEN NEW.completed_at := COALESCE(NEW.completed_at, now());
        WHEN 'cancelled' THEN NEW.cancelled_at := COALESCE(NEW.cancelled_at, now());
        ELSE NULL;
    END CASE;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_orders_transition
    BEFORE INSERT OR UPDATE OF status ON orders
    FOR EACH ROW EXECUTE FUNCTION enforce_order_transition();

-- Audit logging. Must be AFTER: on INSERT the parent row does not exist yet,
-- so a BEFORE trigger writing order_events violates the foreign key.
CREATE FUNCTION log_order_transition() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            INSERT INTO order_events (order_id, from_status, to_status, actor)
            VALUES (NEW.id, NULL, NEW.status, 'system');
        END IF;
    ELSIF NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO order_events (order_id, from_status, to_status, actor)
        VALUES (NEW.id, OLD.status, NEW.status, 'system');
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_orders_transition_log
    AFTER INSERT OR UPDATE OF status ON orders
    FOR EACH ROW EXECUTE FUNCTION log_order_transition();

-- -----------------------------------------------------------------------------
-- Daily order numbers, per restaurant
-- -----------------------------------------------------------------------------

CREATE TABLE order_number_counters (
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    business_date   date NOT NULL,
    last_number     integer NOT NULL DEFAULT 0,
    PRIMARY KEY (restaurant_id, business_date)
);

-- Allocates the next number atomically. Safe under concurrent calls.
CREATE FUNCTION next_order_number(p_restaurant uuid, p_date date)
RETURNS integer AS $$
DECLARE
    n integer;
BEGIN
    INSERT INTO order_number_counters (restaurant_id, business_date, last_number)
    VALUES (p_restaurant, p_date, 1)
    ON CONFLICT (restaurant_id, business_date)
    DO UPDATE SET last_number = order_number_counters.last_number + 1
    RETURNING last_number INTO n;
    RETURN n;
END;
$$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- Reservations
-- -----------------------------------------------------------------------------

CREATE TABLE reservations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    customer_id     uuid REFERENCES customers(id) ON DELETE SET NULL,
    conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL,

    party_size      integer NOT NULL CHECK (party_size > 0),
    reserved_for    timestamptz NOT NULL,
    duration_minutes integer NOT NULL DEFAULT 90,
    status          reservation_status NOT NULL DEFAULT 'requested',

    guest_name      text,
    guest_phone_e164 text,
    special_request text,
    table_label     text,

    confirmation_code text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER trg_reservations_updated
    BEFORE UPDATE ON reservations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE INDEX idx_reservations_lookup
    ON reservations(restaurant_id, reserved_for)
    WHERE status IN ('requested', 'confirmed');

-- Capacity per 15-minute slot. The agent checks this before promising a table.
CREATE TABLE reservation_capacity (
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    day_of_week     smallint NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    slot_start      time NOT NULL,
    max_covers      integer NOT NULL CHECK (max_covers >= 0),
    PRIMARY KEY (restaurant_id, day_of_week, slot_start)
);

-- -----------------------------------------------------------------------------
-- Outbound notifications
-- -----------------------------------------------------------------------------

CREATE TABLE notifications (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    restaurant_id   uuid NOT NULL REFERENCES restaurants(id) ON DELETE CASCADE,
    customer_id     uuid REFERENCES customers(id) ON DELETE SET NULL,
    order_id        uuid REFERENCES orders(id) ON DELETE SET NULL,
    reservation_id  uuid REFERENCES reservations(id) ON DELETE SET NULL,

    channel         notification_channel NOT NULL DEFAULT 'sms',
    to_address      text NOT NULL,
    template        text NOT NULL,
    body            text NOT NULL,
    status          notification_status NOT NULL DEFAULT 'queued',

    -- Same guarantee as orders: one confirmation per event, never two.
    idempotency_key text NOT NULL,
    provider_sid    text,
    error_detail    text,

    attempts        integer NOT NULL DEFAULT 0,
    sent_at         timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (restaurant_id, idempotency_key)
);

CREATE INDEX idx_notifications_pending
    ON notifications(restaurant_id, created_at)
    WHERE status = 'queued';

COMMIT;
