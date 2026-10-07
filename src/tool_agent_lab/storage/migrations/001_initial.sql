CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
    paid_amount_minor INTEGER NOT NULL CHECK (paid_amount_minor > 0),
    promised_delivery_at TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    damage_verified INTEGER NOT NULL CHECK (damage_verified IN (0, 1)),
    refunded_amount_minor INTEGER NOT NULL DEFAULT 0
        CHECK (refunded_amount_minor BETWEEN 0 AND paid_amount_minor),
    coupon_amount_minor INTEGER NOT NULL DEFAULT 0 CHECK (coupon_amount_minor >= 0),
    UNIQUE (order_id, owner_id)
) STRICT;

CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    user_message TEXT NOT NULL,
    order_id TEXT REFERENCES orders(order_id),
    current_attempt_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'running', 'waiting_input', 'waiting_approval', 'completed', 'failed', 'cancelled'
    )),
    created_at TEXT NOT NULL,
    UNIQUE (task_id, owner_id),
    FOREIGN KEY (task_id, current_attempt_id) REFERENCES attempts(task_id, attempt_id)
        DEFERRABLE INITIALLY DEFERRED
) STRICT;

CREATE TABLE attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    thread_id TEXT NOT NULL UNIQUE,
    model_version TEXT NOT NULL,
    config_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued', 'running', 'waiting_input', 'waiting_approval', 'completed', 'failed', 'cancelled'
    )),
    created_at TEXT NOT NULL,
    UNIQUE (task_id, attempt_id),
    UNIQUE (task_id, attempt_id, owner_id),
    FOREIGN KEY (task_id, owner_id) REFERENCES tasks(task_id, owner_id)
) STRICT;

CREATE TABLE proposals (
    proposal_id TEXT NOT NULL,
    proposal_version INTEGER NOT NULL CHECK (proposal_version > 0),
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    parameters_json TEXT NOT NULL CHECK (json_valid(parameters_json) AND json_type(parameters_json) = 'object'),
    created_at TEXT NOT NULL,
    expires_at TEXT,
    PRIMARY KEY (proposal_id, proposal_version),
    FOREIGN KEY (task_id, attempt_id) REFERENCES attempts(task_id, attempt_id)
) STRICT;

CREATE TABLE approvals (
    request_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    proposal_version INTEGER NOT NULL CHECK (proposal_version > 0),
    owner_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    proposal_json TEXT NOT NULL CHECK (json_valid(proposal_json) AND json_type(proposal_json) = 'object'),
    decided_at TEXT NOT NULL,
    consumed_at TEXT,
    CHECK (consumed_at IS NULL OR decision = 'approved'),
    UNIQUE (proposal_id, proposal_version),
    FOREIGN KEY (proposal_id, proposal_version) REFERENCES proposals(proposal_id, proposal_version)
) STRICT;

CREATE TABLE operations (
    operation_key TEXT PRIMARY KEY,
    task_id TEXT,
    attempt_id TEXT,
    owner_id TEXT NOT NULL,
    order_id TEXT,
    action TEXT NOT NULL CHECK (action IN ('request_refund', 'issue_coupon', 'create_handoff')),
    amount_minor INTEGER,
    currency TEXT NOT NULL DEFAULT 'CNY' CHECK (currency = 'CNY'),
    approval_request_id TEXT NOT NULL UNIQUE REFERENCES approvals(request_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
    created_at TEXT NOT NULL,
    committed_at TEXT,
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    CHECK ((task_id IS NULL AND attempt_id IS NULL) OR (task_id IS NOT NULL AND attempt_id IS NOT NULL)),
    CHECK ((action = 'create_handoff' AND amount_minor IS NULL) OR
        (action IN ('request_refund', 'issue_coupon') AND order_id IS NOT NULL
            AND amount_minor IS NOT NULL AND amount_minor > 0)),
    CHECK ((status = 'succeeded' AND committed_at IS NOT NULL) OR
        (status IN ('pending', 'failed') AND committed_at IS NULL)),
    FOREIGN KEY (task_id, attempt_id, owner_id) REFERENCES attempts(task_id, attempt_id, owner_id),
    FOREIGN KEY (order_id, owner_id) REFERENCES orders(order_id, owner_id)
) STRICT;

CREATE UNIQUE INDEX one_successful_money_action_per_order ON operations(order_id, action)
    WHERE status = 'succeeded' AND action IN ('request_refund', 'issue_coupon');

CREATE TABLE events (
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq > 0),
    attempt_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    config_version TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'task_status_changed', 'input_requested', 'input_received', 'action_proposed',
        'approval_recorded', 'tool_call', 'tool_result'
    )),
    occurred_at TEXT NOT NULL,
    call_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(payload_json) AND json_type(payload_json) = 'object'
        AND length(CAST(payload_json AS BLOB)) <= 16384
    ),
    CHECK (event_type NOT IN ('tool_call', 'tool_result') OR call_id IS NOT NULL),
    PRIMARY KEY (task_id, seq),
    FOREIGN KEY (task_id, attempt_id, owner_id) REFERENCES attempts(task_id, attempt_id, owner_id)
) STRICT;
