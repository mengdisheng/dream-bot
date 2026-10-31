from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from personal_bot.browser import Browser


@pytest.mark.asyncio
async def test_new_conversation_resets_page_after_old_transport_is_revoked():
    state=Browser()
    page=SimpleNamespace(close=AsyncMock())
    extra=SimpleNamespace(close=AsyncMock())
    fresh=SimpleNamespace(close=AsyncMock())
    state.page=page
    state.context=SimpleNamespace(pages=[page,extra,fresh],new_page=AsyncMock(return_value=fresh))
    state.task='old-task'
    state.conversation='old-conversation'
    viewer=SimpleNamespace(close=AsyncMock())
    state.viewers={'viewer':viewer}
    state.sessions={'ticket':{'epoch':state.epoch,'task_id':'old-task'}}
    state.stop_vnc=AsyncMock()

    async def create():
        assert not state.sessions and not state.viewers
        viewer.close.assert_awaited_once()
        state.stop_vnc.assert_awaited_once()
        return fresh
    state.context.new_page.side_effect=create
    await state.switch_task('new-task','new-conversation')
    page.close.assert_awaited_once()
    extra.close.assert_awaited_once()
    fresh.close.assert_not_awaited()
    assert state.page is fresh
    assert state.task=='new-task' and state.conversation=='new-conversation'


@pytest.mark.asyncio
async def test_followup_in_same_conversation_keeps_page_but_revokes_old_inputs():
    state=Browser()
    state.page=SimpleNamespace(close=AsyncMock())
    state.task='first-turn'
    state.conversation='conversation'
    state.stop_vnc=AsyncMock()
    old_epoch=state.epoch
    await state.switch_task('second-turn','conversation')
    state.page.close.assert_not_awaited()
    state.stop_vnc.assert_awaited_once()
    assert state.epoch==old_epoch+1


@pytest.mark.asyncio
async def test_failed_page_clear_does_not_associate_new_conversation():
    state=Browser()
    state.page=SimpleNamespace(close=AsyncMock())
    state.context=SimpleNamespace(new_page=AsyncMock(side_effect=TimeoutError()))
    state.task='old-task'
    state.conversation='old-conversation'
    state.stop_vnc=AsyncMock()
    with pytest.raises(TimeoutError):
        await state.switch_task('new-task','new-conversation')
    assert state.task=='old-task' and state.conversation=='old-conversation'
    assert not state.sessions


@pytest.mark.asyncio
async def test_other_conversation_status_hides_page_without_waking_or_rebinding():
    state=Browser()
    state.page=SimpleNamespace(url='https://private.example/old-task')
    state.context=object()
    state.task='old-task'
    state.conversation='old-conversation'
    state.mode='agent'
    state.ensure=AsyncMock()
    state.switch_task=AsyncMock()
    result=await state.action({'action':{'type':'status'},'task_id':'new-task','conversation_id':'new-conversation'})
    assert result['url'] is None and result['task_id'] is None and not result['running']
    state.ensure.assert_not_awaited()
    state.switch_task.assert_not_awaited()
    assert state.task=='old-task'
    same=await state.action({'action':{'type':'status'},'task_id':'followup','conversation_id':'old-conversation'})
    assert same['url']=='https://private.example/old-task'
