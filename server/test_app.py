"""서버 시험. 네트워크 없이 도는 시험(가짜 yt-dlp)과, 직접 켜서 보는 실제 변환 시험(LIVE_URL)이 있다.

  cd server
  python -m pytest -q                         # 가짜 yt-dlp 로 비밀번호·형식·정리 시험
  LIVE_URL=https://www.youtube.com/watch?v=... python -m pytest -q -k live   # 실제 변환(저작권이 허락된 영상만)
"""
import importlib
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))


def load(monkeypatch, **env):
    for k in ("ACCESS_CODE", "ALLOWED_EMAILS", "MAX_SECONDS", "DIAG_ENABLED", "COOKIES_FILE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import app as mod

    importlib.reload(mod)
    mod._fails.clear()
    return mod, TestClient(mod.app)


H = {"X-Access-Code": "tst1234"}
YT = "https://www.youtube.com/watch?v=abc"


class FakeYDL:
    """가짜 yt-dlp: 파일을 실제로 하나 만들어 준다."""

    dur = 60

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return {"id": "abc", "title": "시험: 노래/제목?", "thumbnail": "t.jpg", "uploader": "u", "duration": self.dur,
                "formats": [{"height": 360, "vcodec": "avc1"}, {"height": 720, "vcodec": "avc1"}, {"height": 1080, "vcodec": "vp9"}, {"height": None, "vcodec": "none"}]}

    def process_ie_result(self, d, download=True):
        fmt = "mp3" if "postprocessors" in self.opts else "mp4"
        with open(self.opts["outtmpl"].replace("%(id)s", "abc").replace("%(ext)s", fmt), "wb") as f:
            f.write(b"x" * 100)


def test_health_open(monkeypatch):
    _, c = load(monkeypatch, ACCESS_CODE="tst1234")
    assert c.get("/health").json() == {"ok": True}


def test_no_code_configured_refuses(monkeypatch):
    _, c = load(monkeypatch)
    assert c.post("/check", headers=H).status_code == 503


def test_code_required(monkeypatch):
    _, c = load(monkeypatch, ACCESS_CODE="tst1234")
    assert c.post("/check").status_code == 403
    assert c.post("/check", headers={"X-Access-Code": "0000"}).status_code == 403
    assert c.post("/check", headers=H).status_code == 200


def test_block_after_five_wrong(monkeypatch):
    _, c = load(monkeypatch, ACCESS_CODE="tst1234")
    for _ in range(5):
        assert c.post("/check", headers={"X-Access-Code": "9999"}).status_code == 403
    # 한도를 넘으면 맞는 번호도 잠시 막힌다
    assert c.post("/check", headers=H).status_code == 429


def test_success_resets_counter(monkeypatch):
    _, c = load(monkeypatch, ACCESS_CODE="tst1234")
    for _ in range(4):
        c.post("/check", headers={"X-Access-Code": "9999"})
    assert c.post("/check", headers=H).status_code == 200
    for _ in range(4):
        assert c.post("/check", headers={"X-Access-Code": "9999"}).status_code == 403


def test_only_youtube(monkeypatch):
    _, c = load(monkeypatch, ACCESS_CODE="tst1234")
    for bad in ("http://example.com/a", "file:///etc/passwd", "https://youtube.com.evil.com/x", "ftp://youtube.com/x", ""):
        assert c.post("/info", headers=H, json={"url": bad}).status_code == 400, bad


def test_convert_needs_code(monkeypatch):
    _, c = load(monkeypatch, ACCESS_CODE="tst1234")
    assert c.post("/convert", json={"url": YT, "format": "mp3"}).status_code == 403


def test_bad_format(monkeypatch):
    _, c = load(monkeypatch, ACCESS_CODE="tst1234")
    assert c.post("/convert", headers=H, json={"url": YT, "format": "exe"}).status_code == 400


@pytest.mark.parametrize("fmt,ctype", [("mp3", "audio/mpeg"), ("mp4", "video/mp4")])
def test_convert_download(monkeypatch, fmt, ctype):
    mod, c = load(monkeypatch, ACCESS_CODE="tst1234")
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", FakeYDL)
    r = c.post("/convert", headers=H, json={"url": YT, "format": fmt})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(ctype)
    assert "attachment" in r.headers["content-disposition"]
    assert fmt in r.headers["content-disposition"]
    assert len(r.content) == 100
    # 끝난 뒤 다음 변환이 가능해야 한다(자리 반납)
    assert c.post("/convert", headers=H, json={"url": YT, "format": fmt}).status_code == 200


def test_too_long(monkeypatch):
    mod, c = load(monkeypatch, ACCESS_CODE="tst1234", MAX_SECONDS="30")
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", FakeYDL)
    assert c.post("/info", headers=H, json={"url": YT}).json()["tooLong"] is True
    assert c.post("/convert", headers=H, json={"url": YT, "format": "mp3"}).status_code == 400
    # 실패해도 자리를 반납해서 다음 요청이 막히지 않아야 한다
    assert c.post("/convert", headers=H, json={"url": YT, "format": "mp3"}).status_code == 400


def test_info_quality_options(monkeypatch):
    mod, c = load(monkeypatch, ACCESS_CODE="tst1234")
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", FakeYDL)
    d = c.post("/info", headers=H, json={"url": YT}).json()
    assert d["mp3Rates"] == ["128", "192", "256", "320"]
    assert d["mp4Heights"] == [240, 360, 480, 720]  # 서버 한도 720 이하, 영상 최고 화질(1080)보다 낮은 표준 화질만


def test_quality_validation(monkeypatch):
    mod, c = load(monkeypatch, ACCESS_CODE="tst1234")
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", FakeYDL)
    post = lambda **kw: c.post("/convert", headers=H, json={"url": YT, **kw}).status_code
    assert post(format="mp3", quality="320") == 200
    assert post(format="mp3", quality="999") == 400
    assert post(format="mp4", quality="360") == 200
    assert post(format="mp4", quality="1080") == 400   # 서버 한도(기본 720) 초과
    assert post(format="mp4", quality="abc") == 400
    assert post(format="mp4", quality="500") == 400


def test_quality_reaches_ytdlp(monkeypatch):
    mod, c = load(monkeypatch, ACCESS_CODE="tst1234")
    seen = {}

    class Spy(FakeYDL):
        def __init__(self, opts):
            super().__init__(opts)
            if "outtmpl" in opts:
                seen.update(opts)

    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", Spy)
    c.post("/convert", headers=H, json={"url": YT, "format": "mp3", "quality": "320"})
    assert seen["postprocessors"][0]["preferredquality"] == "320"
    seen.clear()
    c.post("/convert", headers=H, json={"url": YT, "format": "mp4", "quality": "480"})
    assert "height<=480" in seen["format"]


class SearchYDL(FakeYDL):
    def extract_info(self, url, download=False):
        assert url.startswith("ytsearch8:")
        return {"entries": [
            {"id": "aaaaaaaaaaa", "title": "첫째", "uploader": "u1", "duration": 10},
            {"id": "bad id", "title": "버림"},
            {"id": None, "title": "버림2"},
            {"id": "bbbbbbbbbbb", "title": "둘째", "channel": "c2", "duration": 20},
        ]}


def test_search(monkeypatch):
    mod, c = load(monkeypatch, ACCESS_CODE="tst1234")
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", SearchYDL)
    assert c.post("/search", json={"q": "노래"}).status_code == 403          # 비밀번호 필요
    r = c.post("/search", headers=H, json={"q": "  노래   제목 "})
    assert r.status_code == 200
    res = r.json()["results"]
    assert [x["title"] for x in res] == ["첫째", "둘째"]                      # 잘못된 id 는 걸러짐
    assert res[0]["url"] == "https://www.youtube.com/watch?v=aaaaaaaaaaa"
    assert res[1]["uploader"] == "c2"
    assert c.post("/search", headers=H, json={"q": "   "}).status_code == 400
    assert c.post("/search", headers=H, json={"q": "가" * 101}).status_code == 400


class BotBlockedYDL(FakeYDL):
    """기본 접속 방식은 '로봇이 아님을 확인' 으로 막히고, tv_simply 로는 통한다."""

    def __init__(self, opts):
        super().__init__(opts)
        ea = (opts.get("extractor_args") or {}).get("youtube", {})
        self.clients = ea.get("player_client")

    def extract_info(self, url, download=False):
        if self.clients != ["tv_simply"]:
            raise yt_dlp_error("ERROR: [youtube] abc: Sign in to confirm you're not a bot.")
        return super().extract_info(url, download)


def yt_dlp_error(msg):
    import yt_dlp
    return yt_dlp.utils.DownloadError(msg)


def test_fallback_to_working_client(monkeypatch):
    mod, c = load(monkeypatch, ACCESS_CODE="tst1234")
    monkeypatch.setattr(mod, "_good_strategy", None)
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", BotBlockedYDL)
    r = c.post("/info", headers=H, json={"url": YT})
    assert r.status_code == 200, r.text
    assert mod._good_strategy == ["tv_simply"]          # 통한 방식을 기억한다
    r = c.post("/convert", headers=H, json={"url": YT, "format": "mp3"})
    assert r.status_code == 200, r.text


def test_non_block_error_is_not_retried(monkeypatch):
    mod, c = load(monkeypatch, ACCESS_CODE="tst1234")
    monkeypatch.setattr(mod, "_good_strategy", None)
    calls = []

    class Private(FakeYDL):
        def extract_info(self, url, download=False):
            calls.append(1)
            raise yt_dlp_error("ERROR: [youtube] abc: Private video")

    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", Private)
    r = c.post("/info", headers=H, json={"url": YT})
    assert r.status_code == 400 and len(calls) == 1      # 막힘이 아닌 오류는 방식을 바꿔 다시 시도하지 않는다


def test_all_blocked_message(monkeypatch):
    mod, c = load(monkeypatch, ACCESS_CODE="tst1234")
    monkeypatch.setattr(mod, "_good_strategy", None)

    class Blocked(FakeYDL):
        def extract_info(self, url, download=False):
            raise yt_dlp_error("ERROR: Sign in to confirm you're not a bot")

    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", Blocked)
    r = c.post("/info", headers=H, json={"url": YT})
    assert r.status_code == 400 and "bot" in r.json()["detail"]


def test_diag_off_by_default(monkeypatch):
    _, c = load(monkeypatch, ACCESS_CODE="tst1234")
    assert c.get("/diag").status_code == 404


def test_diag_on(monkeypatch):
    mod, c = load(monkeypatch, ACCESS_CODE="tst1234", DIAG_ENABLED="1")
    monkeypatch.setattr(mod, "_diag_last", 0.0)
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", BotBlockedYDL)
    r = c.get("/diag")
    assert r.status_code == 200
    res = {x["client"]: x["ok"] for x in r.json()["results"]}
    assert res["tv_simply"] is True and res["기본"] is False
    assert c.get("/diag").status_code == 429              # 20초 제한


def test_busy(monkeypatch):
    mod, c = load(monkeypatch, ACCESS_CODE="tst1234")
    monkeypatch.setattr(mod.yt_dlp, "YoutubeDL", FakeYDL)
    assert mod._job.acquire(blocking=False)
    try:
        assert c.post("/convert", headers=H, json={"url": YT, "format": "mp3"}).status_code == 429
    finally:
        mod._job.release()


def test_google_required_when_configured(monkeypatch):
    mod, c = load(monkeypatch, ACCESS_CODE="tst1234", ALLOWED_EMAILS="a@example.com")
    assert c.post("/check", headers=H).status_code == 401  # 로그인 없음
    # firebase_admin 을 가짜로 바꿔 허용/비허용 계정을 시험
    import types

    fa = types.ModuleType("firebase_admin")
    fa._apps = {}
    fa.initialize_app = lambda options=None: fa._apps.update({"x": 1})
    au = types.ModuleType("firebase_admin.auth")
    tokens = {
        "good": {"email": "a@example.com", "email_verified": True},
        "other": {"email": "b@example.com", "email_verified": True},
        "unverified": {"email": "a@example.com", "email_verified": False},
    }

    def verify(t):
        if t not in tokens:
            raise ValueError("bad token")
        return tokens[t]

    au.verify_id_token = verify
    fa.auth = au
    monkeypatch.setitem(sys.modules, "firebase_admin", fa)
    monkeypatch.setitem(sys.modules, "firebase_admin.auth", au)

    def hh(tok):
        return {**H, "Authorization": "Bearer " + tok}

    assert c.post("/check", headers=hh("good")).status_code == 200
    assert c.post("/check", headers=hh("other")).status_code == 403
    assert c.post("/check", headers=hh("unverified")).status_code == 403
    assert c.post("/check", headers=hh("nope")).status_code == 401


def test_cors_origin(monkeypatch):
    _, c = load(monkeypatch, ACCESS_CODE="tst1234")
    r = c.options("/check", headers={"Origin": "https://happymp3.web.app", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-access-code,content-type"})
    assert r.headers.get("access-control-allow-origin") == "https://happymp3.web.app"
    r2 = c.options("/check", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert r2.headers.get("access-control-allow-origin") is None


@pytest.mark.skipif(not os.environ.get("LIVE_URL"), reason="LIVE_URL 이 없으면 건너뜀")
def test_live_search(monkeypatch):
    _, c = load(monkeypatch, ACCESS_CODE="tst1234", FFMPEG_LOCATION=os.environ.get("FFMPEG_LOCATION", ""))
    r = c.post("/search", headers=H, json={"q": "Big Buck Bunny"})
    assert r.status_code == 200, r.text
    assert len(r.json()["results"]) >= 3


@pytest.mark.skipif(not os.environ.get("LIVE_URL"), reason="LIVE_URL 이 없으면 건너뜀")
@pytest.mark.parametrize("fmt", ["mp3", "mp4"])
def test_live(monkeypatch, fmt):
    _, c = load(monkeypatch, ACCESS_CODE="tst1234", MAX_HEIGHT="360", FFMPEG_LOCATION=os.environ.get("FFMPEG_LOCATION", ""))
    r = c.post("/info", headers=H, json={"url": os.environ["LIVE_URL"]})
    assert r.status_code == 200, r.text
    r = c.post("/convert", headers=H, json={"url": os.environ["LIVE_URL"], "format": fmt})
    assert r.status_code == 200, r.text
    assert len(r.content) > 10000
    head = r.content[:12]
    if fmt == "mp3":
        assert head[:3] == b"ID3" or head[:2] == b"\xff\xfb" or head[:2] == b"\xff\xf3"
    else:
        assert b"ftyp" in head
