#!/usr/bin/env python3
"""Offline control and validation gate. No collector, strategy, orders or notifications.

Every readiness field is an assertion supplied by a trusted operator/adapter.
Passing these checks does not independently authenticate an exchange connection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from bingx_standard_guard import Rejected, timestamp, validate as validate_source

UTC = timezone.utc
START = "شروع OCO اتوماتیک"
STOP = "کنسل کردن OCO اتوماتیک"


class GateError(ValueError):
    pass


def normalize_command(value: str) -> str:
    if not isinstance(value, str):
        raise GateError("command must be text")
    value = unicodedata.normalize("NFKC", value).replace("\u200c", " ")
    return " ".join(value.split())


def decimal(value, name, *, positive=False):
    if isinstance(value, bool) or isinstance(value, float):
        raise GateError(f"{name}: decimal string/integer required")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise GateError(f"{name}: invalid decimal")
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise GateError(f"{name}: nonnegative finite decimal required")
    return result


def time_value(value, name):
    try:
        return timestamp(value, name)
    except Rejected as exc:
        raise GateError(str(exc)) from exc


def clock_value(now=None):
    now = now or datetime.now(UTC)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise GateError("timezone-aware clock required")
    return now.astimezone(UTC)


def verified_window(record, now, maximum_age, name):
    if not isinstance(record, dict):
        raise GateError(f"{name}: verification required")
    verified = time_value(record.get("verified_at"), f"{name} verified_at")
    expires = time_value(record.get("expires_at"), f"{name} expires_at")
    if not -2 <= (now-verified).total_seconds() <= maximum_age:
        raise GateError(f"{name}: verification stale or in future")
    if not verified < expires <= verified + timedelta(seconds=maximum_age) or now >= expires:
        raise GateError(f"{name}: verification expired or validity too long")


def canonical_decimal(value):
    return format(value.normalize(), "f")


def check_readiness(ready: dict, now=None) -> dict:
    now = clock_value(now)
    if not isinstance(ready, dict):
        raise GateError("readiness missing")
    for key in ("source_verified", "real_feed_connected", "persistence_verified", "delivery_verified"):
        if ready.get(key) is not True:
            raise GateError(f"readiness: {key} must be true")
    expected = {"provider": "BingX", "product": "standard_futures", "symbol": "BTCUSDT", "price_kind": "index_price"}
    for key, value in expected.items():
        if ready.get(key) != value:
            raise GateError(f"readiness: wrong {key}")
    cadence = decimal(ready.get("cadence_seconds"), "cadence_seconds", positive=True)
    if cadence > 15:
        raise GateError("readiness: cadence must be at most 15 seconds; hourly polling is insufficient")
    if ready.get("delivery_kind") not in {"local_file", "verified_external_sink"}:
        raise GateError("readiness: unsupported delivery; this starter cannot create ChatGPT chats")
    if not isinstance(ready.get("provenance_id"), str) or not ready["provenance_id"].strip():
        raise GateError("readiness: provenance_id required")
    verified_window(ready, now, 60, "readiness")
    return ready


def validate_proposal(proposal: dict, ready: dict, now: datetime) -> dict:
    now = clock_value(now)
    check_readiness(ready, now)
    if not isinstance(proposal, dict):
        raise GateError("proposal must be an object")
    if proposal.get("provenance_id") != ready["provenance_id"]:
        raise GateError("proposal provenance does not match readiness")
    try:
        source = validate_source(proposal.get("source"), now=now, max_age_seconds=15)
    except (Rejected, ValueError) as exc:
        raise GateError(f"source: {exc}") from exc
    if source["status"] != "FRESH_SOURCE_TIMESTAMP":
        raise GateError("exchange event timestamp required; observation-only data cannot trigger an alert")
    decision = time_value(proposal.get("decision_at"), "decision_at")
    if not -2 <= (now-decision).total_seconds() <= 15:
        raise GateError("decision is stale or in the future")
    bar = proposal.get("signal_bar")
    if not isinstance(bar, dict) or bar.get("timeframe") != "5m" or bar.get("closed") is not True:
        raise GateError("closed 5m signal bar required")
    opened = time_value(bar.get("open_time"), "bar open_time")
    closed = time_value(bar.get("close_time"), "bar close_time")
    if (closed-opened).total_seconds() != 300 or int(closed.timestamp()) % 300:
        raise GateError("5m bar boundaries are inconsistent")
    latest_close = int(decision.timestamp()) // 300 * 300
    if closed.timestamp() != latest_close or closed > decision:
        raise GateError("signal must use the latest fully closed 5m bar before decision")
    expires = time_value(proposal.get("expires_at"), "expires_at")
    if not decision < expires <= decision + timedelta(minutes=15) or now >= expires:
        raise GateError("proposal expired or expiry exceeds 15 minutes")
    direction = proposal.get("direction")
    entry = decimal(proposal.get("entry"), "entry", positive=True)
    sl = decimal(proposal.get("sl"), "sl", positive=True)
    tp = decimal(proposal.get("tp"), "tp", positive=True)
    if direction == "short" and tp < entry < sl:
        loss, reward = sl-entry, entry-tp
    elif direction == "long" and sl < entry < tp:
        loss, reward = entry-sl, tp-entry
    else:
        raise GateError("Entry/SL/TP direction is invalid")
    entry_range = proposal.get("entry_range")
    if not isinstance(entry_range, dict):
        raise GateError("entry_range required")
    entry_min = decimal(entry_range.get("min"), "entry_range min", positive=True)
    entry_max = decimal(entry_range.get("max"), "entry_range max", positive=True)
    if not entry_min <= entry <= entry_max:
        raise GateError("entry reference outside entry_range")
    quote = decimal(source["price"], "current source price", positive=True)
    if not min(sl, tp) < quote < max(sl, tp):
        raise GateError("current quote has reached/passed TP or SL")
    if not entry_min <= quote <= entry_max:
        raise GateError("current quote outside executable entry_range")
    if not min(sl, tp) < entry_min <= entry_max < max(sl, tp):
        raise GateError("entry_range overlaps TP or SL")
    history = proposal.get("invalidation_check")
    if (not isinstance(history, dict) or history.get("invalidated") is not False
            or history.get("provenance_id") != ready["provenance_id"]
            or not isinstance(history.get("evidence"), str) or not history["evidence"].strip()):
        raise GateError("trusted historical invalidation check required; invalidated proposals cannot revive")
    checked = time_value(history.get("checked_at"), "invalidation checked_at")
    since = time_value(history.get("since"), "invalidation since")
    through = time_value(history.get("through"), "invalidation through")
    if not -2 <= (now-checked).total_seconds() <= 15:
        raise GateError("historical invalidation check stale or future")
    source_time = time_value(source["source_timestamp"], "source timestamp")
    if not since <= opened <= closed <= through <= checked or through < source_time:
        raise GateError("historical invalidation coverage incomplete")
    costs = proposal.get("costs")
    if not isinstance(costs, dict):
        raise GateError("explicit costs required")
    verification = costs.get("verification")
    verified_window(verification, now, 3600, "costs")
    for field, expected in (("provider", "BingX"), ("product", "standard_futures"), ("symbol", "BTCUSDT")):
        if verification.get(field) != expected:
            raise GateError(f"costs: verification wrong {field}")
    if not isinstance(verification.get("evidence"), str) or not verification["evidence"].strip():
        raise GateError("costs: verification evidence required")
    fee_rate = decimal(costs.get("fee_rate_total"), "fee_rate_total")
    if fee_rate >= Decimal("0.05"):
        raise GateError("fee rate is implausible; verify units")
    if fee_rate == 0 and costs.get("zero_fee_verified") is not True:
        raise GateError("zero fee requires explicit verified zero-fee evidence")
    execution = decimal(costs.get("execution_reserve_per_unit"), "execution_reserve_per_unit")
    funding = decimal(costs.get("funding_reserve_per_unit"), "funding_reserve_per_unit")
    cost = entry*fee_rate + execution + funding
    net_reward, net_loss = reward-cost, loss+cost
    minimum_rr = decimal(ready.get("minimum_net_rr", "2"), "minimum_net_rr", positive=True)
    if net_reward <= 0 or net_reward/net_loss < minimum_rr:
        raise GateError("net reward/risk after costs is below configured minimum")
    worst_entry = entry_min if direction == "short" else entry_max
    worst_cost = worst_entry*fee_rate + execution + funding
    if direction == "short":
        worst_loss, worst_reward = sl-worst_entry+worst_cost, worst_entry-tp-worst_cost
    else:
        worst_loss, worst_reward = worst_entry-sl+worst_cost, tp-worst_entry-worst_cost
    worst_rr = worst_reward/worst_loss
    if worst_reward <= 0 or worst_rr < minimum_rr:
        raise GateError("entry_range worst-bound net reward/risk is below configured minimum")
    fingerprint_fields = [ready["provenance_id"], direction,
                          *(canonical_decimal(value) for value in (entry, sl, tp)), closed.isoformat()]
    key = hashlib.sha256(json.dumps(fingerprint_fields).encode()).hexdigest()[:24]
    return {"status": "OFFLINE_CANDIDATE_PASSED_BASIC_GATES", "id": key,
            "direction": direction, "entry_reference": str(entry), "sl": str(sl), "tp": str(tp),
            "entry_range": {"min": str(entry_min), "max": str(entry_max)},
            "decision_at": decision.isoformat(), "expires_at": expires.isoformat(),
            "signal_bar_close": closed.isoformat(), "provenance_id": ready["provenance_id"],
            "source": source, "cost_per_unit": str(cost), "net_loss_per_unit": str(net_loss),
            "net_reward_per_unit": str(net_reward), "net_rr": str(net_reward/net_loss),
            "minimum_net_rr_in_range": str(worst_rr), "worst_entry_in_range": str(worst_entry),
            "worst_net_loss_per_unit": str(worst_loss),
            "strategy_validated": False, "all_agreement_conditions_verified": False,
            "position_or_fill_inferred": False, "exchange_orders_submitted": False,
            "chatgpt_chat_created": False, "delivery": "local_json_only"}


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class Controller:
    def __init__(self, state_path: Path, output_dir: Path, *, lock_timeout_seconds=5):
        self.state_path, self.output_dir = Path(state_path), Path(output_dir)
        self.lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        self.lock_timeout_seconds = lock_timeout_seconds
        self._reload_state()

    def _reload_state(self):
        self.state = {"enabled": False, "queued_unfilled_alerts": [], "seen": {}}
        if self.state_path.exists():
            loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
            if (not isinstance(loaded, dict) or type(loaded.get("enabled")) is not bool
                    or not isinstance(loaded.get("queued_unfilled_alerts", []), list)
                    or not isinstance(loaded.get("seen", {}), dict)):
                raise GateError("persisted state malformed; refusing to enable")
            self.state.update(loaded)

    @contextmanager
    def _locked(self):
        """One shared interprocess lock; abandoned locks fail closed, never auto-deleted."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.lock_timeout_seconds
        fd = None
        while fd is None:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                if time.monotonic() >= deadline:
                    raise GateError("state lock busy/abandoned; fail closed, operator inspection required") from exc
                time.sleep(0.01)
        owned = os.fstat(fd)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            self._reload_state()
            yield
        finally:
            os.close(fd)
            try:
                current = self.lock_path.lstat()
                if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                    self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def control(self, command: str, *, trusted_operator: bool = False, readiness=None, now=None):
        with self._locked():
            return self._control_locked(command, trusted_operator=trusted_operator,
                                        readiness=readiness, now=now)

    def _control_locked(self, command: str, *, trusted_operator: bool = False, readiness=None, now=None):
        if not trusted_operator:
            raise GateError("control is accepted only from a trusted operator channel, never market payloads")
        text = normalize_command(command)
        if text == STOP:
            cleared = len(self.state["queued_unfilled_alerts"])
            self.state.update(enabled=False, queued_unfilled_alerts=[])
            atomic_json(self.state_path, self.state)
            return {"status": "PAUSED", "cleared_local_unfilled_alerts": cleared,
                    "exchange_orders_changed": False, "positions_changed": False}
        if text != START:
            raise GateError("not an exact standalone start/stop command")
        ready = check_readiness(readiness, now)
        self.state.update(enabled=True, provenance_id=ready["provenance_id"])
        atomic_json(self.state_path, self.state)
        return {"status": "STARTER_GATE_ENABLED", "live_feed_independently_verified": False,
                "exchange_orders_changed": False, "chatgpt_chat_created": False}

    def submit(self, proposal: dict, readiness: dict, *, now=None):
        with self._locked():
            return self._submit_locked(proposal, readiness, now=now)

    def _submit_locked(self, proposal: dict, readiness: dict, *, now=None):
        now = clock_value(now)
        if self.state.get("enabled") is not True:
            raise GateError("paused: no candidate output allowed")
        check_readiness(readiness, now)
        if self.state.get("provenance_id") != readiness.get("provenance_id"):
            raise GateError("source changed since activation")
        result = validate_proposal(proposal, readiness, now)
        seen = {key: expiry for key, expiry in self.state["seen"].items()
                if time_value(expiry, "seen expiry") > now}
        if result["id"] in seen:
            return {"status": "DUPLICATE", "id": result["id"]}
        self.output_dir.mkdir(parents=True, exist_ok=True)
        destination = self.output_dir / (result["id"] + ".json")
        try:
            with destination.open("x", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            return {"status": "DUPLICATE", "id": result["id"]}
        seen[result["id"]] = result["expires_at"]
        queue = [item for item in self.state["queued_unfilled_alerts"]
                 if time_value(item["expires_at"], "queue expiry") > now]
        queue.append({"id": result["id"], "expires_at": result["expires_at"]})
        self.state.update(seen=seen, queued_unfilled_alerts=queue)
        atomic_json(self.state_path, self.state)
        return {"status": "LOCAL_CANDIDATE_SAVED", "path": str(destination), "id": result["id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument("--output", type=Path, default=Path("candidates"))
    parser.add_argument("--readiness", type=Path)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--operator-command", help="Trusted operator CLI only; never bind directly to market/LLM text")
    actions.add_argument("--proposal", type=Path)
    args = parser.parse_args()
    try:
        ctl = Controller(args.state, args.output)
        ready = json.loads(args.readiness.read_text(encoding="utf-8")) if args.readiness else None
        if args.operator_command is not None:
            result = ctl.control(args.operator_command, trusted_operator=True, readiness=ready)
        else:
            if ready is None:
                raise GateError("readiness required")
            result = ctl.submit(json.loads(args.proposal.read_text(encoding="utf-8")), ready)
    except (GateError, OSError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
