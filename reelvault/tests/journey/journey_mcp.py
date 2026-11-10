"""The Claude-connector path of the journey: the official mcp client (OAuth + tools) does the member's AI work.

    python journey_mcp.py --base http://localhost:9301 --email E --password P [--seconds 600]

Acts like Claude in the member's Claude app: the SDK's OAuthClientProvider runs the whole flow (401 -> resource
metadata -> AS metadata -> dynamic registration -> /oauth/authorize, where we press Allow with the member's web
session -> token), then the script calls list_pending_work and does what next_tool says: tag batches (looking at
the contact sheets), deep analyses, output briefs and text tasks, saving each through the save_* tools. The
"model" here is a deterministic stand-in (WP5's fake answers; quick tags from the local format guess), because a
real Claude chat is not available on this machine. Prints one JSON line per step and a final SUMMARY line.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests" / "server"))
sys.path.insert(0, str(REPO / "tests" / "mcp"))

import anyio  # noqa: E402
import httpx  # noqa: E402

import _wp5_support as w5  # noqa: E402
from _mcp_support import data_of, images_of, parse_form, result_chars, text_of  # noqa: E402

VF_SUBJECT = {"VF04": "Creator over a screenshot", "VF05": "Screen recording with a face bubble",
              "VF01": "Talking head at a desk"}


def say(**kw):
    print(json.dumps(kw, default=str)[:1500], flush=True)


def tags_for(text: str) -> dict:
    out = w5.fake_answer("QuickTagBatch", text)
    blocks = re.split(r"=== Reel (reel_[a-z0-9]+)", text)
    guess = {}
    for i in range(1, len(blocks) - 1, 2):
        m = re.search(r"Local format guess: (VF\d\d)", blocks[i + 1])
        guess[blocks[i]] = m.group(1) if m else "VF01"
    for t in out["tags"]:
        t["visual_format"] = guess.get(t["reel_id"], "VF01")
        t["visual_format_2"] = []
        t["visual_subject"] = VF_SUBJECT.get(t["visual_format"], "Creator talking")
    return out


async def main_async(a) -> dict:
    import httpx2
    from mcp import Client
    from mcp.client.auth import OAuthClientProvider
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

    mcp_url = a.base + "/mcp"
    lr = httpx.post(a.base + "/api/auth/login", json={"email": a.email, "password": a.password})
    lr.raise_for_status()
    cookie = lr.cookies.get("rv_session")
    got: dict = {}
    summary = {"oauth": None, "tools": [], "tag_batches": 0, "tags_saved": 0, "analyses": 0, "outputs": 0,
               "text_tasks": 0, "errors": [], "max_result_chars": 0, "images_seen": 0}

    class Mem:
        tokens = None
        info = None

        async def get_tokens(self):
            return self.tokens

        async def set_tokens(self, t):
            self.tokens = t

        async def get_client_info(self):
            return self.info

        async def set_client_info(self, i):
            self.info = i

    async def on_redirect(url: str) -> None:
        async with httpx2.AsyncClient(cookies={"rv_session": cookie}, follow_redirects=False) as h:
            page = await h.get(url)
            assert page.status_code == 200, page.text[:300]
            fields = parse_form(page.text)
            fields["decision"] = "allow"
            r = await h.post(a.base + "/oauth/authorize", data=fields, headers={"Origin": a.base})
        assert r.status_code == 303, r.text[:300]
        got.update({k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()})

    async def on_callback():
        return AuthorizationCodeResult(code=got["code"], state=got.get("state"))

    store = Mem()
    auth = OAuthClientProvider(mcp_url, OAuthClientMetadata(
        client_name="Claude (journey)", redirect_uris=[f"http://localhost:{int(os.environ.get('RV_JOURNEY_PORT_BASE', 9300)) + 6}/callback"],
        grant_types=["authorization_code", "refresh_token"], response_types=["code"],
        token_endpoint_auth_method="none"), store, redirect_handler=on_redirect, callback_handler=on_callback)

    async with httpx2.AsyncClient(auth=auth, timeout=240) as hc:
        async with Client(streamable_http_client(mcp_url, http_client=hc)) as c:
            tools = await c.list_tools()
            summary["tools"] = sorted(t.name for t in tools.tools)
            summary["oauth"] = {"client_id": store.info.client_id, "token": store.tokens.access_token[:4] + "..."}
            say(step="oauth+list_tools", **summary["oauth"], n_tools=len(summary["tools"]))
            prof = await c.call_tool("get_my_profile", {})
            say(step="get_my_profile", chars=result_chars(prof), error=prof.is_error)
            tax = await c.call_tool("get_taxonomy", {"part": "all"})
            say(step="get_taxonomy", chars=result_chars(tax))
            end = time.time() + a.seconds
            idle = 0
            while time.time() < end:
                pend = data_of(await c.call_tool("list_pending_work", {}))
                nxt = pend.get("next_tool")
                if not nxt:
                    idle += 1
                    if idle >= a.idle_rounds:
                        break
                    time.sleep(5)
                    continue
                idle = 0
                if nxt == "get_tag_batch":
                    r = await c.call_tool("get_tag_batch", {})
                    summary["max_result_chars"] = max(summary["max_result_chars"], result_chars(r))
                    if r.is_error:
                        summary["errors"].append(("get_tag_batch", text_of(r)[:300]))
                        time.sleep(3)
                        continue
                    t = text_of(r)
                    m = re.search(r"batch_id[\"': ]+(job_[a-z0-9]+)", t)
                    if not m:
                        summary["errors"].append(("tag batch has no batch_id", t[:300]))
                        time.sleep(3)
                        continue
                    summary["images_seen"] += len(images_of(r))
                    tags = tags_for(t)
                    s = await c.call_tool("save_tags", {"batch_id": m.group(1), "tags": tags})
                    summary["tag_batches"] += 1
                    if s.is_error:
                        summary["errors"].append(("save_tags", text_of(s)[:400]))
                    else:
                        summary["tags_saved"] += len(tags["tags"])
                    say(step="tag_batch", batch=m.group(1), reels=len(tags["tags"]), chars=result_chars(r),
                        images=len(images_of(r)), saved=not s.is_error)
                elif nxt == "get_reel_for_analysis":
                    r = await c.call_tool("get_reel_for_analysis", {})
                    summary["max_result_chars"] = max(summary["max_result_chars"], result_chars(r))
                    t = text_of(r)
                    m = re.search(r"reel_id (reel_[a-z0-9]+)", t)
                    if r.is_error or not m:
                        summary["errors"].append(("get_reel_for_analysis", t[:300]))
                        time.sleep(3)
                        continue
                    more = await c.call_tool("get_more_frames", {"reel_id": m.group(1), "start_s": 1, "end_s": 4,
                                                                 "count": 2})
                    s = await c.call_tool("save_analysis", {"analysis": w5.breakdown(m.group(1))})
                    summary["analyses"] += 0 if s.is_error else 1
                    if s.is_error:
                        summary["errors"].append(("save_analysis", text_of(s)[:400]))
                    say(step="analysis", reel=m.group(1), chars=result_chars(r), images=len(images_of(r)),
                        more_frames=len(images_of(more)), saved=not s.is_error)
                elif nxt == "get_output_brief":
                    oid = (pend.get("outputs") or [{}])[0].get("id") if isinstance(pend.get("outputs"), list) else None
                    r = await c.call_tool("get_output_brief", {"output_id": oid} if oid else {})
                    d = data_of(r)
                    if r.is_error:
                        summary["errors"].append(("get_output_brief", text_of(r)[:400]))
                        time.sleep(3)
                        continue
                    brief = d.get("brief") or json.dumps(d)
                    name = d.get("schema_name")
                    chunk = (d.get("chunk") or {}).get("index")
                    data = w5.fake_answer(name, brief)
                    args = {"output_id": d.get("output_id") or oid, "data": data}
                    if chunk is not None:
                        args["chunk_index"] = chunk
                    s = await c.call_tool("save_output", args)
                    summary["outputs"] += 0 if s.is_error else 1
                    if s.is_error:
                        summary["errors"].append(("save_output", text_of(s)[:600]))
                    say(step="output", output=d.get("output_id") or oid, schema=name, chunk=chunk,
                        saved=not s.is_error, reply=data_of(s) if not s.is_error else text_of(s)[:300])
                elif nxt == "get_next_task":
                    r = await c.call_tool("get_next_task", {})
                    d = data_of(r)
                    if r.is_error or not d.get("kind"):
                        summary["errors"].append(("get_next_task", text_of(r)[:300]))
                        time.sleep(3)
                        continue
                    kind, prompt, bid = d.get("kind"), d.get("brief") or "", d.get("batch_id")
                    if kind == "name_formats":
                        g = w5.fake_answer("FormatGuideBatch", prompt)["guides"]
                        s = await c.call_tool("save_format_names", {"batch_id": bid, "guides": g})
                    elif kind == "retag_topics":
                        g = w5.fake_answer("TopicRetagBatch", prompt)["tags"]
                        s = await c.call_tool("save_topic_retag", {"batch_id": bid, "tags": g})
                    elif kind == "draft_profile":
                        s = await c.call_tool("save_profile_draft", {"draft": w5.fake_answer("ProfileDraft", prompt)})
                    else:
                        s = await c.call_tool("save_topics", {"topics": w5.fake_answer("TopicList", prompt)})
                    summary["text_tasks"] += 0 if s.is_error else 1
                    if s.is_error:
                        summary["errors"].append((f"save {kind}", text_of(s)[:400]))
                    say(step="text_task", kind=kind, saved=not s.is_error)
                else:
                    summary["errors"].append(("unknown next_tool", nxt))
                    break
            fin = data_of(await c.call_tool("list_pending_work", {}))
            summary["pending_at_end"] = {k: v for k, v in fin.items() if k != "message"}
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--seconds", type=int, default=900)
    ap.add_argument("--idle-rounds", type=int, default=12)
    a = ap.parse_args()
    s = anyio.run(main_async, a)
    print("SUMMARY " + json.dumps(s, default=str), flush=True)
    return 0 if not s["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
