#!/usr/bin/env python3
"""YouTube動画をフル尺・フルHD(1080p)でダウンロードする。

依存関係:
    pip install yt-dlp

実行方法:
    python3 youtube_downloader.py
"""

import sys

import yt_dlp

# 1080p以下で最良の映像+音声を選び、mp4に結合する
FORMAT = (
    "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
    "/best[height<=1080][ext=mp4]"
    "/best[height<=1080]"
)


def download(url: str, output_dir: str = "downloads") -> None:
    ydl_opts = {
        "format": FORMAT,
        "outtmpl": f"{output_dir}/%(title)s.%(ext)s",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "progress_hooks": [progress_hook],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


def progress_hook(d: dict) -> None:
    if d["status"] == "downloading":
        percent = d.get("_percent_str", "").strip()
        speed = d.get("_speed_str", "").strip()
        print(f"\rダウンロード中... {percent} ({speed})", end="", flush=True)
    elif d["status"] == "finished":
        print("\nダウンロード完了。動画を結合しています...")


def main() -> None:
    url = input("YouTubeのURLを入力してください: ").strip()
    if not url:
        print("URLが入力されませんでした。", file=sys.stderr)
        sys.exit(1)

    try:
        download(url)
    except yt_dlp.utils.DownloadError as e:
        print(f"ダウンロードに失敗しました: {e}", file=sys.stderr)
        sys.exit(1)

    print("保存先: downloads/")


if __name__ == "__main__":
    main()
