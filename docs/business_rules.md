# 售后规则资格检查

`BusinessRules` 从模拟规格 `spec.json` 读取金额、时间阈值、品类、规则 ID、版本和生效区间，接收共享的 `Order` 及 `RefundAction` / `CouponAction` / `HandoffAction`。评估只返回 `RuleDecision`，不修改订单、写入账本、消费确认或调用模型。

```python
import json

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.schemas.actions import RefundAction
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.settings import load_settings

data_dir = load_settings().business_data_dir
rules = BusinessRules.from_file(data_dir / "spec.json")
orders = json.loads((data_dir / "orders.json").read_text(encoding="utf-8"))
order = Order.model_validate(next(item for item in orders if item["order_id"] == "ORD-1001"))
action = RefundAction(order_id=order.order_id, amount_minor=order.paid_amount_minor,
                      reason="到货损坏已核实", policy_refs=(rules.reference("request_refund"),))
decision = rules.evaluate(action, order)
print(decision.model_dump_json(indent=2))
```

当前样例返回 `allowed: true`、`code: eligible`、`rule_id: R-REFUND-01`、`policy_version: mock-policy-v1` 和 `expected_amount_minor: 12900`。`allowed` 仅表示业务资格符合，执行仍需服务核对归属、完整确认快照和操作账本。

## 时间与金额

默认业务时间取规格中的 `2026-09-17T12:00:00+08:00`；传入 `business_time` 可检查其他业务时间，必须带时区。程序不读取电脑当前时间来改变样例资格。生效时间为起点包含、终点不包含；计算持续时长时统一到 UTC。

| 动作 | 资格及金额 |
|---|---|
| request_refund | 未退款、支持品类、已收货且损坏已核实；距收货最多 7×24 小时，等于 7 天仍有效；金额必须等于全部实付 |
| issue_coupon | 支持品类、已收货、未补偿，实际送达至少比承诺晚 24 小时；金额必须为 500 分；不依赖损坏核实或退款期限，可与已完成退款兼容 |
| create_handoff | 原因与订单事实吻合：不支持品类、退款超期或未核实损坏；无订单则须先澄清，仍无法定位时使用 order_unresolved |

上述阈值来自当前规格。实付、已退/已补金额和请求金额使用 CNY 严格整数分；浮点、布尔、非正数请求金额由共享契约拒绝。全额退款、每单一次、需要核实损坏及补偿兼容退款是这个规则版本支持的形状，载入不支持的配置值会直接报错。

## 返回值与调用约定

`RuleDecision` 保留 `allowed`、稳定的 `code`、规则/政策版本，以及适用时的 `expected_amount_minor` 和 `handoff_reason`。期望金额用于解释参数约束；拒绝时它也可能存在，不能据此执行写操作。

- `policy_not_active` 或 `policy_reference_mismatch`：当前业务时间不在有效区间，或引用与已载入规则不符。
- `clarification_required` / `order_unresolved` / `order_mismatch`：先获取订单；只有 `clarification_attempted=True` 且仍缺订单时，空订单转人工才符合。这个参数由运行时提供。
- `already_refunded` / `already_compensated`：禁止新增同类动作，由服务核对原账本结果。
- `unsupported_category` / `not_delivered` / `unverified_damage` / `outside_refund_window` / `insufficient_delay`：订单事实不符合对应动作的资格。
- `amount_mismatch`：请求金额与本规则要求不一致。
- `handoff_not_applicable` / `handoff_reason_mismatch`：当前无需按这些原因转人工，或所提原因不符事实。多个原因同时成立时可选择其中一个；已退款的普通商品不因重复退款请求自动转人工。

`reference(action_name)` 生成规格内规则引用，位置为 `spec.json#/rules/...`。评估要求请求包含该动作的完整引用，附加引用也须属于已载入的规则；ID、版本、位置和生效时间均核对。这些是规格引用，不是检索文档或搜索结果。金额及资格校验独立于模型文本，不自动推测订单或安排工具顺序。

调用方负责提供可信订单和运行时上下文。规则资格不能替代人工批准、当前提案版本/过期检查或同事务写入。订单余额能阻止明显重复申请，实际改键重复写入还须由业务服务核对账本并依赖数据库唯一约束。

运行纯规则与关键边界检查：

```powershell
uv run --no-sync --cache-dir .uv-cache python -m pytest -q tests/unit/test_business_rules.py
```
