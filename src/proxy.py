"""Upstream proxy with key failover"""
import json, time
from curl_cffi import requests as cr

UPSTREAM_TIMEOUT = 180
MAX_RETRIES = 3
MAX_RETRY_SECONDS = 10

_session = cr.Session()


def proxy_chat(channel, body, streaming):
    """(status, data|gen, usage_dict) — usage_dict has prompt_tokens/completion_tokens keys"""
    t0 = time.time()
    tried = 0

    while tried < MAX_RETRIES and (time.time() - t0) < MAX_RETRY_SECONDS:
        key, _ = channel.next_key()
        if not key:
            return 503, b'{"error":"all keys exhausted"}', {}

        tried += 1
        try:
            r = _session.post(
                f"{channel.base_url}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                timeout=UPSTREAM_TIMEOUT,
                stream=streaming,
            )
        except Exception:
            channel.remove_key(key)
            continue

        if r.status_code == 200:
            if streaming:
                usage = {}
                def _gen():
                    nonlocal usage
                    try:
                        for line in r.iter_lines():
                            if line.startswith(b'data: ') and line != b'data: [DONE]':
                                try:
                                    d = json.loads(line[6:])
                                    if "usage" in d and d["usage"]:
                                        usage.clear()
                                        usage.update(d["usage"])
                                except (json.JSONDecodeError, KeyError, TypeError):
                                    pass
                            yield line + b"\n"
                    finally:
                        r.close()
                return 200, _gen(), usage
            data = r.content
            try:
                usage = json.loads(data).get("usage", {})
            except (json.JSONDecodeError, TypeError):
                usage = {}
            return 200, data, usage

        err_text = r.text
        if r.status_code == 400 and "Internal server error" in err_text:
            channel.remove_key(key)
            r.close()
            continue

        err_body = r.content
        r.close()
        return r.status_code, err_body, {}

    return 503, b'{"error":"upstream unavailable"}', {}
