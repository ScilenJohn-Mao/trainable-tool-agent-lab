"""Current-policy groups use one scenario and independently reset shared graph environments."""

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from tool_agent_lab.evaluation.datasets import digest
from tool_agent_lab.evaluation.report import write_report
from tool_agent_lab.evaluation.runner import run_attempt
from art_trainlab.adapter import ARTModelClient
from art_trainlab.recorder import IntegrityError, Recorder, verify_group


@dataclass
class ARTInterfaces:
    wrap_rollout: object
    trajectory: object
    group: object
    attach: object


def art_interfaces():
    import art
    from art.langgraph import wrap_rollout

    return ARTInterfaces(wrap_rollout, art.Trajectory, art.TrajectoryGroup,
                         lambda recorder, trajectory: recorder.attach(trajectory))


async def sample_rollout(model, scenario, config, *, policy_version, output, interfaces=None,
                         client_factory=None):
    interfaces = interfaces or art_interfaces()
    directory = Path(output) / ("rollout-" + uuid4().hex)
    directory.mkdir(parents=True)
    recorder = Recorder(directory / "completions.jsonl")
    experiment = config.experiment()
    experiment = experiment.model_copy(update={"model": experiment.model.model_copy(update={
        "version": config.name + "@" + str(policy_version)})})
    if client_factory is None:
        experiment = experiment.model_copy(update={"model": experiment.model.model_copy(update={
            "name": model.get_inference_name(), "base_url": model.inference_base_url})})
    result = {"scenario_id": scenario.scenario_id, "policy_version": policy_version,
              "initial_state_sha256": digest(scenario.model_dump(mode="json")), "directory": str(directory),
              "attempt": None, "integrity": None, "discard_reason": None}

    async def rollout():
        client = client_factory(scenario, experiment.model, recorder) if client_factory else ARTModelClient(model, experiment.model, recorder)
        result["attempt"] = await run_attempt(scenario, experiment, client, root=directory / "runtime")
        attempt = result["attempt"]
        if attempt["verdict"]["reward"] is None:
            raise IntegrityError("unscorable:" + attempt["verdict"]["category"])
        audit = recorder.audit(attempt["trajectory"], sequence_tokens=config.sequence_tokens,
                               expected_model_version=experiment.model.version)
        result["integrity"] = audit | {k: result[k] for k in ("scenario_id", "policy_version", "initial_state_sha256")}
        trajectory = interfaces.trajectory(reward=float(attempt["verdict"]["reward"]), messages_and_choices=[],
            initial_policy_version=policy_version, final_policy_version=policy_version,
            metadata={"scenario_id": scenario.scenario_id, "policy_version": policy_version,
                      "business_identity": audit["identity"], "recorder": str(recorder.path)})
        return trajectory

    trajectory = None
    try:
        trajectory = await interfaces.wrap_rollout(model, rollout)()
        interfaces.attach(recorder, trajectory)
    except Exception as error:
        result["discard_reason"] = {"type": type(error).__name__, "message": str(error)}
        trajectory = None
    write_report(directory / "rollout.json", result)
    return trajectory, result


async def sample_group(model, scenario, config, *, policy_version, output, interfaces=None,
                       client_factory=None):
    interfaces = interfaces or art_interfaces()
    trajectories, results = [], []
    # Group size is not concurrency. The shared graph still chooses every tool and branch.
    for _ in range(config.group_size):
        trajectory, record = await sample_rollout(model, scenario, config, policy_version=policy_version,
            output=output, interfaces=interfaces, client_factory=client_factory)
        results.append(record)
        if trajectory is not None:
            trajectories.append(trajectory)
    failures = []
    if len(trajectories) != config.group_size:
        failures.append("incomplete_group")
    else:
        try:
            verify_group([r["integrity"] for r in results], size=config.group_size)
        except IntegrityError as error:
            failures.append(str(error))
    rewards = [t.reward for t in trajectories]
    report = {"scenario_id": scenario.scenario_id, "policy_version": policy_version,
              "requested_size": config.group_size, "valid_size": len(trajectories), "results": results,
              "rewards": rewards, "failures": failures,
              "zero_variance": bool(rewards) and len(set(rewards)) == 1}
    group = interfaces.group(trajectories, metadata={"scenario_id": scenario.scenario_id,
                                                  "policy_version": policy_version}) if not failures else None
    return group, report
