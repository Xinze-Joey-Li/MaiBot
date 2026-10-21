"""Non-blocking receipt capture; no network, Replay lookup, retry or success mutation."""

from dataclasses import dataclass, replace
from typing import Any, Optional, Tuple
import time

from . import recorder
from .models import Batch, DeliveryScope, ReceiptFact, ReceiptProbe, identity


@dataclass(frozen=True)
class AttemptBinding:
    scope: DeliveryScope
    generation: Tuple[int, int]
    key: Tuple[Optional[str], ...]
    attempt_id: str
    failure_evidence: Optional[str]


def route_identity(route: Any, outbound_id: str, driver_id: Optional[str]) -> Tuple[Optional[str], ...]:
    return route.platform, route.account_id or None, route.scope or None, outbound_id, driver_id


def scope_for(flow: Any) -> DeliveryScope:
    ref = flow.references[0]
    return DeliveryScope(identity("delivery_scope", recorder.store.run_id, flow.session_id, str(ref.generation)),
                         flow.session_id, recorder.store.run_id, ref.opportunity.policy_version, time.time())


@recorder.isolated
def begin(message: Any, route: Any, attempt_id: str, driver: Any) -> Optional[AttemptBinding]:
    if not recorder.store.policy.enabled:
        return None
    from .reply_flow import current
    from src.platform_io.drivers.plugin_driver import PluginPlatformDriver

    flow = current(message.session_id)
    if flow is None or route.platform != "qq" or (route.account_id or None) != flow.account_id:
        return None
    ref = flow.references[0]
    if recorder.store.generation(flow.session_id) != ref.generation:
        return None
    failure = None
    # These existing branches return before any RPC. Ordinary FAILED alone is insufficient.
    if isinstance(driver, PluginPlatformDriver):
        if not driver._supports_send:
            failure = "gateway_send_disabled"
        elif not driver.descriptor.plugin_id:
            failure = "gateway_plugin_id_missing"
    binding = AttemptBinding(scope_for(flow), ref.generation,
                             route_identity(route, message.message_id, driver.driver_id), attempt_id, failure)
    cache = recorder.store.delivery_bindings
    cache[(binding.key, attempt_id)] = binding
    if len(cache) > 4096:
        cache.popitem(last=False)
    return binding


def _snapshot(receipt: Any, source: str) -> ReceiptFact:
    metadata = receipt.metadata
    retcode = metadata.get("retcode")
    return ReceiptFact(receipt.route_key.platform, receipt.route_key.account_id or None,
                       receipt.route_key.scope or None, receipt.internal_message_id, receipt.driver_id,
                       receipt.driver_kind.value if receipt.driver_kind is not None else None,
                       receipt.status.value, receipt.external_message_id,
                       str(metadata["delivery_id"]) if metadata.get("delivery_id") else None,
                       metadata.get("platform_acknowledged") is True,
                       retcode if isinstance(retcode, int) and not isinstance(retcode, bool) else None,
                       time.time(), source)


@recorder.isolated
def returned(binding: Optional[AttemptBinding], receipt: Any) -> None:
    if not recorder.store.policy.enabled or binding is None:
        return
    if (route_identity(receipt.route_key, receipt.internal_message_id, receipt.driver_id) != binding.key
            or recorder.store.generation(binding.scope.session_id) != binding.generation):
        recorder.store.delivery_unmatched += 1
        return
    raw = _snapshot(receipt, "driver_return")
    fact = replace(raw, attempt_id=binding.attempt_id,
                   failure_evidence=binding.failure_evidence)
    if recorder.store.submit(Batch(binding.scope.session_id, binding.generation, (),
                                   delivery_scope=binding.scope, receipt=fact)):
        # finish_tracking immediately observes this same object. Do not invent a second,
        # ambiguous callback fact for that in-band receipt. Mutated later ACKs still pass.
        recorder.store.delivery_inline_receipt = (receipt, replace(raw, observed_at=0, source=""))


@recorder.isolated
def receipt_observed(receipt: Any) -> None:
    """Tracker/reconcile callbacks carry route+driver+outbound, never a guessed current reply."""
    store = recorder.store
    if not store.policy.enabled:
        return
    fact = _snapshot(receipt, "tracker_callback")
    inline = store.delivery_inline_receipt
    if inline is not None and inline[0] is receipt and inline[1] == replace(fact, observed_at=0, source=""):
        return
    key = route_identity(receipt.route_key, receipt.internal_message_id, receipt.driver_id)
    # Search is bounded (4096); the persistent fallback runs only on the writer thread.
    matches = [b for b in tuple(store.delivery_bindings.values()) if b.key == key
               and store.generation(b.scope.session_id) == b.generation]
    scopes = {b.scope.scope_id for b in matches}
    if len(scopes) == 1:
        binding = matches[0]  # All candidates share this authorization scope, not necessarily an attempt.
        single = len(matches) == 1
        fact = replace(fact, attempt_id=binding.attempt_id if single else None, ambiguous=not single,
                       failure_evidence=binding.failure_evidence if single else None)
        store.submit(Batch(binding.scope.session_id, binding.generation, (),
                           delivery_scope=binding.scope, receipt=fact))
        return
    authorizations = tuple((sid, generation) for sid in store.policy.authorized_session_ids
                           if (generation := store.generation(sid)) is not None)
    if authorizations:
        store.submit_probe(ReceiptProbe(fact, authorizations))
