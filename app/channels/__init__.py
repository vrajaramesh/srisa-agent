"""Messaging channels (WhatsApp, Instagram) behind one ChannelAdapter interface."""

from app.channels.base import ChannelAdapter


def adapter_for(channel: str, firm=None) -> ChannelAdapter:
    """The adapter for a conversation's channel ("whatsapp" | "instagram"); on Instagram, the firm's account."""
    if channel == "instagram":
        from app.channels.instagram import InstagramAdapter
        return InstagramAdapter(firm)
    from app.channels.whatsapp import WhatsAppAdapter
    return WhatsAppAdapter()
