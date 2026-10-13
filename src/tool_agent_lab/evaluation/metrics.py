"""Aggregate scorable coverage, business success and actual observed costs."""

from collections import Counter, defaultdict


def costs(attempts: list[dict]) -> dict:
    calls = [call for a in attempts for call in a["observations"]["model_calls"]]
    usage = [c.get("usage") for c in calls]
    complete = [u for u in usage if isinstance(u, dict) and all(type(u.get(k)) is int and u[k] >= 0
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"))]
    totals = {k: sum(u[k] for u in complete) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
    return {"attempts": len(attempts), "model_calls": len(calls), "usage_known_calls": len(complete),
            "usage_missing_calls": len(calls) - len(complete),
            "tokens": totals if len(complete) == len(calls) and calls else None, "known_tokens": totals,
            "tool_calls": sum(a["observations"]["tool_calls"] for a in attempts),
            "seconds": {k: sum(a["observations"]["seconds"][k] for a in attempts)
                        for k in ("total", "model", "tools", "waiting", "orchestration")}}


def aggregate(cases: list[dict], attempts: list[dict]) -> dict:
    final = {case["scenario_id"]: case["attempts"][-1] if case["attempts"] else None for case in cases}
    groups = defaultdict(list)
    for case in cases:
        groups[case["task_type"]].append(case)

    def rates(rows):
        scores = [final[r["scenario_id"]] for r in rows]
        scored = sum(s is not None and s["verdict"]["reward"] is not None for s in scores)
        successful = sum(s is not None and s["verdict"]["success"] for s in scores)
        return {"planned": len(rows), "scored": scored, "successful": successful,
                "coverage": scored / len(rows) if rows else None,
                "success_rate": successful / len(rows) if rows else None,
                "scored_success_rate": successful / scored if scored else None}

    pairs = defaultdict(dict)
    for case in cases:
        pairs[case["root_scenario_id"]].setdefault(case["variant"], []).append(case["scenario_id"])
    paired = []
    for root, variants in pairs.items():
        normal = variants.get("normal", [])
        if len(normal) != 1:
            continue
        for variant, ids in variants.items():
            if variant == "normal" or len(ids) != 1:
                continue
            left, right = final[normal[0]], final[ids[0]]
            scored = left is not None and right is not None and all(a["verdict"]["reward"] is not None for a in (left, right))
            paired.append({"root_scenario_id": root, "variant": variant, "normal_id": normal[0], "fault_id": ids[0],
                           "scored": scored, "both_successful": all(a["verdict"]["success"] for a in (left, right)) if scored else None})
    holes = [r["scenario_id"] for r in cases if final[r["scenario_id"]] is None or final[r["scenario_id"]]["verdict"]["reward"] is None]
    return {"overall": rates(cases), "by_task_type": {k: rates(v) for k, v in sorted(groups.items())},
            "coverage_holes": holes, "attempt_categories": dict(Counter(a["verdict"]["category"] for a in attempts)),
            "failure_tags": dict(Counter(tag for a in attempts for tag in a["verdict"]["failures"])),
            "costs": costs(attempts) | {"retries": sum(max(0, len(c["attempts"]) - 1) for c in cases)},
            "unsuccessful_costs": costs([a for a in attempts if not a["verdict"]["success"]]),
            "costs_by_task_type": {k: costs([a for c in v for a in c["attempts"]]) for k, v in sorted(groups.items())},
            "pairing": {"available": bool(paired), "pairs": paired,
                        "scored": sum(p["scored"] for p in paired),
                        "both_success_rate": sum(p["both_successful"] is True for p in paired) / sum(p["scored"] for p in paired)
                                             if any(p["scored"] for p in paired) else None}}
