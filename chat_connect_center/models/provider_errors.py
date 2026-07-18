class ChatConnectProviderError(Exception):
    """Base error raised by provider adapters."""


class ChatConnectTransientError(ChatConnectProviderError):
    """The provider request can be retried safely."""


class ChatConnectPermanentError(ChatConnectProviderError):
    """The request is invalid or cannot succeed without operator action."""


class ChatConnectDeliveryUncertain(ChatConnectProviderError):
    """The provider may have accepted the request; automatic retry is unsafe."""
