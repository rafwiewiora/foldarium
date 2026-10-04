"""Conservative observation of acknowledged Modal prediction dispatches.

The Modal API is injected so the portable pipeline has no SDK dependency. This
adapter follows the deployed SDK 1.5.5 exception semantics; uncertain observation
never authorizes another GPU invocation.
"""
from __future__ import annotations

import builtins
from collections.abc import Mapping
from typing import Any, Callable


class PredictionDispatchObservationUncertain(RuntimeError):
    """The prior call cannot be observed reliably; keep its receipt for recovery."""


class PredictionDispatchTerminalError(RuntimeError):
    """An acknowledged call terminated without claiming its exact prediction run."""


def observe_prediction_dispatch(call_id: str, *, modal_api: Any) -> str:
    call = modal_api.FunctionCall.from_id(call_id)
    try:
        call.get(timeout=0)
    except modal_api.exception.FunctionTimeoutError:
        # An actual function-duration timeout, not a local polling deadline.
        return "terminal-function-timeout"
    except modal_api.exception.OutputExpiredError:
        # A missing retained output is not evidence of a running call or a safe
        # replacement. Preserve the original error/receipt for bounded recovery.
        raise
    except builtins.TimeoutError as error:
        # SDK 1.5.5 raises built-in TimeoutError when no output is ready, but also
        # re-raises a worker's own TimeoutError. The best-effort call graph can
        # distinguish a recorded terminal worker failure without a new spawn.
        try:
            pending = list(call.get_call_graph())
            matches = []
            while pending:
                node = pending.pop()
                pending.extend(node.children)
                if node.function_call_id == call_id:
                    matches.append(node)
        except Exception as observation_error:
            raise PredictionDispatchObservationUncertain(
                "cannot inspect acknowledged dispatch state"
            ) from observation_error
        if len(matches) != 1:
            raise PredictionDispatchObservationUncertain(
                "acknowledged dispatch has no unique retained input state"
            ) from error
        status = getattr(matches[0].status, "name", None)
        if status == "PENDING":
            return "accepted-dispatch-still-pending"
        if status in {"SUCCESS", "FAILURE", "INIT_FAILURE", "TERMINATED", "TIMEOUT"}:
            raise PredictionDispatchTerminalError(
                "acknowledged dispatch terminated without an observed run claim"
            ) from error
        raise PredictionDispatchObservationUncertain(
            "acknowledged dispatch state is unknown"
        ) from error
    else:
        raise PredictionDispatchTerminalError(
            "acknowledged dispatch finished without an observed run claim"
        )


def dispatch_prediction(
    private: Any, store: Any, action: Mapping[str, Any], *,
    modal_api: Any, spawn: Callable[[Mapping[str, Any]], str],
) -> dict[str, Any]:
    parameters = action["parameters"]

    def current_run() -> Mapping[str, Any]:
        rows = private.campaign_prediction_run_statuses(parameters["campaign_id"])
        return next(row for row in rows if row["run_id"] == parameters["run_id"])

    def unclaimed(row: Mapping[str, Any]) -> bool:
        return row["status"] in {"pending", "queued"} and row["attempt_count"] == 0

    row = current_run()
    if not unclaimed(row):
        return {"status": "already-claimed"}
    prior = store.snapshot().get("action_history", {}).get(action["action_key"], {}).get("dispatch_receipt")
    if prior:
        if prior.get("provider") != "modal":
            raise ValueError("prediction dispatch provider changed")
        observation = observe_prediction_dispatch(prior["call_id"], modal_api=modal_api)
        if observation == "accepted-dispatch-still-pending":
            return {"status": observation}
        # The old call can have claimed its run between the first read and its
        # terminal timeout. Re-read before considering any replacement spawn.
        row = current_run()
        if not unclaimed(row):
            return {"status": "already-claimed"}
    call_id = spawn(row["task_payload"])
    return {"dispatch_receipt": {"provider": "modal", "call_id": call_id}}
