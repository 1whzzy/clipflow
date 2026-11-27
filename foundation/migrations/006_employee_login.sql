CREATE TABLE login_attempts (
    digest text PRIMARY KEY,
    expires_at timestamptz NOT NULL
);

CREATE TABLE employee_sessions (
    digest text PRIMARY KEY,
    app_id text NOT NULL,
    tenant_key text NOT NULL,
    open_id text NOT NULL,
    name text NOT NULL,
    expires_at timestamptz NOT NULL
);

CREATE INDEX login_attempts_expiry ON login_attempts (expires_at);
CREATE INDEX employee_sessions_expiry ON employee_sessions (expires_at);
