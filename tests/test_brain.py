"""ZGBrain against a local fake 0G Router (no network)."""
from __future__ import annotations

import base64
import io
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image

from brain import checkpoints
from brain.client import ZGBrain, get_brain
from brain.schemas import OUTPUT_SCHEMAS
from brain.zg_router import ZGRouter
from sim.schemas import CheckpointRequest

GOOD_H1 = {"is_hazard": True, "type": "accident", "severity": 0.8, "passable_for": ["drone"],
           "confidence": 0.9, "description": "crash"}


class FakeRouter:
    """Mimics router-api.0g.ai: OpenAI chat completion + x_0g_trace + X-Request-ID header."""

    def __init__(self):
        self.requests: list[dict] = []
        self.replies: list[str] = []          # contents to return, in order (last one repeats)
        self.delay_s = 0.0
        self.status = 200
        self.error_body = ""
        self.billing = {"total_cost": "123"}
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(n))
                srv.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
                if srv.delay_s:
                    time.sleep(srv.delay_s)
                rid = f"req-{uuid.uuid4().hex[:12]}"
                if srv.status != 200:
                    data = srv.error_body.encode()
                    self.send_response(srv.status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("X-Request-ID", rid)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                content = srv.replies.pop(0) if len(srv.replies) > 1 else (
                    srv.replies[0] if srv.replies else json.dumps(GOOD_H1))
                resp = {
                    "id": "chatcmpl-x", "object": "chat.completion", "created": 1,
                    "model": body.get("model"),
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": content}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    "x_0g_trace": {"request_id": rid, "tee_verified": True,
                                   "provider": "0xProvider", "billing": srv.billing},
                }
                data = json.dumps(resp).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Request-ID", rid)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def fake():
    f = FakeRouter()
    yield f
    f.close()


def make_brain(fake, timeout_s=3.0, max_calls=100):
    r = ZGRouter(base_url=fake.base_url, api_key="sk-test-0123456789abcdef", model="0gm-1.0-35b-a3b",
                 timeout_s=timeout_s)
    return ZGBrain(router=r, max_calls=max_calls)


def png_b64():
    b = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 10, 10)).save(b, format="PNG")
    return base64.b64encode(b.getvalue()).decode()


def req(cp="CP-H1", seq=3, **ctx):
    return CheckpointRequest(robot_id="R-07", operator="MinuteMart", robot_kind="rover",
                             checkpoint=cp, sim_time=31.5, episode_id="ep-1", seq=seq,
                             frame_png_b64=png_b64(),
                             context={"task": "deliver", "_mock_truth": {"type": "accident"},
                                      "location": {"edge_id": "e1", "_secret": 1}, **ctx})


def run(brain, r, timeout=20):
    return brain.submit(r).result(timeout=timeout)


def test_success_captures_request_id_trace_and_headers(fake):
    b = make_brain(fake)
    res = run(b, req())
    assert res.ok, res.error
    assert res.output == GOOD_H1
    hdr = fake.requests[0]["headers"]
    assert res.zg_request_id and res.zg_request_id.startswith("req-")
    assert res.zg_trace["request_id"] == res.zg_request_id
    assert res.zg_trace["_hc"]["x_request_id_header"] == res.zg_request_id
    assert res.zg_trace["_hc"]["provider"] == "0xProvider"
    assert res.zg_trace["_hc"]["billing"] == {"total_cost": "123"}
    assert res.tee_verified is True
    assert res.client_request_id == "ep-1:3"
    assert hdr["x-client-request-id"] == "ep-1:3"
    assert hdr["x-0g-provider-trust-mode"] == "private"
    assert hdr["authorization"] == "Bearer sk-test-0123456789abcdef"
    body = fake.requests[0]["body"]
    assert body["verify_tee"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["response_format"] == {"type": "json_object"}
    assert body["model"] == "0gm-1.0-35b-a3b"
    assert res.latency_ms is not None and b.online()


def test_private_keys_stripped_and_image_included(fake):
    b = make_brain(fake)
    run(b, req())
    raw = json.dumps(fake.requests[0]["body"])
    assert "_mock_truth" not in raw and "_secret" not in raw
    user = fake.requests[0]["body"]["messages"][1]["content"]
    assert "accident" not in user[0]["text"]       # the ground truth never leaks
    imgs = [c for c in user if c["type"] == "image_url"]
    assert len(imgs) == 1 and imgs[0]["image_url"]["url"].startswith("data:image/png;base64,iVBOR")
    assert '"edge_id": "e1"' in user[0]["text"]


def test_invalid_json_retries_once_then_fails(fake):
    fake.replies = ["not json at all", '{"is_hazard": "maybe"}']
    b = make_brain(fake)
    res = run(b, req())
    assert not res.ok and res.output is None
    assert "invalid output" in res.error
    assert len(fake.requests) == 2               # exactly one retry
    second = fake.requests[1]["body"]["messages"]
    assert second[-1]["role"] == "user" and "not valid" in second[-1]["content"]
    assert second[-2] == {"role": "assistant", "content": "not json at all"}
    assert fake.requests[1]["headers"]["x-client-request-id"] == "ep-1:3:retry1"
    assert len(res.zg_trace["_hc"]["attempts"]) == 2   # both 0G request ids are logged
    assert b.calls_used == 2


def test_invalid_then_valid_succeeds(fake):
    fake.replies = ["```json\n{oops", json.dumps(GOOD_H1)]
    res = run(make_brain(fake), req())
    assert res.ok and res.output["type"] == "accident" and len(fake.requests) == 2


def test_code_fence_and_think_tolerated(fake):
    fake.replies = ["<think>hmm</think>\n```json\n" + json.dumps(GOOD_H1) + "\n```"]
    res = run(make_brain(fake), req())
    assert res.ok and len(fake.requests) == 1


def test_slow_server_times_out(fake):
    fake.delay_s = 2.5
    b = make_brain(fake, timeout_s=0.8)
    t0 = time.time()
    res = run(b, req())
    assert not res.ok and "timeout" in res.error
    assert time.time() - t0 < 2.4
    assert not b.online()


def test_http_errors_fail(fake):
    fake.status, fake.error_body = 503, json.dumps({"error": {"code": "no_provider_for_trust_mode",
                                                              "message": "no TEE provider"}})
    b = make_brain(fake)
    res = run(b, req())
    assert not res.ok and "no_provider_for_trust_mode" in res.error
    assert len(fake.requests) == 1        # no silent retry / no trust-mode downgrade
    fake.status, fake.error_body = 401, '{"error":"bad key"}'
    res = run(b, req())
    assert not res.ok and "HTTP 401" in res.error


def test_set_unreachable_really_fails_and_restores(fake):
    b = make_brain(fake)
    b.set_unreachable(True)
    assert not b.online()
    res = run(b, req())
    assert not res.ok and ("unreachable" in res.error or "timeout" in res.error)
    assert fake.requests == []            # never reached the router
    b.set_unreachable(False)
    res = run(b, req())
    assert res.ok and b.online()


def test_budget_exceeded(fake):
    b = make_brain(fake, max_calls=2)
    assert run(b, req(seq=1)).ok
    assert run(b, req(seq=2)).ok
    res = run(b, req(seq=3))
    assert not res.ok and "budget" in res.error
    assert len(fake.requests) == 2


def test_submit_never_raises(fake):
    b = make_brain(fake)
    b.shutdown()
    res = b.submit(req()).result(timeout=5)
    assert not res.ok and "submit failed" in res.error


def test_all_checkpoints_build_and_examples_validate():
    for cp in OUTPUT_SCHEMAS:
        ex = json.loads(checkpoints.schema_example(cp))
        assert checkpoints.validate_output(cp, json.dumps(ex)) is not None
        msgs = checkpoints.build_messages(req(cp=cp))
        assert msgs[0]["role"] == "system" and "ONE JSON object" in msgs[0]["content"]
        assert re.search(r'"\w+":', msgs[0]["content"])


def test_h1_consistency_rule():
    bad = dict(GOOD_H1, is_hazard=False)
    with pytest.raises(ValueError):
        checkpoints.validate_output("CP-H1", json.dumps(bad))


def test_get_brain_refuses_mock_in_demo(monkeypatch):
    monkeypatch.setenv("MOCK_BRAIN", "1")
    monkeypatch.setenv("DEMO", "1")
    with pytest.raises(RuntimeError, match="forbidden"):
        get_brain()
    monkeypatch.setenv("DEMO", "0")
    from brain.mock import MockBrain
    assert isinstance(get_brain(), MockBrain)


def test_get_brain_requires_real_key(monkeypatch):
    monkeypatch.delenv("MOCK_BRAIN", raising=False)
    monkeypatch.setenv("ZG_ROUTER_API_KEY", "sk-...")
    with pytest.raises(RuntimeError, match="placeholder"):
        get_brain()
    monkeypatch.setenv("ZG_ROUTER_API_KEY", "sk-real-looking-key-1234567890")
    assert isinstance(get_brain(), ZGBrain)


def test_failed_calls_do_not_use_budget_and_breaker_opens(fake):
    b = make_brain(fake, max_calls=2)
    b.max_consecutive_failures, b.breaker_cooldown_s = 3, 60
    fake.status, fake.error_body = 502, '{"error":"bad gateway"}'
    for i in range(3):
        assert not run(b, req(seq=i)).ok
    assert b.calls_used == 0                      # 502s were refunded
    res = run(b, req(seq=10))
    assert not res.ok and "circuit breaker" in res.error
    n = len(fake.requests)
    run(b, req(seq=11))
    assert len(fake.requests) == n                # breaker open: no call reaches the router
    b.set_unreachable(True); b.set_unreachable(False)   # outage toggle resets it
    fake.status = 200
    assert run(b, req(seq=12)).ok


def test_bus_late_joiner_gets_history_then_state():
    import asyncio
    from server.bus import Bus
    bus = Bus()
    bus.publish("hazard", 1.0, {"id": 1}); bus.publish("state", 2.0, {"n": 1})
    bus.publish("decision", 3.0, {"id": 2}); bus.publish("state", 4.0, {"n": 2})

    async def go():
        snap, q = bus.subscribe_with_history()
        return [(e.topic, e.t) for e in snap]
    assert asyncio.run(go()) == [("hazard", 1.0), ("decision", 3.0), ("state", 4.0)]


def test_per_checkpoint_routes_use_their_own_model(fake):
    b = make_brain(fake)
    alt = ZGRouter(base_url=fake.base_url, api_key="sk-test-0123456789abcdef", model="glm-5.3",
                   timeout_s=3.0, trust_mode="private")
    b.set_routes({"CP-H2": alt})
    fake.replies = [json.dumps({"action": "reroute", "reason": "r", "trust_report": True}), json.dumps(GOOD_H1)]
    assert run(b, req(cp="CP-H2", seq=1)).ok
    assert fake.requests[-1]["body"]["model"] == "glm-5.3"
    assert run(b, req(cp="CP-H1", seq=2)).ok
    assert fake.requests[-1]["body"]["model"] == "0gm-1.0-35b-a3b"
    b.set_unreachable(True)
    assert not run(b, req(cp="CP-H2", seq=3)).ok          # outage toggle reaches routed checkpoints too


def test_outage_profile_routes_vision_to_non_tee_model(monkeypatch):
    from brain.client import build_profile_routes
    monkeypatch.setenv("ZG_ROUTER_API_KEY", "sk-test-0123456789abcdef")
    routes, notes = build_profile_routes("outage")
    assert set(routes) == {"CP-H1", "CP-V", "CP-H2"}
    assert routes["CP-H1"].trust_mode == "standard" and routes["CP-H1"].model == "qwen2.5-vl-72b-instruct"
    assert routes["CP-H2"].model == "glm-5.3" and routes["CP-H2"].trust_mode == "private"
    assert any("NO TEE" in n for n in notes)
    assert build_profile_routes("default") == ({}, [])
