# AI Forklift Safety System — v3 Upgrade Report

## 0. Scope and method

This v3 pass started from a fresh clone of `Amr518/AI_Forklift_Safety` (the "final" version shared by the user), reviewed against the earlier v1 baseline reviewed previously. It did **not** have access to physical hardware, a real camera feed, or PyQt5/ultralytics installed — all tests below ran against hand-written stub modules (`serial`, `PyQt5.QtCore`, `ultralytics`) placed on `PYTHONPATH`, plus one real end-to-end test over a genuine Unix domain socket. Every claim below states exactly what was and wasn't verified this way.

An independent review pass (a separate agent, given only the unified diffs and no visibility into this write-up) caught two real defects in the first draft of this patch set before delivery — both are fixed and re-verified below. That is disclosed here rather than silently folded in, because it is relevant to how much confidence to place in "reviewed and tested."

## 1. What this repo already had right (not touched)

The repo reviewed had already moved past the v1 baseline in ways worth keeping as-is:
- Unix Domain Socket IPC between `hardware_worker.py` (server) and `hardware_manager.py` (client) — a real architectural improvement over a shared JSON file.
- Physical USB port binding (prevents camera "swapping" on cable jostle).
- Normalized (0.0–1.0) ROI coordinates in `roi.py` — correct fix for resize-related ROI drift.
- Camera hotplug recovery, systemd service, OSK touch keyboard, 5-tier RBAC.
- `test_or_gate_logic.py` — 13 real, runnable unit tests covering the OR-gate/relay-mapping logic. Confirmed still passing after every change in this pass.

**Documentation correction:** `PROJECT_AUDIT_AND_BUGS_REFERENCE_AR.md` claimed 4 test suites at 100% pass. Only 1 of the 4 (`test_or_gate_logic.py`) actually exists in the repository — the other three live under a `scratch/` directory that was never committed. That claim has been corrected in place rather than repeated.

## 2. What was wrong and is now fixed

| ID | Gap | Severity | Fix |
|----|-----|----------|-----|
| G-01 | Passwords stored in plaintext JSON | High | PBKDF2-HMAC-SHA256 hashing, transparent migration, forced change on default/migrated accounts, 5-attempt/5-min lockout |
| G-02 | ROI check used bbox **centroid** only — a person with feet in the hazard zone and centroid outside it was missed | **Critical** | Added foot-point (bbox bottom-center) + overlap-fraction check in `alarm.py` |
| G-03 | No AI/GUI liveness watchdog — the existing "heartbeat" was transport-only (relay sync), not a check that the AI process is alive. A frozen/crashed GUI while the zone was clear left relays silently latched off forever with no error reported | **Critical** | New AI-liveness watchdog in `hardware_worker.py` + heartbeat sender in `hardware_manager.py` — forces watchdog-covered relays ON (fail-to-alarm) if no heartbeat arrives within 3s (after a 5s startup grace) |
| G-04 | No noise filter for tiny/far detections | Low | `MIN_BOX_AREA_FRACTION` filter in `alarm.py`, active only when `frame_w`/`frame_h` are passed |
| G-05 | `relay_mapping: [2,1,1,2]` — stations 1&4 share relay 2, stations 2&3 share relay 1 | Needs field verification | Documented in `config.json` as `_RELAY_MAPPING_NOTE` — **verify with a multimeter against actual wiring before relying on this**; this was not changed because it may be an intentional shared-circuit design |

`detector.py`'s confidence threshold was **deliberately left unchanged**. Raising it would have reduced false positives but also risks losing real detections at range/occlusion — a sensitivity tradeoff for whoever owns field validation to make deliberately, not something to change silently in a documentation pass. The noise problem is instead handled by the area filter above, which filters by bbox size rather than model confidence and costs nothing in true-positive sensitivity.

A multi-frame consensus tracker (`detector.PersonTracker`) was added but is **not wired into the GUI by default** — same reasoning: it trades a few frames of latency for noise reduction, and that tradeoff belongs to validation, not to this pass.

## 3. Defects found and fixed during independent review (disclosed)

A second, independent review pass over the diffs (not the full codebase) found two real issues in the first draft, both fixed and re-verified before delivery:

1. **Watchdog bypassing `hardware_coil_enabled=0`.** The first version of the watchdog override forced covered relays ON whenever tripped, with no regard for whether a technician had explicitly disabled the hardware coil (e.g. for maintenance). Fixed: the override now only fires when `hardware_coil_enabled=1` — a deliberate human disable takes precedence over the automatic fail-safe, because forcing outputs ON during an intentionally-opened maintenance window is itself a hazard. Re-verified with a dedicated test (`test_watchdog_coil_gate.py`): watchdog trip produces **no** relay activation while the coil is disabled, and still produces full activation once it's enabled.

2. **Lockout bypass via role aliasing in `security.py`.** `Admin` and `Administrator` resolve to the same password record, but the lockout counter was keyed by the literal role string — an attacker could exhaust 5 attempts as `"Admin"` and keep guessing the identical account as `"Administrator"`, doubling the effective brute-force budget against the highest-privilege account. Fixed: lockout tracking is now keyed by the resolved canonical account, not the literal role string. Re-verified: locking out as `"Admin"` now correctly locks out `"Administrator"` too.

## 4. What was verified and how

| Component | Test | Result |
|---|---|---|
| `security.py` | Hashing round-trip, must_change flag clears on password change, 5-attempt lockout blocks even the correct password, legacy plaintext migrates and still logs in, Admin/Administrator lockout now shared | All pass |
| `alarm.py` | Exact centroid-blind-spot regression case (foot in / centroid out), min-area filter suppresses a 5×10px detection but not a real one, backward-compatible when frame dims omitted | All pass |
| `detector.py` | `PersonTracker`: one-off spurious detection not confirmed; a real person confirmed after 2-of-5 consecutive frames | All pass |
| `hardware_worker.py` watchdog | Unit: no-trip during grace / trip on never-connected / clears on heartbeat / trips again on stale heartbeat / disabled watchdog never trips. Integration: **real** `IPCServer` + `USBRelayManager` over a real Unix socket — watchdog stays clear while the heartbeat thread runs, and correctly trips when only the heartbeat thread is killed while the socket stays open (the "hung but connected" case a simple disconnect check would miss) | All pass |
| `hardware_worker.py` coil gate | Dedicated test: watchdog trip with `hardware_coil_enabled=0` produces zero relay activation; with it enabled, full activation | Pass |
| Existing `test_or_gate_logic.py` | Re-run unmodified against the patched `hardware_worker.py` | All 13 original tests still pass |
| `ai_gui_system.py` | `python3 -m py_compile` only — **not functionally tested**, since PyQt5/a display are not available in this environment. Both edits are two-line, surgical, and limited to call sites already confirmed by direct code inspection | Syntax-verified only |

## 5. What this pass did NOT do — next steps

- **`ai_gui_system.py` is not functionally tested.** The two edits (passing `frame_w`/`frame_h`, the must-change-password dialog) are minimal and targeted at confirmed call sites, but they have not run against a real PyQt5 display or a real camera. Smoke-test this on the actual hardware before trusting it.
- **`relay_mapping: [2,1,1,2]`** needs physical verification against the wiring — see `_RELAY_MAPPING_NOTE` in `config.json`.
- **`ai_watchdog_tripped` is broadcast but not yet shown anywhere in the GUI.** `hardware_manager.USBRelayManager.ai_watchdog_tripped` is a ready-to-read property; wiring a banner/log line for it in `ai_gui_system.py` is a one-line follow-up, deliberately left out of this pass to keep the GUI diff minimal.
- **No full 21 CFR Part 11 / GMP audit trail.** `security.py`'s hashing and lockout are a real security improvement, not a compliance implementation.
- **Radar / seat-sensor integration** (from the original project scope) is not represented anywhere in this codebase.
