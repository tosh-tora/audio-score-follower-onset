"""Tests for audio_score_follower.core.mic_effects_probe (no real WinRT/sounddevice)."""

from audio_score_follower.core.mic_effects_probe import (
    MicEffectsReport,
    is_suspicious_device_name,
    match_capture_device,
    probe_capture_effects,
)


# ---------------------------------------------------------------- fixtures
class _FakeBackend:
    """Stand-in for _WinRTBackend with a scripted device/effects table."""

    def __init__(self, devices, effects_by_id, default_id=None):
        self._devices = devices  # list[(id, name)]
        self._effects_by_id = effects_by_id  # id -> {category: (effect,...)}
        self._default_id = default_id

    def list_capture_devices(self):
        return self._devices

    def default_capture_id(self):
        return self._default_id

    def capture_effects(self, device_id):
        return self._effects_by_id.get(device_id, {})


def _factory(backend):
    return lambda: backend


# --------------------------------------------------------------- name match
def test_match_capture_device_exact():
    devices = [("id1", "マイク配列 (Realtek(R) Audio)"), ("id2", "Other Mic")]
    assert match_capture_device("マイク配列 (Realtek(R) Audio)", devices) == devices[0]


def test_match_capture_device_prefix_fallback():
    # MME/DirectSound truncate long friendly names; sd name is a prefix.
    devices = [("id1", "マイク配列 (Realtek(R) Audio) Full Name")]
    assert match_capture_device("マイク配列 (Realtek(R", devices) == devices[0]


def test_match_capture_device_no_match():
    devices = [("id1", "Completely Different")]
    assert match_capture_device("マイク配列", devices) is None


def test_is_suspicious_device_name():
    assert is_suspicious_device_name("NVIDIA Broadcast (Virtual)")
    assert is_suspicious_device_name("Krisp Microphone")
    assert not is_suspicious_device_name("マイク配列 (Realtek(R) Audio)")
    assert not is_suspicious_device_name(None)


# --------------------------------------------------------------- probe: OK path
def test_probe_detects_noise_suppression():
    devices = [("id1", "USB Mic")]
    effects = {"id1": {"Communications": ("NOISE_SUPPRESSION",), "Media": (), "Other": ()}}
    report = probe_capture_effects(
        "USB Mic",
        backend_factory=_factory(_FakeBackend(devices, effects)),
        name_resolver=lambda d: d,
    )
    assert report.probe_available
    assert report.device_matched
    assert report.has_noise_suppression
    assert "NOISE_SUPPRESSION" in report.detected_effects
    assert "⚠" in report.headline_ja()
    assert "Communications" in report.headline_ja()


def test_probe_clean_device():
    devices = [("id1", "USB Mic")]
    effects = {"id1": {"Communications": (), "Media": (), "Other": ()}}
    report = probe_capture_effects(
        "USB Mic",
        backend_factory=_factory(_FakeBackend(devices, effects)),
        name_resolver=lambda d: d,
    )
    assert report.probe_available
    assert report.device_matched
    assert not report.has_noise_suppression
    assert "✓" in report.headline_ja()


def test_probe_other_effects_reported_but_not_flagged():
    devices = [("id1", "USB Mic")]
    effects = {"id1": {"Communications": ("AUTOMATIC_GAIN_CONTROL",), "Media": (), "Other": ()}}
    report = probe_capture_effects(
        "USB Mic",
        backend_factory=_factory(_FakeBackend(devices, effects)),
        name_resolver=lambda d: d,
    )
    assert not report.has_noise_suppression
    assert report.detected_effects == ["AUTOMATIC_GAIN_CONTROL"]
    assert "✓" in report.headline_ja()


def test_probe_default_device_uses_default_capture_id():
    devices = [("id1", "Mic A"), ("id2", "Mic B")]
    effects = {"id2": {"Communications": ("DEEP_NOISE_SUPPRESSION",), "Media": (), "Other": ()}}
    report = probe_capture_effects(
        None,
        backend_factory=_factory(_FakeBackend(devices, effects, default_id="id2")),
        name_resolver=lambda d: None,
    )
    assert report.device_matched
    assert report.matched_winrt_name == "Mic B"
    assert report.has_noise_suppression


# --------------------------------------------------------- probe: degrade paths
def test_probe_unavailable_when_backend_construction_fails():
    def _boom():
        raise ImportError("no winrt")

    report = probe_capture_effects(
        "USB Mic", backend_factory=_boom, name_resolver=lambda d: d
    )
    assert not report.probe_available
    assert not report.device_matched
    assert report.error is not None
    assert "確認できません" in report.headline_ja()


def test_probe_unmatched_device():
    devices = [("id1", "Completely Different Device")]
    report = probe_capture_effects(
        "USB Mic",
        backend_factory=_factory(_FakeBackend(devices, {})),
        name_resolver=lambda d: d,
    )
    assert report.probe_available
    assert not report.device_matched
    assert "確認できません" in report.headline_ja()


def test_probe_backend_runtime_error_degrades():
    class _ExplodingBackend:
        def list_capture_devices(self):
            raise OSError("WinRT call failed")

    report = probe_capture_effects(
        "USB Mic",
        backend_factory=_factory(_ExplodingBackend()),
        name_resolver=lambda d: d,
    )
    assert not report.probe_available
    assert report.error is not None


def test_suspicious_name_overrides_headline_even_when_available():
    devices = [("id1", "NVIDIA Broadcast Microphone")]
    effects = {"id1": {"Communications": (), "Media": (), "Other": ()}}
    report = probe_capture_effects(
        "NVIDIA Broadcast Microphone",
        backend_factory=_factory(_FakeBackend(devices, effects)),
        name_resolver=lambda d: d,
    )
    assert report.suspicious_name
    assert "仮想マイク" in report.headline_ja()


# --------------------------------------------------------------------- report
def test_report_detected_effects_dedup_and_sorted():
    report = MicEffectsReport(
        probe_available=True,
        device_matched=True,
        effects_by_category={
            "Media": ("NOISE_SUPPRESSION", "AUTOMATIC_GAIN_CONTROL"),
            "Communications": ("NOISE_SUPPRESSION",),
        },
    )
    assert report.detected_effects == ["AUTOMATIC_GAIN_CONTROL", "NOISE_SUPPRESSION"]
