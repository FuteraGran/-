#!/usr/bin/env python3
"""YouTubeのライブチャット（配信中/アーカイブ）を1分ごとに集計し、
グラフをリアルタイムで更新し続ける。

以下のコメント数を別々の色でグラフに表示する。

・通常コメント全体
・「w」「笑」「草」のいずれかを含むコメント
・「かわいい」「可愛い」のいずれかを含むコメント

配信中に起動した場合は、新しいコメントが届くたびに
グラフが自動で更新され続ける（配信が終わるかウィンドウを
閉じるまで）。すでに終わったアーカイブの場合は、チャット
リプレイを取得し終えた時点でグラフが確定する。

初回のみ:
    py -m pip install -U yt-dlp matplotlib

実行方法:
    VS Codeの実行ボタンを押し、動画IDまたはURLを入力する。
    配信中の動画URLでもアーカイブのURLでもどちらでも良い。

APIキーは不要。動画本体はダウンロードしない。
Ctrl+Cまたはグラフウィンドウを閉じるといつでも終了できる。
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, MaxNLocator
except ModuleNotFoundError:
    print("matplotlibがありません。次を実行してください:")
    print("py -m pip install -U matplotlib yt-dlp")
    raise SystemExit(1)


CSV_PATH = Path("comment_counts.csv")

# グラフを更新する間隔（秒）。短くしすぎると重くなるので注意。
UPDATE_INTERVAL_SECONDS = 60.0

# チャットファイルが出現するまでの最大待ち時間（秒）。
FILE_APPEAR_TIMEOUT_SECONDS = 60.0

# ウィンドウを閉じた／配信が終わったことにどれだけ早く気づけるかの
# ポーリング間隔（秒）。UPDATE_INTERVAL_SECONDSより十分短くすることで、
# 「グラフを閉じたら即終了する」を実際に成立させる。
CLOSE_CHECK_INTERVAL_SECONDS = 0.5

# 「w」を笑いの意味とみなす際、英字に挟まれた"w"（wow, when, with, …）を
# 誤検出しないための正規表現。英字が前後に無い"w"/"W"の連続だけを拾う。
_LAUGH_PATTERN = re.compile(r"(?<![A-Za-z])[wW]+(?![A-Za-z])")


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

    if host in {"youtu.be", "www.youtu.be"}:
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


def start_live_chat_capture(
    video_id: str,
    directory: Path,
) -> subprocess.Popen:
    """yt-dlpをバックグラウンドで起動し、チャットを取得し続ける。

    配信中の動画であれば、yt-dlpは配信が終わるまでプロセスを
    終了せず、新しいコメントが届くたびにファイルへ追記し続ける。
    すでに終わったアーカイブであれば、すぐに取得し終えて
    プロセスは終了する。
    """
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

    return subprocess.Popen(command)


def wait_for_chat_file(
    video_id: str,
    directory: Path,
    process: subprocess.Popen,
) -> Path:
    """チャットファイルが作成されるまで待つ。"""
    deadline = time.time() + FILE_APPEAR_TIMEOUT_SECONDS

    while time.time() < deadline:
        candidates = list(
            directory.glob(f"{video_id}.live_chat.json*")
        )

        if candidates:
            return candidates[0]

        if process.poll() is not None:
            raise RuntimeError(
                "取得に失敗しました。"
                "動画IDが正しいか、"
                "チャットリプレイが公開されているか"
                "確認してください。"
            )

        time.sleep(1)

    process.terminate()
    raise RuntimeError(
        "チャットファイルの生成がタイムアウトしました。"
        "yt-dlpを最新版へ更新してください。"
    )


def walk_dicts(value):
    """入れ子になったJSON内のすべての辞書を順番に返す。"""
    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from walk_dicts(child)

    elif isinstance(value, list):
        for child in value:
            yield from walk_dicts(child)


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
    """「w」「笑」「草」のいずれかを含むか判定する。

    単純に文字列へ"w"が含まれるかどうかで判定すると、"wow"や
    "when"、"with"のような英単語まですべて笑いコメット扱いに
    なってしまう。そのため、前後を英字に挟まれていない"w"/"W"の
    連続（単独の"w"や"www"、"WWW"など）だけを対象にする。
    """
    return (
        bool(_LAUGH_PATTERN.search(message_text))
        or "笑" in message_text
        or "草" in message_text
    )


def is_cute_comment(message_text: str) -> bool:
    """「かわいい」「可愛い」のいずれかを含むか判定する。"""
    return (
        "かわいい" in message_text
        or "可愛い" in message_text
    )


def find_action_chunks(record):
    """レコード内から(actionsリスト, videoOffsetTimeMsec)を列挙する。

    アーカイブ（配信終了後のリプレイ取得）では、コメントは
    ``replayChatItemAction`` に包まれ、動画内の経過時間を表す
    ``videoOffsetTimeMsec`` を持つ。

    一方、放送中のライブ配信ではこの包みが無く、``actions`` が
    直接入っており、動画内経過時間の情報を持たない。その場合は
    offsetとして``None``を返し、呼び出し側で投稿時刻ベースの
    集計にフォールバックする。
    """
    for node in walk_dicts(record):
        replay = node.get("replayChatItemAction")

        if isinstance(replay, dict):
            actions = replay.get("actions")

            if isinstance(actions, list):
                try:
                    offset_msec = int(
                        replay["videoOffsetTimeMsec"]
                    )
                except (KeyError, TypeError, ValueError):
                    offset_msec = None

                yield actions, offset_msec

            continue

        # replayChatItemActionを介さず、actionsが直接
        # 入っている（配信中によく見られる）パターン。
        actions = node.get("actions")

        if isinstance(actions, list):
            yield actions, None


def extract_text_renderer(action):
    """actionから通常コメントのrendererを取り出す（無ければNone）。"""
    if not isinstance(action, dict):
        return None

    add_action = action.get("addChatItemAction", {})

    if not isinstance(add_action, dict):
        return None

    item = add_action.get("item", {})

    if not isinstance(item, dict):
        return None

    renderer = item.get("liveChatTextMessageRenderer")

    # Super Chatやメンバー登録通知などは除外する。
    if not isinstance(renderer, dict):
        return None

    return renderer


class ChatCounter:
    """チャットファイルを差分読み込みしながら1分単位で集計する。

    ファイル全体を毎回読み直す実装だと、配信が長引くほど1回の
    更新にかかる時間が伸び、UPDATE_INTERVAL_SECONDSより処理が
    遅くなっていく恐れがある。そのため、前回読み終えたバイト
    位置を覚えておき、追記された分だけを読み進める。

    配信中にファイルへ書き込まれている最中の行（まだ改行が
    来ていない最終行）は次回に持ち越し、途中で切れたJSONを
    誤って読み捨てないようにする。
    """

    def __init__(self, start_usec: int) -> None:
        # 配信中（videoOffsetTimeMsecが無い）コメントの経過時間は、
        # このスクリプトを起動した瞬間（起動時刻）を0分の基準にして
        # 計算する。YouTubeの仕様上、配信中に接続した場合は配信
        # 開始時点からの完全な履歴は取得できず、接続直後に直近の
        # 既存コメント（バックログ）がまとめて届くことがあるため、
        # それらは基準より前＝マイナスの分として扱う。
        self._start_usec = start_usec
        self._read_offset = 0
        self.all_counts: Counter[int] = Counter()
        self.laugh_counts: Counter[int] = Counter()
        self.cute_counts: Counter[int] = Counter()

    def _read_new_lines(self, path: Path) -> list[str]:
        with path.open("rb") as file:
            file.seek(self._read_offset)
            chunk = file.read()

        if not chunk:
            return []

        last_newline = chunk.rfind(b"\n")

        if last_newline == -1:
            # 改行がまだ来ていない＝行が書き込み途中。次回に回す。
            return []

        complete = chunk[: last_newline + 1]
        self._read_offset += len(complete)

        return complete.decode("utf-8", errors="ignore").splitlines()

    def update(self, path: Path) -> None:
        """新しく追記された分だけを取り込んで集計を更新する。"""
        for line in self._read_new_lines(path):
            line = line.strip()

            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # 通常は起きないが、念のため壊れた行は無視する。
                continue

            self._consume_record(record)

    def _consume_record(self, record) -> None:
        for actions, offset_msec in find_action_chunks(record):
            for action in actions:
                renderer = extract_text_renderer(action)

                if renderer is None:
                    continue

                minute = self._resolve_minute(renderer, offset_msec)

                if minute is None:
                    continue

                message_text = extract_message_text(renderer)

                self.all_counts[minute] += 1

                if is_laugh_comment(message_text):
                    self.laugh_counts[minute] += 1

                if is_cute_comment(message_text):
                    self.cute_counts[minute] += 1

    def _resolve_minute(self, renderer: dict, offset_msec: int | None):
        if offset_msec is not None:
            return max(0, offset_msec // 60_000)

        try:
            timestamp_usec = int(renderer.get("timestampUsec"))
        except (TypeError, ValueError):
            # 経過時間を判定できないコメントは集計から除外する。
            return None

        # timestampUsecはUNIX時刻（マイクロ秒）なので、起動時刻との
        # 差分をそのまま分単位に丸める。起動前に投稿されたバックログ
        # コメントは差分が負になり、マイナスの分として扱われる。
        return (timestamp_usec - self._start_usec) // 60_000_000


def format_minute_hhmm(minute: int) -> str:
    """分（負の値も可）をH:MM形式に変換する。

    0分＝起動時刻。負の値は起動前に届いたバックログコメントを表し、
    "-"を付けて表示する。
    """
    sign = "-" if minute < 0 else ""
    hours, mins = divmod(abs(minute), 60)

    return f"{sign}{hours}:{mins:02d}"


def write_csv(
    path: Path,
    all_counts: Counter[int],
    laugh_counts: Counter[int],
    cute_counts: Counter[int],
) -> None:
    """集計結果をCSVファイルに保存する。"""
    if not all_counts:
        return

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

        for minute in range(min(all_counts), max(all_counts) + 1):
            writer.writerow(
                [
                    minute,
                    f"{format_minute_hhmm(minute)}:00",
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
    return format_minute_hhmm(int(round(value)))


class LiveGraph:
    """コメント数のグラフを作成し、繰り返し更新するためのクラス。"""

    def __init__(self, video_id: str):
        self.video_id = video_id
        self.closed = False

        plt.ion()
        self.figure, self.ax = plt.subplots(figsize=(12, 6))
        self.figure.canvas.mpl_connect(
            "close_event", self._on_close
        )

        # データ点が1個しかない時でも見えるよう、marker="o"を付ける。
        (self.all_line,) = self.ax.plot(
            [],
            [],
            color="#ff0033",
            linewidth=1.2,
            marker="o",
            markersize=4,
            label="All comments",
        )
        (self.laugh_line,) = self.ax.plot(
            [],
            [],
            color="#0066ff",
            linewidth=1.4,
            marker="o",
            markersize=4,
            label='Comments containing "w", "笑", or "草"',
        )
        (self.cute_line,) = self.ax.plot(
            [],
            [],
            color="#00a65a",
            linewidth=1.4,
            marker="o",
            markersize=4,
            label='Comments containing "かわいい" or "可愛い"',
        )

        # 起動時刻（0分）を示す縦線。マイナス側はバックログコメント。
        self.ax.axvline(
            0,
            color="#888888",
            linestyle="--",
            linewidth=1,
            alpha=0.7,
            label="Script start (0:00)",
        )

        self.ax.set_ylabel("Comments")
        self.ax.set_xlabel("Elapsed time (H:MM, 0:00 = script start)")
        self.ax.xaxis.set_major_formatter(
            FuncFormatter(format_elapsed_time)
        )
        # 縦軸は小さい値でも見やすいよう整数目盛りにする。
        self.ax.yaxis.set_major_locator(
            MaxNLocator(integer=True)
        )
        self.ax.grid(alpha=0.25)
        self.ax.legend(loc="upper left")

        # データが来る前でも軸だけは見える状態にしておく。
        self.ax.set_xlim(0, 1)
        self.ax.set_ylim(0, 1)

        self._set_title("接続中...")
        self.figure.tight_layout()

        # show(block=False)で確実にウィンドウを画面に出す。
        plt.show(block=False)
        self.figure.canvas.draw()
        plt.pause(0.1)

    def _on_close(self, _event) -> None:
        self.closed = True

    def _set_title(self, status: str) -> None:
        self.ax.set_title(
            f"YouTube {self.video_id}\n"
            f"Comments per minute ({status})"
        )

    def update(
        self,
        all_counts: Counter[int],
        laugh_counts: Counter[int],
        cute_counts: Counter[int],
        status: str,
    ) -> None:
        if self.closed or not all_counts:
            return

        first_minute = min(all_counts)
        last_minute = max(all_counts)
        minutes = list(range(first_minute, last_minute + 1))

        self.all_line.set_data(
            minutes,
            [all_counts[m] for m in minutes],
        )
        self.laugh_line.set_data(
            minutes,
            [laugh_counts[m] for m in minutes],
        )
        self.cute_line.set_data(
            minutes,
            [cute_counts[m] for m in minutes],
        )

        self._set_title(status)
        self.ax.set_xlim(
            min(0, first_minute),
            max(1, last_minute),
        )

        # データがどんなに小さくても軸が潰れて見えなくならない
        # よう、最大値に余白を持たせつつ最低限の高さを確保する。
        max_value = max(
            max(all_counts.values(), default=0),
            max(laugh_counts.values(), default=0),
            max(cute_counts.values(), default=0),
        )
        top = max(5, max_value * 1.2)
        self.ax.set_ylim(0, top)

        self.figure.canvas.draw_idle()
        # flush_eventsだけでは反映されないバックエンドがあるため、
        # 実際にGUIイベントループを回すpauseを使って強制的に描画する。
        plt.pause(0.01)

        # 軸が実際に更新されているか目視確認できるようデバッグ出力。
        print(
            f"[debug] x範囲={first_minute}〜{last_minute} "
            f"y範囲={self.ax.get_ylim()} "
            f"最大値(all/laugh/cute)="
            f"{max(all_counts.values())}/"
            f"{max(laugh_counts.values(), default=0)}/"
            f"{max(cute_counts.values(), default=0)}"
        )

    def wait_final(self) -> None:
        """配信/取得が終わった後もウィンドウを開いたままにする。

        plt.show()の内部ブロック処理はバックエンドによっては
        ion()で表示済みのウィンドウに対して正しく働かず、
        すぐに関数が返ってスクリプトごと終了してしまうことが
        ある。そのため、ウィンドウが閉じられるまで自前で
        GUIイベントを回し続ける方式にしている。
        """
        print(
            "グラフウィンドウを閉じるとスクリプトが終了します。"
        )

        while not self.closed:
            try:
                plt.pause(0.5)
            except Exception:
                # ウィンドウが閉じられた際に例外になる
                # バックエンドがあるため、ここで抜ける。
                break


def wait_for_next_update(
    graph: LiveGraph,
    process: subprocess.Popen,
    total_seconds: float,
) -> None:
    """次の集計までの待ち時間を、短い間隔に分けて消化する。

    plt.pause(60)のように一度に長く待つと、その途中でウィンドウを
    閉じても配信が終わっても、最大でその時間分だけ終了検知が
    遅れてしまう（「閉じればすぐ終了する」という説明に反する）。
    そのため短い間隔でポーリングし、状態が変わったら即座に
    抜けられるようにする。
    """
    deadline = time.monotonic() + total_seconds

    while (
        time.monotonic() < deadline
        and not graph.closed
        and process.poll() is None
    ):
        plt.pause(CLOSE_CHECK_INTERVAL_SECONDS)


def run(video_id: str, directory: Path) -> None:
    # 「コード起動時」を0分の基準として使うため、yt-dlpを起動する
    # 直前の時刻を記録しておく。
    start_usec = int(time.time() * 1_000_000)

    process = start_live_chat_capture(video_id, directory)
    chat_path: Path | None = None

    graph = LiveGraph(video_id)
    counter = ChatCounter(start_usec)

    try:
        chat_path = wait_for_chat_file(
            video_id, directory, process
        )

        while True:
            counter.update(chat_path)

            finished = process.poll() is not None
            status = "配信終了/取得完了" if finished else "配信中"

            graph.update(
                counter.all_counts,
                counter.laugh_counts,
                counter.cute_counts,
                status,
            )

            write_csv(
                CSV_PATH,
                counter.all_counts,
                counter.laugh_counts,
                counter.cute_counts,
            )

            if counter.all_counts:
                print(
                    f"[{status}] 通常コメント: "
                    f"{sum(counter.all_counts.values())}件 / "
                    "「w・笑・草」: "
                    f"{sum(counter.laugh_counts.values())}件 / "
                    "「かわいい・可愛い」: "
                    f"{sum(counter.cute_counts.values())}件",
                    end="\r",
                )

            if finished and process.returncode not in (0, None):
                print()
                print(
                    "[警告] yt-dlpが正常終了しませんでした "
                    f"(code={process.returncode})。"
                    "取得できたところまでのデータを表示しています。",
                    file=sys.stderr,
                )

            if finished or graph.closed:
                break

            # sleep()だとGUIのイベントループが止まって描画が反映
            # されないため、pause()でGUIを動かしながら待つ。
            # ただし一度に長く待たず、閉じた／終わったかをこまめに
            # 確認できるよう短い間隔に分けて待つ。
            wait_for_next_update(
                graph, process, UPDATE_INTERVAL_SECONDS
            )

        print()

        if graph.closed:
            print("グラフのウィンドウが閉じられました。")
        else:
            print(f"CSV保存先: {CSV_PATH.resolve()}")
            graph.wait_final()

    finally:
        if process.poll() is None:
            process.terminate()

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def main() -> int:
    """メイン処理。"""
    value = input(
        "配信中またはアーカイブの動画IDかURLを入力してください: "
    ).strip()

    try:
        video_id = extract_video_id(value)

        print(f"matplotlibバックエンド: {plt.get_backend()}")

        with tempfile.TemporaryDirectory(
            prefix="youtube_chat_"
        ) as temp:
            run(video_id, Path(temp))

    except KeyboardInterrupt:
        print("\n中断しました。")
        return 1

    except (ValueError, RuntimeError) as error:
        print(
            f"エラー: {error}",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
