# Restaurant AI Operator

An AI phone operator and, later, an in-restaurant avatar kiosk. Both channels
are adapters over one shared agent core. All restaurant logic lives behind a
tool layer that neither adapter owns.

## Status

- **M0 Data foundation — done.** Schema, order state machine, menu snapshot, seed.
- **M1 Media bridge — done.** Twilio bridge, mu-law codec, barge-in, latency harness.
- **M2 Tool layer and ordering — done.** Tenant routing, menu injection, constrained
  tools, enforced readback, idempotent confirmation.
- M3 SMS confirmation and kitchen display.
- M4 Reservations, hours, FAQ.
- M5 Human transfer and failure paths.
- M6 Hardening, observability, order-accuracy eval set.

## Run it

Day to day, see **RUNNING.md**: cold start, tunnel, Twilio, and the things
that have cost hours before.


    cp .env.example .env
    make up
    make migrate
    make seed
    make test

`make reset` wipes and rebuilds from scratch.

## What the schema guarantees

These are enforced in Postgres, not in application code, so a bug in the agent
cannot bypass them.

- **No double-fired orders.** `orders.idempotency_key` is unique per restaurant.
  A retried confirmation is a rejected insert, not a second ticket.
- **No illegal order states.** A trigger enforces the transition graph. An order
  cannot skip from draft straight to fired, and a completed order cannot be
  revived. Every transition is logged to `order_events` automatically.
- **No confirming an order the caller never heard.** `confirm_order` refuses
  until `review_order` has run, and any change to the order invalidates a
  previous readback. This is enforced in the dispatcher, not asked for in the
  prompt.
- **No invented dishes.** `add_item` accepts short menu codes only, validated
  against the tenant's own menu. A modifier belonging to a different item is
  rejected. Codes rather than uuids because a native-audio model has to
  reproduce them exactly in a function call, and long random tokens are where
  speech-to-speech models fail.
- **No selling what is 86'd.** `v_sellable_menu` filters on `is_available`, so
  an out-of-stock item is absent from the agent's context entirely rather than
  being something the prompt has to remember to avoid.
- **No rewritten history.** Order lines snapshot name and price. Changing the
  menu tomorrow does not alter what a customer was charged today.
- **No card numbers.** There is no column for one anywhere. Payment is a Stripe
  link sent by SMS, which keeps the whole system out of PCI scope.
- **Tenant-ready.** Every tenant-scoped table carries `restaurant_id` and the
  inbound phone number resolves the tenant. One restaurant today, no migration
  needed for the second.

## Layout

    db/migrations   schema, applied in filename order
    db/seed         pilot restaurant and menu, idempotent
    db/tests        assertions that the guarantees above actually hold
    app/telephony   Twilio webhook and media stream bridge  (M1)
    app/agent       tool layer, session state, prompt assembly (M2)
    app/kitchen     KitchenSink interface, InternalKDS impl   (M3)

## POS integration

Unknown at the pilot. Every kitchen write goes through a `KitchenSink`
interface with one implementation today (`InternalKDS`). A Toast or Square
adapter drops in behind the same interface without the agent changing.
