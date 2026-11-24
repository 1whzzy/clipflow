"""Nodes 1–2: intake and route_mode."""
from langgraph.runtime import Runtime

from ..contracts import RunRequest
from ..graph.context import RunContext
from ..graph.state import RunState
from ._util import request


class IntakeError(ValueError):
    """The job can't start: bad input, unknown product, or no footage. Returned to the caller (40001)."""


def intake(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """1. Validate the request before anything costs money, and complete it:
    - product_id only → load the product sheet from the catalog;
    - slice job without a replay → use the replays ingestion linked to the product.
    The stored request is always complete, so later nodes never resolve anything themselves."""
    ctx = runtime.context
    req = RunRequest.model_validate(state["input"])
    if req.product is None:
        try:
            req = req.model_copy(update={"product": ctx.catalog.get(req.product_id)})
        except KeyError:
            raise IntakeError(f"unknown product {req.product_id!r}: no product sheet in the catalog") from None
    if req.mode == "slice" and not req.source_videos:
        linked = ctx.library.replays_for(req.product.name)
        if not linked:
            raise IntakeError(f"slice mode: no replay given and none linked to {req.product.name!r} by ingestion")
        req = req.model_copy(update={"source_videos": linked[:1]})
    if req.mode == "material" and not (req.product_shots or ctx.library.find(req.product.name, req.product.colors,
                                                                              None, 1)):
        raise IntakeError(f"material mode: no product footage for {req.product.name!r}")
    return {"run_id": req.run_id, "mode": req.mode, "request": ctx.store.put("request", req),
            "status": "running", "phase": "plan", "review_round": 0, "qc_round": 0, "video_round": 0}


def route_mode(state: RunState, runtime: Runtime[RunContext]) -> dict:
    """2. Record what this run is missing before the mode-specific part starts. The branch itself is
    the conditional edge `routing.after_route`."""
    ctx = runtime.context
    req = request(ctx, state)
    flags = []
    rule_id, _ = ctx.rules.priority_for(req.product)
    if rule_id.endswith("其他"):
        flags.append(f"no_category_rule:{req.product.category}")  # spec 2.1 issue 5: rules still missing
    if not req.product.price_verified:
        flags.append("price_unverified")  # the video may not state the price
    return {"flags": flags}
