"""The published reviewable prompt must match the executable new contract."""
import ast
import hashlib
import json
from pathlib import Path

import pytest

from auto_eval.judges.compare_protocols import resolve_compare_protocol
from auto_eval.judges.timeliness_schema import TimelinessObservation
from auto_eval.judges.visual_compare_prompt_timeliness import render_system_prompt


def test_published_full_prompt_is_the_executable_prompt():
    document = Path("docs/timeliness-evaluation-prompt-final.md").read_text(encoding="utf-8")
    system = document.split("```text\n", 1)[1].split("\n```", 1)[0]
    assert system == render_system_prompt(product_count=2)
    assert hashlib.sha256(system.encode("utf-8")).hexdigest() in document
    tree = ast.parse(Path("src/auto_eval/judges/visual_compare_prompt_timeliness.py").read_text(encoding="utf-8"))
    user = next(node.value.args[0].value for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "VISUAL_COMPARE_USER" for t in node.targets))
    assert document.split("```jinja\n", 1)[1].split("\n```", 1)[0] == user


@pytest.mark.parametrize("name,state", [
    ("timeliness-result-v3.json", "yes"),
    ("timeliness-result-na-v3.json", "no"),
])
def test_published_new_examples_validate_and_preserve_classification(name, state):
    data = json.loads((Path("docs/examples") / name).read_text(encoding="utf-8"))
    observation = TimelinessObservation.model_validate(data, context={"product_count": 2})
    assert observation.has_conflict == state
    assert list(observation.model_dump())[-2:] == ["conflict_reason", "has_conflict"]


@pytest.mark.parametrize("revision", ["1.0.0", "1.0.1"])
def test_historical_prompts_do_not_silently_gain_new_detection(revision):
    protocol = resolve_compare_protocol("timeliness_compare@1.0", revision)
    system = protocol.system_template.render(product_count=2)
    assert '"has_conflict"' not in system
    assert '"conflict_reason"' not in system
