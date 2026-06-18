"""Channel/key pool management with background health check"""
import json, os, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from curl_cffi import requests as cr


_PROBE_INTERVAL = 60
_PROBE_BATCH = 5
_PROBE_TIMEOUT = 10


class Channel:
    def __init__(self, name, base_url, keys, models, extras=None):
        self.name = name
        self.base_url = base_url.rstrip("/")
        seen = set()
        self.keys = []
        for k in keys:
            k = k.strip()
            if k and k not in seen:
                self.keys.append(k)
                seen.add(k)
        self.models = list(models)
        self.extras = extras or {}
        self.idx = 0
        self.lock = threading.Lock()
        self._probe_pos = 0
        self._dead_file = None
        self._executor = ThreadPoolExecutor(max_workers=_PROBE_BATCH, thread_name_prefix="probe")
        self._keys_file = None
        self._keys_file_mtime = 0
        self._all_keys = set(self.keys)
        self._dead_count = 0  # 累计移除的 key 数

    def set_dead_file(self, path):
        self._dead_file = Path(path)

    def set_keys_file(self, path):
        self._keys_file = Path(path)

    @property
    def alive_count(self):
        return len(self.keys)

    def next_key(self):
        with self.lock:
            if not self.keys:
                return None, None
            key = self.keys[self.idx % len(self.keys)]
            self.idx += 1
            return key, None

    def remove_key(self, key):
        with self.lock:
            if key in self.keys:
                self.keys.remove(key)
                self._dead_count += 1
        if self._dead_file:
            try:
                with open(self._dead_file, "a") as f:
                    f.write(key + "\n")
            except OSError:
                pass

    def add_keys(self, new_keys):
        """追加新 key 到池中 (只加从未见过的 key，防止死 key 回滚)"""
        added = 0
        with self.lock:
            for k in new_keys:
                k = k.strip()
                if k and k not in self._all_keys:
                    self.keys.append(k)
                    self._all_keys.add(k)
                    added += 1
        return added

    def reload_from_file(self):
        """读取 keys 文件,仅追加未见过的新 key"""
        if not self._keys_file or not self._keys_file.exists():
            return 0
        with open(self._keys_file) as f:
            new_keys = [l.strip() for l in f if l.strip()]
        return self.add_keys(new_keys)

    def check_keys_file(self):
        """轮询 keys 文件 mtime,有变化则重新加载"""
        if not self._keys_file or not self._keys_file.exists():
            return
        try:
            mtime = os.path.getmtime(self._keys_file)
        except OSError:
            return
        if mtime > self._keys_file_mtime:
            n = self.reload_from_file()
            self._keys_file_mtime = mtime
            if n:
                print(f"  [hotreload] [{self.name}] +{n} keys")

    def _probe_one(self, key):
        """Check a single key via chat completion (max_tokens=1, ~4 tokens/key)."""
        try:
            probe_model = self.models[0] if self.models else "qwen35-27b"
            r = cr.post(
                f"{self.base_url}/chat/completions",
                json={"model": probe_model,
                      "messages": [{"role": "user", "content": "a"}],
                      "max_tokens": 1, "stream": False},
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                timeout=_PROBE_TIMEOUT,
            )
            if r.status_code == 400 and "Internal server error" in r.text:
                self.remove_key(key)
            r.close()
        except Exception:
            self.remove_key(key)

    def _probe_batch(self):
        with self.lock:
            keys = list(self.keys)
            if not keys:
                return
            n = min(_PROBE_BATCH, len(keys))
            batch = keys[self._probe_pos:self._probe_pos + n]
            self._probe_pos = (self._probe_pos + n) % len(keys)

        list(self._executor.map(self._probe_one, batch))

    def pool_list(self, search="", offset=0, limit=100):
        """返回 key 池列表 (分页+搜索)"""
        with self.lock:
            all_keys = list(self.keys)
        if search:
            all_keys = [k for k in all_keys if search in k]
        total = len(all_keys)
        page = all_keys[offset:offset + limit]
        return total, [{"key": k[:24] + "..." if len(k) > 24 else k,
                        "key_full": k,
                        "index": i + offset} for i, k in enumerate(page)]

    def probe_key(self, key):
        """探测单个 key,返回 True=存活 False=死亡"""
        try:
            probe_model = self.models[0] if self.models else "qwen35-27b"
            r = cr.post(
                f"{self.base_url}/chat/completions",
                json={"model": probe_model,
                      "messages": [{"role": "user", "content": "a"}],
                      "max_tokens": 1, "stream": False},
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                timeout=_PROBE_TIMEOUT,
            )
            ok = r.status_code == 200
            if not ok and r.status_code == 400 and "Internal server error" in r.text:
                self.remove_key(key)
            r.close()
            return ok
        except Exception:
            self.remove_key(key)
            return False

    def admin_remove_key(self, key):
        """从池中和 _all_keys 彻底删除某 key (管理面板操作)"""
        with self.lock:
            if key in self.keys:
                self.keys.remove(key)
            self._all_keys.discard(key)
        if self._dead_file:
            try:
                with open(self._dead_file, "a") as f:
                    f.write(key + "\n")
            except OSError:
                pass

    def append_keys_to_file(self, keys):
        """追加 key 到 keys 文件,使其重启后仍在"""
        if not self._keys_file:
            return
        try:
            with open(self._keys_file, "a") as f:
                for k in keys:
                    f.write(k.strip() + "\n")
        except OSError:
            pass

    def stats(self):
        return {
            "name": self.name,
            "total": len(self.keys),
            "alive": len(self.keys),
            "dead": self._dead_count,
            "models": self.models,
            "base_url": self.base_url,
        }


def _health_loop(channels):
    tick = 0
    while True:
        time.sleep(_PROBE_INTERVAL)
        for ch in channels:
            ch._probe_batch()
            ch.check_keys_file()
        total = sum(ch.alive_count for ch in channels)
        print(f"  [health] alive: {total}")
        tick += 1
        if tick % 10 == 0:
            from src.database import _cleanup
            _cleanup()


def load_config(path="channels.json"):
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)

    channels = []
    model_map = {}

    for ch_cfg in cfg.get("channels", []):
        keys = list(ch_cfg.get("keys", []))
        keys_file = ch_cfg.get("keysFile")
        if keys_file:
            kf = Path(keys_file)
            if not kf.is_absolute():
                kf = path.parent / kf
            if kf.exists():
                with open(kf) as f:
                    keys.extend(l.strip() for l in f if l.strip())
            else:
                print(f"  [!] keysFile not found: {kf}")

        ch = Channel(
            name=ch_cfg["name"],
            base_url=ch_cfg["baseURL"],
            keys=keys,
            models=ch_cfg.get("models", []),
        )
        # 死 key 文件写到 keys 文件同目录
        if keys_file:
            kf = Path(keys_file)
            if not kf.is_absolute():
                kf = path.parent / kf
            ch.set_dead_file(str(kf) + ".dead")
            ch.set_keys_file(str(kf))
            try:
                ch._keys_file_mtime = os.path.getmtime(kf)
            except OSError:
                pass

        channels.append(ch)
        for m in ch.models:
            if m in model_map:
                print(f"  [!] model '{m}' duplicated: {model_map[m].name} <- {ch.name}")
            model_map[m] = ch

    print(f"  已加载 {len(channels)} 个通道, {sum(c.alive_count for c in channels)} 个有效 key")
    for ch in channels:
        print(f"    [{ch.name}] {ch.alive_count} keys")

    t = threading.Thread(target=_health_loop, args=(channels,), daemon=True)
    t.start()

    return {
        "port": cfg.get("port", 3000),
        "channels": channels,
        "model_map": model_map,
        "api_keys": cfg.get("apiKeys", []),
    }
