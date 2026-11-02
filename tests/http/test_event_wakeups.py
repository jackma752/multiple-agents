"""Commit notifications accelerate reads; they do not contain or authorize data."""
import asyncio
import threading
import time
import pytest
from agenthub_kernel.db import ChatMessageRow, Database
from tests.http.test_conversations import client, conversation
from tests.http.test_conversation_events import stream, parse_sse


def test_only_a_committed_event_wakes_its_conversation(client):
    db=client.app.state.db
    cid=conversation(client);other=conversation(client)
    async def scenario():
        token,signal=db.event_wakeups.subscribe(cid)
        token2,signal2=db.event_wakeups.subscribe(other)
        signal.clear();signal2.clear()
        try:
            def rollback():
                with pytest.raises(RuntimeError):
                    with db.session() as s:
                        s.add(ChatMessageRow(conversation_id=cid,role='user',text='rolled back'))
                        s.flush();raise RuntimeError('rollback')
            await asyncio.to_thread(rollback)
            await asyncio.sleep(0)
            assert not signal.is_set() and not signal2.is_set()
            def commit():
                with db.session() as s:
                    s.add(ChatMessageRow(conversation_id=cid,role='user',text='committed'))
            await asyncio.to_thread(commit)
            await asyncio.wait_for(signal.wait(),timeout=2)
            assert not signal2.is_set()
        finally:
            db.event_wakeups.unsubscribe(cid,token);db.event_wakeups.unsubscribe(other,token2)
    asyncio.run(scenario())


def test_multiple_subscribers_and_unsubscribe_do_not_lose_a_commit(client):
    db=client.app.state.db;cid=conversation(client)
    async def scenario():
        a,first=db.event_wakeups.subscribe(cid);b,second=db.event_wakeups.subscribe(cid)
        first.clear();second.clear()
        try:
            await asyncio.to_thread(db.event_wakeups.publish,{cid})
            await asyncio.wait_for(asyncio.gather(first.wait(),second.wait()),timeout=2)
            first.clear();second.clear();db.event_wakeups.unsubscribe(cid,a)
            await asyncio.to_thread(db.event_wakeups.publish,{cid})
            await asyncio.wait_for(second.wait(),timeout=2)
            assert not first.is_set()
        finally:
            db.event_wakeups.unsubscribe(cid,a);db.event_wakeups.unsubscribe(cid,b)
    asyncio.run(scenario())


def test_separate_database_writer_is_seen_by_polling_fallback(client):
    cid=conversation(client)
    client.app.state.sse_poll_seconds=.05
    separate=Database(client.app.state.db.url);separate.create_all()
    def writer():
        time.sleep(.1)
        with separate.session() as s:
            s.add(ChatMessageRow(conversation_id=cid,role='user',text='separate writer'))
    thread=threading.Thread(target=writer);thread.start()
    try:
        code,_,raw=stream(client,cid,after_seq=1,timeout=.5)
        assert code==200
        assert [frame['event'] for frame in parse_sse(raw)]==['message.created']
    finally:thread.join(2);separate.engine.dispose()
