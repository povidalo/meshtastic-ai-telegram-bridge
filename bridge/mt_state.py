"""Process-wide handles for logger, notifier, shutdown, and active mesh interface."""

import threading
from typing import Optional

from meshtastic.mesh_interface import MeshInterface
from utils.logger.Logger import Logger
from utils.telegram.bot_notifier import Notifier

log: Logger
notifier: Notifier

_shutdown = threading.Event()
_iface_lock = threading.Lock()
_iface_ref: list[Optional[MeshInterface]] = [None]


def set_log_notifier(logger: Logger, note: Notifier) -> None:
    global log, notifier
    log = logger
    notifier = note
