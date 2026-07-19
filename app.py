"""局域网文件互传工具 — Flask 服务端。

私密投递：上传时必须指定接收人，仅接收人可见可下载；
接收人下载后实时通知上传者；上传者可手动删除。
"""

import json
import os
import queue
import secrets
import socket
import threading
import uuid

from flask import Flask, Response, jsonify, render_template, request, send_file, stream_with_context

import db

app = Flask(__name__)
COOKIE_NAME = "lanfile_token"
COOKIE_MAX_AGE = 365 * 24 * 3600
MAX_NICKNAME_LEN = 20
DOWNLOAD_NOTIFY_DEDUP_SECONDS = 60
SSE_HEARTBEAT_SECONDS = 15
MAX_QUEUES_PER_USER = 5


# ---------- SSE 推送中心 ----------

_hub = {}          # user_id -> [queue.Queue, ...]（每打开一个标签页一个队列）
_hub_lock = threading.Lock()


def _sse_register(user_id):
    q = queue.Queue(maxsize=100)
    with _hub_lock:
        queues = _hub.setdefault(user_id, [])
        if len(queues) >= MAX_QUEUES_PER_USER:
            queues.pop(0)  # 踢掉最旧的标签页连接
        queues.append(q)
    return q


def _sse_unregister(user_id, q):
    with _hub_lock:
        queues = _hub.get(user_id)
        if queues and q in queues:
            queues.remove(q)
            if not queues:
                del _hub[user_id]


def is_online(user_id):
    with _hub_lock:
        return user_id in _hub


def notify(user_id, event_type, payload):
    """向某用户的所有标签页推送事件。队列满则丢弃——客户端重连后会全量刷新，通知丢失无害。"""
    message = "event: {}\ndata: {}\n\n".format(event_type, json.dumps(payload, ensure_ascii=False))
    with _hub_lock:
        queues = list(_hub.get(user_id, []))
    for q in queues:
        try:
            q.put_nowait(message)
        except queue.Full:
            pass


# ---------- 鉴权 ----------

def current_user(conn):
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    return db.get_user_by_token(conn, token)


def error(message, status):
    return jsonify({"error": message}), status


# ---------- 路由：身份 ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/me")
def api_me():
    conn = db.connect()
    try:
        user = current_user(conn)
        if not user:
            return error("未登录", 401)
        return jsonify({"id": user["id"], "nickname": user["nickname"]})
    finally:
        conn.close()


@app.route("/api/register", methods=["POST"])
def api_register():
    data = request.get_json(silent=True) or {}
    nickname = (data.get("nickname") or "").strip()
    if not nickname:
        return error("请输入昵称", 400)
    if len(nickname) > MAX_NICKNAME_LEN:
        return error("昵称不能超过 {} 个字".format(MAX_NICKNAME_LEN), 400)

    conn = db.connect()
    try:
        token = secrets.token_urlsafe(32)
        try:
            user_id = db.create_user(conn, nickname, token)
        except Exception:
            return error("该昵称已被使用，请换一个", 409)
        resp = jsonify({"id": user_id, "nickname": nickname})
        resp.set_cookie(
            COOKIE_NAME, token,
            max_age=COOKIE_MAX_AGE, httponly=True, samesite="Lax", path="/",
        )
        return resp
    finally:
        conn.close()


# ---------- 路由：用户 / 文件列表 ----------

@app.route("/api/users")
def api_users():
    conn = db.connect()
    try:
        user = current_user(conn)
        if not user:
            return error("未登录", 401)
        return jsonify([
            {"id": u["id"], "nickname": u["nickname"], "online": is_online(u["id"])}
            for u in db.list_users(conn)
        ])
    finally:
        conn.close()


def _file_base_json(f):
    return {
        "id": f["id"],
        "original_name": f["original_name"],
        "size": f["size"],
        "mime": f["mime"],
        "created_at": f["created_at"],
    }


@app.route("/api/files/inbox")
def api_inbox():
    conn = db.connect()
    try:
        user = current_user(conn)
        if not user:
            return error("未登录", 401)
        result = []
        for f in db.list_inbox(conn, user["id"]):
            item = _file_base_json(f)
            item["uploader"] = {"id": f["uploader_id"], "nickname": f["uploader_nickname"]}
            item["downloaded"] = f["my_downloaded_at"] is not None
            item["downloaded_at"] = f["my_downloaded_at"]
            result.append(item)
        return jsonify(result)
    finally:
        conn.close()


@app.route("/api/files/outbox")
def api_outbox():
    conn = db.connect()
    try:
        user = current_user(conn)
        if not user:
            return error("未登录", 401)
        result = []
        for f in db.list_outbox(conn, user["id"]):
            item = _file_base_json(f)
            item["recipient"] = {"id": f["recipient_id"], "nickname": f["recipient_nickname"]}
            item["downloads"] = [
                {
                    "downloader": {"id": d["downloader_id"], "nickname": d["downloader_nickname"]},
                    "downloaded_at": d["downloaded_at"],
                }
                for d in db.list_downloads_for_file(conn, f["id"])
            ]
            result.append(item)
        return jsonify(result)
    finally:
        conn.close()


# ---------- 路由：上传 / 下载 / 删除 ----------

@app.route("/api/files", methods=["POST"])
def api_upload():
    conn = db.connect()
    tmp_path = None
    try:
        user = current_user(conn)
        if not user:
            return error("未登录", 401)

        upload = request.files.get("file")
        if not upload or not upload.filename:
            return error("请选择要发送的文件", 400)

        recipient_id = request.form.get("recipient_id", "").strip()
        if not recipient_id.isdigit():
            return error("请选择接收人", 400)
        recipient_id = int(recipient_id)
        if recipient_id == user["id"]:
            return error("不能发送给自己", 400)
        recipient = db.get_user_by_id(conn, recipient_id)
        if not recipient:
            return error("接收人不存在", 400)

        file_id = uuid.uuid4().hex
        tmp_path = os.path.join(db.FILES_DIR, ".tmp-" + file_id)
        final_path = os.path.join(db.FILES_DIR, file_id)
        try:
            upload.save(tmp_path)  # werkzeug 流式落盘，不把大文件读进内存
            os.replace(tmp_path, final_path)  # 原子改名
            tmp_path = None
        except OSError:
            return error("文件保存失败，可能是磁盘空间不足", 500)

        size = os.path.getsize(final_path)
        mime = upload.mimetype or "application/octet-stream"
        original_name = os.path.basename(upload.filename)  # 防路径注入，原名仅用于展示
        db.create_file(conn, file_id, user["id"], recipient_id, original_name, file_id, size, mime)

        notify(recipient_id, "file_received", {
            "file_id": file_id,
            "name": original_name,
            "size": size,
            "from": {"id": user["id"], "nickname": user["nickname"]},
            "created_at": db.now(),
        })
        return jsonify({"id": file_id, "original_name": original_name, "size": size}), 201
    finally:
        conn.close()
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@app.route("/api/files/<file_id>/download")
def api_download(file_id):
    conn = db.connect()
    try:
        user = current_user(conn)
        if not user:
            return error("未登录", 401)

        f = db.get_file(conn, file_id)
        if not f or f["status"] != "active":
            return error("文件不存在或已被删除", 404)
        if f["recipient_id"] != user["id"]:
            return error("只有指定接收人可以下载该文件", 403)

        path = os.path.join(db.FILES_DIR, f["stored_name"])
        if not os.path.exists(path):
            return error("文件已丢失", 404)

        # 通知去重：Range 续传请求、60 秒内同一人重复请求，都不重复记录/通知
        range_header = request.headers.get("Range", "")
        is_continuation = range_header and not range_header.startswith("bytes=0-")
        if not is_continuation and not db.recent_download_exists(
            conn, file_id, user["id"], DOWNLOAD_NOTIFY_DEDUP_SECONDS
        ):
            ts = db.record_download(conn, file_id, user["id"])
            notify(f["uploader_id"], "file_downloaded", {
                "file_id": file_id,
                "name": f["original_name"],
                "by": {"id": user["id"], "nickname": user["nickname"]},
                "downloaded_at": ts,
            })

        resp = send_file(
            path,
            as_attachment=True,
            download_name=f["original_name"],  # werkzeug 自动做 RFC 5987 编码，中文名安全
            conditional=True,                  # 支持 Range / 断点续传
        )
        # werkzeug 生成的 ASCII 回退文件名会把中文名剥成 ".txt" 这类纯扩展名，
        # iOS 旧版本用它保存时会变成隐藏文件。重写一个可读的回退名。
        resp.headers["Content-Disposition"] = content_disposition(f["original_name"])
        return resp
    finally:
        conn.close()


@app.route("/api/files/<file_id>", methods=["DELETE"])
def api_delete(file_id):
    conn = db.connect()
    try:
        user = current_user(conn)
        if not user:
            return error("未登录", 401)

        f = db.get_file(conn, file_id)
        if not f or f["status"] != "active":
            return error("文件不存在或已被删除", 404)
        if f["uploader_id"] != user["id"]:
            return error("只有上传者可以删除该文件", 403)

        try:
            os.remove(os.path.join(db.FILES_DIR, f["stored_name"]))
        except FileNotFoundError:
            pass
        db.soft_delete_file(conn, file_id)

        notify(f["recipient_id"], "file_deleted", {
            "file_id": file_id,
            "name": f["original_name"],
        })
        return "", 204
    finally:
        conn.close()


# ---------- 路由：SSE ----------

def content_disposition(filename):
    """构造 Content-Disposition：filename* 用 RFC 5987 编码原名（现代浏览器/Safari 用它），
    filename 提供安全可读的 ASCII 回退（中文剥光时补 download 前缀，避免变成隐藏文件）。"""
    from urllib.parse import quote
    ascii_name = filename.encode("ascii", "ignore").decode().strip()
    if not ascii_name or ascii_name.startswith("."):
        ext = os.path.splitext(filename)[1]
        ascii_name = "download" + (ext if ext else "")
    ascii_name = ascii_name.replace("\\", "_").replace('"', "_")
    return "attachment; filename=\"{}\"; filename*=UTF-8''{}".format(
        ascii_name, quote(filename)
    )


@app.route("/api/events")
def api_events():
    conn = db.connect()
    try:
        user = current_user(conn)
        if not user:
            return error("未登录", 401)
        user_id = user["id"]
    finally:
        conn.close()

    def gen():
        q = _sse_register(user_id)
        try:
            yield "retry: 3000\n: connected\n\n"
            while True:
                try:
                    yield q.get(timeout=SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield ": ping\n\n"  # 心跳，防空闲断连
        finally:
            _sse_unregister(user_id, q)

    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            # 不要设置 Connection 头：它是 hop-by-hop 头，waitress 会按 PEP 3333 拒绝
        },
    )


# ---------- 启动 ----------

def cleanup_orphan_tmp():
    """清理上次异常退出留下的 .tmp-* 孤儿文件。"""
    try:
        for name in os.listdir(db.FILES_DIR):
            if name.startswith(".tmp-"):
                os.remove(os.path.join(db.FILES_DIR, name))
    except OSError:
        pass


def get_lan_ip():
    """UDP 连接外部地址取本机局域网 IP（不实际发包）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    db.init_db()
    cleanup_orphan_tmp()
    port = 8000
    print("=" * 50)
    print("  局域网文件互传已启动")
    print("  本机访问:   http://127.0.0.1:{}".format(port))
    print("  局域网访问: http://{}:{}".format(get_lan_ip(), port))
    print("=" * 50)
    # 用 waitress（生产级 WSGI 服务器）而不是 Flask 自带开发服务器：
    # iOS Safari 对 werkzeug 开发服务器的 HTTP/1.0/连接关闭行为不兼容，下载会卡死。
    # threads 要够大：每个 SSE 长连接占一个线程。
    from waitress import serve
    serve(app, host="0.0.0.0", port=port, threads=32, channel_timeout=300)
