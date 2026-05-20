"""YouTube / SoundOn / 本地音訊逐字稿生成器 (Mac Apple Silicon 最佳化).

支援三種輸入：
1. YouTube URL → 使用 yt-dlp 下載
2. SoundOn player URL → 透過 SoundOn client API 取得 mp3 直接下載
3. 本地音訊 / 影片檔

所有來源最終都透過 mlx-whisper 轉錄為逐字稿，
輸出 .txt / .srt / .md 三種格式，方便後續丟給 Claude 做摘要整理。

Usage:
    python transcriber.py "https://www.youtube.com/watch?v=XXX"
    python transcriber.py "https://player.soundon.fm/p/<pid>/episodes/<eid>"
    python transcriber.py ./local_audio.mp3 --language en --model large-v3
    python transcriber.py <url> --output-dir ./transcripts --keep-audio
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

# 註：mlx-whisper 是 Apple 官方 MLX 框架的 Whisper 實作，
# 在 Apple Silicon 上速度與記憶體表現優於原版 openai-whisper。
# 為了讓 --help 等不需要實際轉錄的指令在未安裝套件時也能執行，
# 我們把 import 延後到 transcribe() 函式中再做。


# Hugging Face 上的 MLX 量化模型倉庫對應表
# 參考：https://huggingface.co/collections/mlx-community/whisper
MLX_MODEL_REPOS: dict[str, str] = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}

# 繁體中文 initial prompt：強制 Whisper 輸出繁體字，避免出現簡體
# Whisper 的輸出語體會受 prompt 影響，這是官方建議的做法
ZH_TW_INITIAL_PROMPT = (
    "以下是普通話的句子，請使用繁體中文輸出，"
    "包含標點符號，例如：你好、謝謝、台灣、軟體、資訊、網路。"
)

# SoundOn 的 client API 與 player 內嵌在 bundle 裡的公開 token
# 來源：https://player.soundon.fm/bundle.<hash>.js 中的 config 物件
# 若未來 SoundOn 換 token 導致 401，重新從 bundle 抓即可
SOUNDON_API_BASE = "https://api.soundon.fm/v2/client"
SOUNDON_API_TOKEN = "KilpEMLQeNzxmNBL55u5"

# 解析 SoundOn player URL 取出 podcast_id 與 episode_id
# 範例：https://player.soundon.fm/p/<podcast_id>/episodes/<episode_id>
# 錨定到字串開頭並強制 http(s) scheme + player.soundon.fm 主機，
# 避免攻擊者構造 https://evil.com/?x=https://player.soundon.fm/p/... 被誤認。
SOUNDON_URL_RE = re.compile(
    r"^https?://(?:www\.)?player\.soundon\.fm"
    r"/p/(?P<pid>[0-9a-f-]{36})/episodes/(?P<eid>[0-9a-f-]{36})"
    r"(?:[/?#]|$)",
    re.IGNORECASE,
)


def check_yt_dlp_available() -> None:
    """確認系統中有可用的 yt-dlp（pip 或 brew 安裝皆可）。"""
    if shutil.which("yt-dlp") is None:
        print(
            "[ERROR] 找不到 yt-dlp。請安裝：\n"
            "  pip install yt-dlp\n"
            "  或 brew install yt-dlp",
            file=sys.stderr,
        )
        sys.exit(1)


def check_ffmpeg_available() -> None:
    """mlx-whisper 與 yt-dlp 都需要 ffmpeg 處理音訊。"""
    if shutil.which("ffmpeg") is None:
        print(
            "[ERROR] 找不到 ffmpeg。請安裝：\n"
            "  brew install ffmpeg",
            file=sys.stderr,
        )
        sys.exit(1)


def is_url(source: str) -> bool:
    """判斷輸入是 URL 還是本地檔案路徑。"""
    return source.startswith(("http://", "https://", "www."))


def is_soundon_url(source: str) -> bool:
    """判斷是否為 SoundOn player URL。"""
    return bool(SOUNDON_URL_RE.search(source))


def parse_soundon_url(url: str) -> tuple[str, str]:
    """從 SoundOn player URL 取出 (podcast_id, episode_id)。"""
    match = SOUNDON_URL_RE.search(url)
    if not match:
        raise ValueError(
            f"無法解析 SoundOn URL：{url}\n"
            "預期格式：https://player.soundon.fm/p/<podcast_id>/episodes/<episode_id>"
        )
    return match.group("pid"), match.group("eid")


def fetch_soundon_episode(podcast_id: str, episode_id: str) -> dict[str, Any]:
    """呼叫 SoundOn client API 取得 episode metadata。

    回傳 response.data.data 物件（含 title、audioUrl、duration 等欄位）。
    """
    api_url = (
        f"{SOUNDON_API_BASE}/podcasts/{podcast_id}/episodes/{episode_id}"
    )
    request = urllib.request.Request(
        api_url,
        headers={
            "api-token": SOUNDON_API_TOKEN,
            "User-Agent": "Mozilla/5.0 (soundon-transcriber)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw_body = response.read()
    except HTTPError as exc:
        raise RuntimeError(
            f"SoundOn API 回傳錯誤 {exc.code}：{exc.reason}（URL：{api_url}）"
        ) from exc
    except URLError as exc:
        raise RuntimeError(f"無法連線 SoundOn API：{exc.reason}") from exc

    # API 偶爾可能回 HTML 錯誤頁或非 UTF-8 內容；
    # 抽離 read 與 parse 以便獨立提供友善訊息（含原始片段方便除錯）
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        snippet = raw_body[:200]
        raise RuntimeError(
            f"SoundOn API 回應非有效 JSON：{exc}（前 200 byte：{snippet!r}）"
        ) from exc

    if payload.get("result") != "success":
        raise RuntimeError(f"SoundOn API 回應非 success：{payload}")

    # 回應結構：{result, status, data: {id, data: {title, audioUrl, ...}}}
    inner = payload.get("data", {})
    episode_data = inner.get("data", {})
    audio_url = episode_data.get("audioUrl")
    if not audio_url:
        raise RuntimeError(
            f"API 回應中找不到 audioUrl，資料可能異常：{episode_data}"
        )
    # 驗證 scheme，避免 SoundOn 端遭滲透 / MITM 後回傳 file:// 等
    # 非預期 scheme 造成本地檔被當成「音訊」讀回來
    if not audio_url.lower().startswith(("http://", "https://")):
        raise RuntimeError(
            f"audioUrl scheme 不合法（必須是 http/https）：{audio_url}"
        )
    return episode_data


def download_soundon_audio(url: str, work_dir: Path) -> tuple[Path, str]:
    """下載 SoundOn podcast 音訊，回傳 (音訊檔路徑, episode 標題)。

    流程：
    1. 解析 player URL 取 podcast_id / episode_id
    2. 呼叫 client API 取 audioUrl 與 title
    3. 串流下載 mp3 到 work_dir
    """
    print(f"[1/3] 解析 SoundOn URL：{url}")
    podcast_id, episode_id = parse_soundon_url(url)

    episode = fetch_soundon_episode(podcast_id, episode_id)
    title = episode.get("title") or f"soundon_{episode_id[:8]}"
    audio_url = episode["audioUrl"]
    duration_sec = episode.get("duration")
    if duration_sec:
        mins, secs = divmod(int(duration_sec), 60)
        print(f"      標題：{title}（時長 {mins} 分 {secs} 秒）")
    else:
        print(f"      標題：{title}")

    safe_title = sanitize_filename(title)
    audio_path = work_dir / f"{safe_title}.mp3"

    print(f"      下載音訊：{audio_url}")
    request = urllib.request.Request(
        audio_url,
        headers={"User-Agent": "Mozilla/5.0 (soundon-transcriber)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            total = response.headers.get("Content-Length")
            total_bytes = int(total) if total and total.isdigit() else None
            downloaded = 0
            chunk_size = 1 << 16  # 64KB
            with audio_path.open("wb") as fh:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if total_bytes:
                        pct = downloaded * 100 / total_bytes
                        print(
                            f"      已下載 {downloaded / 1_048_576:.1f} MB "
                            f"/ {total_bytes / 1_048_576:.1f} MB ({pct:.1f}%)",
                            end="\r",
                            flush=True,
                        )
            print()  # 換行收尾
    except (HTTPError, URLError) as exc:
        raise RuntimeError(f"下載 SoundOn 音訊失敗：{exc}") from exc

    return audio_path, title


def sanitize_filename(name: str) -> str:
    """移除檔名中不安全的字元，保留中英文、數字、基本符號。"""
    name = re.sub(r"[\\/:*?\"<>|]", "_", name)
    name = re.sub(r"\s+", "_", name).strip("._")
    return name[:120] or "audio"


def download_youtube_audio(url: str, work_dir: Path) -> tuple[Path, str]:
    """下載 YouTube 音訊，回傳 (音訊檔路徑, 影片標題)。

    使用 yt-dlp 下載最佳品質音訊並轉成 wav（mlx-whisper 接受 ffmpeg 支援的格式）。
    """
    print(f"[1/3] 下載 YouTube 音訊：{url}")

    # 先取得影片 metadata 以便用 title 當作輸出檔名
    meta_cmd = ["yt-dlp", "--dump-single-json", "--no-playlist", url]
    try:
        meta_result = subprocess.run(
            meta_cmd, capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] 無法取得影片資訊：{exc.stderr}", file=sys.stderr)
        sys.exit(1)

    try:
        meta: dict[str, Any] = json.loads(meta_result.stdout)
    except json.JSONDecodeError as exc:
        print(
            f"[ERROR] 無法解析影片資訊（yt-dlp 回傳非預期內容）：{exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    title = meta.get("title", "youtube_audio")
    safe_title = sanitize_filename(title)

    output_template = str(work_dir / f"{safe_title}.%(ext)s")
    download_cmd = [
        "yt-dlp",
        "-x",  # 只抽取音訊
        "--audio-format", "mp3",
        "--audio-quality", "0",  # 最佳音質
        "--no-playlist",
        "-o", output_template,
        url,
    ]
    try:
        subprocess.run(download_cmd, check=True)
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] yt-dlp 下載失敗：{exc}", file=sys.stderr)
        sys.exit(1)

    audio_path = work_dir / f"{safe_title}.mp3"
    if not audio_path.exists():
        print(f"[ERROR] 下載完成但找不到檔案：{audio_path}", file=sys.stderr)
        sys.exit(1)

    return audio_path, title


def format_timestamp(seconds: float, srt: bool = False) -> str:
    """將秒數轉換成 HH:MM:SS,mmm (SRT) 或 HH:MM:SS (Markdown) 格式。"""
    if seconds < 0:
        seconds = 0
    # Round to total milliseconds first, then carry via divmod so a
    # value like 59.9996s correctly rolls over instead of yielding ",1000".
    total_ms = int(round(seconds * 1000))
    hours, total_ms = divmod(total_ms, 3_600_000)
    minutes, total_ms = divmod(total_ms, 60_000)
    secs, millis = divmod(total_ms, 1000)
    if srt:
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def transcribe(
    audio_path: Path,
    model_name: str,
    language: str | None,
) -> dict[str, Any]:
    """呼叫 mlx-whisper 執行轉錄，回傳完整結果 dict。"""
    if model_name not in MLX_MODEL_REPOS:
        print(
            f"[ERROR] 未支援的模型：{model_name}。"
            f"可用選項：{', '.join(MLX_MODEL_REPOS)}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        import mlx_whisper
    except ImportError:
        print(
            "[ERROR] 找不到 mlx_whisper 套件。\n"
            "請先安裝：pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    repo = MLX_MODEL_REPOS[model_name]
    print(f"[2/3] 使用 {repo} 轉錄中（首次執行會下載模型）...")

    kwargs: dict[str, Any] = {
        "path_or_hf_repo": repo,
        "word_timestamps": False,
        "verbose": False,
    }

    if language == "zh":
        kwargs["language"] = "zh"
        kwargs["initial_prompt"] = ZH_TW_INITIAL_PROMPT
    elif language and language != "auto":
        kwargs["language"] = language
    # language == "auto" 或 None → 讓 Whisper 自動偵測

    result: dict[str, Any] = mlx_whisper.transcribe(str(audio_path), **kwargs)
    return result


def write_txt(result: dict[str, Any], output_path: Path) -> None:
    """輸出純文字逐字稿（無時間戳，方便直接貼給 Claude）。"""
    text = result.get("text", "").strip()
    output_path.write_text(text + "\n", encoding="utf-8")


def write_srt(result: dict[str, Any], output_path: Path) -> None:
    """輸出 SRT 字幕檔，可直接掛在影片上。"""
    lines: list[str] = []
    for idx, seg in enumerate(result.get("segments", []), start=1):
        start = format_timestamp(seg["start"], srt=True)
        end = format_timestamp(seg["end"], srt=True)
        text = seg["text"].strip()
        lines.append(f"{idx}\n{start} --> {end}\n{text}\n")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def write_markdown(
    result: dict[str, Any],
    output_path: Path,
    title: str,
    source: str,
) -> None:
    """輸出帶時間戳的 Markdown 逐字稿，人類閱讀用。"""
    lines: list[str] = [
        f"# {title}",
        "",
        f"- 來源：{source}",
        f"- 語言：{result.get('language', 'unknown')}",
        "",
        "---",
        "",
    ]
    for seg in result.get("segments", []):
        ts = format_timestamp(seg["start"], srt=False)
        text = seg["text"].strip()
        lines.append(f"**[{ts}]** {text}")
        lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")


def process(
    source: str,
    model_name: str,
    language: str | None,
    output_dir: Path,
    keep_audio: bool,
) -> None:
    """端到端處理流程：下載 → 轉錄 → 輸出三種格式。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # 若是 URL，先下載到暫存目錄；若是本地檔案，直接使用
    tmp_dir_obj: tempfile.TemporaryDirectory[str] | None = None

    if is_url(source):
        if keep_audio:
            audio_dir = output_dir
        else:
            tmp_dir_obj = tempfile.TemporaryDirectory(prefix="audio_transcriber_")
            audio_dir = Path(tmp_dir_obj.name)

        if is_soundon_url(source):
            audio_path, title = download_soundon_audio(source, audio_dir)
        else:
            check_yt_dlp_available()
            audio_path, title = download_youtube_audio(source, audio_dir)
    else:
        audio_path = Path(source).expanduser().resolve()
        if not audio_path.exists():
            print(f"[ERROR] 找不到本地檔案：{audio_path}", file=sys.stderr)
            sys.exit(1)
        title = audio_path.stem
        print(f"[1/3] 使用本地音訊：{audio_path}")

    try:
        result = transcribe(audio_path, model_name, language)

        base_name = sanitize_filename(title)
        txt_path = output_dir / f"{base_name}.txt"
        srt_path = output_dir / f"{base_name}.srt"
        md_path = output_dir / f"{base_name}.md"

        print("[3/3] 寫入輸出檔案...")
        write_txt(result, txt_path)
        write_srt(result, srt_path)
        write_markdown(result, md_path, title=title, source=source)

        print("\n✅ 完成！輸出檔案：")
        print(f"  - 純文字：{txt_path}")
        print(f"  - 字幕檔：{srt_path}")
        print(f"  - Markdown：{md_path}")
        detected_lang = result.get("language", "unknown")
        print(f"  - 偵測語言：{detected_lang}")
    finally:
        if tmp_dir_obj is not None:
            tmp_dir_obj.cleanup()


def build_parser() -> argparse.ArgumentParser:
    """建立 CLI 參數解析器。"""
    parser = argparse.ArgumentParser(
        description=(
            "YouTube / SoundOn / 本地音訊逐字稿生成器，"
            "使用 mlx-whisper 在 Apple Silicon 上跑。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "範例：\n"
            "  python transcriber.py 'https://youtu.be/XXX'\n"
            "  python transcriber.py 'https://player.soundon.fm/p/<pid>/episodes/<eid>'\n"
            "  python transcriber.py audio.mp3 --language en --model large-v3\n"
            "  python transcriber.py <url> --language zh -o ./transcripts\n"
        ),
    )
    parser.add_argument(
        "source",
        help="YouTube URL、SoundOn player URL，或本地音訊 / 影片檔案路徑",
    )
    parser.add_argument(
        "--model",
        "-m",
        default="large-v3-turbo",
        choices=list(MLX_MODEL_REPOS.keys()),
        help="Whisper 模型版本（預設：large-v3-turbo，速度/品質最佳平衡）",
    )
    parser.add_argument(
        "--language",
        "-l",
        default="auto",
        help=(
            "語言代碼：zh=繁中(自動加入繁體 prompt)、en=英文、ja=日文、"
            "auto=自動偵測（預設）"
        ),
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default="./output",
        type=Path,
        help="輸出目錄（預設：./output）",
    )
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="保留下載的 mp3 檔案（預設使用暫存目錄並於結束後刪除）",
    )
    return parser


def main() -> None:
    """CLI 進入點。"""
    parser = build_parser()
    args = parser.parse_args()
    check_ffmpeg_available()
    process(
        source=args.source,
        model_name=args.model,
        language=args.language,
        output_dir=args.output_dir,
        keep_audio=args.keep_audio,
    )


if __name__ == "__main__":
    main()
