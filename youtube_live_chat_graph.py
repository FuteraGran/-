#!/usr/bin/env python3
"""YouTube / Twitch配信アーカイブのチャットリプレイを1分ごとに集計する。

以下のコメント数を別々の色でグラフに表示する。

・通常コメント全体
・「w」「笑」「草」のいずれかを含むコメント
・「かわいい」「可愛い」のいずれかを含むコメント

初回のみ:
    py -m pip install -U yt-dlp chat-downloader matplotlib

実行方法:
    VS Codeの実行ボタンを押し、
    YouTubeまたはTwitchのアーカイブURL（もしくは動画ID）を入力する。

APIキーは不要。動画本体はダウンロードしない。
YouTubeはyt-dlp、Twitchはchat-downloaderでチャットのみ取得する。
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter
except ModuleNotFoundError:
    print("matplotlibがありません。次を実行してください:")
    print("py -m pip install -U matplotlib yt-dlp chat-downloader")
    raise SystemExit(1)


CSV_PATH = Path("comment_counts.csv")


def detect_platform(value: str) -> str:
    """入力値がYouTubeかTwitchかを判定する。"""
    stripped = value.strip()

    # TwitchのVOD IDは数字のみのため、YouTube ID判定より先に確認する。
    if re.fullmatch(r"\d+", stripped):
        return "twitch"

    if re.fullmatch(r"[A-Za-z0-9_-]{11}", stripped):
        return "youtube"

    parsed = urlparse(
        stripped if "://" in stripped else f"https://{stripped}"
    )
    host = parsed.netloc.lower().split(":", 1)[0]

    if "twitch.tv" in host:
        return "twitch"

    if "youtu" in host:
        return "youtube"

    raise ValueError(
        "YouTubeまたはTwitchのアーカイブURL、"
        "もしくは動画IDを入力してください。"
    )


def extract_video_id(value: str) -> str:
    """URLまたは11文字の文字列からYouTubeの動画IDを取得する。"""
    value = value.strip()

    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value

    parsed = urlparse(
        value if "://" in value else f"https://{value}"
    )
    host = parsed.netloc.lower().split(":", 1)[0]
    candidate = ""

    if host in {"youtu.be", "[www.youtu.be](https://www.youtu.be)"}:
        candidate = parsed.path.strip("/").split("/", 1)[0]

    elif host.endswith("youtube.com"):
        if parsed.path == "/watch":
            candidate = parse_qs(
                parsed.query
            ).get("v", [""])[0]

        else:
            parts = [
                part
                for part in parsed.path.split("/")
                if part
            ]

            if (
                len(parts) >= 2
                and parts[0] in {
                    "live",
                    "shorts",
                    "embed",
                }
            ):
                candidate = parts[1]

    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
        raise ValueError(
            "動画URLまたは11文字の動画IDを入力してください。"
        )

    return candidate


def extract_twitch_vod_id(value: str) -> str:
    """URLまたは数字の文字列からTwitchのVOD IDを取得する。"""
    value = value.strip()

    if re.fullmatch(r"\d+", value):
        return value

    parsed = urlparse(
        value if "://" in value else f"https://{value}"
    )

    match = re.search(
        r"/(?:v(?:ideo)?|videos)/(\d+)",
        parsed.path,
    )

    if not match:
        match = re.search(
            r"[?&]video=v?(\d+)",
            parsed.query,
        )

    if not match:
        raise ValueError(
            "TwitchのアーカイブURLまたは"
            "動画IDを入力してください。"
        )

    return match.group(1)


def download_live_chat(
    video_id: str,
    directory: Path,
) -> Path:
    """yt-dlpを使ってYouTubeのチャットリプレイだけを取得する。"""
    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--skip-download",
        "--write-subs",
        "--sub-langs",
        "live_chat",
        "--no-overwrites",
        "-o",
        str(directory / "%(id)s.%(ext)s"),
        f"https://www.youtube.com/watch?v={video_id}",
    ]

    print(
        "チャットリプレイを取得しています。"
        "長い配信では数分かかります..."
    )

    try:
        subprocess.run(command, check=True)

    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            "取得に失敗しました。"
            "チャットリプレイが公開されているアーカイブか"
            "確認してください。"
        ) from error

    candidates = list(
        directory.glob(f"{video_id}.live_chat.json*")
    )

    if not candidates:
        candidates = list(
            directory.glob("*.live_chat.json*")
        )

    if not candidates:
        raise RuntimeError(
            "チャットリプレイがありません。"
            "無効・削除済み・処理中の可能性があります。"
        )

    return candidates[0]


def walk_dicts(value):
    """入れ子になったJSON内のすべての辞書を順番に返す。"""
    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from walk_dicts(child)

    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


def iter_json_records(path: Path):
    """改行区切りのJSONファイルを1レコードずつ読み込む。"""
    with path.open(
        "r",
        encoding="utf-8",
    ) as file:
        for line_number, line in enumerate(file, 1):
            line = line.strip()

            if not line:
                continue

            try:
                yield json.loads(line)

            except json.JSONDecodeError as error:
                raise RuntimeError(
                    f"チャットデータの{line_number}行目を"
                    "解析できません。"
                    "yt-dlpを最新版へ更新してください。"
                ) from error


def extract_message_text(renderer: dict) -> str:
    """通常コメントの本文を文字列として取り出す。"""
    message = renderer.get("message", {})
    runs = message.get("runs", [])

    parts: list[str] = []

    for run in runs:
        if not isinstance(run, dict):
            continue

        text = run.get("text")

        if isinstance(text, str):
            parts.append(text)

    return "".join(parts)


def is_laugh_comment(message_text: str) -> bool:
    """「w」「笑」「草」のいずれかを含むか判定する。"""
    return (
        "w" in message_text.lower()
        or "笑" in message_text
        or "草" in message_text
    )


def is_cute_comment(message_text: str) -> bool:
    """「かわいい」「可愛い」のいずれかを含むか判定する。"""
    return (
        "かわいい" in message_text
        or "可愛い" in message_text
    )


def count_youtube_comments(
    path: Path,
) -> tuple[
    Counter[int],
    Counter[int],
    Counter[int],
]:
    """通常・笑い・かわいいコメントを1分単位で集計する。"""
    all_counts: Counter[int] = Counter()
    laugh_counts: Counter[int] = Counter()
    cute_counts: Counter[int] = Counter()

    for record in iter_json_records(path):
        for node in walk_dicts(record):
            replay = node.get("replayChatItemAction")

            if not isinstance(replay, dict):
                continue

            try:
                offset_msec = int(
                    replay["videoOffsetTimeMsec"]
                )
                minute = max(
                    0,
                    offset_msec // 60_000,
                )

            except (KeyError, TypeError, ValueError):
                continue

            for action in replay.get("actions", []):
                if not isinstance(action, dict):
                    continue

                add_action = action.get(
                    "addChatItemAction",
                    {},
                )

                if not isinstance(add_action, dict):
                    continue

                item = add_action.get("item", {})

                if not isinstance(item, dict):
                    continue

                renderer = item.get(
                    "liveChatTextMessageRenderer"
                )

                # Super Chatやメンバー登録通知などは除外する。
                if not isinstance(renderer, dict):
                    continue

                all_counts[minute] += 1

                message_text = extract_message_text(
                    renderer
                )

                if is_laugh_comment(message_text):
                    laugh_counts[minute] += 1

                if is_cute_comment(message_text):
                    cute_counts[minute] += 1

    if not all_counts:
        raise RuntimeError(
            "通常コメントを取得できません。"
            "リプレイの有無を確認し、"
            "yt-dlpを更新してください。"
        )

    return (
        all_counts,
        laugh_counts,
        cute_counts,
    )


def count_twitch_comments(
    vod_id: str,
) -> tuple[
    Counter[int],
    Counter[int],
    Counter[int],
]:
    """Twitchのチャットリプレイを取得し、1分単位で集計する。"""
    try:
        from chat_downloader import ChatDownloader
        from chat_downloader.errors import ChatDownloaderError

    except ModuleNotFoundError as error:
        raise RuntimeError(
            "chat-downloaderがありません。"
            "次を実行してください: "
            "py -m pip install -U chat-downloader"
        ) from error

    url = f"https://www.twitch.tv/videos/{vod_id}"

    print(
        "チャットリプレイを取得しています。"
        "長い配信では数分かかります..."
    )

    all_counts: Counter[int] = Counter()
    laugh_counts: Counter[int] = Counter()
    cute_counts: Counter[int] = Counter()

    try:
        messages = ChatDownloader().get_chat(
            url,
            message_types=["text_message"],
        )

        for message in messages:
            offset_seconds = message.get(
                "time_in_seconds"
            )

            if offset_seconds is None:
                continue

            minute = max(
                0,
                int(offset_seconds) // 60,
            )

            message_text = message.get("message") or ""

            all_counts[minute] += 1

            if is_laugh_comment(message_text):
                laugh_counts[minute] += 1

            if is_cute_comment(message_text):
                cute_counts[minute] += 1

    except ChatDownloaderError as error:
        raise RuntimeError(
            "取得に失敗しました。"
            "TwitchのアーカイブURLが正しいか、"
            "チャットリプレイが利用可能か"
            "確認してください。"
        ) from error

    if not all_counts:
        raise RuntimeError(
            "通常コメントを取得できません。"
            "リプレイの有無を確認してください。"
        )

    return (
        all_counts,
        laugh_counts,
        cute_counts,
    )


def write_csv(
    path: Path,
    all_counts: Counter[int],
    laugh_counts: Counter[int],
    cute_counts: Counter[int],
) -> None:
    """集計結果をCSVファイルに保存する。"""
    with path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.writer(file)

        writer.writerow(
            [
                "elapsed_minute",
                "elapsed_time",
                "comment_count",
                "laugh_comment_count",
                "cute_comment_count",
            ]
        )

        for minute in range(max(all_counts) + 1):
            hours, mins = divmod(minute, 60)

            writer.writerow(
                [
                    minute,
                    f"{hours:02d}:{mins:02d}:00",
                    all_counts[minute],
                    laugh_counts[minute],
                    cute_counts[minute],
                ]
            )


def format_elapsed_time(
    value: float,
    _position: int,
) -> str:
    """グラフの横軸をH:MM形式に変換する。"""
    minute = max(
        0,
        int(round(value)),
    )
    hours, mins = divmod(minute, 60)

    return f"{hours}:{mins:02d}"


def show_graph(
    source_label: str,
    all_counts: Counter[int],
    laugh_counts: Counter[int],
    cute_counts: Counter[int],
) -> None:
    """通常・笑い・かわいいコメントのグラフを表示する。"""
    last_minute = max(all_counts)
    minutes = list(range(last_minute + 1))

    all_values = [
        all_counts[minute]
        for minute in minutes
    ]

    laugh_values = [
        laugh_counts[minute]
        for minute in minutes
    ]

    cute_values = [
        cute_counts[minute]
        for minute in minutes
    ]

    figure, ax = plt.subplots(figsize=(12, 6))

    # 通常コメント全体
    ax.plot(
        minutes,
        all_values,
        color="#ff0033",
        linewidth=1.2,
        label="All comments",
    )

    ax.fill_between(
        minutes,
        all_values,
        color="#ff0033",
        alpha=0.12,
    )

    # 「w」「笑」「草」を含むコメント
    ax.plot(
        minutes,
        laugh_values,
        color="#0066ff",
        linewidth=1.4,
        label='Comments containing "w", "笑", or "草"',
    )

    ax.fill_between(
        minutes,
        laugh_values,
        color="#0066ff",
        alpha=0.15,
    )

    # 「かわいい」「可愛い」を含むコメント
    ax.plot(
        minutes,
        cute_values,
        color="#00a65a",
        linewidth=1.4,
        label='Comments containing "かわいい" or "可愛い"',
    )

    ax.fill_between(
        minutes,
        cute_values,
        color="#00a65a",
        alpha=0.15,
    )

    ax.set_title(
        f"{source_label}\n"
        "Comments per minute"
    )
    ax.set_ylabel("Comments")
    ax.set_xlabel("Elapsed time (H:MM)")

    ax.xaxis.set_major_formatter(
        FuncFormatter(format_elapsed_time)
    )

    ax.set_xlim(
        0,
        max(1, last_minute),
    )
    ax.set_ylim(bottom=0)
    ax.grid(alpha=0.25)
    ax.legend()

    figure.tight_layout()
    plt.show()


def main() -> int:
    """メイン処理。"""
    value = input(
        "アーカイブのURLまたは動画IDを入力してください"
        "(YouTube / Twitch): "
    ).strip()

    try:
        platform = detect_platform(value)

        if platform == "youtube":
            video_id = extract_video_id(value)

            with tempfile.TemporaryDirectory(
                prefix="youtube_chat_"
            ) as temp:
                chat_path = download_live_chat(
                    video_id,
                    Path(temp),
                )

                (
                    all_counts,
                    laugh_counts,
                    cute_counts,
                ) = count_youtube_comments(chat_path)

            source_label = f"YouTube archive {video_id}"

        else:
            video_id = extract_twitch_vod_id(value)

            (
                all_counts,
                laugh_counts,
                cute_counts,
            ) = count_twitch_comments(video_id)

            source_label = f"Twitch archive {video_id}"

        write_csv(
            CSV_PATH,
            all_counts,
            laugh_counts,
            cute_counts,
        )

        print(
            "通常コメント総数: "
            f"{sum(all_counts.values())}件"
        )

        print(
            "「w」「笑」「草」を含むコメント: "
            f"{sum(laugh_counts.values())}件"
        )

        print(
            "「かわいい」「可愛い」を含むコメント: "
            f"{sum(cute_counts.values())}件"
        )

        print(
            f"CSV保存先: {CSV_PATH.resolve()}"
        )

        show_graph(
            source_label,
            all_counts,
            laugh_counts,
            cute_counts,
        )

    except (ValueError, RuntimeError) as error:
        print(
            f"エラー: {error}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
