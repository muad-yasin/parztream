"""Real-browser end-to-end fixtures: a genuine uvicorn subprocess plus
Playwright Chromium, not TestClient.

Deliberately does NOT reuse tests/conftest.py's in-process monkeypatching --
the server here is a separate process, so configuration goes through real
environment variables (the one place env vars *do* work for tests, since the
subprocess imports app.config fresh).

Excluded from the plain `pytest` run via pytest.ini; run explicitly with:

    pytest tests/e2e -o addopts=""
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SERVER_START_TIMEOUT = 30
SCAN_TIMEOUT = 60


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    # The playback tests assert currentTime actually advances; headless
    # Chromium's autoplay policy can otherwise block audible playback even
    # though a real click preceded it.
    args = browser_type_launch_args.get("args", [])
    return {
        **browser_type_launch_args,
        "args": [*args, "--autoplay-policy=no-user-gesture-required"],
    }


def _h264_encoder():
    """Whichever H.264 encoder this ffmpeg ships (GPL builds have libx264,
    parztream's vendored LGPL builds have libopenh264). Duplicated from
    tests/conftest.py on purpose -- this conftest deliberately shares
    nothing with the in-process unit-test fixtures."""
    listed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
    ).stdout
    encoder = next((e for e in ("libx264", "libopenh264") if e in listed), None)
    if encoder is None:
        pytest.skip("this ffmpeg build has no H.264 encoder (libx264/libopenh264)")
    return encoder


@pytest.fixture(scope="session")
def media_root(tmp_path_factory):
    """A tiny real media library, synthesized once per session with ffmpeg.

    Layout matters: the mkv sits alone in its own folder so the scanner's
    movie-folder heuristic titles it "Inception (2010)" and flags it
    is_movie, which is what puts it on the home screen's Movies grid.
    """
    encoder = _h264_encoder()
    root = tmp_path_factory.mktemp("e2e-media")
    movie_dir = root / "media" / "Inception (2010)"
    movie_dir.mkdir(parents=True)
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=blue:size=320x240:duration=8",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
            # Force a keyframe every second: a static synthetic clip has no
            # scene cuts, so the encoder would otherwise emit a single
            # keyframe at t=0 -- and app/transcode.py's segment muxer can
            # only split at keyframes, so segment 0 would swallow the whole
            # clip and the on-demand `-ss 6` job for segment 1 would seek
            # back to that lone keyframe and produce a byte-identical
            # duplicate, which a real browser rejects with a decode error.
            # Real video has regular keyframes; without this the fixture
            # doesn't. (-force_key_frames rather than -g/-sc_threshold
            # because it works identically for libx264 and libopenh264.)
            "-c:v", encoder, "-force_key_frames", "expr:gte(t,n_forced)",
            "-c:a", "aac", "-shortest",
            str(movie_dir / "Inception.2010.1080p.BluRay.x264-GROUP.mkv"),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=330:duration=2",
            str(root / "media" / "Test Song.mp3"),
        ],
        check=True,
    )
    return root


class ServerProcess:
    def __init__(self, url, process):
        self.url = url
        self.process = process


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(state_dir, media_dirs=None, pin=None, home=None):
    port = _free_port()
    env = os.environ.copy()
    # Ambient config (e.g. a PIN exported in the developer's shell) must not
    # leak into the server under test.
    for key in list(env):
        if key.startswith("PARZTREAM_"):
            del env[key]
    env["PARZTREAM_DB_PATH"] = str(state_dir / "e2e.db")
    env["PARZTREAM_CACHE_DIR"] = str(state_dir / "cache")
    env["PARZTREAM_MDNS_ENABLED"] = "false"
    if media_dirs:
        env["PARZTREAM_MEDIA_DIRS"] = os.pathsep.join(str(d) for d in media_dirs)
    if pin:
        env["PARZTREAM_PIN"] = pin
    if home:
        # /api/setup/browse starts at Path.home() -- pointing HOME at the
        # tmp media root is what makes the browse-only setup wizard
        # navigable to the test media (and keeps the test out of the real
        # home directory). USERPROFILE is Path.home()'s Windows source.
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)

    process = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + SERVER_START_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read().decode(errors="replace")
            raise RuntimeError(f"server exited during startup:\n{output}")
        try:
            httpx.get(f"{url}/api/setup/status", timeout=1)
            return ServerProcess(url, process)
        except httpx.TransportError:
            time.sleep(0.2)
    process.terminate()
    raise RuntimeError(f"server not accepting connections after {SERVER_START_TIMEOUT}s")


def _stop_server(server):
    server.process.terminate()
    try:
        server.process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.process.kill()
        server.process.wait()


def _scan_and_wait(url):
    httpx.post(f"{url}/api/scan", timeout=10).raise_for_status()
    deadline = time.monotonic() + SCAN_TIMEOUT
    while time.monotonic() < deadline:
        status = httpx.get(f"{url}/api/scan/status", timeout=10).json()
        if status["status"] != "scanning":
            assert status["status"] != "error", f"library scan failed: {status}"
            return
        time.sleep(0.5)
    raise RuntimeError(f"library scan still running after {SCAN_TIMEOUT}s")


@pytest.fixture(scope="session")
def server(tmp_path_factory, media_root):
    """Configured, no PIN, library already scanned."""
    srv = _start_server(
        tmp_path_factory.mktemp("e2e-server"),
        media_dirs=[media_root / "media"],
    )
    try:
        _scan_and_wait(srv.url)
        yield srv
    finally:
        _stop_server(srv)


@pytest.fixture(scope="session")
def pin_server(tmp_path_factory, media_root):
    """Configured and PIN-gated (PIN 4321). Never scanned -- the login flow
    doesn't need library contents."""
    srv = _start_server(
        tmp_path_factory.mktemp("e2e-pin-server"),
        media_dirs=[media_root / "media"],
        pin="4321",
    )
    try:
        yield srv
    finally:
        _stop_server(srv)


@pytest.fixture()
def unconfigured_server(tmp_path_factory, media_root):
    """Fresh DB, no media dirs -- the first-run state that lands on
    /setup.html. Function-scoped: completing the wizard configures it."""
    srv = _start_server(tmp_path_factory.mktemp("e2e-unconfigured"), home=media_root)
    try:
        yield srv
    finally:
        _stop_server(srv)
