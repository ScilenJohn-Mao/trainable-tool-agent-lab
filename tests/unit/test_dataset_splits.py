import json
from pathlib import PurePosixPath

import pytest

from tool_agent_lab.evaluation.datasets import (
    INTRO, QUERY_STORIES, STORIES, TASK_TYPES, UNVERIFIED_STORIES,
    audit_scenario, audit_splits, build_dataset, generate_splits, load_dataset, policy_assets, text_hash, validate_frozen,
)
from tool_agent_lab.settings import PROJECT_ROOT


@pytest.fixture
def assets():
    return policy_assets(PROJECT_ROOT / "data/business/v1")


def test_full_scale_distinct_instances_balanced_categories_and_test_cohorts(assets):
    splits = generate_splits(assets)
    summary = audit_splits(splits, assets)
    assert [summary[s]["count"] for s in ("train", "validation", "test")] == [400, 100, 200]
    assert [summary[s]["instances"] for s in ("train", "validation", "test")] == [320, 80, 200]
    assert summary["test"]["cohorts"] == {"new_instance": 100, "unseen_combination": 50, "unseen_policy": 50}
    for split, count in (("train", 100), ("validation", 25), ("test", 50)):
        assert summary[split]["task_types"] == dict.fromkeys(TASK_TYPES, count)
    assert len({s.instance_id for rows in splits.values() for s in rows}) == 600
    assert len({s.scenario_id for rows in splits.values() for s in rows}) == 700
    assert any(s.cohort == "unseen_policy" and s.expected.coupon_total_minor == 800 for s in splits["test"])


def test_user_requests_describe_everyday_problems_without_tool_or_workflow_instructions(assets):
    splits = generate_splits(assets)
    forbidden = ("查询订单", "查询政策", "核对原", "原操作键", "分别确认", "自动退款", "转人工", "政策版本", "核对平台")
    for rows in splits.values():
        for scenario in rows:
            text = scenario.request.user_message + "".join(scenario.user.input_messages)
            assert not any(term in text for term in forbidden)
    assert any("碎了" in s.request.user_message for rows in splits.values() for s in rows)
    assert any("瑕疵" in s.request.user_message for rows in splits.values() for s in rows)


def test_template_bodies_are_disjoint_rather_than_only_renaming_greetings():
    bodies = {cohort: {text for rows in types.values() for text in rows} | {QUERY_STORIES[cohort], UNVERIFIED_STORIES[cohort]}
              for cohort, types in STORIES.items()}
    for left, left_set in bodies.items():
        for right, right_set in bodies.items():
            if left != right:
                assert not left_set & right_set


def test_source_fingerprints_ignore_checkout_line_endings(tmp_path):
    lf, crlf = tmp_path / "lf.py", tmp_path / "crlf.py"
    lf.write_bytes(b"first\nsecond\n")
    crlf.write_bytes(b"first\r\nsecond\r\n")
    assert text_hash(lf) == text_hash(crlf)


def test_scenario_audit_accepts_windows_asset_separators_on_posix(assets):
    splits = generate_splits(assets, profile="small")
    for rows in splits.values():
        for row in rows:
            changed = row.model_copy(update={"initial_state": row.initial_state.model_copy(update={
                "data_dir": PurePosixPath("assets\\" + row.fixture_version),
            })})
            audit_scenario(changed, assets[row.fixture_version])


def test_reproducible_build_and_loading_hydrates_portable_asset_paths(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    first = build_dataset(left, profile="small")
    second = build_dataset(right, profile="small")
    assert first == second
    for path in left.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (right / path.relative_to(left)).read_bytes()
            assert b"\r\n" not in path.read_bytes()
    rows = load_dataset(left, "train")
    assert len(rows) == 16
    assert all(s.initial_state.data_dir.is_absolute() and s.initial_state.data_dir.is_dir() for s in rows)
    before = (left / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError, match="immutable"):
        build_dataset(left)
    assert (left / "manifest.json").read_bytes() == before


@pytest.mark.parametrize("leak", ["instance", "template", "held_conditions"])
def test_split_audit_rejects_leakage(assets, leak):
    splits = generate_splits(assets)
    if leak == "instance":
        row = splits["train"][0]
        splits["validation"].append(row.model_copy(update={"scenario_id": "new-id", "cohort": "validation"}))
    elif leak == "template":
        splits["validation"][0] = splits["validation"][0].model_copy(update={"template_family": splits["train"][0].template_family})
    else:
        row = next(s for s in splits["test"] if s.cohort == "new_instance")
        changed = row.model_copy(update={"cohort": "unseen_combination", "template_family": f"unseen_combination:{row.task_type}",
                                        "request": row.request.model_copy(update={"user_message": row.request.user_message.replace(INTRO["new_instance"], INTRO["unseen_combination"])})})
        splits["test"][splits["test"].index(row)] = changed
    with pytest.raises(ValueError, match="leak"):
        audit_splits(splits, assets)


def test_same_instance_paraphrases_cannot_disagree_about_amounts(assets):
    splits = generate_splits(assets)
    row = next(s for s in splits["train"] if s.mode == "refund" and s.scenario_id.endswith("-b"))
    order = row.initial_state.orders[0]
    changed = row.model_copy(update={
        "initial_state": row.initial_state.model_copy(update={"orders": (order.model_copy(update={"paid_amount_minor": order.paid_amount_minor + 1}),)}),
        "expected": row.expected.model_copy(update={"refund_total_minor": order.paid_amount_minor + 1}),
    })
    splits["train"][splits["train"].index(row)] = changed
    with pytest.raises(ValueError, match="Paraphrases"):
        audit_splits(splits, assets)


@pytest.mark.parametrize("field,value", [("refund_total_minor", 1), ("new_actions", ()), ("must_query_operation", True)])
def test_label_audit_rejects_wrong_amount_action_or_lookup(assets, field, value):
    splits = generate_splits(assets)
    row = next(s for s in splits["test"] if s.mode == "refund_boundary")
    splits["test"][splits["test"].index(row)] = row.model_copy(update={"expected": row.expected.model_copy(update={field: value})})
    with pytest.raises(ValueError, match="label mismatch"):
        audit_splits(splits, assets)


def test_development_policy_assets_do_not_expose_held_out_version_and_sources_resolve(assets):
    for version, files in assets.items():
        for doc in files["policies.json"]["documents"]:
            source = files[doc["source"]["file"]]
            for token in doc["source"]["pointer"].strip("/").split("/") if doc["source"]["pointer"] else ():
                source = source[int(token)] if isinstance(source, list) else source[token]
            assert source["policy_version"] == doc["version"]
        active = [doc for doc in files["policies.json"]["documents"] if doc["version"] == files["spec.json"]["policy_version"]]
        assert len(active) == 20
    assert all(d["version"] != "mock-policy-v2" for d in assets["business-v1"]["policies.json"]["documents"])
    text = json.dumps(assets["business-v2"]["policies.json"], ensure_ascii=False)
    assert "10×24" in text and "800 分" in text


@pytest.mark.parametrize("tamper", ["file", "summary", "sources", "index"])
def test_frozen_dataset_rejects_changed_data_metadata_or_fingerprints(tmp_path, tamper):
    output = tmp_path / "dataset"
    build_dataset(output, profile="small")
    if tamper == "file":
        with (output / "test.jsonl").open("ab") as stream:
            stream.write(b" ")
    else:
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        if tamper == "summary":
            manifest["splits"]["test"]["count"] += 1
        elif tamper == "sources":
            manifest["source_fingerprints"]["src/tool_agent_lab/business/rules.py"] = "changed"
        else:
            manifest["index_fingerprints"]["business-v1"] = "changed"
        (output / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="mismatch|changed"):
        validate_frozen(output)
