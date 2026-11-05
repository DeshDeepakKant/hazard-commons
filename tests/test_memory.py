"""EpisodeLog hash chain, batching, index mirror and storage hand-off (no network)."""
from __future__ import annotations

import base64
import io
import json
from concurrent.futures import Future

from PIL import Image

from memory.episode_log import EpisodeLog, record_from_result, record_hash, verify_chain
from memory.zg_storage import ZGStorage, _last_error
from sim.schemas import CheckpointRequest, CheckpointResult, EpisodeRecord, StorageEvent


class StubStorage:
    def __init__(self, fail=False):
        self.uploaded: list[str] = []
        self.fail = fail

    def upload_file(self, local_path):
        self.uploaded.append(local_path)
        f: Future = Future()
        if self.fail:
            f.set_exception(RuntimeError("insufficient funds"))
        else:
            f.set_result({"root_hash": "0x" + "ab" * 32, "tx_hash": "0x" + "cd" * 32})
        return f


def png_b64(c=(0, 0, 255)):
    b = io.BytesIO()
    Image.new("RGB", (4, 4), c).save(b, format="PNG")
    return base64.b64encode(b.getvalue()).decode()


def rec(i, robot="R-07"):
    return EpisodeRecord(episode_id="ep-1", robot_id=robot, operator="MinuteMart", sim_time=float(i),
                         checkpoint="CP-H1", seq=i, inputs={"context": {"task": "deliver"}},
                         model="0gm-1.0-35b-a3b", zg_request_id=f"req-{i}",
                         output={"is_hazard": False}, action_taken="proceed")


def make_log(tmp_path, storage=None, n=3, events=None):
    return EpisodeLog("run-test", storage=storage, base_dir=tmp_path, flush_every=n,
                      on_uploaded=(events.append if events is not None else None))


def test_chain_links_and_hashes(tmp_path):
    log = make_log(tmp_path, n=100)
    a = log.append(rec(1))
    b = log.append(rec(2))
    assert a.prev_record_sha256 is None and a.record_sha256 == record_hash(a)
    assert b.prev_record_sha256 == a.record_sha256
    other = log.append(rec(1, robot="R-01"))
    assert other.prev_record_sha256 is None       # per-robot chains


def test_flush_uploads_writes_index_and_event(tmp_path):
    st, events = StubStorage(), []
    log = make_log(tmp_path, storage=st, n=3, events=events)
    for i in range(3):
        log.append(rec(i), frame_b64=png_b64((i, 0, 0)))
    assert len(st.uploaded) == 1                  # auto-flush at N records
    entry = log.futures[0].result(timeout=5)
    assert entry["status"] == "uploaded" and entry["root_hash"].startswith("0xabab")
    assert entry["zg_request_ids"] == ["req-0", "req-1", "req-2"]
    idx = json.loads((tmp_path / "logs" / "index.json").read_text())
    assert idx["batches"][0]["root_hash"] == entry["root_hash"]
    assert len(idx["batches"][0]["record_sha256s"]) == 3
    assert len(events) == 1 and isinstance(events[0], StorageEvent)
    assert events[0].record_seqs == [0, 1, 2]
    doc = json.loads(open(st.uploaded[0]).read())
    assert len(doc["frames"]) == 3 and doc["records"][0]["inputs"]["frame_sha256"] in doc["frames"]
    assert verify_chain(st.uploaded[0])["ok"]
    assert log.flush("R-07") is None              # nothing pending


def test_manual_flush_and_cross_batch_chain(tmp_path):
    st = StubStorage()
    log = make_log(tmp_path, storage=st, n=100)
    for i in range(2):
        log.append(rec(i))
    log.flush("R-07").result(timeout=5)
    for i in range(2, 5):
        log.append(rec(i))
    log.flush("R-07").result(timeout=5)
    d = tmp_path / "logs/runs/run-test/episodes/R-07"
    rep = verify_chain(d)
    assert rep["ok"] and rep["records"] == 5 and rep["batches"] == 2, rep


def test_tamper_detection(tmp_path):
    log = make_log(tmp_path, n=100)
    for i in range(3):
        log.append(rec(i), frame_b64=png_b64((i, 9, 9)))
    log.flush("R-07")
    p = tmp_path / "logs/runs/run-test/episodes/R-07/batch-0.json"
    assert verify_chain(p)["ok"]
    original = p.read_text()

    doc = json.loads(original)
    doc["records"][1]["output"] = {"is_hazard": True}          # edit a decision
    p.write_text(json.dumps(doc))
    rep = verify_chain(p)
    assert not rep["ok"] and any("modified" in e for e in rep["errors"])

    doc = json.loads(original)
    del doc["records"][1]                                      # drop a record
    p.write_text(json.dumps(doc))
    assert not verify_chain(p)["ok"]

    doc = json.loads(original)
    k = next(iter(doc["frames"]))
    doc["frames"][k] = png_b64((255, 255, 255))                # swap an evidence frame
    p.write_text(json.dumps(doc))
    rep = verify_chain(p)
    assert not rep["ok"] and any("frame" in e for e in rep["errors"])


def test_failed_upload_is_recorded_not_hidden(tmp_path):
    log = make_log(tmp_path, storage=StubStorage(fail=True), n=1)
    log.append(rec(0))
    fut = log.futures[0]
    try:
        fut.result(timeout=5)
        raise AssertionError("should fail")
    except RuntimeError:
        pass
    idx = json.loads((tmp_path / "logs" / "index.json").read_text())
    assert idx["batches"][0]["status"] == "failed" and "insufficient" in idx["batches"][0]["error"]


def test_record_from_result_strips_private_and_hashes_frame():
    img = png_b64()
    r = CheckpointRequest(robot_id="R-07", operator="MinuteMart", robot_kind="rover",
                          checkpoint="CP-V", sim_time=1.0, episode_id="ep", seq=0,
                          frame_png_b64=img, context={"claim": {"type": "road_works"},
                                                      "_mock_truth": {"real": False}})
    res = CheckpointResult(ok=False, checkpoint="CP-V", robot_id="R-07", error="0G timeout")
    e = record_from_result(r, res, action_taken="SAFE_STOP")
    assert "_mock_truth" not in e.inputs["context"] and e.inputs["error"] == "0G timeout"
    assert len(e.inputs["frame_sha256"]) == 64


def test_zg_storage_with_fake_sidecar(tmp_path):
    (tmp_path / "upload.mjs").write_text(
        'console.error("chatty sdk log");\n'
        'if (process.argv[2].endsWith("bad.json")) { console.error("upload failed: insufficient funds"); process.exit(1); }\n'
        'process.stdout.write(JSON.stringify({root_hash: "0x01", tx_hash: "0x02"}) + "\\n");\n')
    good, bad = tmp_path / "good.json", tmp_path / "bad.json"
    good.write_text("{}")
    bad.write_text("{}")
    s = ZGStorage(sidecar_dir=tmp_path, timeout_s=30, retries=1)
    res = s.upload_file(str(good)).result(timeout=30)
    assert res["root_hash"] == "0x01" and res["tx_hash"] == "0x02" and res["attempts"] == 1
    try:
        s.upload_file(str(bad)).result(timeout=30)
        raise AssertionError("should fail")
    except RuntimeError as e:
        assert "insufficient funds" in str(e) and "2 attempts" in str(e)


def test_last_error_picks_sidecar_line():
    assert _last_error("a\nupload failed: boom\nstack line") == "upload failed: boom"
