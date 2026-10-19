"""Messaging channels (WhatsApp, Instagram) behind one ChannelAdapter interface."""

from app.channels.base import ChannelAdapter


def adapter_for(channel: str) -> ChannelAdapter:
    """The adapter for a conversation's channel ("whatsapp" | "instagram")."""
    if channel == "instagram":
        from app.channels.instagram import InstagramAdapter
        return InstagramAdapter()
    from app.channels.whatsapp import WhatsAppAdapter
    return WhatsAppAdapter()
