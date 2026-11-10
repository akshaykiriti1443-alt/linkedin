"""WP5: provider adapters (7.1). A fake OpenAI-compatible server for the compat adapter and the job runner;
httpx.MockTransport request-shape checks for the Anthropic, Gemini and OpenAI SDK adapters. No real network."""

import json

import httpx
import pytest

from _wp5_support import FakeAI, add_account, add_fake_connection, add_reel, add_topics, chat, give_signals

from reelvault import ai
from reelvault import schema_members as S
from reelvault.ai import base as B
from reelvault.ai.compat_p import CompatProvider
from reelvault.ai.net import PinnedTransport

TOPICS = {"topics": [{"slug": "ads", "label": "Ads", "description": "shows ads", "examples": []}]}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200


@pytest.fixture
def fake():
    f = FakeAI()
    yield f
    f.stop()


def compat(fake, **kw):
    return CompatProvider("sk-test", "fake-model", fake.base_url, label="Fake", **kw)


def ok(obj):
    return lambda body: (200, chat(json.dumps(obj)), {})


def test_step_down_json_schema_to_json_object(fake):
    seen = []
    fake.script = [
        lambda b: (400, {"error": {"message": "response_format json_schema is not supported"}}, {}),
        ok(TOPICS),
    ]
    p = compat(fake, on_mode_change=seen.append)
    obj, usage = ai.structured(p, "sys", ["hello"], S.TopicList)
    assert obj.topics[0].slug == "ads" and usage.input == 10
    kinds = [c["body"].get("response_format", {}).get("type") for c in fake.calls if c["method"] == "POST"]
    assert kinds == ["json_schema", "json_object"] and seen == ["json_object"] and p.json_mode == "json_object"
    assert "JSON schema" in fake.calls[-1]["body"]["messages"][0]["content"]  # schema moved into the prompt


def test_retry_with_validation_error_then_fenced_json(fake):
    fake.script = [
        ok({"topics": "nope"}),
        lambda b: (200, chat("Sure!\n```json\n" + json.dumps(TOPICS) + "\n```\nDone."), {}),
    ]
    obj, _ = ai.structured(compat(fake), "sys", ["hello"], S.TopicList)
    assert obj.topics[0].label == "Ads"
    retry_text = json.dumps(fake.calls[-1]["body"]["messages"][-1])
    assert "not valid" in retry_text and "Return the complete JSON again" in retry_text


def test_invalid_twice_raises(fake):
    fake.script = [ok({"x": 1}), ok({"y": 2})]
    with pytest.raises(B.AIError, match="didn't fit the format twice"):
        ai.structured(compat(fake), "sys", ["hello"], S.TopicList)


def test_cross_check_failure_is_retried_like_a_schema_error(fake):
    from reelvault.server.work import Invalid

    fake.script = [ok(TOPICS), ok(TOPICS)]
    n = []

    def check(obj):
        n.append(1)
        if len(n) == 1:
            raise Invalid("pain must be one of: A | B")

    ai.structured(compat(fake), "sys", ["hello"], S.TopicList, check=check)
    assert len(n) == 2 and "pain must be one of" in json.dumps(fake.calls[-1]["body"])


def test_error_mapping(fake):
    fake.script = [lambda b: (401, {"error": {"message": "invalid api key"}}, {})]
    with pytest.raises(B.BadKey, match="That key didn't work"):
        compat(fake).generate(B.Request("s", ["x"]))
    fake.script = [lambda b: (429, {"error": {"message": "slow down"}}, {"retry-after": "7"})]
    with pytest.raises(B.RateLimited) as e:
        compat(fake).generate(B.Request("s", ["x"]))
    assert e.value.retry_after == 7.0
    fake.script = [lambda b: (200, chat("", refusal="I can't help with that"), {})]
    with pytest.raises(B.Refused):
        compat(fake).generate(B.Request("s", ["x"]))
    fake.script = [lambda b: (200, chat("{", finish="length"), {}), lambda b: (200, chat("{", finish="length"), {})]
    with pytest.raises(B.Truncated):
        ai.structured(compat(fake), "s", ["x"], S.TopicList)
    assert "cut off" in json.dumps(fake.calls[-1]["body"])  # the second try asked for a shorter answer
    fake.script = [lambda b: (402, {"error": {"message": "insufficient credits"}}, {})]
    with pytest.raises(B.QuotaExceeded):
        compat(fake).generate(B.Request("s", ["x"]))


def test_text_only_model_gets_no_images_and_the_note(fake):
    fake.script = [ok(TOPICS)]
    ai.structured(compat(fake, vision=False), "sys", ["look", B.Image(JPEG)], S.TopicList)
    content = fake.calls[-1]["body"]["messages"][-1]["content"]
    text = content if isinstance(content, str) else json.dumps(content)
    assert "image_url" not in text and "You cannot see the frames" in text


def test_images_sent_as_data_urls_and_too_many_raise(fake):
    fake.script = [ok(TOPICS)]
    ai.structured(compat(fake, max_images=2), "sys", ["look", B.Image(JPEG), B.Image(JPEG)], S.TopicList)
    parts = fake.calls[-1]["body"]["messages"][-1]["content"]
    assert sum(1 for p in parts if p["type"] == "image_url") == 2
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    with pytest.raises(B.AIError, match="Too many images"):
        ai.structured(compat(fake, max_images=2), "sys", ["x"] + [B.Image(JPEG)] * 3, S.TopicList)


def test_pinned_transport_refuses_a_host_that_resolves_to_loopback(fake):
    calls = []

    def resolver(host, port):
        calls.append(host)
        return ["127.0.0.1"]

    url = f"http://rebind.example:{fake.port}/v1"
    blocked = CompatProvider("k", "m", url, http_client=httpx.Client(transport=PinnedTransport(resolver=resolver)))
    with pytest.raises(B.AIError):
        blocked.generate(B.Request("s", ["x"]))
    assert calls and not [c for c in fake.calls if c["method"] == "POST"]
    fake.script = [ok(TOPICS)]
    allowed = CompatProvider(
        "k", "m", url, http_client=httpx.Client(transport=PinnedTransport(resolver=resolver, allow_private=True))
    )
    assert allowed.generate(B.Request("s", ["x"])).text  # pinned to the resolved IP, Host kept


def test_key_test_lists_models_and_maps_401(fake):
    p = compat(fake)
    assert p.test().startswith("Works.")
    fake.script = ["401"]
    with pytest.raises(B.BadKey):
        compat(fake).test()


# ============================================================================================== SDK adapters


def mock_client(handler, lib=httpx):
    """A client whose transport answers with handler(request). The Anthropic SDK needs httpx2 objects."""
    seen = []

    def h(req):
        seen.append(req)
        return handler(req)

    return lib.Client(transport=lib.MockTransport(h)), seen


def test_anthropic_request_shape_and_errors():
    import httpx2

    from reelvault.ai.anthropic_p import AnthropicProvider

    def mock(handler):
        return mock_client(lambda r: handler(r), httpx2)

    msg = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": json.dumps(TOPICS)}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 0,
        },
    }
    http, seen = mock(lambda r: httpx2.Response(200, json=msg))
    p = AnthropicProvider("sk-ant-test", "claude-sonnet-5-5", http_client=http)
    obj, usage = ai.structured(p, "SYS", ["prompt", B.Image(JPEG)], S.TopicList, "low", max_output_tokens=3000)
    assert obj.topics[0].slug == "ads" and usage.input == 140 and usage.cached == 40
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/messages" and body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 3000
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"} and body["system"][0]["text"] == "SYS"
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["type"] == "base64" and content[1]["type"] == "text"
    assert body["output_config"]["effort"] == "low" and body["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in body and "tool_choice" not in body
    assert "maxLength" not in json.dumps(body["output_config"]["format"]["schema"])
    http2, _ = mock(
        lambda r: httpx2.Response(
            401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}
        )
    )
    with pytest.raises(B.BadKey):
        AnthropicProvider("k", "claude-sonnet-5-5", http_client=http2).generate(B.Request("s", ["x"]))
    http3, _ = mock(lambda r: httpx2.Response(200, json=dict(msg, stop_reason="refusal", content=[])))
    with pytest.raises(B.Refused):
        AnthropicProvider("k", "m", http_client=http3).generate(B.Request("s", ["x"]))
    http4, seen4 = mock(lambda r: httpx2.Response(200, json=msg))
    assert (
        AnthropicProvider("k", "claude-sonnet-5-5", http_client=http4, cheap_model="claude-haiku-5-5")
        .test()
        .startswith("Works")
    )
    t = json.loads(seen4[0].content)
    assert t["max_tokens"] == 1 and t["model"] == "claude-haiku-5-5"


def test_gemini_request_shape_and_errors():
    from reelvault.ai.gemini_p import GeminiProvider

    resp = {
        "candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(TOPICS)}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 50, "candidatesTokenCount": 9, "thoughtsTokenCount": 1},
    }
    http, seen = mock_client(lambda r: httpx.Response(200, json=resp))
    p = GeminiProvider("AIza-test", "gemini-3.8-flash", http_client=http)
    obj, usage = ai.structured(p, "SYS", ["prompt", B.Image(JPEG)], S.TopicList)
    assert obj.topics[0].slug == "ads" and usage.input == 50 and usage.output == 10
    req = seen[0]
    assert req.url.path.endswith("/models/gemini-3.8-flash:generateContent")
    body = json.loads(req.content)
    parts = body["contents"][0]["parts"]
    assert (
        "inlineData" in parts[0]
        and (parts[0]["inlineData"].get("mimeType") or parts[0]["inlineData"].get("mime_type")) == "image/jpeg"
        and parts[1]["text"]
    )
    cfg = body["generationConfig"]
    assert cfg["responseMimeType"] == "application/json" and "responseJsonSchema" in cfg
    assert "$defs" not in json.dumps(cfg["responseJsonSchema"])
    assert body["systemInstruction"]["parts"][0]["text"] == "SYS"
    http2, _ = mock_client(
        lambda r: httpx.Response(
            429,
            json={
                "error": {
                    "code": 429,
                    "status": "RESOURCE_EXHAUSTED",
                    "message": "Quota exceeded for metric: generate_content_free_tier_requests, limit: 20 PerDay",
                }
            },
        )
    )
    with pytest.raises(B.QuotaExceeded, match="free limit"):
        GeminiProvider("k", "gemini-3.5-flash-lite", http_client=http2).generate(B.Request("s", ["x"]))
    http3, _ = mock_client(
        lambda r: httpx.Response(
            200, json={"candidates": [{"content": {"role": "model", "parts": []}, "finishReason": "SAFETY"}]}
        )
    )
    with pytest.raises(B.Refused):
        GeminiProvider("k", "m", http_client=http3).generate(B.Request("s", ["x"]))
    http4, _ = mock_client(
        lambda r: httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "status": "INVALID_ARGUMENT",
                    "message": "API key not valid. Please pass a valid API key.",
                }
            },
        )
    )
    with pytest.raises(B.BadKey):
        GeminiProvider("k", "m", http_client=http4).generate(B.Request("s", ["x"]))


def test_openai_request_shape_and_errors():
    from reelvault.ai.openai_p import OpenAIProvider

    resp = {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "status": "completed",
        "model": "gpt-6.1-terra",
        "output": [
            {
                "type": "message",
                "id": "m1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": json.dumps(TOPICS), "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 30,
            "output_tokens": 7,
            "total_tokens": 37,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }
    http, seen = mock_client(lambda r: httpx.Response(200, json=resp))
    p = OpenAIProvider("sk-test", "gpt-6.1-terra", http_client=http)
    obj, usage = ai.structured(p, "SYS", ["prompt", B.Image(JPEG)], S.TopicList, "medium")
    assert obj.topics[0].slug == "ads" and usage.cached == 3
    body = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/responses" and body["store"] is False and body["instructions"] == "SYS"
    assert body["text"]["format"]["type"] == "json_schema" and body["text"]["format"]["strict"] is True
    content = body["input"][0]["content"]
    assert content[0]["type"] == "input_text" and content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,") and body["reasoning"] == {"effort": "medium"}
    http2, _ = mock_client(
        lambda r: httpx.Response(
            200, json=dict(resp, status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        )
    )
    with pytest.raises(B.Truncated):
        OpenAIProvider("k", "m", http_client=http2).generate(B.Request("s", ["x"]))
    http3, _ = mock_client(lambda r: httpx.Response(403, json={"error": {"message": "no access", "type": "x"}}))
    with pytest.raises(B.BadKey):
        OpenAIProvider("k", "m", http_client=http3).generate(B.Request("s", ["x"]))


def test_get_provider_decrypts_and_routes_models(rv_env, member):
    from reelvault.server import db, security

    cid = db.new_id("aic")
    ct, nonce, kid = security.encrypt_secret("sk-ant-secret", security.connection_aad(member.id, cid, "anthropic"))
    with db.tx() as c:
        c.execute(
            db.ai_connections.insert().values(
                id=cid,
                user_id=member.id,
                provider="anthropic",
                model="claude-sonnet-5-5",
                cheap_model="claude-haiku-5-5",
                key_ciphertext=ct,
                key_nonce=nonce,
                key_kid=kid,
                options={},
            )
        )
        row = c.execute(db.ai_connections.select().where(db.ai_connections.c.id == cid)).first()
    assert ai.get_provider(row, task="analyze_reel").model == "claude-sonnet-5-5"
    assert ai.get_provider(row, task="suggest_topics").model == "claude-haiku-5-5"
    assert ai.get_provider(row, task="tag_reels").model == "claude-haiku-5-5"  # cheap model has vision
    p = ai.get_provider(row, task="make_output")
    assert p.secret == "sk-ant-secret" and p.client.api_key == "sk-ant-secret"
    with db.tx() as c:  # a ciphertext copied to another row will not decrypt
        c.execute(
            db.ai_connections.update()
            .where(db.ai_connections.c.id == cid)
            .values(user_id=member.id, provider="anthropic")
        )
    bad = dict(row._mapping, id="aic_other")
    with pytest.raises(B.BadKey, match="can't be read"):
        ai.get_provider(bad, task="make_output")


# ============================================================================================== end to end (jobs)


def _tag_job_setup(member, member_client, fake, *, vision: bool):
    from reelvault.server import db

    add_topics(member.id)
    aid = add_account(member.id)
    rids = [add_reel(member.id, aid, tag_state="none") for _ in range(3)]
    for r in rids[:2]:
        give_signals(member.id, r)
    with db.tx() as c:
        c.execute(db.reels.update().where(db.reels.c.id == rids[2]).values(signals_state="failed"))
    add_fake_connection(member_client, fake, vision_name=vision, model="plain-text-model")
    return rids


@pytest.mark.parametrize("vision", [True, False])
def test_tag_reels_end_to_end_against_the_fake_server(rv_env, member, member_client, run_jobs, fake, vision):
    from reelvault.server import db, pipeline

    rids = _tag_job_setup(member, member_client, fake, vision=vision)
    for r in rids:
        pipeline.advance(member.id, r)
    done = run_jobs(classes=("ai",))
    tag_jobs = [j for j in done if j.kind == "tag_reels"]
    assert tag_jobs and tag_jobs[0].state == "done" and tag_jobs[0].result["tagged"] == 3
    with db.read() as c:
        reels = {r.id: r for r in c.execute(db.reels.select().where(db.reels.c.user_id == member.id))}
        rows = {a.reel_id: a for a in c.execute(db.analyses.select().where(db.analyses.c.level == "quick"))}
        use = c.execute(db.ai_usage.select()).first()
    assert all(reels[r].tag_state == "done" for r in rids)
    assert rows[rids[0]].tag_quality == ("vision" if vision else "measured")
    assert rows[rids[2]].tag_quality == "measured"  # no frames: measured even with vision
    assert rows[rids[0]].topics == ["ad-creative"] and rows[rids[1]].topics == []  # unknown slug dropped
    assert rows[rids[0]].recipe_key.startswith("VF01|hb=1|cap=") and rows[rids[0]].visual_format_2 == ["VF02"]
    assert len(rows[rids[0]].data["one_line"]) <= 120
    assert rows[rids[0]].data["hook_bank"]["hook_type"] == "bold_claim"  # O >= 2 -> quick hook-bank row
    assert use.calls >= 1 and use.tokens_in >= 10
    posts = [c for c in fake.calls if c["method"] == "POST"]
    body = json.dumps(posts[-1]["body"])
    assert ("image_url" in body) is vision
    assert "Reel ids:" in body and '<untrusted source=\\"caption\\"' in body


def test_bad_key_pauses_the_job_and_marks_the_connection(rv_env, member, member_client, run_jobs, fake):
    from reelvault.server import db, pipeline

    rids = _tag_job_setup(member, member_client, fake, vision=True)
    fake.script = [lambda b: (401, {"error": {"message": "invalid key sk-test-1234567890abcd"}}, {})]
    pipeline.advance(member.id, rids[0])
    done = run_jobs(classes=("ai",))
    j = next(x for x in done if x.kind == "tag_reels")
    assert j.state == "paused" and j.error_code == "bad_key" and "key didn't work" in j.error_message
    with db.read() as c:
        conn = c.execute(db.ai_connections.select()).first()
        reel = c.execute(db.reels.select().where(db.reels.c.id == rids[0])).first()
    assert conn.status == "bad_key" and "sk-test" not in (conn.last_error or "")
    assert reel.tag_state == "queued" and reel.tag_batch is None  # batch released
    r = member_client.post(f"/api/ai/connections/{conn.id}/test", json={})
    assert r.json()["ok"] is True
    with db.read() as c:
        j2 = c.execute(db.jobs.select().where(db.jobs.c.id == j.id)).first()
    assert j2.state == "queued"  # resumed after the key works again


def test_rate_limit_requeues_with_run_after(rv_env, member, member_client, run_jobs, fake):
    from reelvault.server import db, pipeline

    rids = _tag_job_setup(member, member_client, fake, vision=True)
    fake.script = [lambda b: (429, {"error": {"message": "slow"}}, {"retry-after": "120"})]
    pipeline.advance(member.id, rids[0])
    run_jobs(classes=("ai",))
    with db.read() as c:
        j = c.execute(db.jobs.select().where(db.jobs.c.kind == "tag_reels")).first()
        reel = c.execute(db.reels.select().where(db.reels.c.id == rids[0])).first()
    assert j.state == "queued" and j.run_after is not None and reel.tag_state == "leased"


def test_cost_line_is_per_reel_and_plurals():
    """Fix round 2: a second _money() had replaced the per-reel one, so every model read 'under $1 a reel'."""
    from reelvault.ai import presets

    line = presets.cost_line("gemini-3.5-flash-lite")
    assert "cent" in line and "under $1" not in line
    assert "about 1 hour of work" in presets.estimate("desktop", reels_to_tag=12, deep_reads=0)["lines"][0]
