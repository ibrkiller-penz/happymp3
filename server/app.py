"""HappyMP3 서버: 유튜브 링크를 MP3 또는 MP4 파일로 바꿔 내려받게 해 준다.

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

# 유튜브 주소만 받는다(다른 주소로 서버를 이용하는 것을 막는다)
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}

FAIL_LIMIT = 5
BLOCK_SECONDS = 600
_fails: dict[str, list[float]] = {}   # 주소 -> [틀린 횟수, 차단이 풀리는 시각]
_lock = threading.Lock()
_job = threading.BoundedSemaphore(1)  # 한 번에 하나만 변환(무료 서버의 메모리·시간 보호)

app = FastAPI(title="HappyMP3", docs_url=None, redoc_url=None, openapi_url=None)
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


def base_opts() -> dict:
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20, "retries": 2, "restrictfilenames": False}
    if FFMPEG_LOCATION:
        o["ffmpeg_location"] = FFMPEG_LOCATION
    return o


def safe_name(title: str, ext: str) -> str:
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", title or "video")
    t = re.sub(r"\s+", " ", t).strip(" .")[:80] or "video"
    return f"{t}.{ext}"


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/check")
def check(request: Request, _: None = Depends(access)):
    """비밀번호(와 구글 로그인)가 맞는지만 확인한다."""
    return {"ok": True}


@app.post("/info")
def info(body: UrlBody, request: Request, _: None = Depends(access)):
    url = clean_url(body.url)
    try:
        with yt_dlp.YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
            d = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(400, "영상 정보를 가져오지 못했습니다: " + re.sub(r"\x1b\[[0-9;]*m", "", str(e))[:200])
    return {
        "title": d.get("title"),
        "thumbnail": d.get("thumbnail"),
        "uploader": d.get("uploader"),
        "duration": d.get("duration"),
        "tooLong": bool(d.get("duration") and d["duration"] > MAX_SECONDS),
        "maxSeconds": MAX_SECONDS,
    }


@app.post("/convert")
def convert(body: UrlBody, request: Request, _: None = Depends(access)):
    url = clean_url(body.url)
    fmt = (body.format or "mp3").lower()
    if fmt not in ("mp3", "mp4"):
        raise HTTPException(400, "형식은 mp3 또는 mp4 만 가능합니다.")
    if not _job.acquire(blocking=False):
        raise HTTPException(429, "다른 변환이 진행 중입니다. 잠시 뒤에 다시 시도하세요.")
    tmp = tempfile.mkdtemp(prefix="happymp3_")
    started_ok = False
    try:
        opts = {**base_opts(), "outtmpl": os.path.join(tmp, "%(id)s.%(ext)s")}
        if fmt == "mp3":
            opts.update({"format": "bestaudio/best", "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]})
        else:
            h = MAX_HEIGHT
            opts.update({
                "format": f"bv*[vcodec^=avc1][height<={h}]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]/b",
                "merge_output_format": "mp4",
            })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                d = ydl.extract_info(url, download=False)
                if d.get("duration") and d["duration"] > MAX_SECONDS:
                    raise HTTPException(400, f"영상이 너무 깁니다(최대 {MAX_SECONDS // 60}분).")
                ydl.process_ie_result(d, download=True)
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
