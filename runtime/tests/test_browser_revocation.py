"""Broker-route regressions; no Chromium, network, or host process is started."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from personal_bot import browser as browser_module
from personal_bot import broker_support


class CloseOnce:
    """Model Starlette's error on sending websocket.close twice."""
    def __init__(self, events, label, state):
        self.events, self.label, self.state = events, label, state
        self.codes = []

    async def close(self, *, code):
        if self.codes:
            raise RuntimeError("websocket.close sent twice")
        assert self.state.lock.locked()
        self.codes.append(code)
        self.events.append(self.label)


@pytest.fixture
def browser_route(monkeypatch):
    state = browser_module.Browser()
    state.context = object()
    state.page = SimpleNamespace(url="https://example.com/")
    state.mode = "human"
    state.device = "controller-device"
    state.owner_session = "controller-session"
    state.epoch = 7
    state.task = "paused-agent-task"
    state.refs = {"old-ref": object()}
    state.observation = "old-observation"
    state.observed_url = state.page.url
    events = []

    async def stop():
        events.append("stop-vnc")

    async def start():
        events.append("start-vnc")

    state.stop_vnc = AsyncMock(side_effect=stop)
    state.start_vnc = AsyncMock(side_effect=start)
    captured = {}
    monkeypatch.setattr(browser_module, "Browser", lambda: state)
    monkeypatch.setattr(broker_support, "serve", lambda app, path: captured.update(app=app))
    browser_module.main()
    return state, captured["app"], events


@pytest.mark.asyncio
async def test_controller_session_revocation_closes_each_viewer_once_and_keeps_agent_paused(browser_route):
    state, app, events = browser_route
    controller = CloseOnce(events, "controller", state)
    second_tab = CloseOnce(events, "second-tab", state)
    reader = CloseOnce(events, "reader", state)
    state.sessions = {
        "controller-view": {"session_id": "controller-session"},
        "second-tab-view": {"session_id": "controller-session"},
        "reader-view": {"session_id": "reader-session"},
        "unused-ticket": {"session_id": "controller-session"},
    }
    state.viewers = {"controller-view": controller, "second-tab-view": second_tab, "reader-view": reader}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://browser") as client:
        response = await client.post("/revoke-session", json={"session_id": "controller-session"})
        assert response.status_code == 200
        result = response.json()
        assert result["mode"] == "human" and result["device_id"] is None
        assert result["browser_epoch"] == 8 and result["task_id"] == "paused-agent-task"
        assert result["running"] is True
        # A duplicate host revocation must not close streams or reset VNC again.
        repeated = await client.post("/revoke-session", json={"session_id": "controller-session"})
        assert repeated.status_code == 200 and repeated.json()["browser_epoch"] == 8

    assert controller.codes == second_tab.codes == reader.codes == [1008]
    assert state.sessions == {} and state.viewers == {}
    assert state.owner_session is None and state.device is None and state.mode == "human"
    assert state.refs == {} and state.observation is None and state.observed_url is None
    assert events == ["controller", "second-tab", "reader", "stop-vnc"]
    state.stop_vnc.assert_awaited_once()
    state.start_vnc.assert_not_awaited()


@pytest.mark.asyncio
async def test_readonly_session_revocation_preserves_other_session_and_controller(browser_route):
    state, app, events = browser_route
    controller = CloseOnce(events, "controller", state)
    reader = CloseOnce(events, "revoked-reader", state)
    other_reader = CloseOnce(events, "other-reader", state)
    state.sessions = {
        "controller-view": {"session_id": "controller-session"},
        "reader-view": {"session_id": "reader-session"},
        "other-view": {"session_id": "other-session"},
        "unused-reader-ticket": {"session_id": "reader-session"},
    }
    state.viewers = {"controller-view": controller, "reader-view": reader, "other-view": other_reader}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://browser") as client:
        response = await client.post("/revoke-session", json={"session_id": "reader-session"})
        assert response.status_code == 200
        repeated = await client.post("/revoke-session", json={"session_id": "reader-session"})
        assert repeated.status_code == 200

    assert reader.codes == [1008] and controller.codes == other_reader.codes == []
    assert set(state.sessions) == {"controller-view", "other-view"}
    assert set(state.viewers) == {"controller-view", "other-view"}
    assert state.owner_session == "controller-session" and state.device == "controller-device"
    assert state.mode == "human" and state.epoch == 7 and state.observation == "old-observation"
    assert events == ["revoked-reader"]
    state.stop_vnc.assert_not_awaited()
    state.start_vnc.assert_not_awaited()
