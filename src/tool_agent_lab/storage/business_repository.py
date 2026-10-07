"""Persist owned orders and operation records within the caller's transaction."""

import json
import sqlite3
from datetime import datetime
from typing import Literal

from pydantic import JsonValue

from tool_agent_lab.schemas.business import Operation, Order
from tool_agent_lab.storage.database import require_transaction


def _order(row: sqlite3.Row) -> Order:
    values = dict(row)
    values["damage_verified"] = bool(values["damage_verified"])
    return Order.model_validate(values)


def _operation(row: sqlite3.Row) -> Operation:
    values = dict(row)
    result = values.pop("result_json")
    values["result"] = json.loads(result) if result is not None else None
    return Operation.model_validate(values)


class BusinessRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add_order(self, order: Order) -> None:
        require_transaction(self.connection)
        self.connection.execute(
            """INSERT INTO orders (order_id, owner_id, product_name, category, currency,
                paid_amount_minor, promised_delivery_at, delivered_at, damage_verified,
                refunded_amount_minor, coupon_amount_minor)
            VALUES (:order_id, :owner_id, :product_name, :category, :currency,
                :paid_amount_minor, :promised_delivery_at, :delivered_at, :damage_verified,
                :refunded_amount_minor, :coupon_amount_minor)""",
            order.model_dump(mode="json"),
        )

    def get_order(self, order_id: str, owner_id: str) -> Order | None:
        row = self.connection.execute(
            "SELECT * FROM orders WHERE order_id = ? AND owner_id = ?", (order_id, owner_id)
        ).fetchone()
        return _order(row) if row is not None else None

    def set_balances(
        self, order_id: str, owner_id: str, *, refunded_amount_minor: int, coupon_amount_minor: int
    ) -> bool:
        require_transaction(self.connection)
        stored = self.get_order(order_id, owner_id)
        if stored is None:
            return False
        order = Order.model_validate(stored.model_dump() | {
            "refunded_amount_minor": refunded_amount_minor,
            "coupon_amount_minor": coupon_amount_minor,
        })
        return self.connection.execute(
            """UPDATE orders SET refunded_amount_minor = ?, coupon_amount_minor = ?
                WHERE order_id = ? AND owner_id = ?""",
            (order.refunded_amount_minor, order.coupon_amount_minor, order_id, owner_id),
        ).rowcount == 1

    def add_operation(self, operation: Operation) -> None:
        require_transaction(self.connection)
        values = operation.model_dump(mode="json")
        values["result_json"] = json.dumps(values.pop("result"), ensure_ascii=False, allow_nan=False)
        self.connection.execute(
            """INSERT INTO operations (operation_key, task_id, attempt_id, owner_id,
                order_id, action, amount_minor, currency, approval_request_id, status,
                created_at, committed_at, result_json)
            VALUES (:operation_key, :task_id, :attempt_id, :owner_id,
                :order_id, :action, :amount_minor, :currency, :approval_request_id, :status,
                :created_at, :committed_at, :result_json)""",
            values,
        )

    def get_operation(self, operation_key: str, owner_id: str) -> Operation | None:
        row = self.connection.execute(
            "SELECT * FROM operations WHERE operation_key = ? AND owner_id = ?",
            (operation_key, owner_id),
        ).fetchone()
        return _operation(row) if row is not None else None

    def operation_key_exists(self, operation_key: str) -> bool:
        """Check a global key collision without exposing another owner's record."""
        return self.connection.execute(
            "SELECT 1 FROM operations WHERE operation_key = ?", (operation_key,)
        ).fetchone() is not None

    def get_operation_for_approval(self, request_id: str, owner_id: str) -> Operation | None:
        row = self.connection.execute(
            "SELECT * FROM operations WHERE approval_request_id = ? AND owner_id = ?",
            (request_id, owner_id),
        ).fetchone()
        return _operation(row) if row is not None else None

    def get_successful_operation(
        self, order_id: str, action: str, owner_id: str
    ) -> Operation | None:
        row = self.connection.execute(
            """SELECT * FROM operations WHERE order_id = ? AND action = ? AND owner_id = ?
                AND status = 'succeeded'""",
            (order_id, action, owner_id),
        ).fetchone()
        return _operation(row) if row is not None else None

    def get_successful_handoff(
        self, task_id: str, owner_id: str, order_id: str | None, reason: str
    ) -> Operation | None:
        row = self.connection.execute(
            """SELECT * FROM operations WHERE task_id = ? AND owner_id = ?
                AND order_id IS ? AND action = 'create_handoff' AND status = 'succeeded'
                AND json_extract(result_json, '$.reason') = ? LIMIT 1""",
            (task_id, owner_id, order_id, reason),
        ).fetchone()
        return _operation(row) if row is not None else None

    def finish_operation(
        self, operation_key: str, owner_id: str, *, status: Literal["succeeded", "failed"],
        committed_at: datetime | None, result: JsonValue,
    ) -> bool:
        require_transaction(self.connection)
        if status not in ("succeeded", "failed"):
            raise ValueError("finish_operation requires a terminal status")
        stored = self.get_operation(operation_key, owner_id)
        if stored is None or stored.status != "pending":
            return False
        operation = Operation.model_validate(stored.model_dump() | {
            "status": status, "committed_at": committed_at, "result": result,
        })
        values = operation.model_dump(mode="json")
        return self.connection.execute(
            """UPDATE operations SET status = ?, committed_at = ?, result_json = ?
                WHERE operation_key = ? AND owner_id = ? AND status = 'pending'""",
            (
                operation.status, values["committed_at"],
                json.dumps(values["result"], ensure_ascii=False, allow_nan=False),
                operation_key, owner_id,
            ),
        ).rowcount == 1
