#!/usr/bin/env python3
"""mic_effects_probe.py - Detect OS-level audio effects on the capture device.

The follower's feature space (CENS + onset) assumes an UNPROCESSED mic
signal: OS/driver noise suppression (NC) distorts both chroma and attack
envelopes and silently degrades tracking. This module asks Windows which
audio effects (NoiseSuppression / DeepNoiseSuppression / AEC / AGC …)
would be applied to a capture stream on the selected mic, via the WinRT
``Windows.Media.Effects.AudioEffectsManager`` API, so the launcher and
the startup path can warn the operator BEFORE a performance.

Detection only — programmatic disabling was investigated and rejected:
WASAPI exclusive mode conflicts with the second AudioLevelMonitor stream
on the same mic, sounddevice does not expose PortAudio's raw-stream
option, and ``IAudioEffectsManager.SetAudioEffectState`` is per-stream
on an IAudioClient that PortAudio owns internally. The operator turns
NC off in Windows sound settings (``ms-settings:sound``) instead.

Known blind spots (documented, not fixable from software):
- DSP inside the microphone/headset hardware (AirPods, conference USB
  mics) is invisible to every Windows API.
- Third-party virtual mics (NVIDIA Broadcast, Krisp, …) do their
  processing upstream of the endpoint; only a device-name heuristic
  (``suspicious_name``) can flag those.
- Effects are per MediaCategory; all three relevant categories are
  queried and ANY hit is reported (safe side).

Pure logic layer in the launch_options.py sense: imports neither
tkinter nor sounddevice at module level (sounddevice is imported lazily
only to resolve a device index to a name), and the WinRT projection is
imported lazily inside the backend so missing pywinrt packages or a
non-Windows OS degrade to ``probe_available=False`` without noise.
Tests inject a fake backend (tests/test_mic_effects_probe.py).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Effect names (AudioEffectType member names) that mean "the mic signal
# is being denoised" — the condition this project must not run under.
_NOISE_EFFECT_NAMES = {"NOISE_SUPPRESSION", "DEEP_NOISE_SUPPRESSION"}

# Device-name substrings (lowercase) of known software NC processors
# that present themselves as virtual microphones. These process audio
# upstream of the endpoint, so AudioEffectsManager cannot see them.
_SUSPICIOUS_NAME_KEYWORDS = (
    "nvidia broadcast",
    "rtx voice",
    "krisp",
    "steelseries sonar",
)

# Windows deep link the operator uses to actually turn enhancements off.
SOUND_SETTINGS_URI = "ms-settings:sound"


@dataclass
class MicEffectsReport:
    """Result of probing the capture endpoint for OS-level audio effects."""

    # False when WinRT is unusable here (pywinrt missing / non-Windows /
    # API error) — callers must stay silent, not warn.
    probe_available: bool
    # True when the sounddevice-selected mic was matched to a WinRT
    # capture endpoint. False = could not identify → "確認できません".
    device_matched: bool
    # Name of the device as sounddevice reports it (None = OS default).
    device_name: Optional[str] = None
    # Friendly name of the matched WinRT endpoint.
    matched_winrt_name: Optional[str] = None
    # MediaCategory name → tuple of AudioEffectType names active there.
    effects_by_category: dict = field(default_factory=dict)
    # Device name matched a known software-NC virtual mic.
    suspicious_name: bool = False
    error: Optional[str] = None

    @property
    def has_noise_suppression(self) -> bool:
        return any(
            name in _NOISE_EFFECT_NAMES
            for names in self.effects_by_category.values()
            for name in names
        )

    @property
    def detected_effects(self) -> list[str]:
        """Unique effect names across all categories, sorted."""
        return sorted({
            name
            for names in self.effects_by_category.values()
            for name in names
        })

    def headline_ja(self) -> str:
        """One-line operator-facing summary (GUI labels / logs)."""
        if self.suspicious_name:
            return (
                f"⚠ ノイズ抑制ソフトの仮想マイクの可能性があります"
                f"（{self.device_name}）— 物理マイクを直接選択してください"
            )
        if not self.probe_available:
            return (
                "この環境ではフィルターを確認できません — "
                "Windows のサウンド設定で「オーディオ拡張機能」を手動確認してください"
            )
        if not self.device_matched:
            return (
                "マイクデバイスを特定できず確認できません — "
                "Windows のサウンド設定で「オーディオ拡張機能」を手動確認してください"
            )
        if self.has_noise_suppression:
            cats = sorted(
                cat
                for cat, names in self.effects_by_category.items()
                if any(n in _NOISE_EFFECT_NAMES for n in names)
            )
            return (
                f"⚠ ノイズ抑制フィルターが有効です"
                f"（カテゴリ: {', '.join(cats)}）— "
                f"サウンド設定で「オーディオ拡張機能」をオフにしてください"
            )
        others = self.detected_effects
        if others:
            return (
                f"✓ ノイズ抑制は検出されませんでした"
                f"（他の効果: {', '.join(others)}）"
                f"※マイク内蔵の NC は検出できません"
            )
        return (
            "✓ ノイズ抑制フィルターは検出されませんでした"
            "（※マイク内蔵の NC は検出できません）"
        )


def is_suspicious_device_name(name: Optional[str]) -> bool:
    """True when the device name matches a known software-NC virtual mic."""
    if not name:
        return False
    lowered = name.lower()
    return any(k in lowered for k in _SUSPICIOUS_NAME_KEYWORDS)


def match_capture_device(
    sd_name: str, winrt_devices: list[tuple[str, str]]
) -> Optional[tuple[str, str]]:
    """Match a sounddevice device name to a WinRT (id, name) endpoint.

    Exact match wins. Otherwise prefix in either direction: PortAudio's
    WASAPI host API reports the full friendly name (== WinRT name) but
    MME/DirectSound truncate it to ~31 chars, so the sd name may be a
    prefix of the WinRT name.
    """
    for dev_id, name in winrt_devices:
        if name == sd_name:
            return dev_id, name
    for dev_id, name in winrt_devices:
        if name.startswith(sd_name) or sd_name.startswith(name):
            return dev_id, name
    return None


# ------------------------------------------------------------------ backend

def _await_winrt(operation):
    """Synchronously wait for a WinRT IAsyncOperation.

    Callers are Tk handlers / startup code with no running event loop,
    so asyncio.run is safe here.
    """
    async def _wrap():
        return await operation
    return asyncio.run(_wrap())


class _WinRTBackend:
    """Thin wrapper over the pywinrt projection (swapped out in tests).

    Raises ImportError/OSError from __init__ when the projection is
    unavailable — probe_capture_effects turns that into
    ``probe_available=False``.
    """

    def __init__(self) -> None:
        from winrt.windows.devices.enumeration import (  # noqa: PLC0415
            DeviceClass, DeviceInformation,
        )
        from winrt.windows.media.capture import MediaCategory  # noqa: PLC0415
        from winrt.windows.media.devices import (  # noqa: PLC0415
            AudioDeviceRole, MediaDevice,
        )
        from winrt.windows.media.effects import (  # noqa: PLC0415
            AudioEffectsManager,
        )
        self._DeviceClass = DeviceClass
        self._DeviceInformation = DeviceInformation
        self._MediaCategory = MediaCategory
        self._MediaDevice = MediaDevice
        self._AudioDeviceRole = AudioDeviceRole
        self._AudioEffectsManager = AudioEffectsManager

    def list_capture_devices(self) -> list[tuple[str, str]]:
        """All capture endpoints as (winrt_device_id, friendly_name)."""
        found = _await_winrt(
            self._DeviceInformation.find_all_async_device_class(
                self._DeviceClass.AUDIO_CAPTURE
            )
        )
        return [(d.id, d.name) for d in found]

    def default_capture_id(self) -> Optional[str]:
        """WinRT id of the OS-default capture endpoint (None if none)."""
        return self._MediaDevice.get_default_audio_capture_id(
            self._AudioDeviceRole.DEFAULT
        )

    def capture_effects(self, device_id: str) -> dict:
        """MediaCategory name → tuple of effect names on that endpoint.

        All categories a shared-mode stream could be classified under
        are queried; a category that errors is reported as an empty
        tuple (the other categories still count).
        """
        categories = (
            ("Other", self._MediaCategory.OTHER),
            ("Media", self._MediaCategory.MEDIA),
            ("Communications", self._MediaCategory.COMMUNICATIONS),
        )
        out = {}
        for cat_name, cat in categories:
            try:
                mgr = self._AudioEffectsManager.create_audio_capture_effects_manager(
                    device_id, cat
                )
                effects = mgr.get_audio_capture_effects()
                out[cat_name] = tuple(
                    _effect_type_name(e.audio_effect_type) for e in effects
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "AudioEffectsManager failed for category %s: %s",
                    cat_name, exc,
                )
                out[cat_name] = ()
        return out


def _effect_type_name(effect_type) -> str:
    """AudioEffectType enum → its member name (int fallback)."""
    name = getattr(effect_type, "name", None)
    return name if name else f"EFFECT_{int(effect_type)}"


def _resolve_device_name(device) -> Optional[str]:
    """Resolve a sounddevice device spec (index/name/None) to a name.

    None (OS default) and resolution failures both return None — the
    probe then falls back to the WinRT default endpoint.
    """
    if device is None:
        return None
    if isinstance(device, str):
        return device
    try:
        import sounddevice as sd  # noqa: PLC0415

        return sd.query_devices(device, "input")["name"]
    except BaseException as exc:  # noqa: BLE001
        logger.warning("Device %r name resolution failed: %s", device, exc)
        return None


# -------------------------------------------------------------------- probe

def probe_capture_effects(
    device=None,
    *,
    backend_factory: Callable[[], object] = _WinRTBackend,
    name_resolver: Callable[[object], Optional[str]] = _resolve_device_name,
) -> MicEffectsReport:
    """Probe the selected mic for OS-level capture effects.

    ``device`` is a sounddevice device spec (int index, name string, or
    None for the OS default). ``backend_factory`` / ``name_resolver``
    exist for headless tests. Never raises.
    """
    device_name = name_resolver(device)
    suspicious = is_suspicious_device_name(device_name)

    try:
        backend = backend_factory()
    except BaseException as exc:  # noqa: BLE001
        logger.info("Mic effects probe unavailable: %s", exc)
        return MicEffectsReport(
            probe_available=False,
            device_matched=False,
            device_name=device_name,
            suspicious_name=suspicious,
            error=str(exc),
        )

    try:
        devices = backend.list_capture_devices()
        if device_name is None:
            default_id = backend.default_capture_id()
            matched = next(
                ((i, n) for i, n in devices if i == default_id), None
            )
        else:
            matched = match_capture_device(device_name, devices)
        if matched is None:
            return MicEffectsReport(
                probe_available=True,
                device_matched=False,
                device_name=device_name,
                suspicious_name=suspicious,
            )
        dev_id, winrt_name = matched
        effects = backend.capture_effects(dev_id)
        return MicEffectsReport(
            probe_available=True,
            device_matched=True,
            device_name=device_name,
            matched_winrt_name=winrt_name,
            effects_by_category=effects,
            suspicious_name=suspicious,
        )
    except BaseException as exc:  # noqa: BLE001
        logger.warning("Mic effects probe failed: %s", exc)
        return MicEffectsReport(
            probe_available=False,
            device_matched=False,
            device_name=device_name,
            suspicious_name=suspicious,
            error=str(exc),
        )


def main(argv: Optional[list[str]] = None) -> int:
    """CLI diagnostic: ``python -m audio_score_follower.core.mic_effects_probe [device]``."""
    import argparse
    import sys

    # Windows terminals commonly default to cp932, which cannot encode
    # the ✓/⚠ glyphs used in headline_ja(); degrade gracefully instead
    # of crashing the diagnostic CLI.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="マイクの OS 側オーディオ効果（ノイズ抑制等）を確認する"
    )
    parser.add_argument(
        "device", nargs="?", default=None,
        help="sounddevice のデバイス番号または名前（省略時: OS 既定マイク）",
    )
    args = parser.parse_args(argv)
    device = args.device
    if device is not None:
        try:
            device = int(device)
        except ValueError:
            pass
    report = probe_capture_effects(device)
    print(f"device_name        : {report.device_name or '(OS 既定)'}")
    print(f"matched_winrt_name : {report.matched_winrt_name}")
    for cat, names in report.effects_by_category.items():
        print(f"  effects[{cat}]: {list(names) or 'なし'}")
    print(report.headline_ja())
    return 1 if report.has_noise_suppression or report.suspicious_name else 0


if __name__ == "__main__":
    raise SystemExit(main())
