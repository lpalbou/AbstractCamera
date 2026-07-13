"""Config writes, one-shot actions, cache refresh, and the pending-write
honesty ledger (confirmed / reverted with family escalations)."""

from __future__ import annotations

import time


class ConfigLedgerMixin:

    def _config_write_budget_s(self) -> float:
        """Time budget for one config write transaction: generous when idle,
        deadline-derived during a sequence (verify loops must lose to the
        next shot exactly like downloads do)."""
        with self._state_lock:
            sequence = self._interval_sequence
        if sequence is not None and sequence.is_active:
            return max(0.5, (sequence.deadline() - time.time()) - 0.6)
        return 6.0

    # Transient-failure requeue policy: attempts x pacing must outlast the
    # longest measured busy phase (Sony post-burst card flush, ~10-15s).
    CONFIG_RETRY_MAX_ATTEMPTS = 8
    CONFIG_RETRY_PACE_S = 1.5

    def _apply_pending_config(self, camera) -> None:
        if not self._pending_config:
            return
        now = time.time()
        with self._state_lock:
            items: dict[str, str] = {}
            for name, value in list(self._pending_config.items()):
                # Paced retries stay queued until their next-try time.
                if self._config_retry_not_before.get(name, 0.0) > now:
                    continue
                items[name] = value
                del self._pending_config[name]
        if not items:
            return
        # Per-item application (F7): one bad widget must not silently drop
        # the rest, and every failure is a VISIBLE catch-log event (the old
        # code stashed it in last_error, which the UI only console.warn'd).
        applied_any = False
        for name, value in items.items():
            receipt = self._adapter.write_widget(
                camera, name, value, time_budget_s=self._config_write_budget_s())
            if receipt.ok:
                applied_any = True
                with self._state_lock:
                    self._config_retry_counts.pop(name, None)
                    self._config_retry_not_before.pop(name, None)
                if receipt.settled:
                    # Verified on the body (Sony write-verify-retry): settle
                    # the ledger entry now instead of waiting for a refresh.
                    with self._state_lock:
                        entry = self._pending_writes.get(name)
                        if entry is not None and str(entry.get("value")) == str(value):
                            entry["state"] = "confirmed"
                            entry["settled_at"] = time.time()
                continue
            if self._adapter.is_transient_write_error(receipt.error):
                with self._state_lock:
                    attempts = self._config_retry_counts.get(name, 0)
                    if attempts < self.CONFIG_RETRY_MAX_ATTEMPTS:
                        # Bounded paced requeue; a NEWER user value for the
                        # same widget always wins over the retry.
                        self._config_retry_counts[name] = attempts + 1
                        self._config_retry_not_before[name] = time.time() + self.CONFIG_RETRY_PACE_S
                        if name not in self._pending_config:
                            self._pending_config[name] = value
                        # Fresh ledger patience: the retry is still live —
                        # declaring a revert mid-retry would be premature.
                        entry = self._pending_writes.get(name)
                        if entry is not None and str(entry.get("value")) == str(value):
                            entry["requested_at"] = time.time()
                        continue
            with self._state_lock:
                self._config_retry_counts.pop(name, None)
                self._config_retry_not_before.pop(name, None)
                self._last_error = f"Config update failed: {receipt.error}"
            self._append_event(kind="error", reason="config",
                               note=f"could not set {name} = {value}: {receipt.error}")
        if applied_any:
            self._refresh_config_cache(camera)

    def _apply_pending_actions(self, camera) -> None:
        """One-shot action channel (F12): execute each queued drive exactly
        once. Results NEVER enter the config cache and are never replayed.
        The adapter maps canonical names (autofocusdrive/manualfocusdrive)
        to the body's widgets and runs the family's choreography."""
        if not self._pending_actions:
            return
        with self._state_lock:
            actions = list(self._pending_actions)
            self._pending_actions.clear()
            cache_snapshot = dict(self._config_cache)
        for name, value in actions:
            receipt = self._adapter.run_action(camera, name, value, cache_snapshot)
            if not receipt.ok:
                self._append_event(kind="error", reason="focus",
                                   note=f"camera action {name} failed: {receipt.error}")
            elif receipt.note:
                self._append_event(kind="trigger", reason="focus", note=receipt.note)

    def _refresh_config_cache(self, camera) -> None:
        # The adapter owns the widget list and the read mechanics
        # (single-config only where full-tree walks are unsafe). The
        # capability descriptor is derived from the same snapshot so the
        # frontend always sees a consistent pair.
        cache = self._adapter.read_config_cache(camera)
        capabilities = self._adapter.capabilities(cache)
        with self._state_lock:
            self._config_cache = cache
            self._capabilities = capabilities
            self._config_cache_version += 1
        self._settle_pending_writes(cache)

    def _settle_pending_writes(self, cache: dict) -> None:
        now = time.time()
        # Revert declaration: patience covers the family's measured settle
        # lag (Nikon ~5-7s -> 10s patience; Sony verifies in-call -> 6s),
        # two-consecutive-mismatch stability prevents declaring during it.
        patience_s = self._adapter.settle_patience_s() if self._adapter else 10.0
        escalations = self._adapter.settle_escalations() if self._adapter else {}
        reverted: list[tuple[str, str, str]] = []
        with self._state_lock:
            for name, entry in list(self._pending_writes.items()):
                if entry["state"] in ("confirmed", "reverted"):
                    # Kept ~3s so the UI can flash the outcome, then dropped
                    # (time-based: refreshes stop once nothing is pending).
                    if now - entry.get("settled_at", now) > 3.0:
                        self._pending_writes.pop(name, None)
                    continue
                cached = cache.get(name)
                current = None if cached is None else str(cached.get("value"))
                if current is not None and current == entry["value"]:
                    entry["state"] = "confirmed"
                    entry["settled_at"] = now
                    continue
                if now - entry["requested_at"] < patience_s:
                    continue
                entry["mismatches"] = entry.get("mismatches", 0) + 1
                entry["actual"] = current
                if entry["mismatches"] >= 2:
                    # Family escalation before the honest revert (Nikon:
                    # isoauto accepts the write with live view paused).
                    escalation_kind = escalations.get(name)
                    if escalation_kind and not entry.get("escalation_done"):
                        entry["escalation_requested"] = escalation_kind
                        entry["mismatches"] = 0
                        entry["requested_at"] = now  # fresh patience window
                        continue
                    entry["state"] = "reverted"
                    entry["settled_at"] = now
                    reverted.append((name, entry["value"], current if current is not None else "unknown"))
        for name, requested, actual in reverted:
            note = (
                f"the camera reverted {name} → {requested} (kept {actual}) — "
                "a camera-side control owns it right now"
            )
            iso_auto_on = str((cache.get("isoauto") or {}).get("value", "")).lower() == "on"
            if name == "iso" and iso_auto_on:
                note = f"the camera reverted iso → {requested}: ISO Auto is On — turn it Off first"
            elif name == "expprogram":
                note = f"the camera reverted the exposure mode to {actual} — the physical mode dial controls it"
            self._append_event(kind="error", reason="config", note=note)

    def _service_write_escalations(self, camera) -> None:
        """Family-specific rescue attempts for writes about to be declared
        reverted (Nikon lv_pause: release the remote viewfinder, rewrite,
        re-engage). Runs in the worker between frames; a failed escalation
        falls through to the honest revert path."""
        with self._state_lock:
            by_kind: dict[str, list[tuple[str, str]]] = {}
            for name, entry in self._pending_writes.items():
                kind = entry.get("escalation_requested")
                if kind and not entry.get("escalation_done"):
                    by_kind.setdefault(kind, []).append((name, entry["value"]))
                    entry["escalation_requested"] = None
                    entry["escalation_done"] = True
        if not by_kind:
            return
        for kind, items in by_kind.items():
            results = self._adapter.escalate_writes(camera, items, kind)
            for result in results:
                self._append_event(
                    kind="trigger" if result.ok else "error",
                    reason="config", note=result.note)
        self._refresh_config_cache(camera)
