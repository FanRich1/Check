from copy import deepcopy

import pytest

from scripts.stage6_cases import scenarios
from shipping_billing.cli import main
from shipping_billing.domain.engine import normalize
from shipping_billing.io import load_json, write_json


def inputs(tmp_path, cid="authorized-amount"):
    _, source, context, state, _ = deepcopy(next(c for c in scenarios() if c[0] == cid))
    values = {
        "input": normalize(source, context.normalization_context),
        "extraction": source,
        "context": context,
        "state": state,
    }
    paths = {k: tmp_path / (k + ".json") for k in values}
    for k, v in values.items():
        write_json(paths[k], v.model_dump(mode="python"))
    return paths


def command(paths, output):
    return [
        "reconcile",
        str(paths["input"]),
        "--extraction",
        str(paths["extraction"]),
        "--context",
        str(paths["context"]),
        "--state",
        str(paths["state"]),
        "--output",
        str(output),
    ]


def test_cli_update_outputs_and_repeat(tmp_path):
    paths = inputs(tmp_path)
    output, records, state = (tmp_path / n for n in ("result.json", "records.json", "next-state.json"))
    args = command(paths, output) + ["--records-output", str(records), "--state-output", str(state)]
    assert main(args) == 0
    assert load_json(records)[0]["operation"] == "UPDATE"
    assert load_json(state)["bills"][0]["charge_details"]["amount"] == 50
    assert load_json(paths["state"])["bills"][0]["charge_details"]["amount"] == 40
    paths["state"] = state
    assert main(command(paths, output)) == 0
    result = load_json(output)
    assert result["decisions"][0]["operation"] == "NO_CHANGE"
    assert len(result["next_state"]["audit_log"]) == 1


@pytest.mark.parametrize("key", ["input", "extraction", "context", "state"])
@pytest.mark.parametrize("option", ["--records-output", "--state-output"])
def test_cli_protects_every_input(tmp_path, key, option):
    paths = inputs(tmp_path)
    before = paths[key].read_bytes()
    assert main(command(paths, tmp_path / "result.json") + [option, str(paths[key])]) == 2
    assert paths[key].read_bytes() == before


def test_cli_rejects_output_collision(tmp_path):
    paths = inputs(tmp_path)
    output = tmp_path / "result.json"
    assert main(command(paths, output) + ["--state-output", str(output)]) == 2
    assert not output.exists()


def test_cli_invalid_old_is_not_empty_state(tmp_path):
    paths = inputs(tmp_path)
    write_json(paths["state"], {"bills": [{}]})
    output = tmp_path / "result.json"
    assert main(command(paths, output)) == 2
    assert not output.exists()


def test_cli_pending_does_not_apply(tmp_path):
    paths = inputs(tmp_path, "unauthorized-amount")
    output = tmp_path / "result.json"
    assert main(command(paths, output)) == 1
    assert load_json(output)["next_state"] == load_json(paths["state"])


def test_cli_initial_old_array(tmp_path):
    paths = inputs(tmp_path)
    write_json(paths["state"], load_json(paths["state"])["bills"])
    args = command(paths, tmp_path / "result.json")
    args[args.index("--state")] = "--existing-bills"
    assert main(args) == 0
