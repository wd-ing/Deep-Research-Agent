# 部署（单 VPS，面向陌生开发者）

前置：Ubuntu/Debian 主机、Docker（SearXNG）、Caddy 或任意反代、Python 3.12。

## 1. 代码与依赖
```bash
git clone <repo-url> /srv/research-agent && cd /srv/research-agent
python3.12 -m venv .venv
.venv/bin/pip install -e .        # 必须可编辑安装：REPO_ROOT 按文件位置推算
```

## 2. .env（只放两个 key）
```
ARK_API_KEY=…
TAVILY_API_KEY=…
```

## 3. tiktoken 离线坑
服务器连不上 openaipublic.blob.core.windows.net 时，`split.py` 的 tiktoken
会**静默退化为字符估算**（不报错）。预置缓存：
能联网的机器上 `python -c "import tiktoken;tiktoken.get_encoding('cl100k_base')"`，
从 `data-gym-cache` 目录取文件 `9b5ad71b2ce5302211f9c61530b329a4922fc6a4`
（= cl100k_base 词表 URL 的 sha1），放主机的 `TIKTOKEN_CACHE_DIR`。

## 4. SearXNG（中文检索）
```bash
docker run -d --name searxng -p 127.0.0.1:8080:8080 \
  -v $PWD/deploy/searxng-settings.yml:/etc/searxng/settings.yml \
  searxng/searxng:latest
# 先改 secret_key（openssl rand -hex 32）
```

## 5. 用户与限额
```bash
.venv/bin/python -m research_agent.users add --id x --name X \
  [--admin] [--standard N --fast M]   # 默认 2/6，admin 不限
# 输出一次性邀请路径 /api/auth/invite?t=<token>
```

## 6. systemd + Caddy
`research-agent.service` 拷到 /etc/systemd/system/（改 User/路径），
`daemon-reload && enable --now research-agent`。
`Caddyfile.snippet` 的 `research.example.com` 改真实域名。
**单 worker**：`_runs` 并发注册表与 SSE 订阅表都在进程内存里，
多 worker 会互相看不见。`flush_interval -1` 是 SSE 实时推送的关键。

## 7. 重新部署
`git pull && .venv/bin/pip install -e . && cd frontend && npm run build && systemctl restart research-agent`
