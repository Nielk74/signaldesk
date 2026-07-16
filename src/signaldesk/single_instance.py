"""Cross-process ownership and activation handoff for SignalDesk."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from PySide6.QtCore import QIODevice, QLockFile, QObject, QStandardPaths, QThread, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

LOGGER = logging.getLogger("signaldesk")


class SingleInstanceError(RuntimeError):
    """Raised when SignalDesk cannot establish safe process ownership."""


class SingleInstanceGuard(QObject):
    """Allow one SignalDesk process and ask it to activate on later launches."""

    activation_requested = Signal()

    def __init__(
        self,
        app_id: str = "SignalDesk.Alerts",
        *,
        runtime_dir: str | Path | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        root = (
            Path(runtime_dir)
            if runtime_dir is not None
            else Path(
                QStandardPaths.writableLocation(
                    QStandardPaths.StandardLocation.AppLocalDataLocation
                )
            )
            / "runtime"
        )
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SingleInstanceError(
                f"The single-instance directory could not be created: {exc}"
            ) from exc

        lock_path = root / f"{app_id}.lock"
        identity = f"{app_id}|{lock_path.resolve()}".casefold().encode("utf-8")
        self._server_name = f"signaldesk-{hashlib.sha256(identity).hexdigest()[:20]}"
        self._lock = QLockFile(str(lock_path))
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._accept_activation_requests)
        self._owns_lock = False
        self._activation_pending = False

    def acquire(self) -> bool:
        """Return ``True`` for the primary process and notify it otherwise."""
        if self._owns_lock:
            return True
        if not self._lock.tryLock(0):
            if self._lock.error() != QLockFile.LockError.LockFailedError:
                raise SingleInstanceError(
                    "SignalDesk could not create its single-instance lock "
                    f"({self._lock.error().name})."
                )
            if not self._notify_primary():
                LOGGER.warning("Another SignalDesk instance is running but could not be activated")
            return False

        self._owns_lock = True
        # The lock proves no live peer owns this endpoint, so removing a stale
        # local-server entry is safe after crashes or unclean shutdowns.
        QLocalServer.removeServer(self._server_name)
        if not self._server.listen(self._server_name):
            LOGGER.warning(
                "SignalDesk owns the process lock, but activation handoff is unavailable: %s",
                self._server.errorString(),
            )
        return True

    def take_pending_activation(self) -> bool:
        """Consume an activation that arrived before the main window existed."""
        pending = self._activation_pending
        self._activation_pending = False
        return pending

    def release(self) -> None:
        """Release the endpoint and process lock. Safe to call repeatedly."""
        if self._server.isListening():
            self._server.close()
            QLocalServer.removeServer(self._server_name)
        if self._owns_lock:
            self._lock.unlock()
            self._owns_lock = False

    def _notify_primary(self) -> bool:
        for attempt in range(4):
            socket = QLocalSocket()
            socket.connectToServer(
                self._server_name,
                QIODevice.OpenModeFlag.WriteOnly,
            )
            if socket.waitForConnected(250):
                socket.write(b"activate\n")
                socket.flush()
                socket.waitForBytesWritten(250)
                socket.disconnectFromServer()
                return True
            if attempt < 3:
                QThread.msleep(50)
        return False

    def _accept_activation_requests(self) -> None:
        received = False
        while self._server.hasPendingConnections():
            connection = self._server.nextPendingConnection()
            if connection is None:
                break
            received = True
            connection.readAll()
            connection.disconnectFromServer()
            connection.deleteLater()
        if received:
            self._activation_pending = True
            self.activation_requested.emit()
