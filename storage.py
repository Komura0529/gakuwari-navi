"""学割ナビのデータ保存層。

Azure の設定（環境変数）がある場合は Azure Table Storage / Blob Storage を使い、
無い場合はローカルファイル（JSON + ローカルフォルダ）に保存する。
どちらのモードで動いているかは ``STORAGE_MODE`` で判別でき、画面にも表示する。

- Azure モード: AZURE_STORAGE_CONNECTION_STRING がある場合（接続文字列方式）
  または AZURE_STORAGE_ACCOUNT_NAME がある場合（マネージドID / Azure CLI 資格情報方式）
- ローカルモード: 上記が無い場合。データは instance/local_data/ 以下に保存される。
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCAL_DATA_DIR = os.path.join(BASE_DIR, "instance", "local_data")
LOCAL_UPLOAD_DIR = os.path.join(LOCAL_DATA_DIR, "uploads")

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def storage_mode() -> str:
    """'azure' または 'local' を返す。"""
    if os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or os.environ.get(
        "AZURE_STORAGE_ACCOUNT_NAME"
    ):
        return "azure"
    return "local"


# ---------------------------------------------------------------------------
# ローカル保存（JSONファイル）
# ---------------------------------------------------------------------------

def _local_path(name: str) -> str:
    os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
    return os.path.join(LOCAL_DATA_DIR, f"{name}.json")


def _local_read(name: str) -> list[dict]:
    path = _local_path(name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _local_write(name: str, rows: list[dict]) -> None:
    path = _local_path(name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Azure Table Storage
# ---------------------------------------------------------------------------

def _table_client(table_name: str):
    from azure.data.tables import TableServiceClient

    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn:
        service = TableServiceClient.from_connection_string(conn)
    else:
        from azure.identity import DefaultAzureCredential

        account = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
        service = TableServiceClient(
            endpoint=f"https://{account}.table.core.windows.net",
            credential=DefaultAzureCredential(),
        )
    try:
        service.create_table_if_not_exists(table_name)
    except Exception:
        pass
    return service.get_table_client(table_name)


def _azure_list(table_name: str, partition: str) -> list[dict]:
    client = _table_client(table_name)
    rows = []
    for entity in client.query_entities(f"PartitionKey eq '{partition}'"):
        row = {k: v for k, v in entity.items()}
        rows.append(row)
    return rows


def _azure_upsert(table_name: str, entity: dict) -> None:
    client = _table_client(table_name)
    client.upsert_entity(entity)


def _azure_get(table_name: str, partition: str, row_key: str) -> dict | None:
    from azure.core.exceptions import ResourceNotFoundError

    client = _table_client(table_name)
    try:
        entity = client.get_entity(partition, row_key)
    except ResourceNotFoundError:
        return None
    return {k: v for k, v in entity.items()}


# ---------------------------------------------------------------------------
# 共通インターフェース（投稿・コメント）
# ---------------------------------------------------------------------------

POSTS_TABLE = os.environ.get("AZURE_TABLE_POSTS", "Posts")
COMMENTS_TABLE = os.environ.get("AZURE_TABLE_COMMENTS", "Comments")
POST_PARTITION = "post"
COMMENT_PARTITION = "comment"


def list_posts() -> list[dict]:
    if storage_mode() == "azure":
        rows = _azure_list(POSTS_TABLE, POST_PARTITION)
        return [_row_to_post(r) for r in rows]
    return _local_read("posts")


def get_post(post_id: str) -> dict | None:
    if storage_mode() == "azure":
        row = _azure_get(POSTS_TABLE, POST_PARTITION, post_id)
        return _row_to_post(row) if row else None
    for post in _local_read("posts"):
        if post["id"] == post_id:
            return post
    return None


def save_post(post: dict) -> None:
    if storage_mode() == "azure":
        _azure_upsert(POSTS_TABLE, _post_to_row(post))
        return
    with _lock:
        rows = _local_read("posts")
        rows = [r for r in rows if r["id"] != post["id"]]
        rows.append(post)
        _local_write("posts", rows)


def seed_posts_if_empty(seed: list[dict]) -> int:
    """保存先が空ならサンプルデータを投入し、投入件数を返す。"""
    if list_posts():
        return 0
    for post in seed:
        save_post(post)
    return len(seed)


def list_comments(post_id: str) -> list[dict]:
    if storage_mode() == "azure":
        rows = _azure_list(COMMENTS_TABLE, COMMENT_PARTITION)
        comments = [_row_to_comment(r) for r in rows if r.get("post_id") == post_id]
    else:
        comments = [c for c in _local_read("comments") if c["post_id"] == post_id]
    return sorted(comments, key=lambda c: c["created_at"])


def save_comment(comment: dict) -> None:
    if storage_mode() == "azure":
        _azure_upsert(COMMENTS_TABLE, _comment_to_row(comment))
        return
    with _lock:
        rows = _local_read("comments")
        rows.append(comment)
        _local_write("comments", rows)


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def now_iso() -> str:
    return _now_iso()


# Azure Table は型の制約があるため、数値・真偽値を明示的に変換する
def _post_to_row(post: dict) -> dict:
    row = {"PartitionKey": POST_PARTITION, "RowKey": post["id"]}
    for key, value in post.items():
        if key == "id":
            continue
        row[key] = value
    return row


def _row_to_post(row: dict) -> dict:
    post = {k: v for k, v in row.items() if k not in ("PartitionKey", "RowKey", "Timestamp", "etag")}
    post["id"] = row["RowKey"]
    # Table から戻ると数値が int/float 混在になるため整える
    for key in ("regular_price", "student_price", "likes", "reports"):
        if key in post and post[key] is not None:
            post[key] = int(post[key])
    return post


def _comment_to_row(comment: dict) -> dict:
    row = {"PartitionKey": COMMENT_PARTITION, "RowKey": comment["id"]}
    for key, value in comment.items():
        if key == "id":
            continue
        row[key] = value
    return row


def _row_to_comment(row: dict) -> dict:
    comment = {k: v for k, v in row.items() if k not in ("PartitionKey", "RowKey", "Timestamp", "etag")}
    comment["id"] = row["RowKey"]
    return comment


# ---------------------------------------------------------------------------
# 画像保存（Azure Blob / ローカル）
# ---------------------------------------------------------------------------

BLOB_CONTAINER = os.environ.get("AZURE_BLOB_CONTAINER", "post-images")


def _blob_container_client():
    from azure.storage.blob import BlobServiceClient

    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    if conn:
        service = BlobServiceClient.from_connection_string(conn)
    else:
        from azure.identity import DefaultAzureCredential

        account = os.environ["AZURE_STORAGE_ACCOUNT_NAME"]
        service = BlobServiceClient(
            account_url=f"https://{account}.blob.core.windows.net",
            credential=DefaultAzureCredential(),
        )
    container = service.get_container_client(BLOB_CONTAINER)
    try:
        container.create_container()
    except Exception:
        pass
    return container


# 保存する画像名は「UUID(32桁16進数) + 許可拡張子」のみ。
# open_image でも同じ形式だけを受け付け、パストラバーサルを防ぐ。
IMAGE_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(jpg|jpeg|png|gif|webp)$")


def save_image(data: bytes, ext: str) -> str:
    """画像バイト列を保存し、保存先を表す参照文字列を返す。

    Azure: 'blob:<blob名>' / ローカル: 'local:<ファイル名>'
    画像そのものは非公開とし、Flask の /media/<参照> ルート経由で配信する。
    """
    name = f"{uuid.uuid4().hex}{ext}"
    if not IMAGE_NAME_RE.match(name):
        raise ValueError("invalid image name")
    if storage_mode() == "azure":
        container = _blob_container_client()
        container.upload_blob(name, io.BytesIO(data), overwrite=True)
        return f"blob:{name}"
    os.makedirs(LOCAL_UPLOAD_DIR, exist_ok=True)
    path = os.path.join(LOCAL_UPLOAD_DIR, name)
    # 念のため、保存先がアップロードディレクトリ内であることを確認する
    if os.path.commonpath([os.path.abspath(path), LOCAL_UPLOAD_DIR]) != LOCAL_UPLOAD_DIR:
        raise ValueError("invalid image path")
    with open(path, "wb") as f:
        f.write(data)
    return f"local:{name}"


def open_image(ref: str):
    """(画像バイト列, ファイル名) を返す。形式が不正・存在しない場合は None。

    パストラバーサル対策:
    - ファイル名は UUID 形式 + 許可拡張子のみ受け付ける（'..' や '/' を含む名前は弾く）
    - ローカル保存では、解決後のパスが保存ディレクトリ内にあることを確認する
    """
    kind, _, name = ref.partition(":")
    if kind not in ("blob", "local"):
        return None
    if not IMAGE_NAME_RE.match(name or ""):
        return None
    if kind == "blob":
        if storage_mode() != "azure":
            return None
        container = _blob_container_client()
        stream = container.download_blob(name)
        return stream.readall(), name
    # local
    base = os.path.abspath(LOCAL_UPLOAD_DIR)
    path = os.path.abspath(os.path.join(base, name))
    if os.path.commonpath([path, base]) != base:
        return None
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read(), name
    return None


# ---------------------------------------------------------------------------
# ヘルスチェック（実際に接続して確認する）
# ---------------------------------------------------------------------------

def health_check() -> dict:
    """保存先への実接続を確認し、結果を辞書で返す。

    Azure モードでは Table / Blob の両方へ実際にリクエストを送る。
    失敗した場合は ok=False とし、正常とは表示しない。
    エラーの詳細（接続文字列など）は含めず、例外の種類だけを返す。
    """
    mode = storage_mode()
    result: dict = {"storage": mode, "ok": True, "table": "skipped", "blob": "skipped"}
    if mode == "local":
        try:
            os.makedirs(LOCAL_DATA_DIR, exist_ok=True)
            list_posts()
            result["table"] = "ok (local json)"
            result["blob"] = "ok (local folder)"
        except Exception as exc:
            result["ok"] = False
            result["error"] = type(exc).__name__
        return result

    # Azure: Table へ実際にクエリを投げる
    try:
        client = _table_client(POSTS_TABLE)
        next(client.query_entities("PartitionKey eq 'post'", results_per_page=1), None)
        result["table"] = "ok"
    except Exception as exc:
        result["ok"] = False
        result["table"] = f"error ({type(exc).__name__})"
    # Azure: Blob コンテナーの存在を確認する
    try:
        container = _blob_container_client()
        if not container.exists():
            raise RuntimeError("container not found")
        result["blob"] = "ok"
    except Exception as exc:
        result["ok"] = False
        result["blob"] = f"error ({type(exc).__name__})"
    return result
