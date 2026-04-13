"""Meshtastic TCP/serial connection lifecycle and reconnect loop."""

import signal
import time
from typing import Any, Optional

import meshtastic.serial_interface
import meshtastic.tcp_interface
from meshtastic.mesh_interface import MeshInterface

import config

from . import mt_state


def create_mesh_interface() -> MeshInterface:
    if config.MESH_CONNECTION_MODE == "tcp":
        return meshtastic.tcp_interface.TCPInterface(
            hostname=config.MESH_TCP_HOST, portNumber=config.MESH_TCP_PORT
        )
    if config.MESH_CONNECTION_MODE == "serial":
        return meshtastic.serial_interface.SerialInterface(
            devPath=config.SERIAL_PORT
        )
    raise ValueError(f"Unknown MESH_CONNECTION_MODE: {config.MESH_CONNECTION_MODE!r}")


def shutdown_handler(signum: int, frame: Any) -> None:
    mt_state._shutdown.set()
    with mt_state._iface_lock:
        iface = mt_state._iface_ref[0]
        if iface is not None:
            try:
                iface.close()
            except Exception:
                pass
            mt_state._iface_ref[0] = None


def run_session() -> None:
    delay = config.RECONNECT_INITIAL_DELAY_SEC
    while not mt_state._shutdown.is_set():
        iface: Optional[MeshInterface] = None
        try:
            mt_state.log.log(
                "log", f"connecting Meshtastic ({config.MESH_CONNECTION_MODE})..."
            )
            iface = create_mesh_interface()
            with mt_state._iface_lock:
                mt_state._iface_ref[0] = iface
            mt_state.log.log("log", "Meshtastic connected")
            delay = config.RECONNECT_INITIAL_DELAY_SEC

            while not mt_state._shutdown.is_set():
                time.sleep(1.0)
                rx = getattr(iface, "_rxThread", None)
                if rx is not None and not rx.is_alive():
                    mt_state.log.log("log", "Meshtastic reader thread stopped; reconnecting")
                    break
        except Exception as ex:
            mt_state.log.log("log", f"Meshtastic connection error: {ex}")
        finally:
            with mt_state._iface_lock:
                mt_state._iface_ref[0] = None
            if iface is not None:
                try:
                    iface.close()
                except Exception:
                    pass

        if mt_state._shutdown.is_set():
            break
        time.sleep(delay)
        delay = min(delay * 2, config.RECONNECT_MAX_DELAY_SEC)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
