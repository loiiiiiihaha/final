import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://github.com/orgs/google/repositories"
DB_PATH = Path(__file__).resolve().with_name("google_repositories.sqlite3")


class RepositoryPageParser(HTMLParser):
    """HTML に埋め込まれたリポジトリ一覧を読み取る。"""

    def __init__(self):
        super().__init__()
        self.in_data_script = False
        self.page_data = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_data_script = (
                dict(attrs).get("data-target") == "react-app.embeddedData"
            )

    def handle_data(self, data):
        if self.in_data_script:
            payload = json.loads(data).get("payload", {})
            if "orgReposPageRoute" in payload:
                self.page_data = payload["orgReposPageRoute"]

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_data_script = False


def get_retry_delay(retry_after, attempt):
    # 待ち時間の指定がない場合は、1回目は60秒、2回目は120秒待つ。
    minimum_delay = 60 * (attempt + 1)

    if retry_after.isdecimal():
        return max(minimum_delay, int(retry_after))

    if retry_after:
        # Retry-After は秒数のほか、日時で指定される場合もある。
        try:
            retry_at = parsedate_to_datetime(retry_after)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
            return max(minimum_delay, delay)
        except (ValueError, OverflowError):
            pass

    return minimum_delay


def fetch_page(page_number):
    # 取得中にページの順番が変わりにくいよう、名前順で取得する。
    query = urlencode({"q": "sort:name-asc", "page": page_number})
    request = Request(
        f"{BASE_URL}?{query}",
        headers={
            "Accept": "text/html",
            "User-Agent": "StudentRepositoryScraper/1.0",
        },
    )

    for attempt in range(3):
        # 課題の指定どおり1秒待ち、さらに2秒待ってからアクセスする。
        time.sleep(1)
        time.sleep(2)

        try:
            with urlopen(request, timeout=30) as response:
                html = response.read().decode("utf-8")
            break
        except HTTPError as error:
            if error.code != 429:
                raise

            retry_after = error.headers.get("Retry-After", "").strip()
            error.close()

            if attempt == 2:
                raise RuntimeError(
                    f"{page_number}ページ目でアクセス制限が続いています。"
                    "時間をおいて再実行してください。"
                ) from error

            wait_seconds = get_retry_delay(retry_after, attempt)
            print(
                f"{page_number}ページ目でアクセス制限がかかりました。"
                f"{wait_seconds:.0f}秒待ってから同じページを再取得します。",
                flush=True,
            )
            time.sleep(wait_seconds)

    parser = RepositoryPageParser()
    parser.feed(html)
    parser.close()

    page_data = parser.page_data
    if not page_data or page_data.get("errorMessage") or not page_data.get("repositories"):
        raise RuntimeError(
            f"{page_number}ページ目のリポジトリ一覧を取得できませんでした。"
        )

    return page_data


def scrape_repositories():
    repositories = {}
    page_number = 1

    # 1ページ目から最終ページまで順番に取得する。
    while True:
        page_data = fetch_page(page_number)

        for repo in page_data["repositories"]:
            name = repo["name"]
            language = repo["primaryLanguage"]

            if name in repositories:
                raise RuntimeError(
                    f"リポジトリ「{name}」が重複しました。再実行してください。"
                )

            # 主要な言語がない場合は、SQLite に NULL を保存する。
            repositories[name] = (
                name,
                language["name"] if language else None,
                repo["starsCount"],
            )

        page_count = page_data["pageCount"]
        print(
            f"{page_number}/{page_count}ページを取得しました"
            f"（合計 {len(repositories)} 件）",
            flush=True,
        )

        if page_number >= page_count:
            break
        page_number += 1

    # 取得件数を GitHub の一覧に記載された総件数と照合する。
    expected_count = page_data["repositoryCount"]
    if len(repositories) != expected_count:
        raise RuntimeError(
            f"取得件数は {len(repositories)} 件、一覧の総件数は "
            f"{expected_count} 件です。件数が一致しませんでした。"
        )

    return repositories.values()


def save_and_show(repositories):
    with closing(sqlite3.connect(DB_PATH)) as connection:
        # 全件を取得してから、データベースを更新する。
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS repositories (
                    name TEXT PRIMARY KEY NOT NULL,
                    primary_language TEXT,
                    stars INTEGER NOT NULL CHECK (stars >= 0)
                )
            """)

            # 再実行したときは、最新のデータに入れ替える。
            connection.execute("DELETE FROM repositories")
            connection.executemany("""
                INSERT INTO repositories (name, primary_language, stars)
                VALUES (?, ?, ?)
            """, repositories)

        # 保存した全件を SELECT 文で読み出す。
        rows = connection.execute("""
            SELECT name, primary_language, stars
            FROM repositories
            ORDER BY stars DESC, name ASC
        """)

        print("\nリポジトリ名\t主要な言語\tスター数")
        for name, language, stars in rows:
            print(f"{name}\t{language or '—'}\t{stars}")

        count = connection.execute("SELECT COUNT(*) FROM repositories").fetchone()[0]
        print(f"\n保存した件数：{count} 件")
        print(f"データベースの保存先：{DB_PATH}")


if __name__ == "__main__":
    repositories = scrape_repositories()
    save_and_show(repositories)