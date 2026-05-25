#!/usr/bin/env python3
"""
follower_worker.py - Mic capture + CENS + OLTW glue thread.

Replaces the live-score-sync ``matcher.py`` (pymatchmaker wrapper). The
flow is:

    sounddevice InputStream callback (RT audio thread)
        ↓ pushes raw audio blocks to a queue
    worker thread (this module)
        1. accumulate audio into a rolling buffer
        2. once buffer ≥ ``_context_samples``, call compute_cens()
        3. take the freshest ``frames_per_step`` CENS frames
        4. feed each to OnlineDTWFollower.process_frame
        5. invoke ``on_result`` callback with the alignment result

Why a rolling buffer rather than incremental CENS:
    librosa's chroma_cens applies a smoothing window of ``cens_win``
    frames and a quantisation step that both peek backward in time.
    The cleanest way to get correct CENS in a streaming setting is to
    recompute it over a sufficient look-back window every step. The
    cost is bounded (we trim the buffer) and the CPU is fine at our
    feature rate.

Silence gate integration: the caller polls ``AudioLevelMonitor`` and
calls ``oltw.freeze() / unfreeze()`` on transitions. We do not handle
the gate here; the worker just keeps pushing frames.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Union

import numpy as np

from audio_score_follower.core.feature_extractor import (
    FeatureConfig,
    compute_cens,
)
from audio_score_follower.core.oltw_follower import (
    FollowResult,
    OnlineDTWFollower,
)

logger = logging.getLogger(__name__)


class FollowerWorker:
    """Owns the mic stream and pumps CENS frames into the OLTW."""

    def __init__(
        self,
        oltw_follower: OnlineDTWFollower,
        feature_config: FeatureConfig,
        *,
        mic_device: Optional[Union[int, str]] = None,
        on_result: Optional[Callable[[FollowResult], None]] = None,
        frames_per_step: int = 4,
        loopback: bool = False,
    ) -> None:
        """
        Args:
            oltw_follower: pre-built ``OnlineDTWFollower`` (already loaded
                with the reference CENS).
            feature_config: must match the config used to build the
                reference.
            mic_device: sounddevice device hint (None / int / str).
            on_result: callback invoked once per processed frame on the
                worker thread. Keep work light — the worker is also
                doing CENS + DTW.
            frames_per_step: how many new CENS frames to batch per CENS
                computation. Larger values amortise the librosa overhead
                but increase the worst-case latency. 4 ≈ 380ms at
                hop=2048/sr=22050.
            loopback: if True, open a WASAPI loopback stream on the
                *output* device identified by ``mic_device`` (or the
                system default output when ``mic_device`` is None).
                Windows-only; requires sounddevice with WASAPI support.
        """
        self._oltw = oltw_follower
        self._cfg = feature_config
        self._mic_device = mic_device
        self._loopback = bool(loopback)
        self._on_result = on_result or (lambda _r: None)
        self._frames_per_step = int(frames_per_step)

        # Audio buffer math: we need enough trailing audio for the CENS
        # smoothing window (cens_win frames) + librosa's internal STFT
        # window (4096 by default for chroma_cens) to produce stable
        # frames at the right edge.
        self._context_samples = (
            self._cfg.cens_win * self._cfg.hop_length + 4096
        )
        self._step_samples = self._frames_per_step * self._cfg.hop_length

        self._audio_buffer = np.zeros(0, dtype=np.float32)
        self._audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=128)
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._stream = None
        self._fatal_error: Optional[BaseException] = None
        self._ready_event = threading.Event()
        self._last_result: Optional[FollowResult] = None
        self._last_result_lock = threading.Lock()

        # Diagnostic counters.
        self._frames_processed = 0
        self._underruns = 0

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Spawn the worker thread and open the audio stream."""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            logger.warning("FollowerWorker.start called but already running")
            return

        self._stop_event.clear()
        self._ready_event.clear()
        self._fatal_error = None

        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="oltw-worker", daemon=True
        )
        self._worker_thread.start()

        # sounddevice is lazy-imported so the rest of the app still works
        # on machines without PortAudio (CI etc.). Failure to open the
        # stream is recorded into ``_fatal_error`` but does not raise.
        try:
            import sounddevice as sd  # type: ignore

            if self._loopback:
                # WASAPI loopback: capture the output device's playback
                # stream as an input. Requires Windows + PortAudio with
                # WASAPI support. ``device`` must be an output device
                # index; None resolves to the system default output.
                if not hasattr(sd, "WasapiSettings"):
                    raise RuntimeError(
                        "sd.WasapiSettings が見つかりません。"
                        "Windows + WASAPI 対応の sounddevice が必要です。"
                    )
                device = self._mic_device
                if device is None:
                    device = sd.default.device[1]  # system default output
                self._stream = sd.InputStream(
                    samplerate=self._cfg.sample_rate,
                    channels=2,
                    blocksize=self._step_samples,
                    device=device,
                    dtype="float32",
                    callback=self._audio_callback,
                    extra_settings=sd.WasapiSettings(loopback=True),
                )
                logger.info(
                    "FollowerWorker loopback stream open: sr=%d, blocksize=%d, device=%s",
                    self._cfg.sample_rate, self._step_samples, device,
                )
            else:
                self._stream = sd.InputStream(
                    samplerate=self._cfg.sample_rate,
                    channels=1,
                    blocksize=self._step_samples,
                    device=self._mic_device,
                    dtype="float32",
                    callback=self._audio_callback,
                )
                logger.info(
                    "FollowerWorker stream open: sr=%d, blocksize=%d, device=%s",
                    self._cfg.sample_rate, self._step_samples, self._mic_device,
                )
            self._stream.start()
        except BaseException as exc:
            self._fatal_error = exc
            logger.error(
                "Failed to open %s stream (%s: %s). "
                "Check device=%r in config and that sounddevice/PortAudio "
                "is installed.",
                "loopback" if self._loopback else "mic input",
                type(exc).__name__, exc, self._mic_device,
            )
        finally:
            self._ready_event.set()

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return self._ready_event.wait(timeout)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                logger.exception("Error closing mic stream")
            self._stream = None
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
            if self._worker_thread.is_alive():
                logger.warning(
                    "FollowerWorker thread did not exit within %.1fs", timeout
                )
        self._worker_thread = None
        logger.info(
            "FollowerWorker stopped (frames_processed=%d, underruns=%d)",
            self._frames_processed, self._underruns,
        )

    @property
    def last_error(self) -> Optional[BaseException]:
        return self._fatal_error

    def get_last_result(self) -> Optional[FollowResult]:
        with self._last_result_lock:
            return self._last_result

    # --------------------------------------------------------- audio thread
    def _audio_callback(self, indata, frames, time_info, status) -> None:  # noqa: ARG002
        if status:
            logger.debug("audio status: %s", status)
        if indata.size == 0:
            return
        try:
            self._audio_queue.put_nowait(indata[:, 0].astype(np.float32, copy=True))
        except queue.Full:
            # The worker is falling behind — drop oldest block to keep
            # latency bounded. Logged but not fatal.
            self._underruns += 1
            try:
                self._audio_queue.get_nowait()
                self._audio_queue.put_nowait(
                    indata[:, 0].astype(np.float32, copy=True)
                )
            except queue.Empty:
                pass

    # -------------------------------------------------------- worker thread
    def _worker_loop(self) -> None:
        logger.info(
            "OLTW worker started: context_samples=%d (%.2fs), step_samples=%d "
            "(%.2fs), frames_per_step=%d",
            self._context_samples, self._context_samples / self._cfg.sample_rate,
            self._step_samples, self._step_samples / self._cfg.sample_rate,
            self._frames_per_step,
        )

        max_buffer = self._context_samples + self._step_samples * 4

        while not self._stop_event.is_set():
            try:
                chunk = self._audio_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            self._audio_buffer = np.concatenate([self._audio_buffer, chunk])
            if len(self._audio_buffer) < self._context_samples:
                continue

            ctx = self._audio_buffer[-self._context_samples:]
            try:
                cens = compute_cens(ctx, self._cfg)
            except Exception as exc:  # noqa: BLE001
                logger.exception("CENS compute failed: %s", exc)
                continue

            if cens.shape[1] < self._frames_per_step:
                continue
            new_frames = cens[:, -self._frames_per_step:]

            for j in range(new_frames.shape[1]):
                try:
                    result = self._oltw.process_frame(new_frames[:, j])
                except Exception as exc:  # noqa: BLE001
                    logger.exception("OLTW process_frame failed: %s", exc)
                    continue
                with self._last_result_lock:
                    self._last_result = result
                self._frames_processed += 1
                try:
                    self._on_result(result)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("on_result callback raised: %s", exc)

            if len(self._audio_buffer) > max_buffer:
                self._audio_buffer = self._audio_buffer[-max_buffer:]

        logger.info("OLTW worker exiting")


class FileWorker:
    """Diagnostic worker that feeds an audio file (WAV/MP3/...) through
    the exact same CENS → OLTW pipeline used by ``FollowerWorker``.

    Use this to isolate whether problems are in:
        - the mic-capture path (compare FollowerWorker vs FileWorker
          with the same source content), or
        - the OLTW / feature pipeline (FileWorker with the reference
          recording should track near-perfectly; if it doesn't, the
          bug is upstream of audio I/O).

    Interface matches FollowerWorker so ``main.py`` can swap them
    transparently: ``start``, ``stop``, ``wait_ready``, ``last_error``,
    ``get_last_result``.
    """

    def __init__(
        self,
        oltw_follower: OnlineDTWFollower,
        feature_config: FeatureConfig,
        *,
        input_wav: Path,
        on_result: Optional[Callable[[FollowResult], None]] = None,
        realtime: bool = True,
        start_offset_sec: float = 0.0,
        play_audio: bool = False,
    ) -> None:
        """
        Args:
            oltw_follower: pre-built ``OnlineDTWFollower``.
            feature_config: must match the reference build.
            input_wav: path to an audio file (any librosa-supported
                format — WAV / FLAC / MP3 / M4A).
            on_result: callback per processed frame.
            realtime: if True (default), sleeps ``1/feature_rate``
                between frames to simulate real-time playback. Set to
                False for fast batch debugging.
            start_offset_sec: skip the first N seconds of the input
                (e.g. drop a noisy intro before feeding to OLTW).
            play_audio: if True, play the loaded audio through the
                default output device (via sounddevice) in sync with
                the OLTW frame loop. Only active when realtime=True.
        """
        self._oltw = oltw_follower
        self._cfg = feature_config
        self._input_wav = Path(input_wav)
        self._on_result = on_result or (lambda _r: None)
        self._realtime = bool(realtime)
        self._start_offset_sec = float(start_offset_sec)
        self._play_audio = bool(play_audio)

        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._fatal_error: Optional[BaseException] = None
        self._ready_event = threading.Event()
        self._last_result: Optional[FollowResult] = None
        self._last_result_lock = threading.Lock()
        self._frames_processed = 0

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            logger.warning("FileWorker.start called but already running")
            return
        self._stop_event.clear()
        self._ready_event.clear()
        self._fatal_error = None

        if not self._input_wav.exists():
            self._fatal_error = FileNotFoundError(
                f"--input-wav not found: {self._input_wav}"
            )
            self._ready_event.set()
            logger.error("%s", self._fatal_error)
            return

        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="oltw-file-worker", daemon=True
        )
        self._worker_thread.start()
        logger.info(
            "FileWorker started: input=%s realtime=%s start_offset=%.2fs",
            self._input_wav, self._realtime, self._start_offset_sec,
        )

    def wait_ready(self, timeout: float = 30.0) -> bool:
        return self._ready_event.wait(timeout)

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_event.set()
        if self._play_audio:
            try:
                import sounddevice as sd  # type: ignore
                sd.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
            if self._worker_thread.is_alive():
                logger.warning(
                    "FileWorker thread did not exit within %.1fs", timeout
                )
        self._worker_thread = None
        logger.info("FileWorker stopped (frames_processed=%d)", self._frames_processed)

    @property
    def last_error(self) -> Optional[BaseException]:
        return self._fatal_error

    def get_last_result(self) -> Optional[FollowResult]:
        with self._last_result_lock:
            return self._last_result

    # -------------------------------------------------------- worker thread
    def _worker_loop(self) -> None:
        try:
            import librosa  # type: ignore — heavy

            logger.info("Loading audio: %s", self._input_wav)
            audio, _ = librosa.load(
                str(self._input_wav), sr=self._cfg.sample_rate, mono=True,
            )
            audio = audio.astype(np.float32, copy=False)
            if self._start_offset_sec > 0:
                skip = int(self._start_offset_sec * self._cfg.sample_rate)
                audio = audio[skip:]
            logger.info(
                "Audio loaded: %d samples (%.2fs @ %d Hz)",
                len(audio), len(audio) / self._cfg.sample_rate,
                self._cfg.sample_rate,
            )

            logger.info("Computing CENS for full file …")
            cens = compute_cens(audio, self._cfg)
            n_frames = cens.shape[1]
            interval = 1.0 / self._cfg.effective_frame_rate()
            logger.info(
                "CENS shape: %s, feeding to OLTW at %s rate "
                "(interval=%.3fs/frame, total %d frames ≈ %.1fs)",
                cens.shape,
                "real-time" if self._realtime else "max speed",
                interval, n_frames, n_frames * interval,
            )
        except Exception as exc:  # noqa: BLE001
            self._fatal_error = exc
            self._ready_event.set()
            logger.exception("FileWorker load/CENS failed: %s", exc)
            return

        self._ready_event.set()

        if self._play_audio and self._realtime:
            try:
                import sounddevice as sd  # type: ignore
                sd.play(audio, self._cfg.sample_rate)
                logger.info(
                    "FileWorker: playing audio through default output device "
                    "(sr=%d, %.2fs)",
                    self._cfg.sample_rate, len(audio) / self._cfg.sample_rate,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "FileWorker: sd.play failed (%s: %s); continuing without "
                    "audio playback", type(exc).__name__, exc,
                )

        t_next = time.monotonic()
        for j in range(n_frames):
            if self._stop_event.is_set():
                break
            try:
                result = self._oltw.process_frame(cens[:, j])
            except Exception as exc:  # noqa: BLE001
                logger.exception("OLTW process_frame failed at j=%d: %s", j, exc)
                continue
            with self._last_result_lock:
                self._last_result = result
            self._frames_processed += 1
            try:
                self._on_result(result)
            except Exception as exc:  # noqa: BLE001
                logger.exception("on_result callback raised: %s", exc)

            if self._realtime:
                t_next += interval
                sleep = t_next - time.monotonic()
                if sleep > 0:
                    time.sleep(sleep)
                else:
                    # We fell behind — reset the clock so we don't burst
                    # a flurry of catch-up frames into the OLTW.
                    t_next = time.monotonic()

        logger.info("FileWorker exiting (processed %d frames)", self._frames_processed)
