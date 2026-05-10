from __future__ import annotations

import unittest

from easy_asr.model_cache import ModelCache


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CloseableModel:
    def __init__(self, name: str):
        self.name = name
        self.closed = False

    def close(self) -> None:
        self.closed = True


class ModelCacheTests(unittest.TestCase):
    def test_reuses_model_until_idle_ttl_expires(self) -> None:
        clock = FakeClock()
        cache = ModelCache(idle_ttl_seconds=60, sweep_interval_seconds=999, start_sweeper=False, clock=clock)
        loads = 0

        def load_model() -> CloseableModel:
            nonlocal loads
            loads += 1
            return CloseableModel(f"model-{loads}")

        with cache.lease("model", load_model) as first:
            self.assertEqual(first.name, "model-1")

        with cache.lease("model", load_model) as second:
            self.assertIs(first, second)

        self.assertEqual(loads, 1)
        clock.advance(59)
        self.assertEqual(cache.sweep_expired(), [])
        self.assertIn("model", cache.stats())

        clock.advance(1)
        self.assertEqual(cache.sweep_expired(), ["model"])
        self.assertTrue(first.closed)
        self.assertNotIn("model", cache.stats())

        with cache.lease("model", load_model) as third:
            self.assertEqual(third.name, "model-2")
        self.assertEqual(loads, 2)
        cache.close()

    def test_active_model_is_not_evicted(self) -> None:
        clock = FakeClock()
        cache = ModelCache(idle_ttl_seconds=60, sweep_interval_seconds=999, start_sweeper=False, clock=clock)

        with cache.lease("model", lambda: CloseableModel("active")) as model:
            clock.advance(120)
            self.assertEqual(cache.sweep_expired(), [])
            self.assertFalse(model.closed)
            self.assertEqual(cache.stats()["model"]["active_users"], 1)

        self.assertEqual(cache.sweep_expired(), [])
        clock.advance(60)
        self.assertEqual(cache.sweep_expired(), ["model"])
        self.assertTrue(model.closed)

    def test_loader_failure_does_not_leave_broken_entry(self) -> None:
        clock = FakeClock()
        cache = ModelCache(idle_ttl_seconds=60, sweep_interval_seconds=999, start_sweeper=False, clock=clock)

        def fail() -> CloseableModel:
            raise RuntimeError("load failed")

        with self.assertRaises(RuntimeError):
            with cache.lease("model", fail):
                pass

        self.assertEqual(cache.stats(), {})

        with cache.lease("model", lambda: CloseableModel("recovered")) as model:
            self.assertEqual(model.name, "recovered")
        cache.close()


if __name__ == "__main__":
    unittest.main()
