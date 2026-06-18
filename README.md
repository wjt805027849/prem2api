# prem2api

将多个 Prem.ai 免费账户汇聚成一个 OpenAI 兼容的 API 端点。

## 特性

- **多账户聚合** — 把 N 个 Prem 免费账号的 key 合并成一个池
- **自动轮询** — 请求均匀分配到所有可用 key
- **故障自动转移** — key 耗尽 (400 + Internal server error) 自动换下一个,无中断
- **流式传输** — 完整支持 SSE streaming
- **OpenAI 兼容** — 直接替换 `baseURL` 即可接入任何 OpenAI 客户端
- **多通道** — 可配置多个上游,按模型名自动路由
- **健康检查** — `/health` 端点实时查看各通道状态

## 架构

```
OpenCode / 任意客户端
        │
        ▼
  prem2api :3000        ← 本代理
        │
        ▼
  pcci-proxy :3100      ← Prem 加密中继 (confidential-proxy)
        │
        ▼
  Prem.ai API           ← Prem 官方接口
```

## 快速开始

### 1. 安装

```bash
pip install -r requirements.txt
```

### 2. 配置

复制 `config.example.json` 为 `channels.json`,修改 key 文件路径:

```json
{
  "port": 3000,
  "channels": [
    {
      "name": "prem",
      "baseURL": "http://127.0.0.1:3100/openai/v1",
      "keysFile": "prem_api_keys.txt",
      "models": ["deepseek-v4-pro", "qwen35-27b", "qwen36-27b"]
    }
  ]
}
```

### 3. 启动

```bash
# 启动 pcci-proxy (Prem 加密代理)
npx confidential-proxy --api-key YOUR_KEY

# 启动 prem2api
python prem2api.py

# 或双击 run.bat (Windows)
```

### 4. 使用

```python
from openai import OpenAI
client = OpenAI(
    api_key="任意值",  # prem2api 不验证客户端 key (除非配置了 apiKeys)
    base_url="http://127.0.0.1:3000/v1",
)
# 自动轮询所有 Prem key
response = client.chat.completions.create(
    model="deepseek-v4-pro",
    messages=[{"role": "user", "content": "Hello"}],
)
```

## 配置文件

### 选项说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `port` | int | 监听端口,默认 3000 |
| `apiKeys` | string[] | 客户端鉴权 key 列表(可选) |
| `channels` | Channel[] | 上游通道列表 |

### Channel

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 通道名称 |
| `baseURL` | string | 上游请求地址 |
| `keys` | string[] | API Key 列表(内联) |
| `keysFile` | string | API Key 文件路径(与 keys 二选一) |
| `models` | string[] | 该通道支持的模型名列表 |

## API 端点

| 路径 | 方法 | 说明 |
|------|------|------|
| `/v1/models` | GET | 模型列表 |
| `/v1/chat/completions` | POST | 聊天补全(支持 stream) |
| `/health` | GET | 健康检查 + 通道状态 |
| `/admin` | GET | 管理后台 UI |
| `/api/reload` | POST | 热加载 keys 文件 |
| `/api/pool` | GET/POST | Key 池管理 API |

## 多通道示例

如需同时接入 Prem 和其他提供商:

```json
{
  "channels": [
    {
      "name": "prem",
      "baseURL": "http://127.0.0.1:3100/openai/v1",
      "keysFile": "prem_api_keys.txt",
      "models": ["deepseek-v4-pro", "qwen35-27b", "qwen36-27b"]
    },
    {
      "name": "axiom",
      "baseURL": "https://axiomcode.top/v1",
      "keys": ["sk-your-axiom-key"],
      "models": ["deepseek-v4-flash"]
    }
  ]
}
```

请求时根据 `model` 字段自动路由到对应通道。

## 与 OpenCode 集成

在 `opencode.json` 中添加:

```json
{
  "provider": {
    "prem2api": {
      "models": { "deepseek-v4-pro": {} },
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "apiKey": "sk-any",
        "baseURL": "http://127.0.0.1:3000/v1"
      }
    }
  }
}
```

## License

MIT
