-- =============================================================================
-- 004_latency_breakdown.sql
--
-- Total response time alone cannot tell you what to change. A slow model and
-- a slow network look identical in one number and need opposite fixes, and
-- guessing between them has already cost an evening.
--
-- Splitting it:
--   model_ms      last speech frame we forwarded -> first audio back.
--                 Includes the model's own end-of-speech wait, which is
--                 configurable and is usually the largest single term.
--   transport_ms  everything else: Twilio, any tunnel, our own processing.
-- =============================================================================

BEGIN;

ALTER TABLE conversations
    ADD COLUMN p50_model_ms integer,
    ADD COLUMN p50_transport_ms integer;

COMMENT ON COLUMN conversations.p50_model_ms IS
    'Median model round trip, including its end-of-speech wait.';
COMMENT ON COLUMN conversations.p50_transport_ms IS
    'Median total response time minus the model round trip.';

COMMIT;
