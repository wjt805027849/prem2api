"""prem2api — 带管理面板的 Prem.ai 多账户 API 网关"""
import sys, os, json, time, threading

# 确保 src 可导入
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, request as flask_req, jsonify, render_template, Response as FlaskResponse
from src.channel import load_config
from src.proxy import proxy_chat
from src.database import init as db_init, log_request, stats_summary, stats_by_model, stats_by_channel, stats_timeline, get_logs, add_admin_key, list_admin_keys, toggle_admin_key, delete_admin_key, update_channel_stats

app = Flask(__name__, template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16MB request body limit
_config = None

# ========== Auth middleware ==========

AUTH_EXEMPT = {"/admin", "/api/", "/static/"}

def _check_auth():
    path = flask_req.path
    if not _config or not _config.get("api_keys"):
        return None
    if any(path.startswith(p) for p in AUTH_EXEMPT):
        return None
    auth = flask_req.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:] in _config["api_keys"]:
        return None
    if flask_req.args.get("api_key") in _config["api_keys"]:
        return None
    return jsonify({"error": "unauthorized", "message": "provide Bearer token in Authorization header or ?api_key="}), 401

app.before_request(_check_auth)

@app.after_request
def _add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    return resp

# ========== Proxy (OpenAI compatible) ==========

@app.route("/v1/models", methods=["GET"])
@app.route("/<path:subpath>/models", methods=["GET"])
def list_models(subpath=""):
    models = []
    for ch in _config["channels"]:
        for m in ch.models:
            models.append({"id": m, "object": "model"})
    return jsonify({"object": "list", "data": models})


@app.route("/v1/chat/completions", methods=["POST"])
@app.route("/<path:subpath>/chat/completions", methods=["POST"])
def chat_completions(subpath=""):
    body = flask_req.get_json(silent=True) or {}
    model = body.get("model", "")
    streaming = body.get("stream", False)
    client_ip = flask_req.remote_addr or ""

    ch = _config["model_map"].get(model)
    if not ch:
        available = list(_config["model_map"].keys())
        return jsonify({"error": f"model '{model}' not found", "available_models": available}), 404

    t0 = time.time()
    status, data, usage = proxy_chat(ch, body, streaming)
    t_ttft = time.time()
    ttft_ms = int((t_ttft - t0) * 1000)
    prompt_tokens = usage.get("prompt_tokens", 0) or 0
    completion_tokens = usage.get("completion_tokens", 0) or 0

    if status != 200:
        threading.Thread(target=log_request, args=(model, ch.name, status, 0, 0, ttft_ms, ttft_ms, client_ip), daemon=True).start()
        return FlaskResponse(data, status=status, content_type="application/json")

    if streaming:
        t_last = [t_ttft]
        def generate():
            try:
                for chunk in data:
                    t_last[0] = time.time()
                    yield chunk
            finally:
                total = int((t_last[0] - t0) * 1000)
                pt = usage.get("prompt_tokens", 0)
                ct = usage.get("completion_tokens", 0)
                threading.Thread(target=log_request, args=(model, ch.name, 200, pt, ct, total, ttft_ms, client_ip), daemon=True).start()
        resp = FlaskResponse(generate(), status=200, content_type="text/event-stream")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    total = int((time.time() - t0) * 1000)
    threading.Thread(target=log_request, args=(model, ch.name, 200, prompt_tokens, completion_tokens, total, total, client_ip), daemon=True).start()
    try:
        resp_data = json.loads(data)
        return jsonify(resp_data), 200
    except (json.JSONDecodeError, TypeError):
        return FlaskResponse(data, status=200, content_type="application/json")


# ========== Admin API ==========

@app.route("/admin", methods=["GET"])
def admin_index():
    return render_template("dashboard.html")


@app.route("/admin/keys", methods=["GET"])
def admin_keys_page():
    return render_template("keys.html")


@app.route("/admin/logs", methods=["GET"])
def admin_logs_page():
    return render_template("logs.html")


@app.route("/admin/channels", methods=["GET"])
def admin_channels_page():
    return render_template("channels.html")


# ---- API ----

@app.route("/api/stats", methods=["GET"])
def api_stats():
    hours = int(flask_req.args.get("hours", 24))
    return jsonify({
        "summary": stats_summary(hours),
        "by_model": stats_by_model(hours),
        "by_channel": stats_by_channel(hours),
        "timeline": stats_timeline(hours),
    })


@app.route("/api/logs", methods=["GET"])
def api_logs():
    limit = int(flask_req.args.get("limit", 100))
    offset = int(flask_req.args.get("offset", 0))
    model = flask_req.args.get("model") or None
    status_code = flask_req.args.get("status")
    if status_code is not None:
        status_code = int(status_code)
    return jsonify({
        "logs": get_logs(limit, offset, model, status_code),
        "models": sorted(set(ch.name for ch in _config["channels"])),
    })


@app.route("/api/keys", methods=["GET"])
def api_list_keys():
    return jsonify({"keys": list_admin_keys()})


@app.route("/api/keys/import", methods=["POST"])
def api_import_keys():
    data = flask_req.get_json(silent=True) or {}
    raw = data.get("keys", "")
    label = data.get("label", "")
    count = 0
    for line in raw.strip().split("\n"):
        k = line.strip()
        if k and not k.startswith("#"):
            add_admin_key(k, label)
            count += 1
    return jsonify({"imported": count})


@app.route("/api/keys/toggle", methods=["POST"])
def api_toggle_key():
    data = flask_req.get_json(silent=True) or {}
    toggle_admin_key(data["id"], data.get("active", True))
    return jsonify({"ok": True})


@app.route("/api/keys/<int:kid>", methods=["DELETE"])
def api_delete_key(kid):
    delete_admin_key(kid)
    return jsonify({"ok": True})


@app.route("/api/channels", methods=["GET"])
def api_channels():
    return jsonify({
        "channels": [ch.stats() for ch in _config["channels"]],
    })


# ========== Pool Management API ==========

@app.route("/api/pool", methods=["GET"])
def api_pool():
    """获取 key 池列表 (分页+搜索)"""
    ch_name = flask_req.args.get("channel") or _config["channels"][0].name
    search = flask_req.args.get("search", "")
    offset = int(flask_req.args.get("offset", 0))
    limit = min(int(flask_req.args.get("limit", 100)), 500)
    ch = next((c for c in _config["channels"] if c.name == ch_name), None)
    if not ch:
        return jsonify({"error": "channel not found"}), 404
    total, keys = ch.pool_list(search, offset, limit)
    return jsonify({
        "channel": ch.name,
        "total": total,
        "alive": ch.alive_count,
        "dead": ch.stats()["dead"],
        "keys": keys,
        "offset": offset,
        "limit": limit,
    })


@app.route("/api/pool/add", methods=["POST"])
def api_pool_add():
    """添加 key 到池中 (同时追加到 keys 文件,重启后仍保留)"""
    data = flask_req.get_json(silent=True) or {}
    raw = data.get("keys", "")
    ch_name = data.get("channel") or _config["channels"][0].name
    ch = next((c for c in _config["channels"] if c.name == ch_name), None)
    if not ch:
        return jsonify({"error": "channel not found"}), 404

    new_keys = [l.strip() for l in raw.strip().split("\n") if l.strip()]
    added = ch.add_keys(new_keys)
    if added:
        ch.append_keys_to_file(new_keys)
        update_channel_stats(ch.name, len(ch.keys), ch.alive_count)
    return jsonify({"added": added})


@app.route("/api/pool/upload", methods=["POST"])
def api_pool_upload():
    """上传 .txt 文件,逐行导入 key"""
    ch_name = flask_req.form.get("channel") or _config["channels"][0].name
    ch = next((c for c in _config["channels"] if c.name == ch_name), None)
    if not ch:
        return jsonify({"error": "channel not found"}), 404
    f = flask_req.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400
    content = f.read().decode("utf-8", errors="ignore")
    new_keys = [l.strip() for l in content.split("\n") if l.strip()]
    added = ch.add_keys(new_keys)
    if added:
        ch.append_keys_to_file(new_keys)
        update_channel_stats(ch.name, len(ch.keys), ch.alive_count)
    return jsonify({"added": added, "total": len(new_keys)})


@app.route("/api/pool/remove", methods=["POST"])
def api_pool_remove():
    """从池中删除指定 key"""
    data = flask_req.get_json(silent=True) or {}
    key = data.get("key", "")
    ch_name = data.get("channel") or _config["channels"][0].name
    ch = next((c for c in _config["channels"] if c.name == ch_name), None)
    if not ch:
        return jsonify({"error": "channel not found"}), 404
    ch.admin_remove_key(key)
    update_channel_stats(ch.name, len(ch.keys), ch.alive_count)
    return jsonify({"ok": True})


@app.route("/api/pool/check", methods=["POST"])
def api_pool_check():
    """探测一个 key 是否存活"""
    data = flask_req.get_json(silent=True) or {}
    key = data.get("key", "")
    ch_name = data.get("channel") or _config["channels"][0].name
    ch = next((c for c in _config["channels"] if c.name == ch_name), None)
    if not ch:
        return jsonify({"error": "channel not found"}), 404
    alive = ch.probe_key(key)
    return jsonify({"key": key[:24] + "...", "alive": alive})


@app.route("/api/reload", methods=["POST"])
def api_reload():
    """重新加载 keys 文件,追加新 key 到池中"""
    total = 0
    for ch in _config["channels"]:
        n = ch.reload_from_file()
        total += n
    if total:
        for ch in _config["channels"]:
            update_channel_stats(ch.name, len(ch.keys), ch.alive_count)
    return jsonify({"reloaded": total})


# ========== Entry ==========

def run_server(config, port):
    global _config
    _config = config
    db_init()

    for ch in config["channels"]:
        update_channel_stats(ch.name, len(ch.keys), ch.alive_count)

    print(f"\n  prem2api v2 启动 -> http://127.0.0.1:{port}")
    print(f"  管理面板:  http://127.0.0.1:{port}/admin")
    print(f"  API:       http://127.0.0.1:{port}/v1/chat/completions")
    print(f"  Hot-reload: POST /api/reload (or edit keys file, auto-detected in 60s)")
    print()

    try:
        from waitress import serve
        print("  [serve] waitress (production)")
        serve(app, host="0.0.0.0", port=port)
    except ImportError:
        print("  [serve] flask dev server (install waitress for production)")
        print()
        app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    import signal

    def _on_shutdown(*_):
        print("\n  shutting down...")
        from src.database import _get as db_get
        db_get().close()
        os._exit(0)

    signal.signal(signal.SIGINT, _on_shutdown)
    signal.signal(signal.SIGTERM, _on_shutdown)

    from src.channel import load_config
    cfg_path = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "channels.json"
    port = 3000
    for i, a in enumerate(sys.argv):
        if a == "--port" and i + 1 < len(sys.argv):
            port = int(sys.argv[i + 1])

    cfg = load_config(cfg_path)
    run_server(cfg, port)
