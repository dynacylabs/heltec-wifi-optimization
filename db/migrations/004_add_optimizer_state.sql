-- Single-row settings table (the `id = true` check constraint enforces
-- there's ever only one row) backing the dashboard's kill switch - pausing
-- this stops the optimizer from issuing any NEW commands. It does not
-- touch commands already in flight; those still go through their own
-- rollback safety net regardless.
CREATE TABLE optimizer_state (
    id BOOLEAN PRIMARY KEY DEFAULT true CHECK (id = true),
    enabled BOOLEAN NOT NULL DEFAULT true
);
INSERT INTO optimizer_state (enabled) VALUES (true);
