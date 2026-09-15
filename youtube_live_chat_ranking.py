#!/usr/bin/env python3
"""YouTubeのライブチャット（配信中/アーカイブ）から、
「チャットが最も盛り上がった瞬間」のランキングを自動抽出する。

単純な「草・wの件数」だけでランキングを決めると、同接が多い配信や
大規模コラボが必ず上位になってしまう。そのため、各瞬間について

    ・笑い率      = 「草・w・笑・LOL」等を投稿したユニークユーザー数
                    ÷ 同区間の全コメントユーザー数
    ・盛り上がり倍率 = 対象15秒のコメント数 ÷ 前後1分の15秒平均コメント数
    ・ランキングスコア = 笑い率 × 0.6 + 盛り上がり倍率の正規化値 × 0.4

を15秒ごとの窓（1秒刻みでスライド）で計算し、スコア上位かつ互いに
時間の離れた（＝同じオチの重複でない）シーンをTOP10として抽出する。

結果は
    ・スコアの時系列グラフ（TOP10に順位ラベル付き）
    ・TOP10の詳細を数値付きで示す横棒グラフ
    ・全窓データのCSV（ranking_windows.csv）
    ・TOP10だけのCSV（ranking_top10.csv）
として出力する。配信中に起動した場合は配信が終わるまで／グラフの
ウィンドウを閉じるまで、ランキングを更新し続ける。

チャットの取得・解析まわりは youtube_live_chat_graph.py の実装を
そのまま再利用する（同じフォルダに置いておくこと）。

初回のみ:
    py -m pip install -U yt-dlp matplotlib

実行方法:
    VS Codeの実行ボタンを押し、配信中/アーカイブの動画IDまたは
    URLを入力する。APIキーは不要。
    Ctrl+Cまたはグラフウィンドウを閉じるといつでも終了できる。
"""

from __future__ import annotations

import csv
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

try:
    import matplotlib.pyplot as plt
except ModuleNotFoundError:
    print("matplotlibがありません。次を実行してください:")
    print("py -m pip install -U matplotlib yt-dlp")
    raise SystemExit(1)

try:
    import youtube_live_chat_graph as chatlib
except ModuleNotFoundError:
    print(
        "youtube_live_chat_graph.py が見つかりません。"
        "同じフォルダに置いてください。"
    )
    raise SystemExit(1)


WINDOW_CSV_PATH = Path("ranking_windows.csv")
TOP10_CSV_PATH = Path("ranking_top10.csv")

# ランキング対象とする1シーンの長さ（秒）。オチから2〜17秒程度を
# 想定した固定窓で、1秒刻みでスライドさせながら評価する。
WINDOW_SECONDS = 15

# 「前後1分」の意味。この範囲内の15秒平均と比較して盛り上がり倍率を出す。
SURROUND_SECONDS = 60

# 盛り上がり倍率の正規化に使う上限値。これ以上は頭打ちで1.0扱いにする
# （配信ごとにmin-maxで正規化すると、新しいデータが来るたびに過去の
# 順位が揺れてしまうため、固定の基準値を使う）。
MULTIPLIER_CAP = 5.0

RANKING_LAUGH_WEIGHT = 0.6
RANKING_MULTIPLIER_WEIGHT = 0.4

# ノイズ除去用の最低ライン。人数が少なすぎる窓は「たった1人の連投で
# 笑い率100%」のような誤検出を招くため、ランキング候補から除外する。
MIN_WINDOW_COMMENTS = 5
MIN_WINDOW_USERS = 3

# TOP10を選ぶ際、同じオチの重複を避けるために必要な最低間隔（秒）。
MIN_GAP_SECONDS = 20

TOP_N = 10

UPDATE_INTERVAL_SECONDS = 60.0

# 「w」「www」「ｗ」「LOL」「笑」「草」を笑いの合図とみなす。
# 英字に挟まれた"w"（wow, when, with...）は誤検出しないよう除外する。
_LAUGH_W_PATTERN = re.compile(r"(?<![A-Za-z])[wWｗＷ]+(?![A-Za-z])")
_LAUGH_LOL_PATTERN = re.compile(r"\blol\b", re.IGNORECASE)


def is_laugh_comment(message_text: str) -> bool:
    """「草」「笑」「w/www/ｗ」「LOL」のいずれかを含むか判定する。"""
    return (
        bool(_LAUGH_W_PATTERN.search(message_text))
        or bool(_LAUGH_LOL_PATTERN.search(message_text))
        or "笑" in message_text
        or "草" in message_text
    )


def extract_author_id(renderer: dict) -> str:
    """コメント投稿者を一意に識別するIDを取り出す。

    通常はチャンネルIDが入っているが、取得できない場合は表示名で
    代用する（完全に一意ではないが、集計上の目安としては十分）。
    """
    author_id = renderer.get("authorExternalChannelId")

    if isinstance(author_id, str) and author_id:
        return author_id

    author_name = renderer.get("authorName", {})

    if isinstance(author_name, dict):
        name = author_name.get("simpleText")

        if isinstance(name, str) and name:
            return f"name:{name}"

    return "unknown"


def format_seconds_hms(elapsed_seconds: int) -> str:
    """経過秒（負の値も可）をH:MM:SS形式に変換する。"""
    sign = "-" if elapsed_seconds < 0 else ""
    total = abs(elapsed_seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)

    return f"{sign}{hours}:{minutes:02d}:{seconds:02d}"


class SecondBuckets:
    """コメントを投稿秒（配信/取得開始からの経過秒）単位で溜め込む。"""

    def __init__(self) -> None:
        self.all_authors: dict[int, list[str]] = defaultdict(list)
        self.laugh_authors: dict[int, list[str]] = defaultdict(list)

    def add(self, second: int, author_id: str, laugh: bool) -> None:
        self.all_authors[second].append(author_id)

        if laugh:
            self.laugh_authors[second].append(author_id)

    def span(self) -> tuple[int, int] | None:
        if not self.all_authors:
            return None

        return min(self.all_authors), max(self.all_authors)


@dataclass
class WindowStat:
    start_second: int
    all_count: int
    all_users: int
    laugh_count: int
    laugh_users: int
    laugh_rate: float
    surrounding_avg: float
    multiplier: float
    multiplier_norm: float
    score: float
    qualified: bool


def _slide_unique_counts(
    buckets: SecondBuckets,
    min_second: int,
    max_second: int,
) -> dict[int, tuple[int, int, int, int]]:
    """各窓開始秒ごとの(全件数, 全ユニーク数, 笑い件数, 笑いユニーク数)を、
    スライディングウィンドウでO(データ量)で計算する。"""
    last_start = max(min_second, max_second - WINDOW_SECONDS + 1)

    all_counter: Counter[str] = Counter()
    laugh_counter: Counter[str] = Counter()
    all_count = 0
    laugh_count = 0

    def add_second(second: int) -> None:
        nonlocal all_count, laugh_count

        for author in buckets.all_authors.get(second, ()):
            all_counter[author] += 1
            all_count += 1

        for author in buckets.laugh_authors.get(second, ()):
            laugh_counter[author] += 1
            laugh_count += 1

    def remove_second(second: int) -> None:
        nonlocal all_count, laugh_count

        for author in buckets.all_authors.get(second, ()):
            all_counter[author] -= 1
            all_count -= 1

            if all_counter[author] <= 0:
                del all_counter[author]

        for author in buckets.laugh_authors.get(second, ()):
            laugh_counter[author] -= 1
            laugh_count -= 1

            if laugh_counter[author] <= 0:
                del laugh_counter[author]

    # 最初の窓 [min_second, min_second + WINDOW_SECONDS - 1] を構築。
    for second in range(min_second, min_second + WINDOW_SECONDS):
        add_second(second)

    results: dict[int, tuple[int, int, int, int]] = {}
    start = min_second

    while True:
        results[start] = (
            all_count,
            len(all_counter),
            laugh_count,
            len(laugh_counter),
        )

        if start >= last_start:
            break

        remove_second(start)
        add_second(start + WINDOW_SECONDS)
        start += 1

    return results


def compute_windows(buckets: SecondBuckets) -> list[WindowStat]:
    """全スライド窓について、笑い率・盛り上がり倍率・スコアを算出する。"""
    span = buckets.span()

    if span is None:
        return []

    min_second, max_second = span
    raw = _slide_unique_counts(buckets, min_second, max_second)

    surround_offsets = [
        offset
        for offset in range(-SURROUND_SECONDS, SURROUND_SECONDS + 1, WINDOW_SECONDS)
        if offset != 0
    ]

    windows: list[WindowStat] = []

    for start, (all_count, all_users, laugh_count, laugh_users) in sorted(
        raw.items()
    ):
        surrounding_samples = [
            raw[start + offset][0]
            for offset in surround_offsets
            if (start + offset) in raw
        ]
        surrounding_avg = (
            sum(surrounding_samples) / len(surrounding_samples)
            if surrounding_samples
            else 0.0
        )

        if surrounding_avg > 0:
            multiplier = all_count / surrounding_avg
        else:
            multiplier = float(MULTIPLIER_CAP) if all_count > 0 else 0.0

        multiplier_norm = min(multiplier / MULTIPLIER_CAP, 1.0)
        laugh_rate = laugh_users / all_users if all_users > 0 else 0.0
        score = (
            laugh_rate * RANKING_LAUGH_WEIGHT
            + multiplier_norm * RANKING_MULTIPLIER_WEIGHT
        )
        qualified = (
            all_count >= MIN_WINDOW_COMMENTS and all_users >= MIN_WINDOW_USERS
        )

        windows.append(
            WindowStat(
                start_second=start,
                all_count=all_count,
                all_users=all_users,
                laugh_count=laugh_count,
                laugh_users=laugh_users,
                laugh_rate=laugh_rate,
                surrounding_avg=surrounding_avg,
                multiplier=multiplier,
                multiplier_norm=multiplier_norm,
                score=score,
                qualified=qualified,
            )
        )

    return windows


def select_top_n(windows: list[WindowStat], top_n: int = TOP_N) -> list[WindowStat]:
    """スコア上位から、互いに時間の離れたシーンだけを選んでいく
    （同じオチの重複ピークを弾くための貪欲法）。"""
    candidates = sorted(
        (w for w in windows if w.qualified),
        key=lambda w: w.score,
        reverse=True,
    )

    selected: list[WindowStat] = []

    for candidate in candidates:
        if all(
            abs(candidate.start_second - chosen.start_second) >= MIN_GAP_SECONDS
            for chosen in selected
        ):
            selected.append(candidate)

        if len(selected) >= top_n:
            break

    return selected


def write_windows_csv(path: Path, windows: list[WindowStat]) -> None:
    if not windows:
        return

    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "start_second",
                "elapsed_time",
                "all_count",
                "all_users",
                "laugh_count",
                "laugh_users",
                "laugh_rate",
                "surrounding_avg",
                "multiplier",
                "multiplier_norm",
                "score",
                "qualified",
            ]
        )

        for w in windows:
            writer.writerow(
                [
                    w.start_second,
                    format_seconds_hms(w.start_second),
                    w.all_count,
                    w.all_users,
                    w.laugh_count,
                    w.laugh_users,
                    f"{w.laugh_rate:.4f}",
                    f"{w.surrounding_avg:.2f}",
                    f"{w.multiplier:.3f}",
                    f"{w.multiplier_norm:.3f}",
                    f"{w.score:.4f}",
                    int(w.qualified),
                ]
            )


def write_top10_csv(path: Path, top: list[WindowStat]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(
            [
                "rank",
                "elapsed_time",
                "start_second",
                "window_seconds",
                "laugh_comment_count",
                "laugh_unique_users",
                "total_unique_users",
                "laugh_rate_percent",
                "total_comment_count",
                "surrounding_avg_15s",
                "excitement_multiplier",
                "score",
            ]
        )

        for rank, w in enumerate(top, start=1):
            writer.writerow(
                [
                    rank,
                    format_seconds_hms(w.start_second),
                    w.start_second,
                    WINDOW_SECONDS,
                    w.laugh_count,
                    w.laugh_users,
                    w.all_users,
                    f"{w.laugh_rate * 100:.1f}",
                    w.all_count,
                    f"{w.surrounding_avg:.2f}",
                    f"{w.multiplier:.2f}",
                    f"{w.score:.4f}",
                ]
            )


class RankingGraph:
    """スコアの時系列グラフと、TOP10の詳細棒グラフを表示・更新する。"""

    def __init__(self, video_id: str) -> None:
        self.video_id = video_id
        self.closed = False

        plt.ion()
        self.figure, (self.ax_timeline, self.ax_bar) = plt.subplots(
            2, 1, figsize=(13, 10), height_ratios=[1, 1.4]
        )
        self.figure.canvas.mpl_connect("close_event", self._on_close)

        (self.score_line,) = self.ax_timeline.plot(
            [], [], color="#888888", linewidth=1.0, label="ランキングスコア"
        )
        self.top_scatter = self.ax_timeline.scatter(
            [], [], color="#ff0033", zorder=3, label="TOP10"
        )

        self.ax_timeline.set_ylabel("スコア")
        self.ax_timeline.set_xlabel("経過時間 (H:MM:SS、0が起動時刻)")
        self.ax_timeline.grid(alpha=0.25)
        self.ax_timeline.legend(loc="upper left")

        self.figure.suptitle(f"YouTube {video_id} — チャット盛り上がりランキング")
        self._set_status("接続中...")

        plt.show(block=False)
        self.figure.canvas.draw()
        plt.pause(0.1)

    def _on_close(self, _event) -> None:
        self.closed = True

    def _set_status(self, status: str) -> None:
        self.ax_timeline.set_title(f"スコアの推移（{status}）")

    def update(
        self,
        windows: list[WindowStat],
        top: list[WindowStat],
        status: str,
    ) -> None:
        if self.closed or not windows:
            return

        xs = [w.start_second for w in windows]
        ys = [w.score for w in windows]
        self.score_line.set_data(xs, ys)

        top_sorted_by_time = sorted(top, key=lambda w: w.start_second)
        self.top_scatter.set_offsets(
            [(w.start_second, w.score) for w in top_sorted_by_time]
        )

        self.ax_timeline.relim()
        self.ax_timeline.autoscale_view()

        formatter = plt.FuncFormatter(
            lambda value, _pos: format_seconds_hms(int(round(value)))
        )
        self.ax_timeline.xaxis.set_major_formatter(formatter)

        self._set_status(status)
        self._draw_top_bar_chart(top)

        self.figure.tight_layout()
        self.figure.canvas.draw_idle()
        plt.pause(0.01)

    def _draw_top_bar_chart(self, top: list[WindowStat]) -> None:
        self.ax_bar.clear()

        if not top:
            self.ax_bar.set_title("TOP10（まだ十分なデータがありません）")
            return

        # ランクは1位が上に来るよう逆順で並べる。
        ranked = list(enumerate(top, start=1))
        ranked.sort(key=lambda pair: pair[0], reverse=True)

        y_positions = range(len(ranked))
        scores = [w.score for _, w in ranked]
        labels = [f"第{rank}位  {format_seconds_hms(w.start_second)}頃" for rank, w in ranked]

        bars = self.ax_bar.barh(list(y_positions), scores, color="#ff6b6b")
        self.ax_bar.set_yticks(list(y_positions))
        self.ax_bar.set_yticklabels(labels)
        self.ax_bar.set_xlabel("ランキングスコア（笑い率×0.6 + 盛り上がり倍率の正規化値×0.4）")
        self.ax_bar.set_title("TOP10 詳細")
        self.ax_bar.set_xlim(0, 1.0)

        for bar, (_, w) in zip(bars, ranked):
            detail = (
                f"草コメ{w.laugh_count}件（{w.laugh_users}/{w.all_users}人）"
                f" ・笑い率{w.laugh_rate * 100:.0f}%"
                f" ・通常比{w.multiplier:.1f}倍"
                f" ・score {w.score:.2f}"
            )
            self.ax_bar.text(
                min(bar.get_width() + 0.02, 0.78),
                bar.get_y() + bar.get_height() / 2,
                detail,
                va="center",
                fontsize=9,
            )

    def wait_final(self) -> None:
        print("グラフウィンドウを閉じるとスクリプトが終了します。")

        while not self.closed:
            try:
                plt.pause(0.5)
            except Exception:
                break


def wait_for_next_update(
    graph: RankingGraph, process, total_seconds: float
) -> None:
    deadline = time.monotonic() + total_seconds

    while (
        time.monotonic() < deadline
        and not graph.closed
        and process.poll() is None
    ):
        plt.pause(chatlib.CLOSE_CHECK_INTERVAL_SECONDS)


class ChatRankingCounter:
    """チャットファイルを差分読み込みしながら秒単位で集計する。"""

    def __init__(self, start_usec: int) -> None:
        self._start_usec = start_usec
        self._read_offset = 0
        self.buckets = SecondBuckets()

    def update(self, path: Path) -> None:
        with path.open("rb") as file:
            file.seek(self._read_offset)
            chunk = file.read()

        if not chunk:
            return

        last_newline = chunk.rfind(b"\n")

        if last_newline == -1:
            return

        complete = chunk[: last_newline + 1]
        self._read_offset += len(complete)

        for line in complete.decode("utf-8", errors="ignore").splitlines():
            line = line.strip()

            if not line:
                continue

            try:
                import json

                record = json.loads(line)
            except Exception:
                continue

            self._consume_record(record)

    def _consume_record(self, record) -> None:
        for actions, offset_msec in chatlib.find_action_chunks(record):
            for action in actions:
                renderer = chatlib.extract_text_renderer(action)

                if renderer is None:
                    continue

                second = self._resolve_second(renderer, offset_msec)

                if second is None:
                    continue

                message_text = chatlib.extract_message_text(renderer)
                author_id = extract_author_id(renderer)

                self.buckets.add(
                    second, author_id, is_laugh_comment(message_text)
                )

    def _resolve_second(self, renderer: dict, offset_msec: int | None):
        if offset_msec is not None:
            return max(0, offset_msec // 1000)

        try:
            timestamp_usec = int(renderer.get("timestampUsec"))
        except (TypeError, ValueError):
            return None

        return (timestamp_usec - self._start_usec) // 1_000_000


def run(video_id: str, directory: Path) -> None:
    start_usec = int(time.time() * 1_000_000)
    process = chatlib.start_live_chat_capture(video_id, directory)

    graph = RankingGraph(video_id)
    counter = ChatRankingCounter(start_usec)

    try:
        chat_path = chatlib.wait_for_chat_file(video_id, directory, process)

        while True:
            counter.update(chat_path)

            finished = process.poll() is not None
            status = "配信終了/取得完了" if finished else "配信中"

            windows = compute_windows(counter.buckets)
            top = select_top_n(windows)

            graph.update(windows, top, status)
            write_windows_csv(WINDOW_CSV_PATH, windows)
            write_top10_csv(TOP10_CSV_PATH, top)

            if windows:
                print(
                    f"[{status}] 集計窓数: {len(windows)} / "
                    f"TOP10確定数: {len(top)}",
                    end="\r",
                )

            if finished or graph.closed:
                break

            wait_for_next_update(graph, process, UPDATE_INTERVAL_SECONDS)

        print()

        if graph.closed:
            print("グラフのウィンドウが閉じられました。")
        else:
            print(f"全データCSV: {WINDOW_CSV_PATH.resolve()}")
            print(f"TOP10 CSV: {TOP10_CSV_PATH.resolve()}")
            print_top10_table(top)
            graph.wait_final()

    finally:
        chatlib.stop_process(process)


def print_top10_table(top: list[WindowStat]) -> None:
    if not top:
        print("ランキング対象になるシーンが見つかりませんでした。")
        return

    print("\n=== TOP10 チャット盛り上がりランキング ===")

    for rank, w in enumerate(top, start=1):
        print(
            f"第{rank}位 {format_seconds_hms(w.start_second)}頃: "
            f"草コメント{w.laugh_count}件"
            f"（{w.laugh_users}/{w.all_users}人が反応、笑い率{w.laugh_rate * 100:.1f}%）"
            f" ・通常時の{w.multiplier:.1f}倍"
            f" ・score={w.score:.3f}"
        )


def main() -> int:
    value = input(
        "配信中またはアーカイブの動画IDかURLを入力してください: "
    ).strip()

    try:
        video_id = chatlib.extract_video_id(value)
        print(f"matplotlibバックエンド: {plt.get_backend()}")

        with tempfile.TemporaryDirectory(prefix="youtube_ranking_") as temp:
            run(video_id, Path(temp))

    except KeyboardInterrupt:
        print("\n中断しました。")
        return 1

    except (ValueError, RuntimeError) as error:
        print(f"エラー: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
