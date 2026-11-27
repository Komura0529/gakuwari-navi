"""学割ナビ - 高校生向け学割情報共有掲示板（Flask）

起動方法:
    python app.py            # ローカル開発用
    gunicorn app:app         # Azure App Service 用

環境変数で Azure Storage に切り替わる（storage.py 参照）。
Azure 未設定の場合はローカル保存モードでそのまま動作する。
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import time
from datetime import date, datetime
from functools import wraps
from urllib.parse import urlparse

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    session,
    url_for,
)
import io

import storage
from sample_data import AREAS, GENRES, SAMPLE_POSTS

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

app = Flask(__name__)
# 本番では必ず環境変数 SECRET_KEY を設定すること。未設定時は起動ごとにランダム生成する。
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_BYTES", 5 * 1024 * 1024))

ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
ALLOWED_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
# Pillow で判定する実フォーマットと拡張子の対応
PIL_FORMAT_TO_EXT = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp"}
MAX_IMAGE_PIXELS = 40_000_000  # 解凍爆弾（巨大ピクセル数）対策
POST_INTERVAL_SECONDS = 30  # 連投制限
ACTION_INTERVAL_SECONDS = 3  # いいね・通報の連打制限
_last_post_times: dict[str, float] = {}
_last_action_times: dict[str, float] = {}

# 起動時にサンプルデータを投入（保存先が空の場合のみ）
with app.app_context():
    try:
        storage.seed_posts_if_empty(SAMPLE_POSTS)
    except Exception as exc:  # Azure未設定・接続前でもアプリ自体は起動させる
        app.logger.warning("seed failed: %s", exc)


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def discount_rate(post: dict) -> int:
    """割引率（%）を自動計算する。"""
    regular = post.get("regular_price") or 0
    student = post.get("student_price") or 0
    if regular <= 0 or student >= regular:
        return 0
    return round((regular - student) / regular * 100)


def is_expired(post: dict) -> bool:
    try:
        return date.fromisoformat(post.get("valid_until", "")) < date.today()
    except ValueError:
        return False


def image_url(post: dict) -> str:
    image = post.get("image") or ""
    if image.startswith("demo/"):
        return url_for("static", filename=f"images/{image}")
    if ":" in image:  # blob:<name> / local:<name> は Flask 経由で配信
        return url_for("media", ref=image)
    return url_for("static", filename="images/demo/default.svg")


def enrich(post: dict) -> dict:
    post = dict(post)
    post["discount_rate"] = discount_rate(post)
    post["expired"] = is_expired(post)
    post["image_url"] = image_url(post)
    return post


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def check_csrf() -> bool:
    expected = session.get("csrf", "")
    sent = request.form.get("csrf_token", "")
    # セッション側が空の場合に空文字同士で一致してしまうことを防ぐ
    return bool(expected) and bool(sent) and hmac.compare_digest(sent, expected)


def rate_limited(key: str) -> bool:
    now = time.time()
    last = _last_post_times.get(key, 0)
    if now - last < POST_INTERVAL_SECONDS:
        return True
    _last_post_times[key] = now
    return False


def action_limited(key: str) -> bool:
    """いいね・通報などの軽い操作の連打制限。"""
    now = time.time()
    last = _last_action_times.get(key, 0)
    if now - last < ACTION_INTERVAL_SECONDS:
        return True
    _last_action_times[key] = now
    return False


def validate_image(file) -> tuple[bytes, str] | tuple[None, str]:
    """アップロード画像の実体を Pillow で検証する。

    戻り値: (画像バイト列, 拡張子) または (None, エラーメッセージ)。
    拡張子や MIME だけでなく、実際に画像として読み込めるかと
    実フォーマットを確認する。保存は行わない。
    """
    from PIL import Image

    filename = file.filename or ""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTS or file.mimetype not in ALLOWED_IMAGE_MIMES:
        return None, "画像は JPG / PNG / GIF / WebP のいずれかにしてください。"
    data = file.read()
    if not data:
        return None, "画像ファイルが空です。"
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()  # 壊れた画像・偽装ファイルはここで例外になる
        with Image.open(io.BytesIO(data)) as img:
            fmt = img.format
            width, height = img.size
    except Exception:
        return None, "画像ファイルの内容が正しくありません（実際の画像データでない可能性があります）。"
    if fmt not in PIL_FORMAT_TO_EXT:
        return None, "画像は JPG / PNG / GIF / WebP のいずれかにしてください。"
    if width * height > MAX_IMAGE_PIXELS:
        return None, "画像のピクセル数が大きすぎます。小さい画像にしてください。"
    # 拡張子は申告ではなく実フォーマットから決める
    return data, PIL_FORMAT_TO_EXT[fmt]


@app.context_processor
def inject_globals():
    return {
        "csrf_token": csrf_token,
        "genres": GENRES,
        "areas": AREAS,
        "storage_mode": storage.storage_mode(),
        "today": date.today().isoformat(),
    }


@app.before_request
def guard_csrf():
    if request.method == "POST" and not check_csrf():
        abort(400, "不正なリクエストです（CSRFトークンが一致しません）。")


# ---------------------------------------------------------------------------
# ページ
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    q = (request.args.get("q") or "").strip()
    genre = request.args.get("genre") or ""
    area = request.args.get("area") or ""
    sort = request.args.get("sort") or "new"

    posts = [enrich(p) for p in storage.list_posts()]

    def matches(p: dict) -> bool:
        if q and q not in p["store_name"] and q not in p["station"] and q not in p["description"]:
            return False
        if genre and p["genre"] != genre:
            return False
        if area and p["area"] != area:
            return False
        return True

    filtered = [p for p in posts if matches(p)]
    if sort == "discount":
        filtered.sort(key=lambda p: p["discount_rate"], reverse=True)
    elif sort == "price":
        filtered.sort(key=lambda p: p["student_price"])
    elif sort == "popular":
        filtered.sort(key=lambda p: p.get("likes", 0), reverse=True)
    else:  # new
        filtered.sort(key=lambda p: p["created_at"], reverse=True)

    popular = sorted(posts, key=lambda p: p.get("likes", 0), reverse=True)[:3]
    newest = sorted(posts, key=lambda p: p["created_at"], reverse=True)[:3]
    return render_template(
        "index.html",
        posts=filtered,
        popular=popular,
        newest=newest,
        q=q,
        genre=genre,
        area=area,
        sort=sort,
        total=len(posts),
    )


@app.route("/post/<post_id>")
def detail(post_id: str):
    post = storage.get_post(post_id)
    if not post:
        abort(404)
    comments = storage.list_comments(post_id)
    return render_template("detail.html", post=enrich(post), comments=comments)


@app.route("/new", methods=["GET", "POST"])
def new_post():
    if request.method == "GET":
        return render_template("new.html", form={}, errors=[])

    form = {k: (request.form.get(k) or "").strip() for k in (
        "store_name", "genre", "area", "station", "description",
        "regular_price", "student_price", "conditions", "target_grades",
        "valid_until", "official_url",
    )}
    # 1) 入力値の検証（画像の実体検証を含む）。この段階では何も保存しない。
    errors = validate_post_form(form)

    image_data: bytes | None = None
    image_ext = ""
    file = request.files.get("image")
    if file and file.filename:
        image_data, result = validate_image(file)
        if image_data is None:
            errors.append(result)
        else:
            image_ext = result

    if errors:
        return render_template("new.html", form=form, errors=errors), 400

    # 2) 連投制限。検証を通った投稿だけが対象になる。
    if rate_limited(request.remote_addr or "unknown"):
        flash("投稿が短時間に集中しています。少し時間をおいてから投稿してください。")
        return render_template("new.html", form=form, errors=[]), 429

    # 3) ここまで通って初めて画像を保存する。
    image_ref = ""
    if image_data is not None:
        try:
            image_ref = storage.save_image(image_data, image_ext)
        except Exception as exc:
            app.logger.warning("image save failed: %s", exc)
            flash("画像の保存に失敗しました。時間をおいて再度お試しください。")
            return render_template("new.html", form=form, errors=[]), 500

    post = {
        "id": storage.new_id(),
        "store_name": form["store_name"],
        "genre": form["genre"],
        "area": form["area"],
        "station": form["station"],
        "description": form["description"],
        "regular_price": int(form["regular_price"]),
        "student_price": int(form["student_price"]),
        "conditions": form["conditions"],
        "target_grades": form["target_grades"] or "高校生全学年",
        "valid_until": form["valid_until"],
        "confirmed_at": date.today().isoformat(),
        "official_url": form["official_url"],
        "image": image_ref,
        "likes": 0,
        "reports": 0,
        "created_at": storage.now_iso(),
        "is_demo": False,
    }
    storage.save_post(post)
    flash("投稿しました。ありがとうございます！")
    return redirect(url_for("detail", post_id=post["id"]))


def validate_post_form(form: dict) -> list[str]:
    errors = []
    if not (2 <= len(form["store_name"]) <= 60):
        errors.append("店舗名は2〜60文字で入力してください。")
    if form["genre"] not in GENRES:
        errors.append("ジャンルを選択してください。")
    if form["area"] not in AREAS:
        errors.append("エリアを選択してください。")
    if not (1 <= len(form["station"]) <= 30):
        errors.append("最寄り駅を入力してください（30文字まで）。")
    if not (10 <= len(form["description"]) <= 500):
        errors.append("学割内容は10〜500文字で入力してください。")
    for key, label in (("regular_price", "通常価格"), ("student_price", "学割価格")):
        if not re.fullmatch(r"\d{1,7}", form[key] or ""):
            errors.append(f"{label}は数値で入力してください。")
    if not errors and int(form["student_price"]) >= int(form["regular_price"]):
        errors.append("学割価格は通常価格より安くしてください。")
    if len(form["conditions"]) > 300:
        errors.append("利用条件は300文字までです。")
    try:
        date.fromisoformat(form["valid_until"])
    except ValueError:
        errors.append("有効期限の日付が正しくありません。")
    url = form["official_url"]
    if url:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            errors.append("公式サイトURLの形式が正しくありません。")
    return errors


@app.route("/post/<post_id>/comment", methods=["POST"])
def add_comment(post_id: str):
    post = storage.get_post(post_id)
    if not post:
        abort(404)
    name = (request.form.get("name") or "匿名").strip()[:20] or "匿名"
    body = (request.form.get("body") or "").strip()
    if not (1 <= len(body) <= 300):
        flash("コメントは1〜300文字で入力してください。")
        return redirect(url_for("detail", post_id=post_id))
    if rate_limited("comment:" + (request.remote_addr or "unknown")):
        flash("コメントが短時間に集中しています。少し時間をおいてください。")
        return redirect(url_for("detail", post_id=post_id))
    storage.save_comment(
        {
            "id": storage.new_id(),
            "post_id": post_id,
            "name": name,
            "body": body,
            "created_at": storage.now_iso(),
        }
    )
    flash("コメントを投稿しました。")
    return redirect(url_for("detail", post_id=post_id))


@app.route("/post/<post_id>/like", methods=["POST"])
def like_post(post_id: str):
    post = storage.get_post(post_id)
    if not post:
        abort(404)
    # 連打対策：同一セッションでは同じ投稿に1回まで＋短時間の連続操作を制限
    liked = session.setdefault("liked_posts", [])
    if post_id in liked:
        flash("この投稿にはすでに「いいね」しています。")
        return redirect(url_for("detail", post_id=post_id))
    if action_limited("like:" + (request.remote_addr or "unknown")):
        flash("操作が短時間に集中しています。少し時間をおいてください。")
        return redirect(url_for("detail", post_id=post_id))
    post["likes"] = int(post.get("likes", 0)) + 1
    storage.save_post(post)
    liked.append(post_id)
    session["liked_posts"] = liked[-200:]
    return redirect(url_for("detail", post_id=post_id))


@app.route("/post/<post_id>/report", methods=["POST"])
def report_post(post_id: str):
    """不適切な投稿への対応手段：通報を受け付け、管理確認用に件数を記録する。"""
    post = storage.get_post(post_id)
    if not post:
        abort(404)
    # 連打対策：同一セッションでは同じ投稿に1回まで＋短時間の連続操作を制限
    reported = session.setdefault("reported_posts", [])
    if post_id in reported:
        flash("この投稿はすでに通報済みです。運営が内容を確認します。")
        return redirect(url_for("detail", post_id=post_id))
    if action_limited("report:" + (request.remote_addr or "unknown")):
        flash("操作が短時間に集中しています。少し時間をおいてください。")
        return redirect(url_for("detail", post_id=post_id))
    post["reports"] = int(post.get("reports", 0)) + 1
    storage.save_post(post)
    reported.append(post_id)
    session["reported_posts"] = reported[-200:]
    flash("通報を受け付けました。運営が内容を確認します。")
    return redirect(url_for("detail", post_id=post_id))


@app.route("/favorites")
def favorites():
    return render_template("favorites.html")


@app.route("/media/<path:ref>")
def media(ref: str):
    """画像配信。Blob/ローカルとも非公開のまま Flask 経由で返す。"""
    result = storage.open_image(ref)
    if not result:
        abort(404)
    data, name = result
    ext = os.path.splitext(name)[1].lower()
    mime = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml",
    }.get(ext, "application/octet-stream")
    return send_file(io.BytesIO(data), mimetype=mime, download_name=name)


STATIC_SITE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static_site")


@app.route("/static-demo/")
def static_demo_index():
    """静的サイト版を同じ App Service から公開する（/static-demo/）。"""
    return send_from_directory(STATIC_SITE_DIR, "index.html")


@app.route("/static-demo/<path:filename>")
def static_demo_files(filename: str):
    return send_from_directory(STATIC_SITE_DIR, filename)


@app.route("/health")
def health():
    """保存先への実接続を確認する。失敗時は 503 で正常とは表示しない。"""
    result = storage.health_check()
    body = {
        "status": "ok" if result["ok"] else "error",
        "storage": result["storage"],
        "table": result["table"],
        "blob": result["blob"],
    }
    return body, 200 if result["ok"] else 503


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="ページが見つかりませんでした。"), 404


@app.errorhandler(413)
def too_large(_e):
    return render_template(
        "error.html", code=413,
        message="画像ファイルが大きすぎます（上限5MB）。サイズを小さくして再度お試しください。",
    ), 413


@app.errorhandler(400)
def bad_request(_e):
    return render_template("error.html", code=400, message="リクエストが正しくありません。"), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
