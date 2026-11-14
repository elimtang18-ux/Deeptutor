"""Edge TTS adapter — Microsoft Edge's free online speech service.

No API key or account is required: ``edge-tts`` talks to the same public
endpoint the Edge browser uses. Output is always MP3. A proxy can be supplied
through ``EDGE_TTS_PROXY`` (falling back to ``HTTPS_PROXY`` / ``http_proxy``)
for networks that cannot reach the service directly.
"""

from __future__ import annotations

import logging
import os

from deeptutor.services.voice.base import BaseTTSAdapter, VoiceProviderError
from deeptutor.services.voice.config import TTSConfig

logger = logging.getLogger(__name__)

# A natural female English voice; callers can override it per model.
DEFAULT_VOICE = "en-US-AvaNeural"
CONTENT_TYPE = "audio/mpeg"


def _rate_from_speed(speed: float | None) -> str:
    """Map a ``speed`` multiplier onto Edge TTS's ``+N%`` rate string."""
    if speed is None:
        return "+0%"
    percent = max(-50, min(100, round((speed - 1) * 100)))
    return f"{percent:+d}%"


def _proxy_from_env() -> str | None:
    for name in ("EDGE_TTS_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return None


class EdgeTTSAdapter(BaseTTSAdapter):
    """Synthesize through ``edge-tts`` and return the concatenated MP3 stream."""

    async def synthesize(self, text: str, config: TTSConfig) -> tuple[bytes, str]:
        voice = (config.voice or "").strip() or DEFAULT_VOICE
        try:
            import edge_tts
        except ImportError as exc:  # pragma: no cover - dependency install guard
            raise VoiceProviderError(
                "edge-tts is not installed. Install it with `pip install edge-tts`."
            ) from exc

        logger.debug(
            "Edge TTS synthesize voice=%s rate=%s chars=%d",
            voice,
            _rate_from_speed(config.speed),
            len(text),
        )
        audio = bytearray()
        try:
            communicate = edge_tts.Communicate(
                text,
                voice,
                rate=_rate_from_speed(config.speed),
                proxy=_proxy_from_env(),
                connect_timeout=min(10, config.request_timeout),
                receive_timeout=config.request_timeout,
            )
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    audio.extend(chunk["data"])
        except Exception as exc:
            raise VoiceProviderError(
                f"Edge TTS synthesis failed: {exc.__class__.__name__}. "
                "Check the network connection or voice name."
            ) from exc

        if not audio:
            raise VoiceProviderError(
                "Edge TTS returned no audio. Check the voice name and network connection."
            )
        return bytes(audio), CONTENT_TYPE


__all__ = ["EdgeTTSAdapter", "DEFAULT_VOICE"]
