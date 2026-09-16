"""Test-only fault runner for scripts/receipt_inventory_supervisor.py.

Invoked as::

    python3 -I -S tests/receipt_supervisor_faults.py --fault NAME \\
        --pgid-file DIR/wrapper.pgid [supervisor options] -- ARGV...

The runner imports the production supervisor as a module, replaces exactly one
of ``publish``, ``read_capture`` or ``tick`` on the module object, and calls
``main``. The production script has no fault switch. Every fault writes
``DIR/fault.fired`` by atomic rename immediately before acting; every gate is a
bounded 4 s wait whose expiry exits 13, never a silent fallthrough.

Gate order for identity-gated faults: ``producer.json`` -> ``identity.ready``
(written by the worker only after live /proc and readiness checks) ->
``fault.fired`` -> action. Publication-free faults gate on ``producer.json``
only, so neither side of the harness waits on the other.
"""

import os
import signal
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scripts.receipt_inventory_supervisor as supervisor  # noqa: E402

GATE_SECONDS = 4.0
GATE_TIMEOUT_EXIT = 13
INJECTED_SIGNAL = signal.SIGTERM


def _atomic_touch(directory, name):
    path = os.path.join(directory, name)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="ascii") as handle:
        handle.write(f"{time.time_ns()}\n")
    os.replace(temporary, path)


def _gate(directory, name):
    """Wait at most GATE_SECONDS for DIR/name; expiry is exit 13."""
    path = os.path.join(directory, name)
    deadline = time.monotonic() + GATE_SECONDS
    while not os.path.exists(path):
        if time.monotonic() >= deadline:
            os._exit(GATE_TIMEOUT_EXIT)
        time.sleep(0.02)


def _hang():
    while True:
        time.sleep(3600)


def _die():
    os.kill(os.getpid(), signal.SIGKILL)
    _hang()


def _raise_signals(count):
    for _ in range(count):
        # raise_signal runs the Python-level handler before returning, so
        # each repetition is observed as a distinct delivery.
        signal.raise_signal(INJECTED_SIGNAL)


def install(fault, directory):
    production_tick = supervisor.tick
    fired = {"done": False}
    gated = {"done": False}

    def fire_once():
        if not fired["done"]:
            fired["done"] = True
            _atomic_touch(directory, "fault.fired")

    def identity_gate_once():
        if not gated["done"]:
            gated["done"] = True
            _gate(directory, "identity.ready")

    if fault == "stall_wait":
        def tick(phase, state):
            if phase == "wait":
                identity_gate_once()
                fire_once()
                _atomic_touch(directory, "supervisor.stalled")
                _hang()
            production_tick(phase, state)
        supervisor.tick = tick
    elif fault == "stall_cleanup":
        def tick(phase, state):
            if phase == "wait":
                identity_gate_once()
            elif phase == "cleanup":
                fire_once()
                _atomic_touch(directory, "supervisor.stalled")
                _hang()
            production_tick(phase, state)
        supervisor.tick = tick
    elif fault == "publish_error":
        def publish(path, pgid):
            _gate(directory, "producer.json")
            fire_once()
            raise OSError("injected publication failure")
        supervisor.publish = publish
    elif fault == "die_before_publish":
        def publish(path, pgid):
            _gate(directory, "producer.json")
            fire_once()
            _die()
        supervisor.publish = publish
    elif fault == "die_after_ready":
        def tick(phase, state):
            if phase == "wait":
                identity_gate_once()
                fire_once()
                _die()
            production_tick(phase, state)
        supervisor.tick = tick
    elif fault == "capture_error":
        def read_capture(fd, state):
            identity_gate_once()
            fire_once()
            raise RuntimeError("injected capture failure")
        supervisor.read_capture = read_capture
    elif fault.startswith("inject_signal:"):
        _, phase_name, count = fault.split(":")
        count = int(count)
        if phase_name not in ("startup", "wait", "cleanup"):
            raise SystemExit(f"unknown injection phase: {phase_name}")

        def tick(phase, state):
            if phase_name == "startup" and phase == "startup":
                fire_once()
                _raise_signals(count)
            elif phase_name == "wait" and phase == "wait" and not fired["done"]:
                identity_gate_once()
                fire_once()
                _raise_signals(count)
            elif phase_name == "cleanup":
                if phase == "wait":
                    identity_gate_once()
                elif phase == "cleanup" and not fired["done"]:
                    fire_once()
                    _raise_signals(count)
            production_tick(phase, state)
        supervisor.tick = tick
    else:
        raise SystemExit(f"unknown fault: {fault}")


def main(argv):
    if len(argv) < 2 or argv[0] != "--fault":
        raise SystemExit("usage: --fault NAME --pgid-file PATH [options] -- ARGV...")
    fault = argv[1]
    rest = argv[2:]
    if "--pgid-file" not in rest:
        raise SystemExit("--pgid-file is required to derive the fixture directory")
    pgid_file = rest[rest.index("--pgid-file") + 1]
    directory = os.path.dirname(os.path.abspath(pgid_file))
    install(fault, directory)
    return supervisor.main(rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
