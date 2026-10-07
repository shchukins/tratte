import json
from argparse import Namespace
from pathlib import Path

import pytest

from experiments.product_normalization.evaluate import (
    LABELS,
    ROOT,
    balanced_cases,
    decision,
    load_cases,
    payload,
    run,
    summarize,
)


def response(content, finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {"content": content}}]}


def test_fixture_ids_labels_and_pilot_coverage():
    cases = load_cases(ROOT / "cases.json")
    assert len(cases["categories"]) == 100
    assert len({case["name"] for case in cases["categories"]}) == 100
    pilot = balanced_cases(cases["categories"], 10)
    assert {case["expected"] for case in pilot} == set(LABELS)
    assert {case["expected"] for case in balanced_cases(cases["pairs"], 3)} == {
        "same",
        "different",
        "insufficient",
    }


def test_request_contains_no_reference_label_and_limits_schema():
    case = {"id": "p1", "left": "a", "right": "b", "expected": "same"}
    body = payload(case, "pairs", "test", False, True)
    assert json.loads(body["messages"][1]["content"]) == {"left": "a", "right": "b"}
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert body["max_tokens"] == 96


@pytest.mark.parametrize(
    "content,finish",
    [
        ('{"relation":"same", "confidence":1}', "stop"),
        ('{"relation":"maybe"}', "stop"),
        ('{"relation":"same"}', "length"),
        ('<think>reason</think>{"relation":"same"}', "stop"),
        ("[]", "stop"),
    ],
)
def test_invalid_decisions_do_not_become_matches(content, finish):
    with pytest.raises(ValueError):
        decision(response(content, finish), "pairs")


def record(case_id, expected, predicted, repeat=0):
    return {
        "id": case_id,
        "task": "pairs",
        "expected": expected,
        "prediction": predicted,
        "repeat": repeat,
        "latency_seconds": 1,
        "error": None if predicted else "http_500",
    }


def test_false_merge_and_failure_denominators_are_explicit():
    rows = [
        record("p1", "same", "same"),
        record("p2", "same", None),
        record("p3", "different", "same"),
        record("p4", "insufficient", "same"),
    ]
    result = summarize(rows)["pairs"]
    assert result["false_merges"] == 2
    assert result["missed_matches"] == 1
    assert result["merge_precision"] == pytest.approx(1 / 3)
    assert result["merge_recall"] == 0.5
    assert result["accuracy_including_failures"] == 0.25
    assert result["valid_response_rate"] == 0.75


def test_invalid_repeats_are_not_stable_decisions():
    assert (
        summarize([record("p1", "same", None, 0), record("p1", "same", None, 1)])["pairs"][
            "repeat_agreement"
        ]
        == 0
    )
    assert (
        summarize([record("p1", "same", "same", 0), record("p1", "same", "same", 1)])["pairs"][
            "repeat_agreement"
        ]
        == 1
    )


def test_resume_reuses_only_identical_experiment_config(tmp_path, monkeypatch):
    calls = []

    def fake_request(url, body, timeout):
        calls.append(body)
        return response('{"category":"молочные продукты"}')

    monkeypatch.setattr("experiments.product_normalization.evaluate.request_json", fake_request)
    args = Namespace(
        cases=ROOT / "cases.json",
        limit=1,
        task="categories",
        model="fake",
        base_url="http://localhost:1234/v1",
        thinking=False,
        no_schema=False,
        timeout=10,
        repeats=2,
        output=tmp_path,
        resume=False,
    )
    result = run(args)
    assert len(calls) == 2
    assert result["metrics"]["categories"]["repeated_cases"] == 1
    args.resume = True
    run(args)
    assert len(calls) == 2
    args.model = "different-model"
    with pytest.raises(ValueError, match="configuration changed"):
        run(args)
    assert len(calls) == 2
    assert Path(tmp_path / "summary.json").exists()


def test_transport_failures_stop_and_preserve_incomplete_report(tmp_path, monkeypatch):
    from urllib.error import URLError

    def unavailable(*args):
        raise URLError("unavailable")

    monkeypatch.setattr("experiments.product_normalization.evaluate.request_json", unavailable)
    args = Namespace(
        cases=ROOT / "cases.json",
        limit=5,
        task="categories",
        model="fake",
        base_url="http://localhost:1234/v1",
        thinking=False,
        no_schema=False,
        timeout=10,
        repeats=1,
        output=tmp_path,
        resume=False,
    )
    with pytest.raises(RuntimeError, match="Three consecutive"):
        run(args)
    report = json.loads((tmp_path / "summary.json").read_text())
    assert report["complete"] is False
    assert report["planned_requests"] == 5
    assert report["completed_requests"] == 3
    assert report["metrics"]["categories"]["valid_response_rate"] == 0
