"""browser_e2e mobile layout: the sidebar collapse is awaited, real defects still fail.

The page below reproduces the Streamlit 1.60 sidebar the gate measures: the ``stSidebar``
section collapses through a CSS transform transition while it overlays the content on
narrow screens. Its ``aria-expanded`` attribute is not authoritative: in the live run it
stayed ``true`` although the sidebar was off-screen, so collapse is judged by geometry and
animation state only. Tests run in the same headless Chromium the gate uses and are
skipped when it is not installed.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from scripts import browser_e2e_gate as gate

MOBILE = {"width": 390, "height": 844}
PAGE = """
<!doctype html><html><head><style>
  html, body {{ margin: 0; }}
  [data-testid=stSidebar] {{
    position: fixed; top: 0; left: 0; width: 300px; height: 100vh; background: #eee;
    z-index: 10; transition: transform {duration}ms linear, max-width {duration}ms linear;
    max-width: 300px;
  }}
  [data-testid=stSidebar][aria-expanded=false],
  [data-testid=stSidebar].collapsed {{ transform: translateX(-300px); max-width: 0; }}
  [data-testid=stSidebar].partial {{ transform: translateX(-200px); max-width: 300px; }}
  [data-testid=stSidebar].pulse {{ animation: pulse 400ms linear infinite; }}
  @keyframes pulse {{ from {{ opacity: 1; }} to {{ opacity: 0.9; }} }}
  [data-testid=stMainBlockContainer] {{ padding: 16px; }}
  [data-testid=stChatInputTextArea] {{ display: block; width: 300px; height: 40px; }}
  .wide {{ width: 900px; height: 10px; background: red; }}
  .toast {{ position: fixed; left: 0; top: 30px; width: 390px; height: 200px; z-index: 20; }}
</style></head><body>
  <section data-testid="stSidebar" aria-expanded="true">
    <button data-testid="stSidebarCollapseButton">collapse</button>
  </section>
  <div data-testid="stMainBlockContainer">
    <textarea data-testid="stChatInputTextArea"></textarea>
    {extra}
  </div>
  <script>
    const bar = document.querySelector('[data-testid=stSidebar]');
    const actions = {{
      collapse: () => bar.setAttribute('aria-expanded', 'false'),
      // The live Streamlit state: off-screen while aria-expanded still says true.
      collapse_keep_aria: () => bar.classList.add('collapsed'),
      // aria-expanded claims collapsed, yet 100 px of the sidebar stay visible.
      partial: () => {{
        bar.setAttribute('aria-expanded', 'false');
        bar.classList.add('partial');
      }},
      animate_forever: () => {{ bar.classList.add('collapsed', 'pulse'); }},
      unstable: () => {{
        bar.classList.add('collapsed');
        const main = document.querySelector('[data-testid=stMainBlockContainer]');
        let step = 0;
        const shift = () => {{ main.style.width = (200 + (step++ % 50)) + 'px';
                              requestAnimationFrame(shift); }};
        requestAnimationFrame(shift);
      }},
      none: () => {{}},
    }};
    document.querySelector('[data-testid=stSidebarCollapseButton]').onclick = actions['{action}'];
  </script>
</body></html>
"""


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    playwright_api = pytest.importorskip("playwright.sync_api")
    with playwright_api.sync_playwright() as playwright:
        try:
            instance = playwright.chromium.launch(channel="chromium", headless=True)
        except Exception as exc:  # noqa: BLE001 - no local browser build
            pytest.skip(f"headless Chromium unavailable: {type(exc).__name__}")
        yield instance
        instance.close()


def page_with(browser, *, duration: int = 600, action: str = "collapse", extra: str = ""):
    context = browser.new_context(viewport=MOBILE)
    page = context.new_page()
    page.set_content(PAGE.format(duration=duration, action=action, extra=extra))
    return context, page


def chat(page):
    return page.get_by_test_id("stChatInputTextArea")


# --------------------------------------------------------------------------- A delayed transition


def test_a_delayed_collapse_is_awaited_and_measured_correctly(browser) -> None:
    # A slow transition keeps the chat covered for well over the click round trip.
    context, page = page_with(browser, duration=2000)
    try:
        # The race of the failed run: right after the click the sidebar still covers the chat.
        page.get_by_test_id("stSidebarCollapseButton").click()
        assert gate.unobstructed(page, chat(page)) is False
        gate.wait_for_collapsed_sidebar(page)
        layout = gate.mobile_layout(page, chat(page))
        assert layout["no_horizontal_overflow"] is True
        assert layout["chat_input_unobscured"] is True
        assert layout["sidebar_right"] <= 0.5
        assert layout["viewport_width"] == 390 and layout["document_width"] <= 391
    finally:
        context.close()


def test_collapse_sidebar_waits_for_the_transition_itself(browser) -> None:
    context, page = page_with(browser, duration=600)
    try:
        gate.collapse_sidebar(page)
        assert gate.unobstructed(page, chat(page)) is True
    finally:
        context.close()


# --------------------------------------------------------------------------- B never collapses


def test_a_sidebar_that_never_collapses_fails_within_a_bounded_time(browser) -> None:
    context, page = page_with(browser, action="none")
    try:
        started = time.monotonic()
        with pytest.raises(gate.SidebarNotCollapsed) as raised:
            gate.collapse_sidebar(page, timeout_ms=800)
        assert time.monotonic() - started < 5
        message = str(raised.value)
        assert "aria-expanded=true" in message and "viewport=390x844" in message
        assert "sidebar_right=300" in message and "stable_frames=" in message
    finally:
        context.close()


# --------------------------------------------------------------------------- live run c5db9191c0


def test_an_offscreen_settled_sidebar_counts_although_aria_expanded_stays_true(browser) -> None:
    context, page = page_with(browser, duration=300, action="collapse_keep_aria")
    try:
        gate.collapse_sidebar(page)
        layout = gate.mobile_layout(page, chat(page))
        assert layout["sidebar_expanded"] is True  # diagnostic only, as in the live run
        assert layout["sidebar_right"] <= 0.5 and layout["sidebar_animations"] == 0
        assert layout["no_horizontal_overflow"] is True
        assert layout["chat_input_unobscured"] is True
    finally:
        context.close()


@pytest.mark.parametrize(
    ("action", "detail"),
    [
        ("partial", "sidebar_right=100"),
        ("animate_forever", "running_animations=1"),
        ("unstable", "stable_frames=0"),
    ],
    ids=["partially-visible", "animation-never-settles", "geometry-never-stable"],
)
def test_an_unsettled_sidebar_still_fails_within_a_bounded_time(browser, action, detail) -> None:
    context, page = page_with(browser, duration=100, action=action)
    try:
        started = time.monotonic()
        with pytest.raises(gate.SidebarNotCollapsed) as raised:
            gate.collapse_sidebar(page, timeout_ms=1000)
        assert time.monotonic() - started < 5
        assert detail in str(raised.value)
    finally:
        context.close()


# --------------------------------------------------------------------------- C/D real defects


def test_a_genuine_overflow_after_the_collapse_still_fails(browser) -> None:
    context, page = page_with(browser, duration=100, extra='<div class="wide"></div>')
    try:
        gate.collapse_sidebar(page)
        layout = gate.mobile_layout(page, chat(page))
        assert layout["no_horizontal_overflow"] is False and layout["document_width"] > 391
    finally:
        context.close()


def test_a_genuinely_obscured_chat_input_still_fails(browser) -> None:
    context, page = page_with(browser, duration=100, extra='<div class="toast"></div>')
    try:
        gate.collapse_sidebar(page)
        layout = gate.mobile_layout(page, chat(page))
        assert layout["chat_input_unobscured"] is False
        assert layout["chat_input_box"]["width"] > 0
    finally:
        context.close()


def test_desktop_viewports_are_untouched(browser) -> None:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    try:
        page.set_content(PAGE.format(duration=600, action="collapse", extra=""))
        gate.collapse_sidebar(page)  # no click and no wait on wide screens
        expanded = page.get_by_test_id("stSidebar").get_attribute("aria-expanded")
        assert expanded == "true"
    finally:
        context.close()


# --------------------------------------------------------------------------- E/F evidence


def test_each_mobile_sub_check_is_required_and_reported() -> None:
    assert set(gate.MOBILE_CHECKS) <= set(gate.REQUIRED)
    assert len(gate.MOBILE_CHECKS) == 4 and "responsive_mobile" in gate.REQUIRED
    result = gate.GateResult(checks=dict.fromkeys(gate.REQUIRED, True))
    evidence = gate.build_evidence(result, {"browser_version": "153"})
    assert evidence["responsive_mobile_checks"] == dict.fromkeys(gate.MOBILE_CHECKS, True)
    assert "mobile_layout" in evidence


@pytest.mark.parametrize("failed", ["responsive_mobile_chat_input_unobscured", "missing"])
def test_the_aggregate_fails_when_any_sub_check_fails(failed) -> None:
    result = gate.GateResult()
    for name in gate.MOBILE_CHECKS:
        result.check(name, True)
    if failed == "missing":
        del result.checks[gate.MOBILE_CHECKS[0]]
    else:
        result.check(failed, False)
    gate.derive_responsive_mobile(result)
    assert result.checks["responsive_mobile"] is False
    complete = gate.GateResult()
    for name in gate.MOBILE_CHECKS:
        complete.check(name, True)
    gate.derive_responsive_mobile(complete)
    assert complete.checks["responsive_mobile"] is True


def test_record_mobile_layout_sets_one_check_and_keeps_diagnostics() -> None:
    result = gate.GateResult()
    layout = {
        "viewport_width": 390,
        "viewport_height": 844,
        "document_width": 512,
        "no_horizontal_overflow": False,
        "sidebar_expanded": False,
        "sidebar_right": 0.0,
    }
    gate.record_mobile_layout(result, "responsive_mobile_answer_no_overflow", layout)
    assert result.checks["responsive_mobile_answer_no_overflow"] is False
    diagnostics = result.metrics["mobile_layout"]["responsive_mobile_answer_no_overflow"]
    assert diagnostics["document_width"] == 512 and diagnostics["viewport_width"] == 390
