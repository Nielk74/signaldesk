from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from signaldesk.single_instance import SingleInstanceGuard


def _run_primary_instance(
    runtime_dir: str,
    ready: object,
    activated: object,
    stop: object,
) -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    guard = SingleInstanceGuard("SignalDesk.ProcessTest", runtime_dir=runtime_dir)
    if not guard.acquire():
        return
    guard.activation_requested.connect(activated.set)
    ready.set()
    try:
        while not stop.is_set():
            app.processEvents()
            time.sleep(0.01)
    finally:
        guard.release()


def test_second_instance_exits_and_activates_primary(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    primary = SingleInstanceGuard("SignalDesk.Test", runtime_dir=tmp_path)
    secondary = SingleInstanceGuard("SignalDesk.Test", runtime_dir=tmp_path)
    activations: list[bool] = []
    primary.activation_requested.connect(lambda: activations.append(True))

    try:
        assert primary.acquire() is True
        assert secondary.acquire() is False
        app.processEvents()

        assert activations == [True]
        assert primary.take_pending_activation() is True

        primary.release()
        assert secondary.acquire() is True
    finally:
        secondary.release()
        primary.release()


def test_single_instance_guard_works_across_processes(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    activated = context.Event()
    stop = context.Event()
    process = context.Process(
        target=_run_primary_instance,
        args=(str(tmp_path), ready, activated, stop),
    )
    process.start()
    secondary = SingleInstanceGuard("SignalDesk.ProcessTest", runtime_dir=tmp_path)
    try:
        assert ready.wait(5)
        assert secondary.acquire() is False
        assert activated.wait(5)
    finally:
        secondary.release()
        stop.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0
