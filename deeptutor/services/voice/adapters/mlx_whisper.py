"""In-process MLX Whisper speech-to-text adapter.

mlx-whisper transcribes locally with Apple's MLX backend using weights
already on disk (e.g. downloaded by LM Studio). ``config.base_url`` carries
the local model directory containing ``config.json`` and
``weights.safetensors``; no network is involved. mlx-whisper caches the
loaded model per directory inside the process (``ModelHolder`` in
``mlx_whisper.transcribe``), so the first request pays the load cost and
later requests reuse the weights.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from deeptutor.services.voice.base import BaseSTTAdapter, VoiceProviderError
from deeptutor.services.voice.config import STTConfig

logger = logging.getLogger(__name__)


def _run_transcribe(audio_path: str, config: STTConfig) -> str:
    import mlx_whisper

    language = (config.language or "").strip() or None
    result = mlx_whisper.transcribe(
        audio_path,
        path_or_hf_repo=config.base_url,
        language=language,
        verbose=None,
    )
    text = (result.get("text") or "").strip()
    if not text:
        raise VoiceProviderError("Local Whisper returned an empty transcript.")
    return text


class MLXWhisperSTTAdapter(BaseSTTAdapter):
    """Transcribe audio in-process with mlx-whisper over local MLX weights."""

    async def transcribe(
        self,
        audio: bytes,
        config: STTConfig,
        *,
        filename: str = "audio.webm",
        content_type: str = "application/octet-stream",
    ) -> str:
        model_dir = Path((config.base_url or "").strip())
        if not (model_dir / "config.json").exists() or not (
            model_dir / "weights.safetensors"
        ).exists():
            raise VoiceProviderError(
                "Local Whisper model directory not found. Point the provider's "
                "Base URL at the model folder containing config.json and "
                "weights.safetensors."
            )
        if not audio:
            raise VoiceProviderError("No audio data to transcribe.")

        suffix = os.path.splitext(filename or "audio.webm")[1] or ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio)
            tmp_path = tmp.name
        try:
            logger.debug(
                "MLX Whisper transcribe model_dir=%s audio_bytes=%d",
                model_dir,
                len(audio),
            )
            return await asyncio.to_thread(_run_transcribe, tmp_path, config)
        except VoiceProviderError:
            raise
        except Exception as exc:
            raise VoiceProviderError(f"Local Whisper transcription failed: {exc}") from exc
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


__all__ = ["MLXWhisperSTTAdapter"]