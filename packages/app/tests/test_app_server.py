"""本机服务端（P3b）：安全检查、课程与资料接口、导入、学习、会话互斥、对话记录。"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import SAMPLES
from fakes import Brain

from examprep import cli
from examprep.ingest.pipeline import COURSES_DIR, ingest
from examprep.learner import lease
from examprep.learner.records import LearnerRecords
from examprep.server import create_app
from examprep.server.security import hostname
from examprep.server.sessions import SessionManager
from examprep.server.sse import frame, parse
from examprep.store import Database
from examprep_llm import Gateway
from examprep_llm.testing import FakeClock, FakeProvider

BASE = "http://127.0.0.1:8765"


def fake_gateway() -> Gateway:
    return Gateway(
        {"fake": FakeProvider(responder=Brain())},
        {r: ["fake/m"] for r in ("tutor", "grader", "verifier")},
        clock=FakeClock(),
    )


def blank_pdf(path: Path, pages: int = 2) -> None:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(200, 300)
    pdf.save(str(path))
    pdf.close()


@pytest.fixture
async def data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    db = Database.in_dir(d)
    await ingest(db, d, SAMPLES / "zh-data-structures")
    pdf_course = tmp_path / "pdf课"
    pdf_course.mkdir()
    blank_pdf(pdf_course / "lec1.pdf")
    (pdf_course / "第1章_引言.md").write_text("# 第1章 引言\n\n这门课讲图论。\n", encoding="utf-8")
    await ingest(db, d, pdf_course, name="pdf课")
    await db.close()
    return d


class Client:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)

    @property
    def manager(self) -> SessionManager:
        return self.app.state.manager

    async def sse(self, url: str, **kw: Any) -> list[tuple[str, Any]]:
        r = await self.http.post(url, **kw)
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("text/event-stream")
        return parse(r.text)

    async def course_id(self, name: str) -> str:
        rows = (await self.http.get("/api/courses")).json()
        return next(c["id"] for c in rows if c["name"] == name)


@pytest.fixture
async def client(data: Path, tmp_path: Path) -> AsyncIterator[Client]:
    app = create_app(data, gateway=fake_gateway(), web_dir=tmp_path / "no-web")
    async with app.router.lifespan_context(app):
        c = Client(app)
        yield c
        await c.http.aclose()


def kinds(events: list[tuple[str, Any]]) -> list[str]:
    return [k for k, _ in events]


# ---------------------------------------------------------------- 安全


async def test_host_and_origin_checks(client: Client) -> None:
    http = client.http
    assert (await http.get("/api/health")).json()["model_configured"] is True
    r = await http.get("/api/health", headers={"host": "evil.example:8765"})
    assert r.status_code == 403
    assert "本机" in r.json()["detail"]
    # 别的网站借浏览器发 POST：拒绝；本机 Vite 开发服务器（其他端口）：允许
    cid = await client.course_id("zh-data-structures")
    r = await http.post(f"/api/study/{cid}/cancel", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = await http.post(f"/api/study/{cid}/cancel", headers={"origin": "http://localhost:5173"})
    assert r.status_code == 200
    # GET 不改状态，带外部 Origin 也放行（浏览器的同源策略会挡住读取）
    assert (await http.get("/api/courses", headers={"origin": "https://evil.example"})).status_code == 200


def test_hostname_parsing() -> None:
    assert hostname("127.0.0.1:8765") == "127.0.0.1"
    assert hostname("[::1]:8765") == "::1"
    assert hostname("LocalHost") == "localhost"
    assert hostname("[::1") == ""  # 括号不配对
    assert hostname("") == ""


async def test_placeholder_page_and_settings(client: Client) -> None:
    r = await client.http.get("/")
    assert "服务已启动" in r.text
    s = (await client.http.get("/api/settings")).json()
    assert s["roles"]["tutor"] == ["fake/m"]
    assert s["compaction"]["auto"] is True
    assert "key" not in str(s).lower()


async def test_static_frontend_is_served(data: Path, tmp_path: Path) -> None:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>ExamPrep 前端</h1>", encoding="utf-8")
    app = create_app(data, gateway=fake_gateway(), web_dir=web)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http,
    ):
        assert "ExamPrep 前端" in (await http.get("/")).text
        assert (await http.get("/api/health")).status_code == 200  # 接口不被静态文件挡住


async def test_without_model_config(data: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "EXAMPREP_OLLAMA_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    app = create_app(data, web_dir=tmp_path / "no-web")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as http,
    ):
        assert (await http.get("/api/health")).json()["model_configured"] is False
        assert (await http.get("/api/settings")).json()["roles"] == {}
        rows = (await http.get("/api/courses")).json()
        assert len(rows) == 2  # 课程、检索不需要模型
        r = await http.post(f"/api/study/{rows[0]['id']}/open")
        assert r.status_code == 503
        assert "还没有配置模型" in r.json()["detail"]


# ---------------------------------------------------------------- 课程与资料


async def test_courses_detail_search_page(client: Client) -> None:
    http = client.http
    cid = await client.course_id("zh-data-structures")
    d = (await http.get(f"/api/courses/{cid}")).json()
    assert [c["title"] for c in d["chapters"]] == ["线性表", "栈和队列", "树与二叉树"]
    assert [c["questions"] for c in d["chapters"]] == [2, 6, 4]
    assert d["questions"] == 12
    assert len(d["materials"]) == 5
    assert (await http.get("/api/courses/nope")).status_code == 404

    hits = (await http.get(f"/api/courses/{cid}/search", params={"q": "循环队列 判满", "k": 2})).json()
    assert hits
    assert hits[0]["chapter"] == 2
    assert (await http.get(f"/api/courses/{cid}/search", params={"q": ""})).status_code == 422

    file, page = hits[0]["file"], hits[0]["page"]
    p = (await http.get(f"/api/courses/{cid}/page", params={"file": file, "page": page})).json()
    assert "循环队列" in p["text"]
    assert hits[0]["id"] in [e["id"] for e in p["evidence"]]
    assert p["renderable"] is False
    assert (await http.get(f"/api/courses/{cid}/page", params={"file": "x.md", "page": 1})).status_code == 404
    assert (await http.get(f"/api/courses/{cid}/page", params={"file": file, "page": 99})).status_code == 404


async def test_render_pdf_page_and_assets(client: Client, data: Path) -> None:
    http = client.http
    cid = await client.course_id("pdf课")
    r = await http.get(f"/api/courses/{cid}/render", params={"file": "lec1.pdf", "page": 2, "scale": 1})
    assert r.status_code == 200
    assert r.content.startswith(b"\x89PNG")
    again = await http.get(f"/api/courses/{cid}/render", params={"file": "lec1.pdf", "page": 2, "scale": 1})
    assert again.content == r.content  # 第二次直接读缓存
    bad = {"file": "lec1.pdf", "page": 3}
    assert (await http.get(f"/api/courses/{cid}/render", params=bad)).status_code == 404
    assert (
        await http.get(f"/api/courses/{cid}/render", params={"file": "第1章_引言.md", "page": 1})
    ).status_code == 400
    traversal = {"file": "../../data/examprep.db.pdf", "page": 1}
    assert (await http.get(f"/api/courses/{cid}/render", params=traversal)).status_code == 404
    assert (
        await http.get(f"/api/courses/{cid}/render", params={"file": "lec1.pdf", "page": 1, "scale": 9})
    ).status_code == 422

    figs = data / COURSES_DIR / cid / "figures"
    figs.mkdir(parents=True, exist_ok=True)
    (figs / "a.png").write_bytes(b"\x89PNG fake")
    assert (await http.get(f"/api/courses/{cid}/assets/figures/a.png")).content == b"\x89PNG fake"
    assert (await http.get(f"/api/courses/{cid}/assets/../../examprep.db")).status_code == 404
    assert (await http.get(f"/api/courses/{cid}/assets/figures/none.png")).status_code == 404


async def test_ingest_over_sse(client: Client, tmp_path: Path) -> None:
    src = tmp_path / "数据结构副本"
    shutil.copytree(SAMPLES / "zh-data-structures", src)
    events = await client.sse("/api/courses/ingest", json={"path": str(src), "name": "副本"})
    ks = kinds(events)
    assert ks[0] == "ingest_started"
    assert "file_done" in ks
    assert ks[-2:] == ["ingest_done", "end"]
    report = events[-2][1]["report"]
    assert (report["name"], report["questions"]) == ("副本", 12)
    assert (
        await client.http.post("/api/courses/ingest", json={"path": str(tmp_path / "无")})
    ).status_code == 400
    assert (
        await client.http.post("/api/courses/ingest", json={"path": str(src), "lang": "fr"})
    ).status_code == 422


async def test_ingest_conflicts(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    import examprep.server.app as app_mod
    from examprep.store.jobs import IngestBusyError

    cid = await client.course_id("zh-data-structures")
    await client.sse(f"/api/study/{cid}/open")
    live = client.manager.live(cid)
    assert live is not None
    async with live.lock:  # 正在讲解
        r = await client.http.post("/api/courses/ingest", json={"path": str(SAMPLES / "zh-data-structures")})
        assert r.status_code == 409

    # 不在讲解：导入前先关掉会话、释放租约
    async def busy(*_a: Any, **_k: Any) -> None:
        raise IngestBusyError("课程正在导入中")

    monkeypatch.setattr(app_mod, "ingest", busy)
    r = await client.http.post("/api/courses/ingest", json={"path": str(SAMPLES / "zh-data-structures")})
    assert r.status_code == 409
    assert client.manager.live(cid) is None

    async def broken(*_a: Any, **_k: Any) -> None:
        raise OSError("磁盘满了")

    monkeypatch.setattr(app_mod, "ingest", broken)
    r = await client.http.post("/api/courses/ingest", json={"path": str(SAMPLES / "zh-data-structures")})
    assert r.status_code == 400
    assert "磁盘满了" in r.json()["detail"]

    async def midway(*_a: Any, on_event: Any, **_k: Any) -> None:
        from examprep.ingest.events import IngestStarted

        on_event(IngestStarted(course_id="c", name="n", files=1, new=1, changed=0, unchanged=0, removed=0))
        raise OSError("读到一半坏了")

    monkeypatch.setattr(app_mod, "ingest", midway)
    events = parse((await client.http.post("/api/courses/ingest", json={"path": str(SAMPLES)})).text)
    assert kinds(events) == ["ingest_started", "error"]
    assert "读到一半坏了" in events[1][1]["detail"]


# ---------------------------------------------------------------- 学习


async def test_study_flow_transcript_and_state(client: Client) -> None:
    http = client.http
    cid = await client.course_id("zh-data-structures")
    assert (await http.get(f"/api/study/{cid}/state")).json() == {"open": False, "busy": False}
    assert (await http.get(f"/api/study/{cid}/transcript")).json() == []  # 没学过，也不会因此建选课记录

    opened = await client.sse(f"/api/study/{cid}/open")
    assert kinds(opened)[0] == "progress"
    assert kinds(opened)[-2:] == ["suggestions", "end"]

    events = await client.sse(f"/api/study/{cid}/message", json={"text": "继续"})
    ks = kinds(events)
    assert "tutor_delta" in ks
    assert "tutor_message" in ks
    teach = next(d for k, d in events if k == "tutor_message")
    assert "第1章_线性表.md p.1" in teach["text"]

    st = (await http.get(f"/api/study/{cid}/state")).json()
    assert (st["open"], st["busy"], st["chapter"], st["state"], st["awaiting"]) == (
        True,
        False,
        1,
        "CHECK",
        "check",
    )
    assert 0 < st["context_tokens"] < st["compact_limit"]

    ui = (await http.get(f"/api/study/{cid}/transcript")).json()
    assert ui[0] == {"seq": ui[0]["seq"], "role": "student", "text": "继续"}
    agent = [x["event"]["type"] for x in ui if x["role"] == "agent"]
    assert "tutor_message" in agent
    assert "tutor_delta" not in agent  # 流式片段不进对话记录
    assert "progress" not in agent  # 打开会话的欢迎信息不进对话记录
    assert len((await http.get(f"/api/study/{cid}/transcript", params={"limit": 2})).json()) == 2

    compact = await client.sse(f"/api/study/{cid}/compact")
    assert "compacted" in kinds(compact)
    assert (await http.post(f"/api/study/{cid}/message", json={"text": ""})).status_code == 422


async def test_busy_turn_and_cancel(client: Client) -> None:
    cid = await client.course_id("zh-data-structures")
    await client.sse(f"/api/study/{cid}/open")
    live = client.manager.live(cid)
    assert live is not None
    assert (await client.http.post(f"/api/study/{cid}/cancel")).json() == {"cancelled": False}
    async with live.lock:
        r = await client.http.post(f"/api/study/{cid}/message", json={"text": "继续"})
        assert r.status_code == 409
        assert "还在处理" in r.json()["detail"]
        assert (await client.http.get(f"/api/study/{cid}/state")).json()["busy"] is True
        assert (await client.http.post(f"/api/study/{cid}/cancel")).json() == {"cancelled": True}
        assert live.session.cancel.cancelled


async def test_lease_held_elsewhere_returns_423(client: Client, data: Path) -> None:
    cid = await client.course_id("zh-data-structures")
    db = Database.in_dir(data)
    enr = await LearnerRecords(db).enrollment(cid)
    assert await lease.acquire(db, enr.id, "命令行窗口")
    r = await client.http.post(f"/api/study/{cid}/open")
    assert r.status_code == 423
    assert "另一个窗口" in r.json()["detail"]
    await lease.release(db, enr.id, "命令行窗口")
    await db.close()
    assert (await client.http.post(f"/api/study/{cid}/open")).status_code == 200


async def test_turn_survives_disconnect(client: Client) -> None:
    """浏览器断开：只读了第一个事件就不读了，这一轮照样跑完、写进对话记录。"""
    cid = await client.course_id("zh-data-structures")
    m = client.manager
    events = await m.run(cid, "message", "继续")
    first = await anext(events)
    assert first.type == "state" or first.type in ("figures", "tutor_delta")
    await events.aclose()
    live = m.live(cid)
    assert live is not None
    for _ in range(100):
        if not live.busy:
            break
        await asyncio.sleep(0.01)
    assert not live.busy
    ui = await m.transcript(cid)
    assert any(x["role"] == "agent" and x["event"]["type"] == "tutor_message" for x in ui)


async def test_turn_error_becomes_notice(client: Client, monkeypatch: pytest.MonkeyPatch) -> None:
    cid = await client.course_id("zh-data-structures")
    await client.sse(f"/api/study/{cid}/open")
    live = client.manager.live(cid)
    assert live is not None

    async def boom(_text: str) -> AsyncIterator[Any]:
        raise RuntimeError("意外")
        yield  # pragma: no cover

    monkeypatch.setattr(live.session, "handle", boom)
    events = await client.sse(f"/api/study/{cid}/message", json={"text": "继续"})
    assert events[0][0] == "notice"
    assert "意外" in events[0][1]["text"]
    assert not live.busy  # 出错后锁已释放


async def test_manager_reap_lost_lease_and_shutdown(data: Path) -> None:
    from examprep.settings import SESSIONS_DB
    from examprep_runtime.session import SQLiteEventStore

    db = Database.in_dir(data)
    store = SQLiteEventStore(data / SESSIONS_DB)
    now = [0.0]
    gw = fake_gateway()
    m = SessionManager(db, store, lambda: gw, owner="web", idle_seconds=60, clock=lambda: now[0])
    rows = await LearnerRecords(db).enrollment((await _first_course(db)).id)
    cid = rows.course_id
    live = await m.get(cid)
    assert await m.get(cid) is live
    assert await lease.holder(db, rows.id) is not None
    assert await m.reap_idle() == []
    now[0] = 61.0
    assert await m.reap_idle() == [cid]
    assert m.live(cid) is None
    assert await lease.holder(db, rows.id) is None

    live = await m.get(cid)
    live.lease.lost = True  # 被别的进程接手过
    again = await m.get(cid)
    assert again is not live
    await m.shutdown()
    assert await lease.holder(db, rows.id) is None
    assert await m.transcript("nope") == []
    await store.close()
    await db.close()


async def _first_course(db: Database) -> Any:
    from examprep.catalog import list_courses

    return (await list_courses(db))[0]


# ---------------------------------------------------------------- SSE 与命令行


def test_sse_frames_roundtrip() -> None:
    raw = frame("tutor_delta", '{"text": "a"}') + frame("note", '{"text":\n"多行"}')
    assert parse(raw.decode()) == [("tutor_delta", {"text": "a"}), ("note", {"text": "多行"})]


def test_serve_command(
    data: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io

    import uvicorn

    seen: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw, app=app))
    out = io.StringIO()
    assert cli.main(["--data-dir", str(data), "serve", "--port", "9000"], out=out) == 0
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == 9000
    assert "http://127.0.0.1:9000" in out.getvalue()
    out = io.StringIO()
    assert cli.main(["--data-dir", str(data), "serve", "--host", "192.168.1.5"], out=out) == 0
    assert "没有登录鉴权" in out.getvalue()
    assert cli.main(["--data-dir", str(data), "serve", "--host", "0.0.0.0"], out=io.StringIO()) == 2
    assert "具体的地址" in capsys.readouterr().err


def test_study_command_refuses_when_lease_held(data: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import io

    async def hold() -> None:
        db = Database.in_dir(data)
        cid = (await _first_course(db)).id
        enr = await LearnerRecords(db).enrollment(cid)
        assert await lease.acquire(db, enr.id, "网页服务")
        await db.close()

    asyncio.run(hold())
    rc = cli.main(
        ["--data-dir", str(data), "study", "zh-data-structures"],
        out=io.StringIO(),
        read_line=lambda: "",
        gateway=fake_gateway(),
    )
    assert rc == 3
    assert "另一个窗口或网页" in capsys.readouterr().err
