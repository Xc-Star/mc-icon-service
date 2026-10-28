# mc-icon-service

给 Minecraft 材料统计类插件（如 `astrbot_plugin_mc_admin`）提供**完整、可自动更新**的方块/物品图标。

![预览](preview.png)

Mojang 官方客户端 jar 里只有 16×16 贴图，楼梯、台阶、栅栏、门这类方块是靠模型拼出来的，没有独立贴图。所以这个服务把两个来源合并：

| 来源 | 提供什么 | 覆盖 |
| --- | --- | --- |
| Minecraft Wiki 渲染图 | 等距视角方块/物品图标（含合成方块、动画 GIF） | 1697 个 |
| Mojang 客户端 jar | 官方贴图，wiki 缺图时兜底 | 54 个 |

26.3 实测：1815 个方块/物品 id 里产出 **1751 个图标**（96.5%），剩下 64 个是 `air`、`light`、`moving_piston`、彩色床这类本身没有独立外观的 id。

产物统一成 **96×96 透明画布**，命名与插件一致：`minecraft_<id>.png` / `.gif`，并额外生成 `material_icon_map.json`（`基础名 → 后缀`）。

## 快速开始

```bash
cd mc-icon-service
python3 -m venv .venv && .venv/bin/pip install fastapi uvicorn httpx pillow
cp .env.example .env                            # 填上传参数（见下）

.venv/bin/python main.py list-versions          # 看最新版本
.venv/bin/python main.py update                 # 构建最新正式版
.venv/bin/python main.py update --upload        # 构建后立刻上传（等价于 UPLOAD_AFTER_BUILD=true）
.venv/bin/python main.py update --limit 20      # 冒烟测试，只跑前 20 个 id
.venv/bin/python main.py info                   # 查看当前构建
.venv/bin/python main.py upload --dry-run       # 预览上传请求，不真传
.venv/bin/python main.py upload                 # 把图标推到图床
.venv/bin/python main.py serve                  # 起服务，默认 :8618
```

首次全量构建 1815 个 id 约 16 分钟（受 wiki 限速影响）；之后有 `data/cache/wiki/` 缓存，重建只要几分钟。客户端 jar 缓存在 `data/cache/`，不重复下载。

配置从 `.env`（或 `ENV_FILE` 指定的文件）读取，**同名环境变量优先级更高**，所以 `PORT=9000 python main.py serve` 可以临时覆盖。

## 上传到图床

对接 CloudFlare ImgBed 的 `POST /upload`（普通 multipart 上传），所有请求参数都在 `.env` 里指定：

| 环境变量 | API 参数 | 默认 | 说明 |
| --- | --- | --- | --- |
| `UPLOAD_BASE_URL` | — | 空 | 图床站点，如 `https://cfbed.sanyue.de` |
| `UPLOAD_PATH` | — | `/upload` | 上传端点 |
| `UPLOAD_AUTH_CODE` | `authCode` | 空 | 上传认证码（query） |
| `UPLOAD_TOKEN` | — | 空 | API Token，走 `Authorization: Bearer`，需 `upload` 权限 |
| `UPLOAD_CHANNEL` | `uploadChannel` | `telegram` | `telegram`/`cfr2`/`s3`/`discord`/`huggingface`/`webdav` |
| `UPLOAD_CHANNEL_NAME` | `channelName` | 空 | 多渠道时指定渠道名 |
| `UPLOAD_FOLDER` | `uploadFolder` | `mc_admin` | 上传目录，插件按 `/file/mc_admin/<文件名>` 取图 |
| `UPLOAD_NAME_TYPE` | `uploadNameType` | `origin` | **必须 `origin`**，否则文件名被改写，插件拼的 URL 就对不上 |
| `UPLOAD_RETURN_FORMAT` | `returnFormat` | `default` | `default` 返回 `/file/id`，`full` 返回完整链接 |
| `UPLOAD_SERVER_COMPRESS` | `serverCompress` | `false` | 服务端压缩（仅 Telegram 图片），关掉以免动 PNG/GIF |
| `UPLOAD_AUTO_RETRY` | `autoRetry` | `true` | 失败自动切渠道 |
| `UPLOAD_EXTRA_PARAMS` | 任意 | 空 | 额外 query，逗号分隔 `k=v`，图床加参数时不用改代码 |

上传行为：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `UPLOAD_CONCURRENCY` | `2` | 并发上传数 |
| `UPLOAD_TIMEOUT` | `60` | 单请求超时（秒） |
| `UPLOAD_SKIP_EXISTING` | `true` | 用 HEAD 探测已存在的文件并跳过 |
| `UPLOAD_PUBLIC_BASE` | 空 | 公开访问前缀（如 `https://img.xcstar.top/file`），**不填则无法探测、每次全量上传** |
| `UPLOAD_AFTER_BUILD` | `false` | 每次构建成功后自动上传 |

上传结果写到 `data/upload-report.json`（`authCode` 已打码），失败的条目带原因，可以直接 `--force` 重跑。报告里的 `name_mismatch` 不为 0 说明文件名被图床改写了，检查 `UPLOAD_NAME_TYPE`。

> **目录要和插件对得上**：`astrbot_plugin_mc_admin` 拼的是 `https://img.xcstar.top/file/mc_admin/<文件名>`，所以 `UPLOAD_FOLDER` 应该是 `mc_admin`。如果填成 `mc_admin/task_icon`，实际地址会变成 `/file/mc_admin/task_icon/...`，插件取不到图，需要同步改插件里的路径。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 存活 + 是否已就绪 |
| GET | `/version` | 当前构建的 MC 版本、图标数、来源统计 |
| GET | `/map` | `material_icon_map.json`，可直接替换插件里的同名文件 |
| GET | `/icons/{name}` | `minecraft_stone.png` 或简写 `stone` |
| POST | `/refresh?force=true` | 立即检查更新/重建，配了 `REFRESH_TOKEN` 时需 `X-Token` 头 |

**启动行为**（`UPDATE_ON_START=true` 时）：

1. 先起 HTTP 服务，随后后台拉一次版本清单（`piston-meta`，约 2 秒）。
2. 没有构建、或清单里的版本和 `data/state.json` 不一致 → 构建（空目录首次约 16 分钟，这期间 `/version`、`/icons/*` 返回 503）。
3. 版本一致 → 日志打 `already on <版本>, nothing to build`，直接复用，不重建。
4. 之后每 `CHECK_INTERVAL` 秒再检查一次，发现新版本才重建并清理旧目录。

重建是**原地后台进行**的，旧版本目录在 `state.json` 更新前继续对外服务，所以升级 MC 版本不会中断请求。`UPDATE_ON_START=false` 则启动完全不检查，只按 `CHECK_INTERVAL` 定时查。

上传只在**真正发生构建**时自动触发（`UPLOAD_AFTER_BUILD=true`）；启动检查发现版本没变不会重复上传，要强制推送用 `upload` 或 `update --upload`。

## 配置

全部通过环境变量，见 `.env.example`：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATA_DIR` | `./data` | 缓存、构建产物、状态文件 |
| `PORT` / `HOST` | `8618` / `0.0.0.0` | 监听地址 |
| `MC_VERSION` | `latest` | `latest`、`latest-snapshot` 或具体版本号 |
| `MC_MIRROR_URL` | 空 | Mojang 镜像，如 `https://bmclapi2.bangbang93.com`；设置后清单/版本 json/jar 全走镜像 |
| `SOURCES` | `wiki,jar` | 只留 `jar` 则完全离线、纯贴图（快，但合成方块没有图标） |
| `ICON_SIZE` | `96` | 输出画布边长 |
| `CHECK_INTERVAL` | `21600` | 自动检查间隔（秒） |
| `UPDATE_ON_START` | `true` | 启动时是否补建 |
| `CONCURRENCY` | `4` | 并发上游请求数 |
| `LOG_LEVEL` | `INFO` | `DEBUG` 额外打印探针、wiki 请求等细节 |
| `REFRESH_TOKEN` | 空 | 保护 `/refresh` |

### serve 日志

所有输出走 stdout，格式统一为 `时间 级别 模块 内容`，uvicorn 自己的启动/关闭信息也并进来了：

```
11:27:19 INFO  src.server   starting mc-icon-service, data dir: /app/data
11:27:19 INFO  src.server   sources=wiki,jar icon_size=96 concurrency=4 check_interval=21600s
11:27:19 INFO  src.server   upload: enabled -> https://img.xcstar.top/upload (channel=s3, folder=mc_admin, ...)
11:27:19 INFO  src.server   loaded build 26.3: 1750 icons (1693 wiki, 57 jar), 65 missing, dir /app/data/icons-26.3
11:27:19 INFO  src.updater  refresh loop started, next check in 6h
11:27:20 INFO  src.updater  already on 26.3, nothing to build
11:27:25 INFO  src.server   GET /icons/stone -> 200 in 12.3ms, 7376 bytes (127.0.0.1)
11:27:25 INFO  src.server   GET /icons/nope -> 404 in 3.7ms, 31 bytes (127.0.0.1)
```

- 每条请求都带状态码、耗时、响应字节数和来源 IP，方便排查插件取图 404。
- `/healthz`、`/readyz` 只在 `LOG_LEVEL=DEBUG` 下记录，否则 30 秒一条会把日志刷满。
- 构建期间每 100 个 id 打一条进度，包含 wiki/jar 来源计数和缺失数。
- 定时检查会打「下次检查时间」和「检查耗时 + 结果」；手动 `POST /refresh` 也会留一条记录。

## 排查

### 日志停在 `building icons for Minecraft ...` 不动了

启动构建跑在后台 task 里，**早期版本会把异常吞掉**（`asyncio.create_task` 不 await 就看不到异常），表现就是日志卡住。现在的版本会打完整 traceback、重试 3 次，之后交给后台每 5 分钟重试一次。看到 `startup build attempt 1/3 failed` 后往下翻 traceback 即可。

按频率排的三个原因：

1. **data 目录不可写**（Docker bind mount 属主不是 uid 1000）。日志会直接给出：
   ```
   ERROR src.server  data dir /app/data is not writable (uid=1000): [Errno 13] Permission denied
   ```
   解决：`sudo chown -R 1000:1000 ./data`，或启动时加 `--user $(id -u):$(id -g)`。
2. **连不上 Mojang**。先验证：
   ```bash
   docker exec mc-icons python -c "import httpx; print(httpx.get('https://piston-meta.mojang.com/mc/game/version_manifest_v2.json', timeout=10).status_code)"
   ```
   不通就加 `MC_MIRROR_URL=https://bmclapi2.bangbang93.com`（清单、版本 json、jar 全走镜像，sha1 校验不变）。
3. **磁盘满或挂载路径不对**。明明构建过却提示 `no usable build on disk`，检查 `data/state.json` 和 `data/icons-*` 是否真的在挂载目录里。

## 部署

镜像基于 `python:3.12-slim`，实测 194 MB，内置 `HEALTHCHECK`（探测 `/readyz`）。

```bash
docker build -t mc-icon-service .

# 方式一：直接跑
docker run -d --name mc-icons --restart unless-stopped \
  -p 8618:8618 --env-file .env -v $PWD/data:/app/data mc-icon-service

# 方式二：compose
docker compose up -d
```

几个部署时要注意的点：

- **`.env` 不会打进镜像**（`.dockerignore` 已排除），必须用 `--env-file .env` 或 compose 的 `env_file` 传进去。想用挂载也行：`-v $PWD/.env:/app/.env:ro`。
- **首次启动是空目录**：容器会立刻开始构建（约 16 分钟），这期间 `/healthz` 返回 200 但 `ready:false`，`/version`、`/icons/*` 一律 503，`/readyz` 也是 503。健康检查的 `start-period` 设成了 15 分钟，所以不会一上来就 flapping。
- **重启不会白干活**：启动只做一次版本比对，版本没变就直接复用现有构建；只有 Mojang 发了新版本才会重建。
- 想跳过等待，可以先在宿主机或容器里构建好再起服务：
  ```bash
  docker run --rm -v $PWD/data:/app/data --env-file .env mc-icon-service python main.py update
  ```
- **文件属主**：镜像内以 uid 1000 的 `app` 用户运行，绑定挂载的 `./data` 会被写成 1000:1000。宿主机用户不是 1000 时加 `--user $(id -u):$(id -g)`。
- **时区**：容器默认 UTC，日志时间会比本地早 8 小时，加 `-e TZ=Asia/Shanghai` 即可（镜像自带 tzdata）。
- 首次构建要访问 `piston-meta.mojang.com` 和 `minecraft.wiki`，网络受限的环境建议用 `SOURCES=jar` 或提前构建好数据卷。
- 容器内也能跑管理命令：`docker exec mc-icons python main.py upload --dry-run`。

不想常驻也可以用 cron 每天跑一次 `python main.py update --upload`。

## 注意

- wiki 的 API 有速率限制，脚本默认每个请求间隔 0.3 秒并带 `User-Agent`，不要调太激进。
- 图标素材版权归 Mojang / Minecraft Wiki（CC BY-NC-SA）所有，仅用于自建服务内部展示。
- 判定逻辑在 `src/wiki.py`：文件名必须形如 `Acacia_Stairs_(N)_JE4_BE2.png`，后缀只接受 `UD/N/NS/EW/E/S/W/top/side/inventory/item`，其余（`MCD`、`Texture`、`Screenshot` 等）一律丢弃，避免抓到截图。
