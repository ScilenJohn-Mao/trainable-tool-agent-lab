import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts.check_business_data import check_business_data
from tool_agent_lab.settings import PROJECT_ROOT


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return Path(shutil.copytree(PROJECT_ROOT / "data/business/v1", tmp_path / "business"))


def read_json(data_dir: Path, name: str):
    return json.loads((data_dir / f"{name}.json").read_text(encoding="utf-8"))


def write_json(data_dir: Path, name: str, value) -> None:
    (data_dir / f"{name}.json").write_text(json.dumps(value), encoding="utf-8")


def test_fixtures_cover_four_tasks_and_have_independent_expected_outcomes(data_dir: Path) -> None:
    report = check_business_data(data_dir)
    assert (report["order_count"], report["scenario_count"], len(report["task_types"])) == (4, 5, 4)
    expected = {row["scenario_id"]: row for row in report["expectations"]}
    assert expected["refund-damaged"]["refund_total_minor"] == 12900
    assert expected["refund-with-delay-coupon"]["coupon_total_minor"] == 500
    assert expected["check-uncertain-refund"]["new_actions"] == []
    assert expected["clarify-order-before-refund"]["must_clarify"] is True
    assert expected["handoff-digital-goods"]["new_actions"] == ["create_handoff"]
    # Hand-check eligibility facts separately from the consistency checker.
    now = datetime.fromisoformat("2026-09-17T12:00:00+08:00")
    orders = read_json(data_dir, "orders")
    for order in orders[:2]:
        assert order["category"] == "general_goods" and order["damage_verified"] is True
        assert timedelta(0) <= now - datetime.fromisoformat(order["delivered_at"]) <= timedelta(days=7)
    delayed = orders[1]
    assert (datetime.fromisoformat(delayed["delivered_at"]) -
            datetime.fromisoformat(delayed["promised_delivery_at"])) == timedelta(hours=26)
    assert orders[3]["category"] == "digital_goods"


@pytest.mark.parametrize("bad_amount", [129.0, True, -1])
def test_invalid_money_is_rejected(data_dir: Path, bad_amount) -> None:
    orders = read_json(data_dir, "orders")
    orders[0]["paid_amount_minor"] = bad_amount
    write_json(data_dir, "orders", orders)
    with pytest.raises(ValueError, match="integer fen"):
        check_business_data(data_dir)


def test_missing_historical_refund_is_rejected(data_dir: Path) -> None:
    write_json(data_dir, "operations", [])
    with pytest.raises(ValueError, match="initial refund ledger mismatch"):
        check_business_data(data_dir)


def test_mismatched_historical_approval_is_rejected(data_dir: Path) -> None:
    operations = read_json(data_dir, "operations")
    operations[0]["approval"]["amount_minor"] = 1
    write_json(data_dir, "operations", operations)
    with pytest.raises(ValueError, match="approval mismatch"):
        check_business_data(data_dir)


def test_scenario_cannot_expect_another_refund_for_refunded_order(data_dir: Path) -> None:
    scenarios = read_json(data_dir, "scenarios")
    scenarios[2]["expected"]["new_actions"] = ["request_refund"]
    write_json(data_dir, "scenarios", scenarios)
    with pytest.raises(ValueError, match="would refund twice"):
        check_business_data(data_dir)


def test_scenario_owner_must_match_order(data_dir: Path) -> None:
    scenarios = read_json(data_dir, "scenarios")
    scenarios[0]["owner_id"] = "another-user"
    write_json(data_dir, "scenarios", scenarios)
    with pytest.raises(ValueError, match="owner mismatch"):
        check_business_data(data_dir)


def test_cli_from_another_directory_is_read_only(data_dir: Path, tmp_path: Path) -> None:
    before = {path.name: path.read_bytes() for path in data_dir.iterdir()}
    runtime = tmp_path / "runtime"
    command = [sys.executable, str(PROJECT_ROOT / "scripts/check_business_data.py")]
    result = subprocess.run(
        command, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", check=True,
        env={**os.environ, "TTAL_BUSINESS_DATA_DIR": str(data_dir), "TTAL_RUNTIME_DIR": str(runtime)},
    )
    assert json.loads(result.stdout)["business_execution"] == "not_run"
    assert before == {path.name: path.read_bytes() for path in data_dir.iterdir()}
    assert not runtime.exists()
