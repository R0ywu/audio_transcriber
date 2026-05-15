# YouTube Transcriber

YouTube／本地音訊逐字稿生成器，使用 `yt-dlp` 下載音訊、`mlx-whisper` 轉錄，針對 Apple Silicon 最佳化。輸出 `.txt`／`.srt`／`.md` 三種格式，方便後續丟給 LLM 做摘要整理。

## 特性

- **Apple Silicon 最佳化**：使用 Apple MLX 框架的 Whisper 實作，速度與記憶體優於原版 openai-whisper
- **兩種輸入**：YouTube URL 或本地音訊／影片檔案
- **三種輸出**：`.txt`（純文字無時間戳）、`.srt`（字幕檔）、`.md`（帶時間戳，人類閱讀）
- **繁體中文友善**：`--language zh` 會自動加入 initial prompt 強制輸出繁體字
- **自動語言偵測**：預設讓 Whisper 自行判斷語言
- **暫存自動清理**：下載的音訊預設用暫存目錄，結束後自動刪除（可用 `--keep-audio` 保留）

## 需求與安裝

需要 macOS（Apple Silicon）、Python 3.10+，以及 `ffmpeg`：

```bash
brew install ffmpeg

# Python 套件（yt-dlp + mlx-whisper）
pip install -r requirements.txt
```

> `yt-dlp` 也可改用 `brew install yt-dlp` 安裝。

## 用法

```bash
# YouTube 影片
python transcriber.py "https://www.youtube.com/watch?v=XXXX"

# 本地音訊，指定英文模型
python transcriber.py ./local_audio.mp3 --language en --model large-v3

# 繁體中文，自訂輸出目錄
python transcriber.py "https://youtu.be/XXXX" --language zh -o ./transcripts

# 保留下載的 mp3
python transcriber.py "https://youtu.be/XXXX" --keep-audio
```

## 參數

| 參數 | 說明 | 預設 |
|------|------|------|
| `source` | YouTube URL 或本地音訊／影片檔路徑（必填） | — |
| `--model`, `-m` | Whisper 模型：`tiny` / `base` / `small` / `medium` / `large-v2` / `large-v3` / `large-v3-turbo` | `large-v3-turbo` |
| `--language`, `-l` | 語言代碼：`zh`（繁中，自動加繁體 prompt）、`en`、`ja`、`auto`（自動偵測） | `auto` |
| `--output-dir`, `-o` | 輸出目錄 | `./output` |
| `--keep-audio` | 保留下載的 mp3（預設用暫存目錄並於結束後刪除） | 關閉 |

## 輸出格式

每次執行會以影片標題為檔名，產生三個檔案：

- **`<title>.txt`** — 純文字逐字稿，無時間戳，適合直接貼給 LLM
- **`<title>.srt`** — 標準 SRT 字幕檔，可直接掛在影片上
- **`<title>.md`** — 帶時間戳的 Markdown，含來源與偵測語言，適合人類閱讀

## 備註

- 首次使用某個模型時會自動從 Hugging Face 下載權重（`mlx-community/whisper-*`），之後快取重用
- `--language zh` 透過 initial prompt 引導 Whisper 輸出繁體字，避免簡體
- `large-v3-turbo` 為預設，速度／品質平衡最佳；追求最高品質可用 `large-v3`

## 授權

本專案以 [MIT License](LICENSE) 釋出，Copyright (c) 2026 Roy Wu。

> 注意：透過本工具下載 YouTube 內容仍須遵守 YouTube 服務條款，請自行確認使用情境合法。
