"""Pubsub handler: mesh text -> Telegram; optional auto-reply."""

from typing import Any

import config

from . import mt_state
from . import mt_stats
from .mt_ai_reply import (
    maybe_automated_pong,
    maybe_automated_weather_forecast,
    record_mesh_context_incoming,
    schedule_ai_reply,
)
from .mt_emoji import is_ignored_mesh_noise_packet
from .mt_packets import MeshMessageDetails, packet_to_details
from .mt_telegram import format_telegram_message


def on_incoming_mesh_message(details: MeshMessageDetails, interface: Any) -> None:
    if not config.AUTO_REPLY_ENABLED:
        return
    if interface.myInfo is not None:
        try:
            if details.sender_node_id == int(interface.myInfo.my_node_num):
                return
        except (TypeError, ValueError):
            pass
    try:
        if maybe_automated_pong(details, interface):
            return
        if maybe_automated_weather_forecast(details, interface):
            return
        schedule_ai_reply(details, interface)
    except Exception as ex:
        mt_state.log.log("log", f"mesh auto-reply schedule failed: {ex}")


def on_mesh_text_receive(packet: dict[str, Any], interface: Any) -> None:
    if is_ignored_mesh_noise_packet(packet):
        return
    details = packet_to_details(packet, interface)
    if details is None:
        return
    is_self_message = False
    if interface.myInfo is not None:
        try:
            is_self_message = details.sender_node_id == int(interface.myInfo.my_node_num)
        except (TypeError, ValueError):
            is_self_message = False

    if not is_self_message:
        try:
            record_mesh_context_incoming(details)
        except Exception as ex:
            mt_state.log.log("log", f"mesh context incoming record failed: {ex}")
        try:
            mt_stats.record_received_message(
                sender_node_id=details.sender_node_id,
                message=details.message,
            )
        except Exception as ex:
            mt_state.log.log("log", f"stats record received failed: {ex}")

    try:
        mt_state.notifier.send(
            format_telegram_message(details), config.TELEGRAM_CHAT_ID
        )
    except Exception as ex:
        mt_state.log.log("log", f"telegram forward failed: {ex}")

    try:
        on_incoming_mesh_message(details, interface)
    except Exception as ex:
        mt_state.log.log("log", f"on_incoming_mesh_message error: {ex}")
