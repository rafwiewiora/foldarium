# Acknowledged prediction dispatch observation

The injected adapter follows Modal SDK 1.5.5 semantics. This public module
imports no provider SDK and does not configure or deploy any worker. Its `_Invocation.poll_function`
implementation returns three materially different outcomes:

- No output with unfinished inputs raises Python's built-in `TimeoutError`.
- No output and no unfinished inputs raises `modal.exception.OutputExpiredError`.
- A backend `GENERIC_STATUS_TIMEOUT` result raises
  `modal.exception.FunctionTimeoutError`.

The latter two inherit Modal's own `TimeoutError`, which is unrelated to the
built-in type. The old broad Modal timeout handler therefore missed ordinary
polling deadlines and treated expired outputs as still pending.

A worker can itself raise built-in `TimeoutError`, and `get()` re-raises it.
Exception type alone cannot distinguish that terminal failure from a polling
deadline. On a built-in timeout, the adapter reads the public `get_call_graph()`
API and locates exactly one input for the previously acknowledged call ID,
including when that input is nested under another call:

| Observation | Reconciler behavior |
| --- | --- |
| Exact input still `PENDING` | Normal dependency wait; no additional spawn |
| Exact input terminal | `PredictionDispatchTerminalError`; retain receipt |
| Missing/ambiguous/unknown graph or graph transport failure | `PredictionDispatchObservationUncertain`; retain receipt |
| Expired output | Propagate `OutputExpiredError`; retain receipt |
| Other worker or transport exception | Propagate its type; retain receipt |
| Returned output but run still unclaimed | Terminal dispatch error; no spawn |
| Actual `FunctionTimeoutError` | Re-read the exact DB run, then permit the existing replacement path only if still pending/queued with zero attempts |

Modal documents the call graph as best-effort and potentially delayed. It is
used only to stop or defer work, never to authorize a new invocation. A stale
pending node defers observation; missing state is an explicit error, not proof
that no worker exists. The durable outbox retains the original receipt and
applies its existing bounded error budget to terminal and uncertain observations.
If output retention expires, the adapter fails explicitly rather than guessing
that another GPU invocation is safe.

The fresh DB read after a genuine function timeout prevents a stale pre-poll
zero-attempt row from authorizing a replacement after the old worker claimed it.
Any new scientific retry after a claimed failure still requires the existing
retry/attempt authorization path. No retry budget, gate, schedule or scientific
output is changed here.

Portable tests cover receipt retention, nested exact-call binding, uncertainty,
normal waiting, initial dispatch, terminal worker failures, and a run-claim race
before replacement. The operational integration also verified synthetic responses
through SDK 1.5.5 without network or credentials. Provider SDK and deployment
wiring tests remain outside this public tree.
