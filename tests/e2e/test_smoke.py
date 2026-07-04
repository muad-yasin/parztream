"""Browser smoke tests -- one pass over each flow a real user hits, in a
real Chromium. Behavior depth (edge cases, error paths) stays in the unit
suite; these exist to catch the "passes under TestClient, breaks in an
actual browser" class, e.g. HLS output a real decoder rejects.
"""
import re
import shutil

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="requires ffmpeg on PATH"
)

# Segment generation spawns ffmpeg on first request -- slow on a loaded CI
# runner, so playback assertions get a generous ceiling.
PLAYBACK_TIMEOUT_MS = 30_000


def _wait_for_playback(page, selector):
    # currentTime advancing proves actual decode, not just element creation;
    # a playback error would have replaced the element with .player-message.
    page.wait_for_function(
        f"() => {{ const el = document.querySelector('{selector}'); return el && el.currentTime > 0.5; }}",
        timeout=PLAYBACK_TIMEOUT_MS,
    )
    expect(page.locator("#player-container .player-message")).to_have_count(0)


def test_home_grid_shows_the_scanned_movie(page: Page, server):
    page.goto(server.url + "/")
    tile = page.locator("#movies-grid .poster-tile")
    expect(tile).to_have_count(1)
    expect(tile).to_contain_text("Inception (2010)")


def test_search_filters_the_library(page: Page, server):
    page.goto(server.url + "/")
    page.fill("#search-input", "Test Song")

    rows = page.locator("#media-list .row-btn")
    expect(rows).to_have_count(1)
    expect(rows.first).to_contain_text("Test Song")

    page.fill("#search-input", "no such thing anywhere")
    expect(page.locator("#media-list .empty-message")).to_contain_text("No results")

    page.fill("#search-input", "")
    expect(page.locator("#home-view")).to_be_visible()


def test_mkv_plays_via_hls_in_a_real_browser(page: Page, server):
    page.goto(server.url + "/")
    page.locator("#movies-grid .poster-tile", has_text="Inception (2010)").click()

    expect(page.locator("#player-container video")).to_be_visible(
        timeout=PLAYBACK_TIMEOUT_MS
    )
    _wait_for_playback(page, "#player-container video")


def test_audio_direct_plays(page: Page, server):
    page.goto(server.url + "/")
    page.fill("#search-input", "Test Song")
    page.locator("#media-list .row-btn", has_text="Test Song").click()

    _wait_for_playback(page, "#player-container audio")


def test_login_wrong_then_correct_pin(page: Page, pin_server):
    page.goto(pin_server.url + "/")
    expect(page).to_have_url(re.compile(r"/login\.html"))

    # fill() dispatches a single input event with the full value, which is
    # exactly what triggers the page's 4-digit auto-submit.
    page.fill("#pin", "0000")
    expect(page.locator("#error")).to_contain_text("Incorrect PIN")

    page.fill("#pin", "4321")
    expect(page).to_have_url(pin_server.url + "/")
    expect(page.locator("#scan-btn")).to_be_visible()


def test_setup_wizard_first_run(page: Page, unconfigured_server):
    page.goto(unconfigured_server.url + "/")
    expect(page).to_have_url(re.compile(r"/setup\.html$"))

    # The browser starts at $HOME, which the fixture pointed at the tmp
    # media root -- descend into "media" and select it.
    page.locator("#folder-list .row-btn", has_text="media").click()
    expect(page.locator("#current-path")).to_contain_text("media")
    page.click("#add-folder-btn")
    expect(page.locator("#selected-list li")).to_have_count(1)

    page.click("#save-btn")
    expect(page).to_have_url(unconfigured_server.url + "/")

    # Saving kicked off a background scan; the movie tile appearing proves
    # the whole persist -> scan -> render loop, not just the redirect.
    expect(
        page.locator("#movies-grid .poster-tile", has_text="Inception (2010)")
    ).to_be_visible(timeout=PLAYBACK_TIMEOUT_MS)
