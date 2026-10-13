"""Retain actual completions and check per-trajectory policy tokens and tool pairing."""

from copy import deepcopy
import json
import math
from pathlib import Path

from tool_agent_lab.agent.context import history_blocks


class IntegrityError(ValueError):
    pass


def token_data(record):
    response = record["response"]
    if len(response["choices"]) != 1:
        raise IntegrityError("choice_count")
    choice = response["choices"][0]
    prompt = choice.get("prompt_token_ids", response.get("prompt_token_ids"))
    generated = choice.get("token_ids")
    if not isinstance(prompt, list) or not isinstance(generated, list) or not generated:
        raise IntegrityError("missing_token_ids")
    if any(type(t) is not int or t < 0 for t in prompt + generated):
        raise IntegrityError("invalid_token_ids")
    logprobs = choice.get("logprobs") or {}
    entries = (logprobs.get("content") or []) + (logprobs.get("refusal") or [])
    if len(entries) != len(generated):
        raise IntegrityError("missing_logprobs")
    values = []
    for token, entry in zip(generated, entries, strict=True):
        value = entry["logprob"]
        if type(value) not in (float, int) or not math.isfinite(value):
            raise IntegrityError("invalid_logprob")
        if entry["token"] != f"token_id:{token}":
            raise IntegrityError("logprob_token_mismatch")
        values.append(value)
    usage = response.get("usage")
    if usage and (usage["prompt_tokens"] != len(prompt) or usage["completion_tokens"] != len(generated)):
        raise IntegrityError("usage_token_mismatch")
    return prompt, generated, values


class Recorder:
    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.records = []
        if self.path and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                self.add(json.loads(line), persist=False)

    def add(self, record, *, persist=True):
        record = deepcopy(record)
        key = record["response"]["id"]
        for existing in self.records:
            if existing["response"]["id"] == key:
                if existing != record:
                    raise IntegrityError("conflicting_completion_id")
                return existing
        self.records.append(record)
        if self.path and persist:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        return record

    def audit(self, trace, *, sequence_tokens, expected_model_version):
        state = trace["state"]
        identity = state["identity"]
        if identity["model_version"] != expected_model_version:
            raise IntegrityError("mixed_model_version")
        if len(self.records) != state["model_calls"]:
            raise IntegrityError("missing_or_repeated_generation")
        history_blocks(state["messages"])
        observed = [m for m in state["messages"] if m["role"] == "assistant"]
        if len(observed) != len(self.records):
            raise IntegrityError("assistant_count_mismatch")
        sequences = []
        current = {"token_ids": [], "assistant_mask": [], "logprobs": []}
        for record, message in zip(self.records, observed, strict=True):
            if record["model_version"] != expected_model_version:
                raise IntegrityError("mixed_behavior_policy")
            raw = record["response"]["choices"][0]["message"]
            if raw.get("content") != message.get("content") or raw.get("tool_calls", []) != message.get("tool_calls", []):
                raise IntegrityError("assistant_output_changed")
            history_blocks(record["messages"])
            prompt, generated, values = token_data(record)
            ids = current["token_ids"]
            if ids and prompt[:len(ids)] != ids:
                sequences.append(current)
                current = {"token_ids": [], "assistant_mask": [], "logprobs": []}
                ids = current["token_ids"]
            suffix = prompt[len(ids):]
            current["token_ids"].extend(suffix + generated)
            current["assistant_mask"].extend([False] * len(suffix) + [True] * len(generated))
            current["logprobs"].extend([None] * len(suffix) + values)
        if current["token_ids"]:
            sequences.append(current)
        total = sum(len(s["token_ids"]) for s in sequences)
        if not sequences or total > sequence_tokens:
            raise IntegrityError("training_sequence_budget_exceeded")
        return {"identity": identity, "model_version": expected_model_version,
                "completion_ids": [r["response"]["id"] for r in self.records], "sequences": sequences,
                "training_tokens": total, "assistant_tokens": sum(sum(s["assistant_mask"]) for s in sequences),
                "tool_calls": sum(len(m.get("tool_calls", [])) for m in observed)}

    def attach(self, trajectory):
        """Supplement wrapper conversion with actual input histories and unmodified raw choices."""
        from art.trajectories import LegacyHistory
        from openai.types.chat.chat_completion import Choice

        histories = []
        for record in self.records:
            response = record["response"]
            raw = deepcopy(response["choices"][0])
            raw["prompt_token_ids"] = raw.get("prompt_token_ids", response.get("prompt_token_ids"))
            raw["art_response_id"] = response["id"]
            histories.append(LegacyHistory(messages_and_choices=deepcopy(record["messages"]) + [Choice.model_validate(raw)],
                                           tools=deepcopy(record["tools"])))
        if not histories:
            raise IntegrityError("empty_trajectory")
        trajectory.messages_and_choices = histories[0].messages_and_choices
        trajectory.tools = histories[0].tools
        trajectory.additional_histories = histories[1:]


def verify_group(records, *, size):
    if len(records) != size or size < 2:
        raise IntegrityError("invalid_group_size")
    if len({r["scenario_id"] for r in records}) != 1 or len({r["initial_state_sha256"] for r in records}) != 1:
        raise IntegrityError("mixed_scenario_group")
    if len({r["policy_version"] for r in records}) != 1:
        raise IntegrityError("mixed_policy_group")
    if len({r["identity"]["thread_id"] for r in records}) != size or len({r["identity"]["task_id"] for r in records}) != size:
        raise IntegrityError("shared_trajectory_state")
    ids = [key for r in records for key in r["completion_ids"]]
    if len(ids) != len(set(ids)):
        raise IntegrityError("repeated_group_completion")


def compare_art_sequences(expected, actual):
    """Compare the official ART assembler's output before any update."""
    if len(expected) != len(actual):
        raise IntegrityError("art_sequence_count_mismatch")
    for packet, sequence in zip(expected, actual, strict=True):
        if packet["token_ids"] != list(sequence.token_ids) or packet["assistant_mask"] != list(sequence.assistant_mask):
            raise IntegrityError("art_token_or_mask_mismatch")
        if len(sequence.logprobs) != len(packet["logprobs"]):
            raise IntegrityError("art_logprob_length_mismatch")
        for sampled, wanted, value in zip(packet["assistant_mask"], packet["logprobs"], sequence.logprobs, strict=True):
            if sampled and (not math.isfinite(value) or not math.isclose(value, wanted, abs_tol=1e-6)):
                raise IntegrityError("art_logprob_mismatch")
