"""Local transcription via faster-whisper on CUDA.

Model is loaded lazily on first use and cached for the lifetime of the process.
Re-use across requests — model load is the expensive part (~2 GB VRAM allocation).

On Windows, ctranslate2 looks up cuBLAS/cuDNN in PATH but NOT inside pip-installed
nvidia-*-cu12 packages. We register those dirs via os.add_dll_directory() before
importing faster_whisper so the DLLs are discoverable without a system CUDA install.
"""
import asyncio
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _register_nvidia_dll_dirs() -> None:
    """Make pip-installed nvidia/{cublas,cudnn}/bin discoverable to ctranslate2.

    On Windows Python 3.8+ only `os.add_dll_directory` affects most loaders, BUT
    ctranslate2's native code uses LoadLibrary with flags that don't consult
    that path. The fix is to prepend the DLL dirs to PATH *before* import.
    """
    if os.name != "nt":
        return
    import site
    bases = list(site.getsitepackages())
    try:
        bases.append(site.getusersitepackages())
    except Exception:
        pass
    found: list[str] = []
    for base in bases:
        for name in ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime"):
            candidate = Path(base) / "nvidia" / name / "bin"
            if candidate.is_dir():
                found.append(str(candidate))
                try:
                    os.add_dll_directory(str(candidate))
                except (OSError, FileNotFoundError):
                    pass
    if found:
        # Prepend to PATH so ctranslate2's native LoadLibrary finds them.
        os.environ["PATH"] = os.pathsep.join(found) + os.pathsep + os.environ.get("PATH", "")
        print(f"[whisper] registered {len(found)} NVIDIA DLL dirs", file=sys.stderr)


_register_nvidia_dll_dirs()

from faster_whisper import WhisperModel


@dataclass
class TranscriptionResult:
    text: str
    language: str
    duration_sec: float
    segments: list[dict]  # [{start, end, text}]


_model_cache: dict[str, WhisperModel] = {}


def _get_model(model_name: str, device: str = "cuda", compute_type: str = "float16") -> WhisperModel:
    key = f"{model_name}:{device}:{compute_type}"
    if key not in _model_cache:
        print(f"[whisper] loading model {model_name} on {device} ({compute_type})", file=sys.stderr)
        try:
            _model_cache[key] = WhisperModel(model_name, device=device, compute_type=compute_type)
        except Exception as e:
            if device == "cuda":
                print(f"[whisper] CUDA failed ({e}) — falling back to CPU int8", file=sys.stderr)
                return _get_model(model_name, device="cpu", compute_type="int8")
            raise
    return _model_cache[key]


def _run_transcribe(model: WhisperModel, audio_path: str, language: str | None) -> TranscriptionResult:
    segments_iter, info = model.transcribe(
        audio_path,
        beam_size=5,
        language=language,             # None = auto-detect
        vad_filter=True,               # skip silence
        vad_parameters={"min_silence_duration_ms": 500},
    )
    segments = [
        {"start": s.start, "end": s.end, "text": s.text.strip()}
        for s in segments_iter
        if s.text.strip()
    ]
    text = " ".join(s["text"] for s in segments)
    return TranscriptionResult(
        text=text, language=info.language,
        duration_sec=info.duration, segments=segments,
    )


def _transcribe_sync(audio_path: str, model_name: str, language: str | None = None) -> TranscriptionResult:
    # Try CUDA first; if a CUDA-specific error bubbles up here (e.g. missing
    # cuBLAS DLL that wasn't caught at model load), transparently downgrade
    # to CPU int8 so the pipeline keeps moving.
    try:
        return _run_transcribe(_get_model(model_name, "cuda", "float16"), audio_path, language)
    except Exception as e:
        msg = str(e).lower()
        if "cublas" in msg or "cudnn" in msg or "cuda" in msg:
            print(f"[whisper] CUDA runtime error ({e}) — falling back to CPU int8", file=sys.stderr)
            return _run_transcribe(_get_model(model_name, "cpu", "int8"), audio_path, language)
        raise


async def transcribe_file(audio_path: str, model_name: str = "medium", language: str | None = None) -> TranscriptionResult:
    """Async wrapper — actual work runs in a thread to avoid blocking the event loop."""
    return await asyncio.to_thread(_transcribe_sync, audio_path, model_name, language)
