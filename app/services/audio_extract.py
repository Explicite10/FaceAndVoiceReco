import av
import numpy as np

SAMPLE_RATE = 16000


class AudioExtractionError(Exception):
    pass


def extract_audio_16k_mono(path: str) -> np.ndarray:
    """Decodifica el audio de un video a float32 mono 16kHz en [-1, 1].

    16kHz mono es lo que espera Whisper; resamplear aqui evita que
    faster-whisper lance un subproceso de ffmpeg.
    """
    chunks: list[np.ndarray] = []

    try:
        with av.open(path) as container:
            streams = [s for s in container.streams if s.type == "audio"]
            if not streams:
                raise AudioExtractionError("El video no contiene pista de audio.")

            stream = streams[0]
            stream.thread_type = "AUTO"
            resampler = av.audio.resampler.AudioResampler(
                format="s16", layout="mono", rate=SAMPLE_RATE
            )

            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))

            for out in resampler.resample(None):
                chunks.append(out.to_ndarray().reshape(-1))

    except AudioExtractionError:
        raise
    except Exception as exc:
        raise AudioExtractionError(f"No se pudo decodificar el audio: {exc}") from exc

    if not chunks:
        raise AudioExtractionError("La pista de audio esta vacia.")

    return np.concatenate(chunks).astype(np.float32) / 32768.0
