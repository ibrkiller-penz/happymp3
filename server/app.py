"""HappyMpX 서버: 유튜브 링크를 MP3 또는 MP4 파일로 바꿔 내려받게 해 준다.

입장 비밀번호(ACCESS_CODE)를 서버가 직접 확인한다. 틀린 시도가 5번 쌓이면 그 주소를 10분 동안 막는다.
ALLOWED_EMAILS 를 정하면 구글 로그인 확인도 함께 한다(선택).

환경변수
  ACCESS_CODE        입장 비밀번호(필수). 비어 있으면 서버가 모든 요청을 거절한다.
  ALLOWED_ORIGINS    CORS 허용 주소, 쉼표로 구분 (기본: https://happymp3.web.app,http://localhost:5000)
  MAX_SECONDS        변환할 영상의 최대 길이(초, 기본 1800)
  MAX_HEIGHT         MP4 최대 세로 해상도(기본 720)
  ALLOWED_EMAILS     (선택) 쉼표로 구분한 구글 이메일. 정하면 Authorization: Bearer <ID 토큰> 도 필요
  FIREBASE_PROJECT_ID (선택) ID 토큰 확인용 프로젝트(기본 happymp3)
  FFMPEG_LOCATION    (선택) ffmpeg 폴더 또는 실행 파일 경로
  COOKIES_FILE       (선택) 유튜브 쿠키 파일 경로(Render 의 Secret File). 서버 주소가 막힐 때의 마지막 수단
  DIAG_ENABLED       1 이면 /diag(접속 방식 진단)를 연다. 확인이 끝나면 끈다
"""
import hmac
import os
import re
import shutil
import tempfile
import threading
import time
from urllib.parse import urlparse

import yt_dlp
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

ACCESS_CODE = os.environ.get("ACCESS_CODE", "")
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "https://happymp3.web.app,http://localhost:5000").split(",") if o.strip()]
MAX_SECONDS = int(os.environ.get("MAX_SECONDS", "1800"))
MAX_HEIGHT = int(os.environ.get("MAX_HEIGHT", "720"))
ALLOWED_EMAILS = [e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()]
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "happymp3")
FFMPEG_LOCATION = os.environ.get("FFMPEG_LOCATION", "")
COOKIES_FILE = os.environ.get("COOKIES_FILE", "")
DIAG_ENABLED = os.environ.get("DIAG_ENABLED", "") == "1"
DIAG_URL = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"  # 저작권이 허락된 영상(Blender 재단, CC-BY)

# 유튜브 주소만 받는다(다른 주소로 서버를 이용하는 것을 막는다)
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}

MP3_RATES = ("128", "192", "256", "320")
MP4_HEIGHTS = (240, 360, 480, 720, 1080)

# 유튜브는 클라우드 서버 주소를 "로봇이 아닌지 확인하세요"로 막는 일이 많다. 접속 방식(클라이언트)에 따라 막힘이 다르므로
# 막히면 다음 방식으로 차례로 시도하고, 통한 방식은 기억해서 다음에 먼저 쓴다.
CLIENT_STRATEGIES = [None, ["tv_simply"], ["android_vr"], ["tv_downgraded"], ["web_embedded"], ["visionos"], ["mweb"], ["ios"], ["web_safari"]]
_good_strategy = None

FAIL_LIMIT = 5
BLOCK_SECONDS = 600
_fails: dict[str, list[float]] = {}   # 주소 -> [틀린 횟수, 차단이 풀리는 시각]
_lock = threading.Lock()
_job = threading.BoundedSemaphore(1)  # 한 번에 하나만 변환(무료 서버의 메모리·시간 보호)

app = FastAPI(title="HappyMpX", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "X-Access-Code", "Authorization"],
    expose_headers=["Content-Disposition"],
)


class UrlBody(BaseModel):
    url: str
    format: str = "mp3"
    quality: str | None = None   # mp3: 128·192·256·320(kbps), mp4: 240·360·480·720·1080(세로 해상도)


class SearchBody(BaseModel):
    q: str


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")) or "?"


def check_code(request: Request, code: str | None) -> None:
    """비밀번호가 맞는지 확인한다. 틀리면 횟수를 쌓고, 한도를 넘으면 잠시 막는다."""
    if not ACCESS_CODE:
        raise HTTPException(503, "서버에 입장 비밀번호(ACCESS_CODE)가 설정되지 않았습니다.")
    ip = client_ip(request)
    now = time.time()
    with _lock:
        rec = _fails.get(ip)
        if rec and rec[1] > now:
            raise HTTPException(429, f"틀린 횟수가 많아 잠시 막혔습니다. {int((rec[1] - now) // 60) + 1}분 뒤에 다시 시도하세요.")
        if rec and rec[1] and rec[1] <= now:
            _fails.pop(ip, None)
            rec = None
        ok = bool(code) and hmac.compare_digest(code.encode(), ACCESS_CODE.encode())
        if ok:
            _fails.pop(ip, None)
            return
        rec = _fails.setdefault(ip, [0, 0.0])
        rec[0] += 1
        if rec[0] >= FAIL_LIMIT:
            rec[1] = now + BLOCK_SECONDS
            rec[0] = 0
    raise HTTPException(403, "비밀번호가 맞지 않습니다.")


def check_google(authorization: str | None) -> None:
    """ALLOWED_EMAILS 가 정해져 있을 때만 구글 로그인(ID 토큰)을 확인한다."""
    if not ALLOWED_EMAILS:
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "구글 로그인이 필요합니다.")
    try:
        import firebase_admin
        from firebase_admin import auth as fb_auth

        if not firebase_admin._apps:
            firebase_admin.initialize_app(options={"projectId": FIREBASE_PROJECT_ID})
        user = fb_auth.verify_id_token(authorization[7:])
    except Exception:
        raise HTTPException(401, "구글 로그인 확인에 실패했습니다.")
    if (user.get("email") or "").lower() not in ALLOWED_EMAILS or not user.get("email_verified", False):
        raise HTTPException(403, "허용된 계정이 아닙니다.")


def access(request: Request, x_access_code: str | None = Header(default=None), authorization: str | None = Header(default=None)) -> None:
    check_code(request, x_access_code)
    check_google(authorization)


def clean_url(url: str) -> str:
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("http", "https") or (p.hostname or "").lower() not in YOUTUBE_HOSTS:
        raise HTTPException(400, "유튜브 주소(youtube.com, youtu.be)만 사용할 수 있습니다.")
    return url


def base_opts(clients=None) -> dict:
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20, "retries": 2, "restrictfilenames": False}
    if FFMPEG_LOCATION:
        o["ffmpeg_location"] = FFMPEG_LOCATION
    if clients:
        o["extractor_args"] = {"youtube": {"player_client": list(clients)}}
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        # yt-dlp 는 쿠키 파일에 다시 쓰므로 읽기 전용 위치(Secret File)를 그대로 쓰지 않고 복사본을 쓴다
        tmpc = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        if not os.path.exists(tmpc):
            shutil.copyfile(COOKIES_FILE, tmpc)
        o["cookiefile"] = tmpc
    return o


def is_block_error(e: Exception) -> bool:
    t = str(e).lower()
    return "sign in to confirm" in t or "not a bot" in t or "confirm you" in t or "po token" in t or "http error 403" in t


def with_fallback(fn):
    """fn(clients) 를 접속 방식을 바꿔 가며 부른다. 막힘 오류일 때만 다음 방식으로 넘어간다."""
    global _good_strategy
    order = list(CLIENT_STRATEGIES)
    if _good_strategy in order:
        order.remove(_good_strategy)
        order.insert(0, _good_strategy)
    last = None
    for c in order:
        try:
            r = fn(c)
            _good_strategy = c
            return r
        except yt_dlp.utils.DownloadError as e:
            last = e
            if not is_block_error(e):
                raise
    raise last


def safe_name(title: str, ext: str) -> str:
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", title or "video")
    t = re.sub(r"\s+", " ", t).strip(" .")[:80] or "video"
    return f"{t}.{ext}"


def pick_rate(q: str | None) -> str:
    if q in (None, ""):
        return "192"
    if str(q) not in MP3_RATES:
        raise HTTPException(400, "MP3 음질은 128·192·256·320 중에서 고르세요.")
    return str(q)


def pick_height(q: str | None) -> int:
    if q in (None, ""):
        return MAX_HEIGHT
    try:
        h = int(q)
    except ValueError:
        raise HTTPException(400, "MP4 화질이 올바르지 않습니다.")
    if h not in MP4_HEIGHTS or h > MAX_HEIGHT:
        raise HTTPException(400, f"MP4 화질은 {', '.join(str(x) for x in MP4_HEIGHTS if x <= MAX_HEIGHT)} 중에서 고르세요.")
    return h


def height_options(d: dict) -> list[int]:
    """영상에 실제로 있는 화질 중, 서버 한도(MAX_HEIGHT) 이하의 표준 화질 목록."""
    have = [f.get("height") or 0 for f in (d.get("formats") or []) if f.get("vcodec") not in (None, "none")]
    top = max(have) if have else 0
    return [h for h in MP4_HEIGHTS if h <= MAX_HEIGHT and h <= top]


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/check")
def check(request: Request, _: None = Depends(access)):
    """비밀번호(와 구글 로그인)가 맞는지만 확인한다."""
    return {"ok": True}


@app.post("/search")
def search(body: SearchBody, request: Request, _: None = Depends(access)):
    """검색어로 유튜브 영상을 찾는다(y2mate 처럼 링크 대신 검색어를 넣을 수 있게)."""
    q = re.sub(r"\s+", " ", (body.q or "")).strip()
    if not q or len(q) > 100:
        raise HTTPException(400, "검색어를 1~100자로 입력하세요.")
    try:
        with yt_dlp.YoutubeDL({**base_opts(), "extract_flat": True, "skip_download": True}) as ydl:
            d = ydl.extract_info(f"ytsearch8:{q}", download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, "검색하지 못했습니다: " + re.sub(r"\x1b\[[0-9;]*m", "", str(e))[:200])
    out = []
    for e in (d.get("entries") or []):
        vid = e.get("id")
        if not vid or not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            continue
        out.append({
            "url": f"https://www.youtube.com/watch?v={vid}",
            "title": e.get("title"),
            "uploader": e.get("uploader") or e.get("channel"),
            "duration": e.get("duration"),
            "thumbnail": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        })
    return {"results": out}


@app.post("/info")
def info(body: UrlBody, request: Request, _: None = Depends(access)):
    url = clean_url(body.url)

    def go(clients):
        with yt_dlp.YoutubeDL({**base_opts(clients), "skip_download": True}) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        d = with_fallback(go)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, "영상 정보를 가져오지 못했습니다: " + re.sub(r"\x1b\[[0-9;]*m", "", str(e))[:200])
    return {
        "title": d.get("title"),
        "thumbnail": d.get("thumbnail"),
        "uploader": d.get("uploader"),
        "duration": d.get("duration"),
        "tooLong": bool(d.get("duration") and d["duration"] > MAX_SECONDS),
        "maxSeconds": MAX_SECONDS,
        "mp3Rates": list(MP3_RATES),
        "mp4Heights": height_options(d),
    }


@app.post("/convert")
def convert(body: UrlBody, request: Request, _: None = Depends(access)):
    url = clean_url(body.url)
    fmt = (body.format or "mp3").lower()
    if fmt not in ("mp3", "mp4"):
        raise HTTPException(400, "형식은 mp3 또는 mp4 만 가능합니다.")
    rate = pick_rate(body.quality) if fmt == "mp3" else None
    height = pick_height(body.quality) if fmt == "mp4" else None
    if not _job.acquire(blocking=False):
        raise HTTPException(429, "다른 변환이 진행 중입니다. 잠시 뒤에 다시 시도하세요.")
    tmp = tempfile.mkdtemp(prefix="happympx_")
    started_ok = False
    try:
        def go(clients):
            for f in os.listdir(tmp):          # 앞선 시도가 남긴 조각은 지운다
                os.remove(os.path.join(tmp, f))
            opts = {**base_opts(clients), "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s")}
            if fmt == "mp3":
                opts.update({"format": "bestaudio/best", "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": rate}]})
            else:
                h = height
                opts.update({
                    "format": f"bv*[vcodec^=avc1][height<={h}]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/b",
                    "merge_output_format": "mp4",
                })
            with yt_dlp.YoutubeDL(opts) as ydl:
                d = ydl.extract_info(url, download=False)
                if d.get("duration") and d["duration"] > MAX_SECONDS:
                    raise HTTPException(400, f"영상이 너무 깁니다(최대 {MAX_SECONDS // 60}분).")
                ydl.process_ie_result(d, download=True)
                return d

        try:
            d = with_fallback(go)
        except yt_dlp.utils.DownloadError as e:
            raise HTTPException(400, "변환에 실패했습니다: " + re.sub(r"\x1b\[[0-9;]*m", "", str(e))[:200])
        files = [f for f in os.listdir(tmp) if f.lower().endswith("." + fmt)]
        if not files:
            raise HTTPException(500, "변환된 파일을 찾지 못했습니다.")
        path = os.path.join(tmp, files[0])

        def cleanup():
            shutil.rmtree(tmp, ignore_errors=True)
            _job.release()

        started_ok = True
        return FileResponse(
            path,
            media_type="audio/mpeg" if fmt == "mp3" else "video/mp4",
            filename=safe_name(d.get("title"), fmt),
            background=BackgroundTask(cleanup),
        )
    finally:
        if not started_ok:
            shutil.rmtree(tmp, ignore_errors=True)
            _job.release()


_diag_last = 0.0


@app.get("/diag")
def diag():
    """서버에서 어떤 접속 방식이 유튜브에 막히지 않는지 본다. 고정된 허락 영상 하나만 조회하고(내려받지 않음),
    20초에 한 번만 돌며, DIAG_ENABLED=1 일 때만 열린다."""
    global _diag_last
    if not DIAG_ENABLED:
        raise HTTPException(404, "not found")
    now = time.time()
    with _lock:
        if now - _diag_last < 20:
            raise HTTPException(429, "잠시 뒤에 다시 시도하세요.")
        _diag_last = now
    res = []
    for c in CLIENT_STRATEGIES:
        name = ",".join(c) if c else "기본"
        try:
            with yt_dlp.YoutubeDL({**base_opts(c), "skip_download": True}) as ydl:
                d = ydl.extract_info(DIAG_URL, download=False)
            res.append({"client": name, "ok": True, "formats": len(d.get("formats") or [])})
        except Exception as e:  # noqa: BLE001
            res.append({"client": name, "ok": False, "error": re.sub(r"\x1b\[[0-9;]*m", "", str(e))[:160]})
    return {"ytdlp": yt_dlp.version.__version__, "cookies": bool(COOKIES_FILE and os.path.exists(COOKIES_FILE)), "results": res}
