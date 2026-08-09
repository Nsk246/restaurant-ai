-- Seed a single pilot restaurant. Tenant-ready, but only one tenant for now.
-- Idempotent: safe to re-run.

BEGIN;

INSERT INTO restaurants (slug, name, timezone, address_line1, city, region,
                         postal_code, tax_bps, transfer_phone_e164, agent_config)
VALUES (
    'pilot',
    'Broadway Kitchen',
    'America/Chicago',
    '412 Broadway', 'Nashville', 'TN', '37203',
    925,
    '+16155550100',
    jsonb_build_object(
        'greeting', 'Thanks for calling Broadway Kitchen. This call is recorded for quality. How can I help you today?',
        'voice', 'default',
        'persona', 'Warm, efficient, never chatty. Confirms every order before sending it.',
        'max_party_size', 12,
        'default_quote_minutes', 25,
        'allow_delivery', false,
        'upsell', 'offer_sides_once'
    )
)
ON CONFLICT (slug) DO NOTHING;

-- Only seed a number if the restaurant has none. The real number is set
-- from RESTAURANT_PHONE at release time, and ON CONFLICT (e164) would
-- happily re-add this placeholder alongside it on the next deploy, leaving
-- the tenant with two numbers and the portal showing the wrong one.
INSERT INTO phone_numbers (restaurant_id, e164, provider)
SELECT r.id, '+16155550111', 'twilio'
FROM restaurants r
WHERE r.slug = 'pilot'
  AND NOT EXISTS (
      SELECT 1 FROM phone_numbers p WHERE p.restaurant_id = r.id
  )
ON CONFLICT (e164) DO NOTHING;

-- Hours: closed Monday, 11:00 to 22:00 Tue-Thu and Sun, 11:00 to 23:00 Fri-Sat.
INSERT INTO service_hours (restaurant_id, day_of_week, opens_at, closes_at, service)
SELECT r.id, d.dow, d.o, d.c, 'dining'
FROM restaurants r,
     (VALUES (0,'11:00'::time,'22:00'::time),
             (2,'11:00','22:00'),
             (3,'11:00','22:00'),
             (4,'11:00','22:00'),
             (5,'11:00','23:00'),
             (6,'11:00','23:00')) AS d(dow, o, c)
WHERE r.slug = 'pilot'
ON CONFLICT DO NOTHING;

-- Categories
INSERT INTO menu_categories (restaurant_id, name, description, position)
SELECT r.id, c.name, c.descr, c.pos
FROM restaurants r,
     (VALUES ('Starters', 'Small plates to share', 1),
             ('Mains', 'Hot off the line', 2),
             ('Sides', NULL, 3),
             ('Drinks', NULL, 4),
             ('Desserts', NULL, 5)) AS c(name, descr, pos)
WHERE r.slug = 'pilot'
ON CONFLICT (restaurant_id, name) DO NOTHING;

-- Modifier groups
INSERT INTO modifier_groups (restaurant_id, name, prompt, min_select, max_select)
SELECT r.id, g.name, g.prompt, g.mn, g.mx
FROM restaurants r,
     (VALUES ('Heat Level', 'How hot would you like that?', 1, 1),
             ('Add-ons', 'Anything you would like added?', 0, 4),
             ('Cook Temp', 'How would you like that cooked?', 1, 1),
             ('Drink Size', 'What size?', 1, 1)) AS g(name, prompt, mn, mx)
WHERE r.slug = 'pilot'
ON CONFLICT (restaurant_id, name) DO NOTHING;

INSERT INTO modifiers (modifier_group_id, name, price_delta_cents, position)
SELECT g.id, m.name, m.delta, m.pos
FROM modifier_groups g
JOIN restaurants r ON r.id = g.restaurant_id AND r.slug = 'pilot'
JOIN (VALUES
        ('Heat Level', 'Mild', 0, 1),
        ('Heat Level', 'Medium', 0, 2),
        ('Heat Level', 'Hot', 0, 3),
        ('Heat Level', 'Extra Hot', 0, 4),
        ('Add-ons', 'Extra cheese', 150, 1),
        ('Add-ons', 'Bacon', 250, 2),
        ('Add-ons', 'Fried egg', 200, 3),
        ('Add-ons', 'Avocado', 250, 4),
        ('Cook Temp', 'Medium rare', 0, 1),
        ('Cook Temp', 'Medium', 0, 2),
        ('Cook Temp', 'Medium well', 0, 3),
        ('Cook Temp', 'Well done', 0, 4),
        ('Drink Size', 'Regular', 0, 1),
        ('Drink Size', 'Large', 125, 2)
     ) AS m(grp, name, delta, pos) ON m.grp = g.name
ON CONFLICT (modifier_group_id, name) DO NOTHING;

-- Items. aliases are what callers actually say out loud.
INSERT INTO menu_items (restaurant_id, category_id, name, description, price_cents,
                        aliases, tags, prep_minutes, position)
SELECT r.id, c.id, i.name, i.descr, i.price, i.aliases, i.tags, i.prep, i.pos
FROM restaurants r
JOIN menu_categories c ON c.restaurant_id = r.id
JOIN (VALUES
    ('Starters', 'Nashville Hot Wings', 'Six wings, house hot oil, pickles', 1400,
        ARRAY['wings','hot wings','the wings'], ARRAY['spicy'], 14, 1),
    ('Starters', 'Pimento Cheese Dip', 'Served with grilled sourdough', 1100,
        ARRAY['pimento','cheese dip','the dip'], ARRAY['vegetarian'], 8, 2),
    ('Starters', 'Fried Green Tomatoes', 'Buttermilk remoulade', 1200,
        ARRAY['green tomatoes','tomatoes'], ARRAY['vegetarian'], 10, 3),
    ('Mains', 'Nashville Hot Chicken', 'Quarter bird, white bread, pickles', 1850,
        ARRAY['hot chicken','the chicken','nashville chicken'], ARRAY['spicy'], 20, 1),
    ('Mains', 'Smash Burger', 'Double patty, American, house sauce', 1650,
        ARRAY['burger','the burger','smashburger'], ARRAY[]::text[], 14, 2),
    ('Mains', 'Hot Chicken Sandwich', 'Same bird, brioche bun, slaw', 1550,
        ARRAY['chicken sandwich','the sandwich'], ARRAY['spicy'], 14, 3),
    ('Mains', 'Ribeye', '12oz, herb butter', 3800,
        ARRAY['steak','the ribeye'], ARRAY[]::text[], 25, 4),
    ('Mains', 'Blackened Catfish', 'Cajun spice, lemon', 2200,
        ARRAY['catfish','fish'], ARRAY[]::text[], 18, 5),
    ('Mains', 'Garden Grain Bowl', 'Farro, roasted veg, tahini', 1500,
        ARRAY['grain bowl','the bowl','veggie bowl'], ARRAY['vegan','gluten_free'], 10, 6),
    ('Sides', 'Mac and Cheese', NULL, 700,
        ARRAY['mac','mac n cheese'], ARRAY['vegetarian'], 6, 1),
    ('Sides', 'Collard Greens', NULL, 600,
        ARRAY['collards','greens'], ARRAY['gluten_free'], 5, 2),
    ('Sides', 'Fries', NULL, 500,
        ARRAY['french fries','the fries'], ARRAY['vegan'], 6, 3),
    ('Sides', 'Cornbread', 'Honey butter', 550,
        ARRAY['corn bread'], ARRAY['vegetarian'], 5, 4),
    ('Drinks', 'Sweet Tea', NULL, 350,
        ARRAY['tea','iced tea'], ARRAY['vegan'], 2, 1),
    ('Drinks', 'Lemonade', NULL, 400,
        ARRAY['lemonade'], ARRAY['vegan'], 2, 2),
    ('Drinks', 'Fountain Soda', NULL, 350,
        ARRAY['soda','coke','pop'], ARRAY['vegan'], 2, 3),
    ('Desserts', 'Banana Pudding', NULL, 800,
        ARRAY['pudding','banana puddin'], ARRAY['vegetarian'], 3, 1),
    ('Desserts', 'Pecan Pie', 'Contains nuts', 900,
        ARRAY['pie','the pie'], ARRAY['vegetarian','contains_nuts'], 3, 2)
) AS i(cat, name, descr, price, aliases, tags, prep, pos) ON i.cat = c.name
WHERE r.slug = 'pilot'
ON CONFLICT (restaurant_id, name) DO NOTHING;

-- Attach modifier groups to the items that need them.
INSERT INTO menu_item_modifier_groups (menu_item_id, modifier_group_id, position)
SELECT mi.id, mg.id, 1
FROM menu_items mi
JOIN restaurants r ON r.id = mi.restaurant_id AND r.slug = 'pilot'
JOIN modifier_groups mg ON mg.restaurant_id = r.id
JOIN (VALUES
        ('Nashville Hot Wings', 'Heat Level'),
        ('Nashville Hot Chicken', 'Heat Level'),
        ('Hot Chicken Sandwich', 'Heat Level'),
        ('Smash Burger', 'Add-ons'),
        ('Garden Grain Bowl', 'Add-ons'),
        ('Ribeye', 'Cook Temp'),
        ('Sweet Tea', 'Drink Size'),
        ('Lemonade', 'Drink Size'),
        ('Fountain Soda', 'Drink Size')
     ) AS pair(item, grp) ON pair.item = mi.name AND pair.grp = mg.name
ON CONFLICT DO NOTHING;

-- Reservation capacity: 20 covers per 15 minute slot, 11:00 to 21:00, every day.
INSERT INTO reservation_capacity (restaurant_id, day_of_week, slot_start, max_covers)
SELECT r.id, d.dow, s.slot, 20
FROM restaurants r
CROSS JOIN generate_series(0, 6) AS d(dow)
CROSS JOIN (
    SELECT (TIME '11:00' + (n * INTERVAL '15 minutes'))::time AS slot
    FROM generate_series(0, 40) AS n
) AS s
WHERE r.slug = 'pilot'
ON CONFLICT DO NOTHING;

COMMIT;
