"""Provider-neutral Jev/OpenJev golden-set benchmark engine.

The benchmark deliberately keeps the provider contract small: every provider
receives ``state`` and typed ``questions`` and returns a System One-shaped
response. Cloud and local backends can therefore be compared without changing
the scoring code.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable


DEFAULT_TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_LOCAL_SYSTEMONE_ENDPOINT = "http://127.0.0.1:8644/v1/systemone"
DEFAULT_LOCAL_OPENAI_ENDPOINT = "http://127.0.0.1:8083/v1/chat/completions"


def _clamp_probability(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        raise ValueError(f"invalid probability: {value!r}") from None


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(float(value), digits)


def _log_loss(probability: float, truth: bool) -> float:
    p = max(1e-15, min(1.0 - 1e-15, probability))
    return -math.log(p if truth else 1.0 - p)


def _default_transport(url: str, payload: dict, headers: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class CloudTypeSafeProvider:
    """Client for TypeSafe's hosted ``/v1/systemone`` endpoint."""

    name = "typesafe-cloud"
    input_cost_per_million = 0.042
    output_cost_per_million = 0.0

    def __init__(
        self,
        api_key: str | None = None,
        *,
        endpoint: str = DEFAULT_TYPESAFE_ENDPOINT,
        model: str = "jev-1.13.0",
        timeout: float = 120.0,
        retries: int = 3,
        transport: Callable[[str, dict, dict, float], dict] | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY", "")).strip()
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.transport = transport or _default_transport
        if not self.api_key:
            raise ValueError("TYPESAFE_API_KEY is not available")

    def evaluate(self, state: Any, questions: dict) -> dict:
        payload = {"model": self.model, "state": state, "questions": questions}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "BDH-Jev-Benchmark/1.0",
        }
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.transport(self.endpoint, payload, headers, self.timeout)
                if not isinstance(response, dict):
                    raise ValueError("provider response must be a JSON object")
                return response
            except urllib.error.HTTPError as exc:
                last_error = exc
                retryable = exc.code == 429 or exc.code == 529 or exc.code >= 500
                if not retryable or attempt + 1 >= self.retries:
                    raise
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = exc
                if attempt + 1 >= self.retries:
                    raise
            time.sleep(2**attempt)
        raise RuntimeError(f"provider failed after retries: {type(last_error).__name__}")


class LocalSystemOneProvider(CloudTypeSafeProvider):
    """Client for an OpenJev-compatible local ``/v1/systemone`` server."""

    name = "openjev-local"

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_LOCAL_SYSTEMONE_ENDPOINT,
        model: str = "jev-local",
        timeout: float = 120.0,
        retries: int = 2,
        transport: Callable[[str, dict, dict, float], dict] | None = None,
    ) -> None:
        self.api_key = ""
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.transport = transport or _default_transport

    def evaluate(self, state: Any, questions: dict) -> dict:
        payload = {"model": self.model, "state": state, "questions": questions}
        headers = {"Content-Type": "application/json", "User-Agent": "BDH-Jev-Benchmark/1.0"}
        return self.transport(self.endpoint, payload, headers, self.timeout)


class LocalChatJsonProvider:
    """Local fallback for oMLX/OpenAI-compatible servers without System One API.

    This is a generated-JSON baseline, not a direct-logit OpenJev implementation.
    It is useful for a first local comparison while the MLX direct-logit backend
    is being built.
    """

    name = "local-chat-json"

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_LOCAL_OPENAI_ENDPOINT,
        model: str,
        timeout: float = 120.0,
        transport: Callable[[str, dict, dict, float], dict] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.timeout = timeout
        self.transport = transport or _default_transport

    def evaluate(self, state: Any, questions: dict) -> dict:
        instruction = {
            "state": state,
            "questions": questions,
            "contract": {
                "noul": "return {noul: number 0..1}",
                "choice": "return {choice: option, probabilities: object summing to 1}",
                "score": "return {score: number using zero-based criterion index}",
            },
        }
        messages = [
            {
                "role": "system",
                "content": "Return only valid JSON. Do not explain. Use exactly one answer object per question id.",
            },
            {"role": "user", "content": json.dumps(instruction, ensure_ascii=False)},
        ]
        payload = {"model": self.model, "messages": messages, "temperature": 0, "stream": False}
        headers = {"Content-Type": "application/json", "User-Agent": "BDH-Jev-Benchmark/1.0"}
        response = self.transport(self.endpoint, payload, headers, self.timeout)
        content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
        answers = json.loads(content)
        if isinstance(answers, dict) and isinstance(answers.get("answers"), dict):
            answers = answers["answers"]
        # Small local models often omit the question id when only one question
        # is present and return the answer object directly (e.g. {"noul": 1}).
        # Normalize that shorthand to the System One response shape.
        answer_fields = {"type", "noul", "choice", "score", "legend", "probabilities", "confidence"}
        if (
            len(questions) == 1
            and isinstance(answers, dict)
            and set(answers).issubset(answer_fields)
            and any(field in answers for field in ("noul", "choice", "score"))
        ):
            answers = {next(iter(questions)): answers}
        if not isinstance(answers, dict):
            raise ValueError("local model JSON must be an answer mapping")
        return {"model": self.model, "answers": answers}


class LocalMLXDirectProvider:
    """Direct option-logit OpenJev-style provider on Apple Silicon.

    This reads the next-token logits for labels A..Z instead of asking the
    model to generate JSON. It reproduces the decision interface pattern, not
    TypeSafe's proprietary RLCD training or calibration.
    """

    name = "openjev-mlx-direct"

    def __init__(self, *, model_id: str = "mlx-community/Qwen3.5-4B-MLX-4bit") -> None:
        try:
            import mlx.core as mx
            from mlx_vlm import load
        except ImportError as exc:  # pragma: no cover - environment-specific
            raise RuntimeError("LocalMLXDirectProvider requires the oMLX/MLX Python environment") from exc
        self.mx = mx
        self.model_id = model_id
        self._model, self.processor = load(model_id, lazy=True)
        self.model = model_id
        self.tokenizer = self.processor.tokenizer

    def _prompt(self, state: Any, question: dict, labels: list[str], meanings: list[str]) -> str:
        state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False, sort_keys=True)
        options = "\n".join(f"{label}: {meaning}" for label, meaning in zip(labels, meanings))
        messages = [
            {
                "role": "system",
                "content": "You are a decision-only classifier. Read the state and question, then output exactly one option label and nothing else.",
            },
            {
                "role": "user",
                "content": (
                    f"STATE:\n{state_text}\n\nQUESTION:\n{question.get('instructions', '')}\n\n"
                    f"OPTIONS:\n{options}\n\nANSWER LABEL:"
                ),
            },
        ]
        return self.processor.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )

    def _option_probabilities(self, prompt: str, labels: list[str]) -> list[float]:
        try:
            token_ids = self.tokenizer.encode(prompt, add_special_tokens=False)
        except TypeError:
            token_ids = self.tokenizer.encode(prompt)
        label_ids = []
        for label in labels:
            try:
                encoded = self.tokenizer.encode(label, add_special_tokens=False)
            except TypeError:
                encoded = self.tokenizer.encode(label)
            if len(encoded) != 1:
                raise ValueError(f"label {label!r} is not a single token for {self.model_id}")
            label_ids.append(encoded[0])
        output = self._model(self.mx.array([token_ids]))
        logits = output.logits[0, -1, :]
        selected = self.mx.array([logits[token_id] for token_id in label_ids])
        probabilities = self.mx.softmax(selected.astype(self.mx.float32))
        self.mx.eval(probabilities)
        return [float(value) for value in probabilities.tolist()]

    def evaluate(self, state: Any, questions: dict) -> dict:
        answers: dict[str, dict] = {}
        input_tokens = 0
        for question_id, question in questions.items():
            q_type = question["type"]
            if q_type == "noul":
                option_keys = ["true", "false"]
                meanings = [question["criteria"][key] for key in option_keys]
            elif q_type == "choice":
                option_keys = list(question["criteria"].keys())
                meanings = [question["criteria"][key] or key for key in option_keys]
            elif q_type == "score":
                option_keys = [str(index) for index in range(len(question["criteria"]))]
                meanings = list(question["criteria"])
            else:
                raise ValueError(f"unsupported question type: {q_type}")
            labels = option_labels(len(option_keys))
            prompt = self._prompt(state, question, labels, [str(item) for item in meanings])
            try:
                token_count = len(self.tokenizer.encode(prompt, add_special_tokens=False))
            except TypeError:
                token_count = len(self.tokenizer.encode(prompt))
            input_tokens += token_count
            probabilities = self._option_probabilities(prompt, labels)
            winner = max(range(len(probabilities)), key=probabilities.__getitem__)
            distribution = dict(zip(option_keys, probabilities))
            if q_type == "noul":
                answers[question_id] = {"type": "noul", "noul": probabilities[0]}
            elif q_type == "choice":
                answers[question_id] = {
                    "type": "choice",
                    "choice": option_keys[winner],
                    "probabilities": distribution,
                    "confidence": probabilities[winner],
                }
            else:
                answers[question_id] = {
                    "type": "score",
                    "score": sum(index * probability for index, probability in enumerate(probabilities)),
                    "legend": dict(zip(option_keys, meanings)),
                    "probabilities": distribution,
                    "confidence": probabilities[winner],
                }
        return {
            "model": self.model_id,
            "answers": answers,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0},
        }


def option_labels(count: int) -> list[str]:
    if not 1 <= count <= 26:
        raise ValueError("direct-logit provider currently supports 1-26 options")
    return [chr(ord("A") + index) for index in range(count)]


def normalize_option_scores(logits: list[float]) -> list[float]:
    if not logits:
        raise ValueError("at least one option logit is required")
    maximum = max(logits)
    exponentials = [math.exp(value - maximum) for value in logits]
    total = sum(exponentials)
    return [value / total for value in exponentials]


def _score_question(question: dict, expected: Any, answer: dict) -> dict:
    q_type = question["type"]
    result: dict[str, Any] = {"type": q_type, "expected": expected, "raw": answer}
    if q_type == "noul":
        probability = _clamp_probability(answer.get("noul"))
        truth = bool(expected)
        result.update(
            {
                "probability": _round(probability),
                "predicted": probability >= 0.5,
                "correct": (probability >= 0.5) == truth,
                "brier_score": _round((probability - float(truth)) ** 2),
                "log_loss": _round(_log_loss(probability, truth)),
            }
        )
    elif q_type == "choice":
        choice = answer.get("choice")
        options = question["criteria"]
        probabilities = answer.get("probabilities") or {}
        result.update(
            {
                "predicted": choice,
                "correct": choice == expected,
                "probability": _round(probabilities.get(choice)) if choice in probabilities else None,
                "abstained": choice not in options,
            }
        )
    elif q_type == "score":
        score = float(answer["score"])
        error = abs(score - float(expected))
        result.update(
            {
                "predicted": score,
                "absolute_error": _round(error),
                "within_tolerance": error <= 0.5,
            }
        )
    else:
        raise ValueError(f"unsupported question type: {q_type}")
    return result


def evaluate_case(record: dict, provider: Any) -> dict:
    started = time.perf_counter()
    try:
        response = provider.evaluate(record["state"], record["questions"])
        answers = response.get("answers", {})
        question_results = {}
        for question_id, question in record["questions"].items():
            answer = answers.get(question_id)
            if not isinstance(answer, dict):
                raise ValueError(f"missing answer for {question_id}")
            question_results[question_id] = _score_question(
                question, record["expected"][question_id], answer
            )
        return {
            "id": record["id"],
            "split": record.get("split"),
            "task": record.get("task"),
            "category": record.get("category"),
            "status": "ok",
            "model": response.get("model", getattr(provider, "model", None)),
            "usage": response.get("usage") if isinstance(response.get("usage"), dict) else {},
            "latency_ms": _round((time.perf_counter() - started) * 1000, 3),
            "questions": question_results,
        }
    except Exception as exc:
        return {
            "id": record["id"],
            "split": record.get("split"),
            "task": record.get("task"),
            "category": record.get("category"),
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": _round((time.perf_counter() - started) * 1000, 3),
            "questions": {},
        }


def aggregate_question_results(
    case_results: list[dict],
    *,
    input_cost_per_million: float | None = None,
    output_cost_per_million: float | None = None,
) -> dict:
    questions = [
        question
        for case in case_results
        for question in case.get("questions", {}).values()
    ]
    nouls = [q for q in questions if q["type"] == "noul" and "brier_score" in q]
    choices = [q for q in questions if q["type"] == "choice" and "correct" in q]
    scores = [q for q in questions if q["type"] == "score" and "absolute_error" in q]
    usage_input = sum(int(case.get("usage", {}).get("input_tokens", 0) or 0) for case in case_results)
    usage_output = sum(int(case.get("usage", {}).get("output_tokens", 0) or 0) for case in case_results)
    estimated_cost = None
    if input_cost_per_million is not None or output_cost_per_million is not None:
        estimated_cost = (
            usage_input * float(input_cost_per_million or 0.0)
            + usage_output * float(output_cost_per_million or 0.0)
        ) / 1_000_000
    classified = [q for q in questions if "correct" in q]
    return {
        "question_count": len(questions),
        "answered_count": len(questions),
        "accuracy": _round(sum(bool(q["correct"]) for q in classified) / len(classified), 4) if classified else None,
        "choice_accuracy": _round(sum(bool(q["correct"]) for q in choices) / len(choices), 4) if choices else None,
        "noul_accuracy": _round(sum(bool(q["correct"]) for q in nouls) / len(nouls), 4) if nouls else None,
        "brier_score": _round(sum(q["brier_score"] for q in nouls) / len(nouls), 4) if nouls else None,
        "log_loss": _round(sum(q["log_loss"] for q in nouls) / len(nouls), 4) if nouls else None,
        "score_mae": _round(sum(q["absolute_error"] for q in scores) / len(scores), 4) if scores else None,
        "score_within_tolerance": _round(sum(bool(q["within_tolerance"]) for q in scores) / len(scores), 4) if scores else None,
        "abstention_rate": _round(sum(bool(q.get("abstained")) for q in choices) / len(choices), 4) if choices else 0.0,
        "usage": {
            "input_tokens": usage_input,
            "output_tokens": usage_output,
            "estimated_cost_usd": _round(estimated_cost, 8),
        },
        "case_error_count": sum(1 for case in case_results if case.get("status") != "ok"),
    }


def _group_summary(
    case_results: list[dict],
    key: str,
    *,
    input_cost_per_million: float | None = None,
    output_cost_per_million: float | None = None,
) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for case in case_results:
        grouped[str(case.get(key, "unknown"))].append(case)
    return {
        group: aggregate_question_results(
            items,
            input_cost_per_million=input_cost_per_million,
            output_cost_per_million=output_cost_per_million,
        )
        for group, items in sorted(grouped.items())
    }


def run_benchmark(records: list[dict], provider: Any, *, split: str | None = None) -> dict:
    selected = [record for record in records if split is None or record.get("split") == split]
    results = [evaluate_case(record, provider) for record in selected]
    input_cost = getattr(provider, "input_cost_per_million", None)
    output_cost = getattr(provider, "output_cost_per_million", None)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider": getattr(provider, "name", provider.__class__.__name__),
        "model": getattr(provider, "model", None),
        "split": split or "all",
        "case_count": len(results),
        "summary": aggregate_question_results(
            results,
            input_cost_per_million=input_cost,
            output_cost_per_million=output_cost,
        ),
        "by_task": _group_summary(
            results,
            "task",
            input_cost_per_million=input_cost,
            output_cost_per_million=output_cost,
        ),
        "by_split": _group_summary(
            results,
            "split",
            input_cost_per_million=input_cost,
            output_cost_per_million=output_cost,
        ),
        "cases": results,
    }
