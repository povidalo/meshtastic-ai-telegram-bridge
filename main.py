#!/usr/bin/env python3
"""Forward Meshtastic text traffic to Telegram; optional auto-reply on the radio."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from pubsub import pub

from utils.logger.Logger import Logger
from utils.telegram.bot_notifier import Notifier

import config
from bridge import mt_state
from bridge import mt_weather
from bridge.mt_handler import on_mesh_text_receive
from bridge.mt_packets import load_mesh_packet_origin_cache
from bridge.mt_stats import load_stats_from_disk
from bridge.mt_session import install_signal_handlers, run_session


def main() -> None:
    log = Logger(config.LOGGER_POOL_SIZE, config.LOG_DIR)
    notifier = Notifier(log)
    mt_state.set_log_notifier(log, notifier)
    load_mesh_packet_origin_cache()
    load_stats_from_disk()
    pub.subscribe(on_mesh_text_receive, "meshtastic.receive.text")
    mt_weather.start_background_scheduler()

    install_signal_handlers()

    log.log("log", "meshtastic telegram bridge started")
    try:
        run_session()
    finally:
        notifier.stop()
        log.log("log", "meshtastic telegram bridge stopped")
        log.stop()


if __name__ == "__main__":
    main()
