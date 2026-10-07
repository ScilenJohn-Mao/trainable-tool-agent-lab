"""Check policy evidence against its versioned business specifications."""

import json
import tomllib
from collections import Counter
from datetime import datetime, timedelta

import pytest

from scripts.package_project import collect_sources
from tool_agent_lab.business.rules import BusinessRules, RuleCode
from tool_agent_lab.schemas.actions import CouponAction, PolicyReference, RefundAction
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.settings import PROJECT_ROOT
from tool_agent_lab.tools.contracts import PolicyDocument

DATA = PROJECT_ROOT / "data/business/v1"
SPEC = json.loads((DATA / "spec.json").read_text(encoding="utf-8"))
CORPUS = json.loads((DATA / "policies.json").read_text(encoding="utf-8"))
DOCUMENTS = CORPUS["documents"]
ARCHIVE = json.loads((DATA / "policy_specs.json").read_text(encoding="utf-8"))


def source_spec(document: dict) -> dict:
    source = document["source"]
    value = json.loads((DATA / source["file"]).read_text(encoding="utf-8"))
    for token in source["pointer"].split("/")[1:]:
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def document(policy_id: str, version: str = "mock-policy-v1") -> dict:
    return next(item for item in DOCUMENTS if (item["policy_id"], item["version"]) == (policy_id, version))


def text(policy_id: str, version: str = "mock-policy-v1") -> str:
    return "\n".join(section["text"] for section in document(policy_id, version)["sections"])


def test_catalog_has_distinct_topics_versions_categories_and_no_answer_labels() -> None:
    assert CORPUS["format_version"] == ARCHIVE["format_version"] == 1
    assert len(DOCUMENTS) == 24
    assert len({(item["policy_id"], item["version"]) for item in DOCUMENTS}) == 24
    assert len({item["policy_id"] for item in DOCUMENTS}) == 20
    assert Counter(item["version"] for item in DOCUMENTS) == {
        "mock-policy-v1": 20, "mock-policy-v0": 2, "mock-policy-v2": 2,
    }
    assert {item["category"] for item in DOCUMENTS} == {"general_goods", "digital_goods", "all"}
    assert {rule for item in DOCUMENTS for rule in item["source"]["rule_ids"]} == {
        "R-REFUND-01", "R-DELAY-01", "R-HANDOFF-01",
    }
    assert "expected" not in json.dumps(CORPUS) and "clarification_reply" not in json.dumps(CORPUS)


@pytest.mark.parametrize("item", DOCUMENTS, ids=lambda item: f'{item["policy_id"]}:{item["version"]}')
def test_document_source_and_each_citable_section_match_shared_contract(item: dict) -> None:
    assert set(item) == {
        "policy_id", "version", "title", "category", "effective_from", "effective_to", "source", "sections",
    }
    spec = source_spec(item)
    assert item["version"] == spec["policy_version"]
    assert item["effective_from"] == spec["effective_from"]
    assert item["effective_to"] == spec["effective_to"]
    assert item["source"]["rule_ids"]
    assert set(item["source"]["rule_ids"]) <= {rule["rule_id"] for rule in spec["rules"].values()}
    assert item["sections"]
    ids = [section["section_id"] for section in item["sections"]]
    assert len(set(ids)) == len(ids)
    for section in item["sections"]:
        assert set(section) == {"section_id", "text"}
        reference = PolicyReference(
            policy_id=item["policy_id"], version=item["version"], section=section["section_id"],
            effective_from=item["effective_from"], effective_to=item["effective_to"],
        )
        result = PolicyDocument(reference=reference, title=item["title"], category=item["category"], text=section["text"])
        assert PolicyDocument.model_validate_json(result.model_dump_json()) == result
        located = next(entry for entry in DOCUMENTS if (
            entry["policy_id"], entry["version"]
        ) == (reference.policy_id, reference.version))
        assert next(s["text"] for s in located["sections"] if s["section_id"] == reference.section) == result.text


# Literal statements are reviewed independently of the rule evaluator.
TEXT_REQUIREMENTS = {
    "P-REFUND-ELIGIBILITY": ("damage_verified=true", "7×24 小时", "全额退款", "每订单最多一笔", "人工确认"),
    "P-REFUND-AMOUNT": ("paid_amount_minor", "100 分等于 1 元", "12900 分", "不提供部分退款"),
    "P-REFUND-WINDOW": ("恰好 7 天包含在内", "outside_refund_window", "不使用电脑当天时间"),
    "P-REFUND-ONCE": ("refunded_amount_minor 大于 0", "更换操作键或重新确认也不能再次退"),
    "P-DELAY-ELIGIBILITY": ("至少 24 小时", "恰好 24 小时符合", "不要求损坏已核实", "不继承退款的 7 天期限"),
    "P-DELAY-AMOUNT": ("500 分 CNY，即 5 元券", "不按实付比例计算", "错误金额即使确认也不能执行"),
    "P-DELAY-ONCE": ("每订单最多一张", "coupon_amount_minor 大于 0", "已有退款不等于已有补偿"),
    "P-COMBINED-ACTIONS": ("可以组合", "两个动作分别满足各自条件", "逐项提案和确认", "可能部分完成"),
    "P-DIGITAL-HANDOFF": ("digital_goods", "不能自动退款或发券", "unsupported_category", "人工确认"),
    "P-HANDOFF-REASONS": ("unsupported_category", "outside_refund_window", "unverified_damage", "order_unresolved", "转人工无金额"),
    "P-ORDER-CLARIFICATION": ("先请求用户补充订单号", "已经澄清一次仍无法定位订单", "order_id 为 null"),
    "P-DAMAGE-VERIFICATION": ("damage_verified=true", "unverified_damage", "损坏未核实不阻断独立的延迟补偿资格"),
    "P-DELIVERY-FACTS": ("尚未实际收货", "不满足自动退款或延迟补偿", "不以用户报出的天数替换订单记录"),
    "P-POLICY-VALIDITY": ("包含生效时刻、不包含失效时刻", "不能选过期 v0 或尚未生效 v2", "不能自行放行业务写入"),
    "P-WRITE-APPROVAL": ("request_refund、issue_coupon、create_handoff", "人工拒绝后不执行", "错误金额"),
    "P-PROPOSAL-CHANGES": ("重新请求人工确认", "旧确认绑定原快照", "不重复消费批准"),
    "P-OPERATION-LOOKUP": ("pending", "succeeded", "failed", "查无记录为 null", "其他归属的键都不暴露记录"),
    "P-UNCERTAIN-OUTCOME": ("不等于业务未执行", "先查询原键", "不能换键重新退款或发券", "政策说明本身不会执行重试或恢复"),
    "P-FAILED-OPERATION": ("failed 是明确失败终态", "不能把 failed 重置成 pending", "pending 记录可能仍然存在"),
    "P-HANDOFF-ONCE": ("转人工无退款或补偿金额", "订单与原因", "改键或重新确认不能重复创建"),
}


def test_current_policy_text_covers_reviewed_business_and_execution_constraints() -> None:
    assert set(TEXT_REQUIREMENTS) == {item["policy_id"] for item in DOCUMENTS if item["version"] == SPEC["policy_version"]}
    for policy_id, required in TEXT_REQUIREMENTS.items():
        for statement in required:
            assert statement in text(policy_id), (policy_id, statement)
    assert SPEC["rules"]["refund"]["window_days"] == 7
    assert SPEC["rules"]["delay_coupon"]["amount_minor"] == 500
    assert SPEC["write_actions_requiring_approval"] == ["request_refund", "issue_coupon", "create_handoff"]


@pytest.mark.parametrize("version,days,amount", [
    ("mock-policy-v0", 5, 300), ("mock-policy-v1", 7, 500), ("mock-policy-v2", 10, 800),
])
def test_version_text_and_independent_rule_boundary_expectations(version: str, days: int, amount: int) -> None:
    spec = source_spec(document("P-REFUND-ELIGIBILITY", version))
    assert spec["rules"]["refund"]["window_days"] == days
    assert spec["rules"]["delay_coupon"]["amount_minor"] == amount
    assert f"{days}×24 小时" in text("P-REFUND-ELIGIBILITY", version)
    assert f"{amount} 分 CNY" in text("P-DELAY-AMOUNT", version)
    rules = BusinessRules(spec)
    now = rules.business_time
    order = Order.model_validate(json.loads((DATA / "orders.json").read_text(encoding="utf-8"))[0])
    delivered = now - timedelta(days=days)
    order = order.model_copy(update={"delivered_at": delivered, "promised_delivery_at": delivered - timedelta(hours=24)})
    refund = RefundAction(order_id=order.order_id, amount_minor=12900, reason="verified damage", policy_refs=(rules.reference("request_refund"),))
    coupon = CouponAction(order_id=order.order_id, amount_minor=amount, reason="delayed", policy_refs=(rules.reference("issue_coupon"),))
    assert rules.evaluate(refund, order).code == RuleCode.ELIGIBLE
    assert rules.evaluate(refund, order.model_copy(update={"delivered_at": delivered - timedelta(seconds=1)})).code == RuleCode.OUTSIDE_REFUND_WINDOW
    assert rules.evaluate(coupon, order).code == RuleCode.ELIGIBLE
    assert rules.evaluate(coupon, order.model_copy(update={"promised_delivery_at": delivered - timedelta(hours=24) + timedelta(seconds=1)})).code == RuleCode.INSUFFICIENT_DELAY


def test_only_current_documents_apply_at_fixed_clock_with_half_open_intervals() -> None:
    now = datetime.fromisoformat(SPEC["business_time"])
    active = [item for item in DOCUMENTS if datetime.fromisoformat(item["effective_from"]) <= now < datetime.fromisoformat(item["effective_to"])]
    assert len(active) == 20 and {item["version"] for item in active} == {"mock-policy-v1"}
    for version in ["mock-policy-v0", "mock-policy-v1", "mock-policy-v2"]:
        spec = source_spec(document("P-REFUND-ELIGIBILITY", version))
        rules = BusinessRules(spec)
        order = Order.model_validate(json.loads((DATA / "orders.json").read_text(encoding="utf-8"))[0])
        start = datetime.fromisoformat(spec["effective_from"])
        order = order.model_copy(update={"delivered_at": start})
        action = RefundAction(order_id=order.order_id, amount_minor=12900, reason="damage", policy_refs=(rules.reference("request_refund"),))
        assert rules.evaluate(action, order, business_time=start).code == RuleCode.ELIGIBLE
        assert rules.evaluate(action, order, business_time=start - timedelta(seconds=1)).code == RuleCode.POLICY_NOT_ACTIVE
        assert rules.evaluate(action, order, business_time=datetime.fromisoformat(spec["effective_to"])).code == RuleCode.POLICY_NOT_ACTIVE


def test_document_citation_does_not_replace_execution_rule_reference() -> None:
    item = document("P-REFUND-ELIGIBILITY")
    reference = PolicyReference(
        policy_id=item["policy_id"], version=item["version"], section="eligibility",
        effective_from=item["effective_from"], effective_to=item["effective_to"],
    )
    order = Order.model_validate(json.loads((DATA / "orders.json").read_text(encoding="utf-8"))[0])
    action = RefundAction(order_id=order.order_id, amount_minor=12900, reason="damage", policy_refs=(reference,))
    assert BusinessRules(SPEC).evaluate(action, order).code == RuleCode.POLICY_REFERENCE_MISMATCH


def test_package_includes_policy_corpus_and_versioned_sources(tmp_path) -> None:
    bundle = collect_sources(PROJECT_ROOT, tmp_path / "packages")
    for name in ["policies.json", "policy_specs.json"]:
        relative = f"data/business/v1/{name}"
        assert bundle.files[relative] == (DATA / name).read_bytes()
    rules = tomllib.loads((PROJECT_ROOT / "deploy/package_rules.toml").read_text(encoding="utf-8"))
    assert {"data/business/v1/policies.json", "data/business/v1/policy_specs.json"} <= set(rules["required_files"])
