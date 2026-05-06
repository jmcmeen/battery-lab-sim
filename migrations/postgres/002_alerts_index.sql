-- Alerts index — Grafana panels and ad-hoc triage queries filter by severity
-- and order by recency. This index makes "show me the last 20 critical alerts"
-- a constant-time index scan instead of a full table scan + sort.

CREATE INDEX IF NOT EXISTS alerts_severity_created
    ON alerts (severity, created_at DESC);
