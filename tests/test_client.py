"""Browser client end-to-end tests: the real server (in-process), the real page
served over HTTP, and real headless Firefox driven by selenium.

The test plays the caption source itself, so it controls timing and the
source_ts stamps exactly. Skipped when selenium, Firefox or geckodriver are
missing (for example on WSL2); the manual checklist in TESTING.md covers the UI
there. Run only these with `pytest -m browser`, or skip them with
`pytest -m "not browser"`.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest
import websockets
from websockets.sync.client import connect as ws_connect

import solution_server
from liverecall.state import CaptionState

selenium = pytest.importorskip("selenium")
from selenium import webdriver  # noqa: E402
from selenium.webdriver.firefox.options import Options  # noqa: E402
from selenium.webdriver.firefox.service import Service  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402

GECKODRIVER = shutil.which("geckodriver") or (
    "/snap/bin/geckodriver" if Path("/snap/bin/geckodriver").exists() else None)
if not shutil.which("firefox") or GECKODRIVER is None:
    pytest.skip("firefox + geckodriver not available", allow_module_level=True)

pytestmark = pytest.mark.browser

CLIENT_DIR = Path(__file__).resolve().parent.parent / "client"


# ------------------------------------------------------------------ fixtures

class ServerThread:
    """solution_server.handler on a free port, in a background asyncio loop."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.port = None
        self.ready = threading.Event()
        self._stop = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        self._stop = self.loop.create_future()
        async with websockets.serve(solution_server.handler, "127.0.0.1", 0,
                                    write_limit=4096) as server:
            self.port = server.sockets[0].getsockname()[1]
            self.ready.set()
            await self._stop

    def start(self):
        self.thread.start()
        assert self.ready.wait(5), "server did not start"

    def stop(self):
        self.loop.call_soon_threadsafe(self._stop.set_result, None)
        self.thread.join(5)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    srv = ServerThread()
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture(scope="module")
def http_port():
    handler = functools.partial(QuietHandler, directory=str(CLIENT_DIR))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


@pytest.fixture(scope="module")
def driver():
    options = Options()
    options.add_argument("-headless")
    drv = webdriver.Firefox(options=options, service=Service(GECKODRIVER))
    yield drv
    drv.quit()


@pytest.fixture(autouse=True)
def fresh_server_state(monkeypatch):
    """Each test starts from an empty server, like tests/test_server.py."""
    monkeypatch.setattr(solution_server, "STATE", CaptionState())
    monkeypatch.setattr(solution_server, "CLOSED_TOTALS",
                        {"coalesced": 0, "lag_entries": 0, "hopeless_closes": 0})
    solution_server.CHANNELS.clear()
    yield
    solution_server.CHANNELS.clear()


# ------------------------------------------------------------------- helpers

class Source:
    """A stand-in for caption_source.py that the test controls message by message."""

    def __init__(self, port):
        self.ws = ws_connect(f"ws://127.0.0.1:{port}")
        self.ws.send(json.dumps({"type": "SOURCE_HELLO", "session": "L1"}))

    def send(self, segment, revision, text, final, source_ts=None):
        self.ws.send(json.dumps({
            "type": "CAPTION_UPDATE", "session": "L1", "segment": segment,
            "revision": revision, "source_ts": time.time() if source_ts is None else source_ts,
            "final": final, "text": text,
        }))

    def close(self):
        self.ws.close()


@pytest.fixture
def source(server):
    src = Source(server.port)
    yield src
    src.close()


def open_client(driver, http_port, server, **query):
    """Load the page with ?ws=... (the query-string configuration) and wait for the hello."""
    query = {"ws": f"ws://127.0.0.1:{server.port}", **query}
    driver.get(f"http://127.0.0.1:{http_port}/?{urlencode(query)}")
    wait_until(driver, lambda d: d.execute_script("return liverecall.counters.connections") >= 1)


def wait_until(driver, condition, timeout=10):
    WebDriverWait(driver, timeout).until(condition)


def rows(driver):
    return driver.execute_script("""
        return [...document.querySelectorAll('#live .caption')].map(r => ({
            segment: Number(r.dataset.segment), revision: Number(r.dataset.revision),
            final: r.classList.contains('final'), text: r.querySelector('.text').textContent}));
    """)


def has_row(segment, revision=None):
    def check(d):
        for row in rows(d):
            if row["segment"] == segment and (revision is None or row["revision"] == revision):
                return True
        return False
    return check


def counters(driver):
    return driver.execute_script("return liverecall.counters")


def status_state(driver):
    return driver.execute_script("return document.getElementById('status').dataset.state")


def recall_items(driver):
    return driver.execute_script(
        "return [...document.querySelectorAll('#recall p')].map(p => Number(p.dataset.segment))")


def click(driver, selector):
    driver.find_element("css selector", selector).click()


# --------------------------------------------------------------------- tests

def test_revisions_replace_in_place(driver, http_port, server, source):
    open_client(driver, http_port, server)
    source.send(1, 1, "TCP uses", False)
    source.send(1, 2, "TCP uses cumulative", False)
    source.send(1, 3, "TCP uses cumulative acknowledgements.", True)
    source.send(2, 1, "Retransmission", False)
    wait_until(driver, has_row(2, 1))

    assert rows(driver) == [
        {"segment": 1, "revision": 3, "final": True, "text": "TCP uses cumulative acknowledgements."},
        {"segment": 2, "revision": 1, "final": False, "text": "Retransmission"},
    ]

    # A stale revision must not change what is shown; a marker message tells us it was processed.
    source.send(1, 2, "TCP uses cumulative", False)
    source.send(2, 2, "Retransmission timers", False)
    wait_until(driver, has_row(2, 2))
    assert rows(driver)[0] == {
        "segment": 1, "revision": 3, "final": True, "text": "TCP uses cumulative acknowledgements."}
    assert len(rows(driver)) == 2
    # The server rejected the stale message, so the page never saw it.
    assert counters(driver)["stale"] == 0


def test_store_rules_match_the_server(driver, http_port, server):
    open_client(driver, http_port, server)
    verdicts = driver.execute_script("""
        const s = new CaptionStore(100, 3);
        const upd = (seg, rev, final, ts = 1000, fromHistory = false) =>
            s.accept({segment: seg, revision: rev, final, text: `s${seg}r${rev}`, source_ts: ts, extra: 1}, fromHistory);
        const hist = (seg, rev, final, ts = 1000) => upd(seg, rev, final, ts, true);
        return [
            upd(1, 1, false), upd(1, 2, false), upd(1, 1, false), upd(1, 2, false),  // newer, newer, stale, dup
            upd(1, 3, true), hist(1, 4, false), hist(1, 3, true),                   // final: nothing replaces it
            s.accept({segment: "2", revision: 1, final: false, text: "x", source_ts: 1}),
            s.accept({segment: 2, revision: true, final: false, text: "x", source_ts: 1}),
            s.accept("garbage"),
            upd(2, 1, true, 1001), upd(3, 1, true, 1002), upd(4, 1, true, 1003),     // 4 segments > cap 3
            hist(1, 5, false),                                                     // pruned: below the floor
            JSON.stringify(s.resumeState()), s.floor, [...s.segments.keys()],
            upd(1, 1, false, 2000), [...s.segments.keys()], s.floor,               // live seg 1 again: a restart
        ];
    """)
    assert verdicts == [
        "accepted", "accepted", "stale", "duplicate",
        "accepted", "stale", "duplicate",
        "invalid", "invalid", "invalid",
        "accepted", "accepted", "accepted",
        "stale",
        '{"last_segment":4,"last_revision":1}', 1, [2, 3, 4],
        "restart", [1], 0,
    ]


def test_recall_uses_local_state_only(driver, http_port, server, source):
    open_client(driver, http_port, server)
    now = time.time()
    source.send(10, 1, "fifty seconds ago", True, source_ts=now - 50)
    source.send(11, 1, "twenty-two seconds ago", True, source_ts=now - 22)
    source.send(12, 1, "just now", True, source_ts=now)
    wait_until(driver, has_row(12))

    click(driver, "button.primary")           # "What did I miss?" starts at 30 s
    assert recall_items(driver) == [11, 12]
    assert "Last 30 s" in driver.find_element("id", "recall-title").text
    click(driver, "button[data-seconds='15']")
    assert recall_items(driver) == [12]
    click(driver, "button[data-seconds='60']")
    assert recall_items(driver) == [10, 11, 12]
    # Local only: still the one connection, and the server received exactly one CLIENT_HELLO.
    assert counters(driver)["connections"] == 1
    assert len(solution_server.CHANNELS) == 1


def test_reconnect_recovers_missed_finals_exactly_once(driver, http_port, server, source):
    open_client(driver, http_port, server)
    for seg in (1, 2, 3):
        source.send(seg, 1, f"segment {seg} draft", False)
        source.send(seg, 2, f"segment {seg} final", True)
    wait_until(driver, has_row(3, 2))

    hellos = []
    state = solution_server.STATE
    original = state.history_for

    def spy(last_segment, last_revision, history_seconds):
        hellos.append((last_segment, last_revision))
        return original(last_segment, last_revision, history_seconds)

    state.history_for = spy

    driver.execute_script("liverecall.dropConnection(3000)")
    wait_until(driver, lambda d: status_state(d) == "disconnected")
    source.send(4, 1, "segment 4 draft", False)     # missed while disconnected
    source.send(4, 2, "segment 4 final", True)
    source.send(5, 1, "segment 5 final", True)
    # The server must hold them before the tab comes back (well inside the 3 s hold).
    wait_until(driver, lambda d: any(c.segment == 5 for c in original(0, 0, 60)))
    wait_until(driver, lambda d: counters(d)["connections"] == 2 and has_row(5)(d))

    assert hellos == [(3, 2)]                        # resumed from the latest final, not a provisional
    c = counters(driver)
    assert c["history"] == 2 and c["duplicate"] == 0 and c["stale"] == 0
    assert [r["segment"] for r in rows(driver)] == [1, 2, 3, 4, 5]
    assert all(r["final"] for r in rows(driver))
    assert rows(driver)[3]["text"] == "segment 4 final"
    assert status_state(driver) == "connected"


def test_late_join_gets_context_then_live_in_two_tabs(driver, http_port, server, source):
    for seg in (1, 2, 3):
        source.send(seg, 1, f"segment {seg} final", True)
    time.sleep(0.2)  # let the server store them before anyone joins

    open_client(driver, http_port, server)           # late joiner: context from HISTORY...
    wait_until(driver, has_row(3))
    c = counters(driver)
    assert c["history"] == 3 and c["live"] == 0 and c["duplicate"] == 0
    assert recall_items(driver) == [1, 2, 3]         # recovered context is shown without a click
    assert "Last 30 s" in driver.find_element("id", "recall-title").text
    first_tab = driver.current_window_handle

    driver.switch_to.new_window("tab")               # a second client (new tab = new client id)
    open_client(driver, http_port, server)
    wait_until(driver, has_row(3))
    source.send(4, 1, "segment 4 draft", False)      # ...then live updates reach both tabs
    wait_until(driver, has_row(4, 1))
    assert rows(driver)[-1] == {"segment": 4, "revision": 1, "final": False, "text": "segment 4 draft"}
    second_id = driver.execute_script("return sessionStorage.getItem('liverecall-client-id')")
    driver.close()

    driver.switch_to.window(first_tab)
    wait_until(driver, has_row(4, 1))
    assert counters(driver)["live"] == 1
    first_id = driver.execute_script("return sessionStorage.getItem('liverecall-client-id')")
    assert first_id != second_id
    source.send(4, 2, "segment 4 final", True)       # an open recall panel follows new finals
    wait_until(driver, has_row(4, 2))
    assert recall_items(driver) == [1, 2, 3, 4]


def test_server_restart_is_detected_and_rejoined(driver, http_port, server, source, monkeypatch):
    open_client(driver, http_port, server)
    for seg in (1, 2, 3):
        source.send(seg, 1, f"old run {seg}", True)
    wait_until(driver, has_row(3))

    # A restarted server has empty state and the restarted source numbers from 1 again.
    monkeypatch.setattr(solution_server, "STATE", CaptionState())
    source.send(1, 1, "new run 1 final", True)       # detected on a final: must still rejoin fresh
    source.send(2, 1, "new run 2 draft", False)
    source.send(2, 2, "new run 2 final", True)
    wait_until(driver, lambda d: counters(d)["connections"] == 2 and has_row(2, 2)(d)
               and has_row(1)(d) and status_state(d) == "connected")

    c = counters(driver)
    assert c["restarts"] == 1 and c["duplicate"] == 0
    assert [(r["segment"], r["text"]) for r in rows(driver)] == [
        (1, "new run 1 final"), (2, "new run 2 final")]   # old rows gone, new run exactly once
    assert "fresh join" in driver.find_element("id", "status").text
