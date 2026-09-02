"""Unit tests for watcher.py's generic JobRunner/WatchSpec framework.

These tests exercise JobRunner in isolation with fake `export` callables (no real kicad-cli or
CadQuery subprocess calls), so they run fast and without any of the heavy dependencies baked
into the container image. They cover:

- Debounce coalescing: a burst of triggers within the debounce window results in exactly one
  export run.
- No-concurrent-runs + eventual-consistency rerun: a trigger that arrives while an export is
  already running for the same path does not start a second concurrent run, but does guarantee
  exactly one more run happens immediately after the current one finishes.
- .error.txt lifecycle: written on export failure, cleared on the next successful export.
- WatchSpec matching helpers: is_kicad_file, is_model_file (including .cqsignore support).

Run with: pytest tests/test_watcher.py
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import watcher  # noqa: E402


@pytest.fixture
def model_file(tmp_path):
    path = tmp_path / "fake.kicad_sch"
    path.write_text("(kicad_sch)")
    return str(path)


def make_spec(name, export_fn):
    return watcher.WatchSpec(
        name=name,
        matches=lambda p: True,
        export=export_fn,
        error_path=watcher.error_path_for,
        outputs_missing=lambda p: True,
    )


def test_debounce_coalesces_burst_into_single_run(model_file):
    calls = []

    def fake_export(path):
        calls.append(path)

    spec = make_spec("test", fake_export)
    runner = watcher.JobRunner(delay=0.1)

    for _ in range(5):
        runner.trigger(spec, model_file)
        time.sleep(0.02)  # well within the debounce window

    time.sleep(0.4)  # let the single debounced run fire
    assert calls == [os.path.abspath(model_file)]


def test_concurrent_trigger_defers_to_single_guaranteed_rerun(model_file):
    calls = []

    def slow_export(path):
        calls.append(time.time())
        time.sleep(0.3)

    spec = make_spec("slow", slow_export)
    runner = watcher.JobRunner(delay=0.05)

    runner.trigger(spec, model_file)
    time.sleep(0.15)  # let the first run start (it sleeps for 0.3s)
    runner.trigger(spec, model_file)  # should NOT start a second concurrent run

    time.sleep(1.0)  # long enough for: first run to finish + guaranteed rerun to complete

    assert len(calls) == 2, "expected exactly one rerun after the in-flight run finished"
    assert calls[1] - calls[0] >= 0.3, "rerun should not start until the first run finished"


def test_export_failure_writes_error_file_and_success_clears_it(model_file):
    should_fail = {"value": True}

    def flaky_export(path):
        if should_fail["value"]:
            raise RuntimeError("boom: something went wrong")

    spec = make_spec("flaky", flaky_export)
    runner = watcher.JobRunner(delay=0.05)
    error_path = watcher.error_path_for(model_file)

    runner.trigger(spec, model_file)
    time.sleep(0.3)
    assert os.path.exists(error_path)
    assert "boom: something went wrong" in open(error_path).read()

    should_fail["value"] = False
    runner.trigger(spec, model_file)
    time.sleep(0.3)
    assert not os.path.exists(error_path)


def test_run_skipped_if_file_removed_before_debounce_fires(tmp_path):
    calls = []
    path = str(tmp_path / "vanishing.kicad_sch")
    open(path, "w").write("x")

    spec = make_spec("vanish", lambda p: calls.append(p))
    runner = watcher.JobRunner(delay=0.05)

    runner.trigger(spec, path)
    os.remove(path)
    time.sleep(0.3)

    assert calls == []


def test_is_kicad_file():
    assert watcher.is_kicad_file("/app/board.kicad_pcb")
    assert watcher.is_kicad_file("/app/schematic.kicad_sch")
    assert not watcher.is_kicad_file("/app/models/box.py")
    assert not watcher.is_kicad_file("/app/readme.md")


def test_is_model_file_scopes_to_flat_models_dir(tmp_path, monkeypatch):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "box.py").write_text("# model")
    (models_dir / "_helper.py").write_text("# helper, should be excluded")
    (models_dir / ".hidden.py").write_text("# dotfile, should be excluded")
    subdir = models_dir / "sub"
    subdir.mkdir()
    (subdir / "nested.py").write_text("# nested, non-recursive scan should exclude this")

    monkeypatch.setattr(watcher, "CQ_MODELS_DIR", str(models_dir))

    assert watcher.is_model_file(str(models_dir / "box.py"))
    assert not watcher.is_model_file(str(models_dir / "_helper.py"))
    assert not watcher.is_model_file(str(models_dir / ".hidden.py"))
    assert not watcher.is_model_file(str(subdir / "nested.py"))
    assert not watcher.is_model_file(str(models_dir / "box.txt"))


def test_is_model_file_respects_cqsignore(tmp_path, monkeypatch):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "box.py").write_text("# model")
    (models_dir / "ignored.py").write_text("# should be ignored")
    (models_dir / ".cqsignore").write_text("ignored.py\n# a comment\n")

    monkeypatch.setattr(watcher, "CQ_MODELS_DIR", str(models_dir))

    assert watcher.is_model_file(str(models_dir / "box.py"))
    assert not watcher.is_model_file(str(models_dir / "ignored.py"))


def test_watch_specs_are_mutually_exclusive_by_extension(tmp_path, monkeypatch):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    monkeypatch.setattr(watcher, "CQ_MODELS_DIR", str(models_dir))

    kicad_path = str(tmp_path / "board.kicad_pcb")
    model_path = str(models_dir / "box.py")

    assert watcher.KICAD_SPEC.matches(kicad_path)
    assert not watcher.MODEL_SPEC.matches(kicad_path)
    assert watcher.MODEL_SPEC.matches(model_path)
    assert not watcher.KICAD_SPEC.matches(model_path)
