ALTER TABLE attempts ADD COLUMN execution_epoch INTEGER NOT NULL DEFAULT 0
    CHECK (execution_epoch >= 0);
ALTER TABLE attempts ADD COLUMN lease_worker_id TEXT
    CHECK (lease_worker_id IS NULL OR length(trim(lease_worker_id)) > 0);
ALTER TABLE attempts ADD COLUMN lease_expires_at TEXT;
ALTER TABLE attempts ADD COLUMN heartbeat_at TEXT
    CHECK (
        (lease_worker_id IS NULL AND lease_expires_at IS NULL AND heartbeat_at IS NULL)
        OR
        (lease_worker_id IS NOT NULL AND lease_expires_at IS NOT NULL AND heartbeat_at IS NOT NULL
            AND execution_epoch > 0)
    );

CREATE INDEX idx_attempts_lease_expiry ON attempts(lease_expires_at)
    WHERE lease_worker_id IS NOT NULL;
