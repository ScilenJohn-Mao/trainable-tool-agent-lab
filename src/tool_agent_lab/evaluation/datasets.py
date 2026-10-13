"""Build, audit and load immutable scenario splits with independent business instances."""

import hashlib
import json
import random
import shutil
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field

from tool_agent_lab.business.rules import BusinessRules
from tool_agent_lab.knowledge.ingest import PolicyCatalog
from tool_agent_lab.knowledge.search import B, K1, tokenize
from tool_agent_lab.schemas.actions import CouponAction, HandoffAction, RefundAction
from tool_agent_lab.schemas.business import Order
from tool_agent_lab.schemas.scenarios import (
    HistoricalRefund, Scenario, ScenarioExpectation, ScenarioInitialState, TaskType, UserBehaviour, WriteAction,
)
from tool_agent_lab.schemas.tasks import TaskCreate
from tool_agent_lab.settings import PROJECT_ROOT

DATA_VERSION = "scenarios-v1"
DEFAULT_SEED = 20261013
TASK_TYPES = ("policy_refund", "refund_and_coupon", "reconcile_operation", "clarify_or_handoff")
KNOWN_MODES = {
    "policy_refund": ("refund", "refund_rejected", "query"),
    "refund_and_coupon": ("combined", "combined_rejected"),
    "reconcile_operation": ("lookup",),
    "clarify_or_handoff": ("clarify_refund", "digital", "unverified", "unresolved"),
}
HELD_MODES = {"policy_refund": "refund_boundary", "refund_and_coupon": "combined_partial",
              "reconcile_operation": "lookup_coupon", "clarify_or_handoff": "digital_late"}
# Declarative labels are specified here; BusinessRules is used only to audit them.
MODES = {
    "refund": (("request_refund",), ("approved",), ("request_refund",), None),
    "refund_rejected": (("request_refund",), ("rejected",), (), None),
    "query": ((), (), (), None),
    "combined": (("request_refund", "issue_coupon"), ("approved", "approved"), ("request_refund", "issue_coupon"), None),
    "combined_rejected": (("request_refund", "issue_coupon"), ("rejected",), (), None),
    "lookup": ((), (), (), None),
    "clarify_refund": (("request_refund",), ("approved",), ("request_refund",), None),
    "digital": (("create_handoff",), ("approved",), ("create_handoff",), "unsupported_category"),
    "unverified": (("create_handoff",), ("approved",), ("create_handoff",), "unverified_damage"),
    "unresolved": (("create_handoff",), ("approved",), ("create_handoff",), "order_unresolved"),
    "refund_boundary": (("request_refund",), ("approved",), ("request_refund",), None),
    "combined_partial": (("request_refund", "issue_coupon"), ("approved", "rejected"), ("request_refund",), None),
    "lookup_coupon": (("issue_coupon",), ("approved",), ("issue_coupon",), None),
    "digital_late": (("create_handoff",), ("approved",), ("create_handoff",), "unsupported_category"),
}
INTRO = {"train": "你好，", "validation": "想问一下，", "new_instance": "麻烦问下，",
         "unseen_combination": "有件事挺闹心的，", "unseen_policy": "我这单碰到点问题，"}
TRAIN_STORIES = {
    "policy_refund": ("刚收到的{product}{problem}，我想退掉，能帮我看看吗？",
                      "{product}拆开就发现{problem}，这个能退钱吗？", "才买的{product}{problem}，不想要了，怎么退啊？"),
    "refund_and_coupon": ("{product}比说好的晚了很久才到，打开又发现{problem}，想退钱，晚到这么久有没有补偿？",
                          "等了好久的{product}总算到了，结果{problem}，能退钱吗？迟到的事能给点补偿吗？",
                          "这单晚来了一大截，{product}还{problem}，想把钱退回来，这么晚到有补偿吗？"),
    "reconcile_operation": ("之前这单退钱我点过同意，后来页面没反应了，现在到底退了没有？",
                            "我前面同意退钱了，但一直没看到结果，是成功了还是卡住了？",
                            "上回退钱的页面突然没动静了，我已经点了同意，这笔钱现在是什么情况？"),
    "clarify_or_handoff": ("买的{product}{problem}，这可怎么办，钱还能退吗？",
                           "{product}用不了，感觉有问题，这个能退吗？", "我买的{product}{problem}，还能怎么处理啊？"),
}
STORIES = {
    "train": TRAIN_STORIES,
    "validation": {
        "policy_refund": ("包裹里的{product}{problem}。才收到就这样，我想把钱退回来。",
                          "收到货后看见{product}{problem}，这单我不想留了，可以退款吗？",
                          "{product}到手时就{problem}，需要我怎么弄才能退钱？"),
        "refund_and_coupon": ("这单没按说好的时候到，{product}拿到又{problem}。退款和晚到补偿能处理吗？",
                              "{product}迟到了很久，收到以后还{problem}，我想退这单，迟到有没有补偿？",
                              "送来的{product}{problem}，还耽误了好久才送到，我要退钱，也想问问补偿的事。"),
        "reconcile_operation": ("退钱那次我已经同意了，最后没有显示结果，我怎么知道钱退没退？",
                                "上次点同意退钱以后页面不动了，能告诉我这单后面怎样了吗？",
                                "前面那个退钱申请，我点了同意就没消息了，钱到底有没有退回来？"),
        "clarify_or_handoff": ("这次买的{product}有点问题，{problem}，我该怎么办，能退钱吗？",
                               "{product}收到后{problem}，还能退掉吗，我不太懂怎么处理。",
                               "我用不了这个{product}，{problem}，有没有办法把钱退回来？"),
    },
    "new_instance": {
        "policy_refund": ("开箱一看，{product}{problem}，真没想到刚到就坏了，想退钱。",
                          "昨天拆包才发现{product}{problem}，这种情况还能退款吗？",
                          "这个{product}我刚拿到就{problem}，不打算留了，钱能退回来吧？"),
        "refund_and_coupon": ("{product}送来已经晚了好多，东西还{problem}，这单想退款，晚到有赔偿吗？",
                              "等到现在才收到{product}，一拿出来又{problem}，退钱和迟到补偿能不能办？",
                              "这单迟迟没到，到手的{product}又{problem}，想退掉，晚到能补偿一点吗？"),
        "reconcile_operation": ("我有一单退钱出了点状况，点同意后没结果，想知道钱有没有退回来。",
                                "退钱的进度怎么突然没了，我之前点过同意了，现在是啥状态？",
                                "当时那个退钱的同意按钮我点了，后面就没显示了，这钱退了吗？"),
        "clarify_or_handoff": ("{product}我买回来后一直没法正常用，{problem}，还可以退钱吗？",
                               "我收的{product}{problem}，想问问能不能把这单退了。",
                               "有个{product}买了没多久就发现{problem}，能给我个处理办法吗，最好能退钱。"),
    },
    "unseen_combination": {
        "policy_refund": ("{product}这单收到就是{problem}，一直没顾上问，现在想退钱还来得及吗？",
                          "这件{product}到货时{problem}，我想退款，不知道现在还能不能退。",
                          "前面送来的{product}{problem}，最近才想起来处理，这单能退吗？"),
        "refund_and_coupon": ("好不容易等到的{product}又{problem}，退钱和晚来这么久的补偿，我想分别听听怎么处理。",
                              "这单{product}晚到了不少，还{problem}，退款我想要，补偿能不能也给我说说？",
                              "钱想退掉，因为{product}{problem}，另外送得那么晚是不是也有补偿？"),
        "reconcile_operation": ("上回退钱时页面卡住过，我点的是同意，想知道那笔钱究竟处理得怎样了。",
                                "退钱的事情之前说过，我也同意了，但页面一直没有结尾，这单现在算退了吗？",
                                "我之前答应退这单的钱，页面后面却没结果了，钱的去向能看一下吗？"),
        "clarify_or_handoff": ("这个{product}{problem}，用不了还拖了挺久，这个钱能退吗？",
                               "买的{product}{problem}，遇到这种情况该怎么弄，我希望能退钱。",
                               "我这件{product}{problem}，前面也耽误了不少时间，想看看还可以退款吗。"),
    },
    "unseen_policy": {
        "policy_refund": ("{product}收到的时候就{problem}，放着也没用，我想退回这单的钱。",
                          "这单{product}{problem}，我不想继续留着，想问能不能退款。",
                          "拆出来的{product}{problem}，钱还能给我退回来吗？"),
        "refund_and_coupon": ("晚了很久才送来的{product}{problem}，实在不想留了，退款还有迟到补偿能帮忙处理吗？",
                              "送货拖了好久，拿到的{product}还{problem}，希望退钱，另外能有些补偿吗？",
                              "包裹来得很晚，{product}又{problem}，这笔钱想退了，晚到的事有没有补偿？"),
        "reconcile_operation": ("我之前同意退钱的那笔，一直没看到后续，能告诉我最后怎样了吗？",
                                "退钱页面没有结果，我那会儿已经同意了，这单的钱究竟退了没有？",
                                "有笔退钱的事我已经点了同意，但最后一页没显示出来，想问问现在的结果。"),
        "clarify_or_handoff": ("送来的{product}{problem}，这种情况我可以要求退钱吗？",
                               "这个{product}{problem}，买回来就有问题，我想看看能否退款。",
                               "这单的{product}{problem}，钱还能退吗，或者能给我想个办法吗？"),
    },
}
QUERY_STORIES = {"train": "我想看看这单买的是什么、当时付了多少钱，现在是什么状态？",
                 "validation": "我忘记这单具体买了啥，花了多少，现在是什么情况？",
                 "new_instance": "这单的东西叫什么，当时付了多少，现在处理到哪儿了？",
                 "unseen_combination": "这单买的东西、实付的钱和现在的情况能告诉我吗？",
                 "unseen_policy": "我有些记不清这单的商品和付款数了，也想问下现在是什么情况。"}
UNVERIFIED_STORIES = {"train": "买的{product}看着有点瑕疵，我也不确定算不算质量问题，可以退吗？",
                      "validation": "{product}好像有点不对劲，也不知道是不是质量问题，这种能退款吗？",
                      "new_instance": "我看这个{product}像有瑕疵，可我也说不准，想问能不能退钱。",
                      "unseen_combination": "这件{product}看起来有些毛病，还不太确定，能退掉吗？",
                      "unseen_policy": "感觉{product}有点问题，我拿不准是正常情况还是瑕疵，这能退吗？"}
PRODUCTS = ("保温杯", "玻璃花瓶", "收纳盒", "小夜灯", "数据线", "毛巾套装")
PROBLEMS = {"保温杯": "外壳磕坏了", "玻璃花瓶": "碎了", "收纳盒": "裂了", "小夜灯": "亮不起来",
            "数据线": "插上也没反应", "毛巾套装": "边上脱线了", "数字兑换码": "输进去一直报错"}


class DatasetScenario(Scenario):
    instance_id: str = Field(min_length=1)
    template_family: str = Field(min_length=1)
    condition_key: str = Field(min_length=1)
    cohort: Literal["train", "validation", "new_instance", "unseen_combination", "unseen_policy"]
    requested_actions: tuple[WriteAction, ...]
    mode: str = Field(min_length=1)


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_hash(path: Path) -> str:
    """Fingerprint text independently of Windows/Linux checkout line endings."""
    return hashlib.sha256(path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")


def policy_assets(source: Path) -> dict[str, dict]:
    spec = json.loads((source / "spec.json").read_text(encoding="utf-8"))
    catalog = json.loads((source / "policies.json").read_text(encoding="utf-8"))
    alternatives = json.loads((source / "policy_specs.json").read_text(encoding="utf-8"))
    next_spec = next(s for s in alternatives["versions"] if s["policy_version"] == "mock-policy-v2")
    next_spec = next_spec | {"version": "business-v2", "purpose": "固定场景业务规则", "task_types": list(TASK_TYPES)}
    current = [doc for doc in catalog["documents"] if doc["version"] == "mock-policy-v1"]
    transformed = []
    for doc in current:
        replacement = json.loads(json.dumps(doc, ensure_ascii=False)
                                 .replace("mock-policy-v1", "mock-policy-v2").replace("7×24", "10×24")
                                 .replace("7 天", "10 天").replace("七天", "十天").replace("500", "800"))
        replacement.update(effective_from=next_spec["effective_from"], effective_to=next_spec["effective_to"])
        transformed.append(replacement)
    inactive = []
    for doc in catalog["documents"]:
        if doc["version"] == "mock-policy-v2":
            continue
        doc = json.loads(json.dumps(doc))
        if doc["version"] == "mock-policy-v1":
            doc["source"] = doc["source"] | {"file": "policy_specs.json", "pointer": "/versions/1"}
        inactive.append(doc)
    old_spec = next(s for s in alternatives["versions"] if s["policy_version"] == "mock-policy-v0")
    return {
        "business-v1": {"spec.json": spec | {"purpose": "固定场景业务规则"},
                        "policies.json": {"format_version": 1, "documents": [d for d in catalog["documents"] if d["version"] != "mock-policy-v2"]},
                        "policy_specs.json": {"format_version": 1, "versions": [old_spec]}},
        "business-v2": {"spec.json": next_spec, "policies.json": {"format_version": 1, "documents": inactive + transformed},
                        "policy_specs.json": {"format_version": 1, "versions": [old_spec, spec]}},
    }


def index_fingerprint(spec: dict, catalog: dict) -> str:
    now = datetime.fromisoformat(spec["business_time"])
    entries = []
    for record in PolicyCatalog.model_validate(catalog).documents:
        if record.effective_from <= now < record.effective_to:
            for section in record.sections:
                document = record.document(section.section_id)
                entries.append({"reference": document.reference.model_dump(mode="json"), "category": record.category,
                                "tokens": sorted(Counter(tokenize(document.title + "\n" + document.text)).items())})
    return digest({"business_time": spec["business_time"], "k1": K1, "b": B, "entries": entries})


def conditions(scenario: DatasetScenario, spec: dict) -> dict:
    order = scenario.initial_state.orders[0] if scenario.initial_state.orders else None
    window = timedelta(days=spec["rules"]["refund"]["window_days"])
    age = scenario.business_time - order.delivered_at if order else None
    delay = order.delivered_at - order.promised_delivery_at if order else None
    minimum = timedelta(hours=spec["rules"]["delay_coupon"]["minimum_delay_hours"])
    return {"task_type": scenario.task_type, "policy_version": spec["policy_version"],
            "category": order.category if order else None, "damage_verified": order.damage_verified if order else None,
            "refund_age": "none" if age is None else "inside" if age < window else "equal" if age == window else "outside",
            "delay": "none" if delay is None else "below" if delay < minimum else "equal" if delay == minimum else "above",
            "initial_refund": bool(order and order.refunded_amount_minor), "clarification": bool(scenario.user.input_messages),
            "requested_actions": scenario.requested_actions, "decisions": scenario.user.approval_decisions}


def make_scenario(rng: random.Random, task_type: TaskType, cohort: str, index: int, spec: dict) -> DatasetScenario:
    modes = KNOWN_MODES[task_type]
    mode = HELD_MODES[task_type] if cohort == "unseen_combination" else modes[index % len(modes)]
    requested, decisions, committed, handoff = MODES[mode]
    identity = f"{rng.getrandbits(64):016x}"
    instance_id, owner_id, order_id = "case-" + identity, "customer-" + identity, "ORD-" + identity.upper()
    now = datetime.fromisoformat(spec["business_time"])
    window = spec["rules"]["refund"]["window_days"]
    age = timedelta(days=window if mode == "refund_boundary" else rng.randint(1, window - 1))
    delay = timedelta(hours=rng.randint(26, 72) if mode in ("combined", "combined_rejected", "combined_partial", "lookup_coupon", "digital_late") else rng.randint(1, 23))
    paid = rng.randint(49, 999) * 100 + rng.choice((0, 1, 50, 99))
    historical = task_type == "reconcile_operation"
    unresolved = mode == "unresolved"
    order = Order(order_id=order_id, owner_id=owner_id,
                  product_name="数字兑换码" if mode in ("digital", "digital_late") else rng.choice(PRODUCTS),
                  category="digital_goods" if mode in ("digital", "digital_late") else "general_goods",
                  paid_amount_minor=paid, promised_delivery_at=now - age - delay, delivered_at=now - age,
                  damage_verified=mode not in ("digital", "digital_late", "unverified"),
                  refunded_amount_minor=paid if historical else 0)
    operation_key = "refund-" + identity if historical else None
    history = (HistoricalRefund(order_id=order_id, operation_key=operation_key, approval_request_id="history-" + identity,
                                approved_at=now - timedelta(hours=1, minutes=1), committed_at=now - timedelta(hours=1)),) if historical else ()
    clarify = mode in ("clarify_refund", "unresolved")
    message = INTRO[cohort] + STORIES[cohort][task_type][index % 3].format(product=order.product_name, problem=PROBLEMS[order.product_name])
    if mode == "query":
        message = INTRO[cohort] + QUERY_STORIES[cohort]
    if mode == "unverified":
        message = INTRO[cohort] + UNVERIFIED_STORIES[cohort].format(product=order.product_name)
    if not clarify:
        message += f"订单号是 {order_id}。"
    if operation_key:
        message += f"页面上留了这个记录号 {operation_key}。"
    if mode == "lookup_coupon":
        message += "对了，这单还晚来了好多天，之前说的晚到补偿也没见着。"
    reply = "我找了半天，实在找不到订单号了。" if unresolved else f"找到了，是 {order_id}。"
    expected = ScenarioExpectation(
        order_id=None if unresolved else order_id, refund_total_minor=paid if historical or "request_refund" in committed else 0,
        coupon_total_minor=spec["rules"]["delay_coupon"]["amount_minor"] if "issue_coupon" in committed else 0,
        new_actions=committed, must_clarify=clarify, must_query_operation=historical, handoff_reason=handoff,
        rule_ids=tuple(dict.fromkeys({"request_refund": "R-REFUND-01", "issue_coupon": "R-DELAY-01", "create_handoff": "R-HANDOFF-01"}[a]
                                    for a in (("request_refund",) if historical else ()) + requested)),
        outcome="resolved" if committed else "rejected" if "rejected" in decisions else "answered",
    )
    scenario = DatasetScenario(
        scenario_id=instance_id + "-a", instance_id=instance_id, family_id=instance_id,
        task_type=task_type, cohort=cohort, mode=mode, template_family=f"{cohort}:{task_type}", condition_key="pending",
        fixture_version=spec["version"], business_time=now, owner_id=owner_id,
        request=TaskCreate(user_message=message, order_id=None if clarify else order_id), known_operation_key=operation_key,
        requested_actions=requested, user=UserBehaviour(input_messages=(reply,) if clarify else (), approval_decisions=decisions),
        expected=expected, initial_state=ScenarioInitialState(data_dir=Path("assets") / spec["version"],
                                                             orders=() if unresolved else (order,), historical_refunds=history),
    )
    return scenario.model_copy(update={"condition_key": digest(conditions(scenario, spec))})


def generate_splits(assets: dict, *, seed: int = DEFAULT_SEED, profile: str = "full") -> dict[str, list[DatasetScenario]]:
    if profile not in ("full", "small"):
        raise ValueError("profile must be full or small")
    rng = random.Random(seed)
    splits = {"train": [], "validation": [], "test": []}
    current = assets["business-v1"]["spec.json"]
    future = assets["business-v2"]["spec.json"]
    for task_type in TASK_TYPES:
        for split, root_count, rewrites in (("train", 80, 20), ("validation", 20, 5)) if profile == "full" else (("train", 3, 1), ("validation", 2, 0)):
            for index in range(root_count):
                scenario = make_scenario(rng, task_type, split, index, current)
                splits[split].append(scenario)
                if index < rewrites:
                    rewritten = scenario.model_copy(update={"scenario_id": scenario.instance_id + "-b", "request": TaskCreate(
                        user_message=scenario.request.user_message + "这事有点着急，麻烦回复一下，谢谢。", order_id=scenario.request.order_id,
                    )})
                    splits[split].append(rewritten)
        for index in range(25 if profile == "full" else 1):
            splits["test"].append(make_scenario(rng, task_type, "new_instance", index, current))
        held_count = 25 if profile == "full" else 1
        for index in range(held_count):
            unseen_policy = (index + TASK_TYPES.index(task_type)) % 2 == 0
            splits["test"].append(make_scenario(rng, task_type, "unseen_policy" if unseen_policy else "unseen_combination",
                                                index // 2 if unseen_policy else index, future if unseen_policy else current))
    for rows in splits.values():
        rng.shuffle(rows)
    return splits


def intended_action(name: str, scenario: DatasetScenario, rules: BusinessRules):
    if name == "create_handoff":
        return HandoffAction(order_id=scenario.expected.order_id, reason=scenario.expected.handoff_reason,
                             summary="Review the recorded order facts", policy_refs=(rules.reference(name),))
    amount = scenario.initial_state.orders[0].paid_amount_minor if name == "request_refund" else rules.spec.rules.delay_coupon.amount_minor
    action_type = RefundAction if name == "request_refund" else CouponAction
    return action_type(order_id=scenario.expected.order_id, amount_minor=amount, reason="Recorded delivery facts",
                       policy_refs=(rules.reference(name),))


def audit_scenario(scenario: DatasetScenario, asset: dict) -> None:
    spec, rules = asset["spec.json"], BusinessRules(asset["spec.json"])
    if (scenario.fixture_version, scenario.business_time) != (spec["version"], rules.business_time):
        raise ValueError("Scenario policy binding mismatch")
    if scenario.condition_key != digest(conditions(scenario, spec)):
        raise ValueError("Scenario condition key mismatch")
    if scenario.template_family != f"{scenario.cohort}:{scenario.task_type}" or not scenario.request.user_message.startswith(INTRO[scenario.cohort]):
        raise ValueError("Scenario template family mismatch")
    if scenario.initial_state.data_dir.as_posix() != f"assets/{scenario.fixture_version}":
        raise ValueError("Scenario asset path mismatch")
    order = scenario.initial_state.orders[0] if scenario.initial_state.orders else None
    if order and (order.owner_id != scenario.owner_id or order.order_id != scenario.expected.order_id):
        raise ValueError("Scenario order ownership mismatch")
    if scenario.request.order_id is not None and (order is None or scenario.request.order_id != order.order_id):
        raise ValueError("Request order mismatch")
    if bool(scenario.user.input_messages) != scenario.expected.must_clarify:
        raise ValueError("Clarification label mismatch")
    if bool(scenario.known_operation_key) != scenario.expected.must_query_operation:
        raise ValueError("Operation lookup label mismatch")
    if scenario.known_operation_key and not any(h.operation_key == scenario.known_operation_key for h in scenario.initial_state.historical_refunds):
        raise ValueError("Unknown historical operation")
    historical_total = 0
    for history in scenario.initial_state.historical_refunds:
        if order is None or history.order_id != order.order_id or not order.delivered_at <= history.approved_at <= history.committed_at <= scenario.business_time:
            raise ValueError("Invalid historical refund")
        before = order.model_copy(update={"refunded_amount_minor": 0})
        action = RefundAction(order_id=order.order_id, amount_minor=order.paid_amount_minor, reason="Historical verified damage",
                              policy_refs=(rules.reference("request_refund"),))
        if not rules.evaluate(action, before, business_time=history.committed_at).allowed:
            raise ValueError("Historical refund violates policy")
        historical_total += order.paid_amount_minor
    if order and (order.refunded_amount_minor != historical_total or order.coupon_amount_minor != 0):
        raise ValueError("Initial balances do not match history")
    committed = []
    for index, name in enumerate(scenario.requested_actions):
        action = intended_action(name, scenario, rules)
        if not rules.evaluate(action, order, clarification_attempted=scenario.expected.must_clarify).allowed:
            raise ValueError("Declared goal is not legally solvable")
        if index >= len(scenario.user.approval_decisions):
            break
        if scenario.user.approval_decisions[index] == "rejected":
            break
        committed.append(name)
        if order and name in ("request_refund", "issue_coupon"):
            field = "refunded_amount_minor" if name == "request_refund" else "coupon_amount_minor"
            order = order.model_copy(update={field: action.amount_minor})
    expected = scenario.expected
    outcome = "resolved" if committed else "rejected" if "rejected" in scenario.user.approval_decisions else "answered"
    if tuple(committed) != expected.new_actions or expected.outcome != outcome:
        raise ValueError("Action or outcome label mismatch")
    if (order.refunded_amount_minor if order else 0, order.coupon_amount_minor if order else 0) != (expected.refund_total_minor, expected.coupon_total_minor):
        raise ValueError("Amount label mismatch")


def audit_splits(splits: dict[str, list[DatasetScenario]], assets: dict) -> dict:
    instances, families, templates, order_ids, scenario_ids = {}, {}, {}, {}, set()
    initial_fingerprints = {}
    known, held = set(), set()
    for split, rows in splits.items():
        for scenario in rows:
            if scenario.scenario_id in scenario_ids:
                raise ValueError("Duplicate scenario ID")
            scenario_ids.add(scenario.scenario_id)
            allowed = (split,) if split != "test" else ("new_instance", "unseen_combination", "unseen_policy")
            if scenario.cohort not in allowed:
                raise ValueError("Cohort in wrong split")
            if scenario.family_id != scenario.instance_id:
                raise ValueError("Instance family mismatch")
            for value, registry in ((scenario.instance_id, instances), (scenario.family_id, families), (scenario.template_family, templates)):
                if value in registry and registry[value] != split:
                    raise ValueError("Instance, family or template leaks across splits")
                registry[value] = split
            for order in scenario.initial_state.orders:
                if order.order_id in order_ids and order_ids[order.order_id] != (split, scenario.instance_id):
                    raise ValueError("Order instance leaks across splits or aliases another instance")
                order_ids[order.order_id] = (split, scenario.instance_id)
            initial_hash = digest({"initial": scenario.initial_state.model_dump(mode="json"), "owner": scenario.owner_id,
                                   "user": scenario.user.model_dump(mode="json"), "goal": scenario.requested_actions,
                                   "expected": scenario.expected.model_dump(mode="json")})
            if scenario.instance_id in initial_fingerprints and initial_fingerprints[scenario.instance_id] != initial_hash:
                raise ValueError("Paraphrases have inconsistent initial states or labels")
            initial_fingerprints[scenario.instance_id] = initial_hash
            audit_scenario(scenario, assets[scenario.fixture_version])
            (held if scenario.cohort == "unseen_combination" else known if split != "test" else set()).add(scenario.condition_key)
            if scenario.cohort == "unseen_policy" and scenario.fixture_version != "business-v2":
                raise ValueError("Unseen policy cohort must use business-v2")
            if split != "test" and scenario.fixture_version != "business-v1":
                raise ValueError("Held-out policy leaks into development splits")
    if known & held:
        raise ValueError("Held-out condition combination leaks into development splits")
    return {split: {"count": len(rows), "instances": len({s.instance_id for s in rows}),
                    "task_types": dict(sorted(Counter(s.task_type for s in rows).items())),
                    "cohorts": dict(sorted(Counter(s.cohort for s in rows).items()))} for split, rows in splits.items()}


def source_fingerprints() -> dict[str, str]:
    files = ("src/tool_agent_lab/business/rules.py", "src/tool_agent_lab/knowledge/search.py",
             "src/tool_agent_lab/knowledge/ingest.py", "data/business/v1/spec.json", "data/business/v1/policy_specs.json",
             "data/business/v1/policies.json", "data/cases/boundary.jsonl")
    return {name: text_hash(PROJECT_ROOT / name) for name in files}


def build_dataset(output: str | Path, *, seed: int = DEFAULT_SEED, profile: str = "full") -> dict:
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError(f"Dataset is immutable; choose a new output directory: {output}")
    assets = policy_assets(PROJECT_ROOT / "data/business/v1")
    splits = generate_splits(assets, seed=seed, profile=profile)
    summary = audit_splits(splits, assets)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / (".dataset-" + uuid4().hex)
    staging.mkdir()
    try:
        for version, files in assets.items():
            for name, value in files.items():
                write_json(staging / "assets" / version / name, value)
        for split, rows in splits.items():
            (staging / (split + ".jsonl")).write_bytes(b"".join(canonical(s.model_dump(mode="json")) + b"\n" for s in rows))
        pool = {s.instance_id: s for rows in splits.values() for s in rows}
        review_rng = random.Random(seed + 1)
        shuffled = sorted(pool.values(), key=lambda s: s.scenario_id)
        review_rng.shuffle(shuffled)
        groups = {}
        for scenario in shuffled:
            groups.setdefault((scenario.cohort, scenario.mode), scenario)
        selected = list(groups.values())
        selected_ids = {s.instance_id for s in selected}
        selected.extend(s for s in shuffled if s.instance_id not in selected_ids)
        selected = selected[:min(50, len(pool))]
        (staging / "review_sample.jsonl").write_bytes(b"".join(canonical(s.model_dump(mode="json")) + b"\n" for s in selected))
        files = {p.relative_to(staging).as_posix(): {"sha256": file_hash(p), "bytes": p.stat().st_size}
                 for p in sorted(staging.rglob("*")) if p.is_file()}
        manifest = {"version": DATA_VERSION, "frozen": True, "seed": seed, "profile": profile, "splits": summary,
                    "files": files, "source_fingerprints": source_fingerprints(),
                    "generator_sha256": text_hash(Path(__file__)), "index_fingerprints": {
                        version: index_fingerprint(files["spec.json"], files["policies.json"]) for version, files in assets.items()},
                    "isolation": {"instance_and_paraphrases": "same_split", "template_families": "disjoint_splits",
                                  "new_instance_conditions": "development_conditions_allowed",
                                  "unseen_combinations": "disjoint_train_validation", "unseen_policy": "business-v2_test_only"},
                    "review_sample_ids": [s.scenario_id for s in selected]}
        write_json(staging / "manifest.json", manifest)
        staging.rename(output)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def validate_frozen(path: str | Path) -> dict:
    root = Path(path).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest["version"] != DATA_VERSION or manifest["frozen"] is not True:
        raise ValueError("Dataset manifest is not a frozen supported version")
    for name, expected in manifest["files"].items():
        target = (root / name).resolve()
        if not target.is_relative_to(root) or not target.is_file() or file_hash(target) != expected["sha256"] or target.stat().st_size != expected["bytes"]:
            raise ValueError(f"Frozen file mismatch: {name}")
    if manifest["source_fingerprints"] != source_fingerprints():
        raise ValueError("Frozen rule, index source or boundary fingerprints changed")
    assets = {version: {name: json.loads((root / "assets" / version / name).read_text(encoding="utf-8"))
                       for name in ("spec.json", "policies.json", "policy_specs.json")} for version in ("business-v1", "business-v2")}
    if {version: index_fingerprint(files["spec.json"], files["policies.json"]) for version, files in assets.items()} != manifest["index_fingerprints"]:
        raise ValueError("Frozen index fingerprint mismatch")
    splits = {split: [DatasetScenario.model_validate_json(line) for line in (root / (split + ".jsonl")).read_text(encoding="utf-8").splitlines()]
              for split in ("train", "validation", "test")}
    if audit_splits(splits, assets) != manifest["splits"]:
        raise ValueError("Frozen split summary mismatch")
    return manifest


def load_dataset(path: str | Path, split: Literal["train", "validation", "test"]) -> tuple[DatasetScenario, ...]:
    if split not in ("train", "validation", "test"):
        raise ValueError("Unknown dataset split")
    root = Path(path).resolve()
    validate_frozen(root)
    rows = [DatasetScenario.model_validate_json(line) for line in (root / (split + ".jsonl")).read_text(encoding="utf-8").splitlines()]
    return tuple(s.model_copy(update={"initial_state": s.initial_state.model_copy(update={"data_dir": root / s.initial_state.data_dir})}) for s in rows)
