from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from easy_asr.capture import CaptureRecord, PlaybackCaptureManager
from easy_asr.engines.base import EngineOptions


class DummyJobManager:
    def submit(self, *args, **kwargs):
        raise AssertionError("short empty capture should not submit live chunks")

    def get_job(self, job_id: str):
        return None


class FakeStream:
    def __init__(self, callback):
        self.callback = callback
        self.active = True
        self.closed = False

    def is_active(self) -> bool:
        return self.active

    def start_stream(self) -> None:
        self.active = True

    def close(self) -> None:
        self.closed = True
        self.active = False

    def read(self, *args, **kwargs):
        raise AssertionError("capture should use callback mode, not blocking read")


class FakeAudio:
    def __init__(self):
        self.stream: FakeStream | None = None
        self.terminated = False

    def get_default_wasapi_loopback(self) -> dict:
        return {
            "index": 0,
            "name": "Fake Loopback",
            "defaultSampleRate": 48000,
            "maxInputChannels": 2,
            "isLoopbackDevice": True,
        }

    def get_sample_size(self, data_format) -> int:
        return 2

    def open(self, **kwargs) -> FakeStream:
        self.stream = FakeStream(kwargs["stream_callback"])
        return self.stream

    def terminate(self) -> None:
        self.terminated = True


class FakePyAudioModule:
    paInt16 = 8
    paContinue = 0
    paAbort = 2

    def PyAudio(self) -> FakeAudio:
        return FakeAudio()


class CaptureStopTests(unittest.TestCase):
    def test_stop_does_not_depend_on_blocking_stream_read(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = PlaybackCaptureManager(Path(temp_dir), DummyJobManager())
            manager.ensure_dirs()

            with patch("easy_asr.capture._load_pyaudio", return_value=FakePyAudioModule()):
                record = manager.start(EngineOptions(), {"json"})
                self._wait_for_status(manager, record.id, "recording")

                started_at = time.monotonic()
                stopped = manager.stop(record.id)

            self.assertLess(time.monotonic() - started_at, 2.0)
            self.assertEqual(stopped.status, "completed")
            self.assertFalse(manager._threads[record.id].is_alive())

    def test_stop_timeout_marks_session_failed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = PlaybackCaptureManager(Path(temp_dir), DummyJobManager())
            manager.ensure_dirs()
            record = CaptureRecord(
                id="stuck",
                status="recording",
                created_at="2026-05-09T00:00:00+00:00",
                updated_at="2026-05-09T00:00:00+00:00",
                wav_path=Path(temp_dir) / "stuck.wav",
            )
            manager._sessions[record.id] = record
            manager._stop_events[record.id] = threading.Event()
            manager._threads[record.id] = StuckThread()

            with self.assertRaises(TimeoutError):
                manager.stop(record.id)

            self.assertEqual(record.status, "failed")
            self.assertIn("停止采集超时", record.error)

    def _wait_for_status(self, manager: PlaybackCaptureManager, session_id: str, status: str) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            record = manager.get_session(session_id)
            if record is not None and record.status == status:
                return
            time.sleep(0.01)
        current = manager.get_session(session_id)
        self.fail(f"capture did not reach {status}; current={current.status if current else None}")


class StuckThread:
    def join(self, timeout: float | None = None) -> None:
        return None

    def is_alive(self) -> bool:
        return True


if __name__ == "__main__":
    unittest.main()
