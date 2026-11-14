# CHINA-IPTV · 自动同步 + 频道图标版

自动镜像 [xisohi/CHINA-IPTV](https://github.com/xisohi/CHINA-IPTV)，并在每次同步后重新生成**带频道图标**的直播源文件。

上游一有新提交，本仓库就会自动跟进，无需任何手工操作。

## 订阅地址

产物发布在 `dist` 分支的**根目录**，每次构建覆盖为最新快照（分支只保留一份快照，不会膨胀历史）。

| 用途 | 地址 |
| --- | --- |
| **推荐**：全量源，每频道限 5 个地址 | `https://raw.githubusercontent.com/zzzzzhy/CHINA-IPTV/dist/iptv.m3u` |
| 全量源，不限地址数 | `https://raw.githubusercontent.com/zzzzzhy/CHINA-IPTV/dist/iptv-full.m3u` |
| 单播源（unicast） | `https://raw.githubusercontent.com/zzzzzhy/CHINA-IPTV/dist/unicast.m3u` |
| 组播源（multicast） | `https://raw.githubusercontent.com/zzzzzhy/CHINA-IPTV/dist/multicast.m3u` |
| DIYP / TVBox 风格 TXT | `https://raw.githubusercontent.com/zzzzzhy/CHINA-IPTV/dist/iptv.txt` |
| 分省分运营商 | `https://raw.githubusercontent.com/zzzzzhy/CHINA-IPTV/dist/unicast/<省>/<运营商>.m3u` |
| 图标覆盖率报告 | `https://raw.githubusercontent.com/zzzzzhy/CHINA-IPTV/dist/logo-coverage.txt` |

国内访问 raw.githubusercontent.com 不畅时，把域名换成 jsDelivr CDN（路径中的分支名写成 `@dist`）：

```
https://cdn.jsdelivr.net/gh/zzzzzhy/CHINA-IPTV@dist/iptv.m3u
```

> 组播源（`multicast.m3u`）只能在运营商 IPTV 内网使用，且播放设备须与光猫同网段。

`dist` 分支是**纯产物分支**，只有一份最新快照，不含源码，也不含图标 PNG。

## 产物说明

- **M3U**：每个频道带 `tvg-logo`（图标）、`tvg-id`（与 `e.xml` 节目单关联）、`tvg-name`、`group-title`（分类）。
  示例：

  ```
  #EXTINF:-1 tvg-id="6" tvg-name="CCTV5+" group-title="央视" tvg-logo="https://cdn.jsdelivr.net/gh/zzzzzhy/CHINA-IPTV@main/logo/CCTV5%2B.png",CCTV5+
  ```

- **TXT**：`分类,#genre#` + `频道名,地址`，供 DIYP、TVBox 等使用。
- **图标**：图标本体（约 30MB 的 PNG）不入库，图标 URL 直连上游 `logo/`，通过 jsDelivr CDN 分发。本仓库只保存一份文件清单 `.github/logo-index.txt`（约 950 个图标名），由同步流程从上游 `logo/` 目录自动生成。

## 工作原理

```
上游 xisohi/CHINA-IPTV 有新提交
        │
        │  sync.yml：每 2 小时检查一次（也可手动触发）
        ▼
本仓库 main 分支（Unicast/ Multicast/ TV/ logo/ 分省模板/ e.xml 被镜像更新）
        │
        │  push 事件触发
        ▼
build-with-logos.yml
        │  用 composite action .github/actions/add-logos 运行 add_logos.py
        ▼
dist 分支（每次覆盖，只保留最新快照，不膨胀仓库历史）
```

两个工作流都在 `.github/workflows/` 下：

| 文件 | 作用 |
| --- | --- |
| `sync.yml` | 每 2 小时第 17 分检查上游 `main` 的提交号，与 `.upstream-sha` 比对；有变化则镜像 `Unicast`/`Multicast`/`TV`/`分省模板`/`e.xml`，重新生成 `.github/logo-index.txt`，并把上游 README 存为 `README.upstream.md`，最后推送到本仓库。若上游把目录清空则主动报错中止，避免误删数据。 |
| `build-with-logos.yml` | 同步成功（或 main 有推送）后运行，生成带图标的产物并发布到 `dist` 分支。 |

## 图标匹配如何提升覆盖率

纯精确匹配大约只能覆盖 70% 的频道行，因为图标文件名往往带品牌前缀（`NEWTV军旅剧场`、`CHC家庭影院`、`IHOT爱体育`、`IPTV5+`），而源里的频道名没有。

脚本 `.github/scripts/add_logos.py` 依次尝试：

1. 手工别名表 `.github/logos-aliases.txt`（优先级最高）；
2. `TV/channel_mapping.txt` 里的上游别名映射；
3. 精确 → 忽略大小写 → 去标点三级匹配；
4. 频道名变换闭包：去画质后缀（高清/4K/HD…）、去尾部"频道/台/卫视"、去台标前缀（CCTV/IPTV/NEWTV/百视通…）、去省名前缀、去加号；
5. 图标名前后缀唯一包含（`家庭影院` → `CHC家庭影院.png`）。

当前覆盖率 **20568 / 27012 行（76.1%）**，剩余多为本身就没有图标的占位频道（`无信号`、`信源检修`、`剧场` 类）。

### 补充图标别名

如果发现某个频道图标缺失或匹配错误，在 `.github/logos-aliases.txt` 中加一行即可，格式为 `频道名,logo 文件名（不含 .png）`：

```
军旅剧场,NEWTV军旅剧场
家庭影院,CHC家庭影院
```

提交后重新运行 `Build with logos` 生效。`dist/logo-coverage.txt` 里列出了出现次数最多的未匹配频道，方便按需补全。

## 本地运行

```bash
# 在仓库根目录
python3 .github/scripts/add_logos.py \
  --root . --out dist \
  --logo-base "https://cdn.jsdelivr.net/gh/zzzzzhy/CHINA-IPTV@main/logo" \
  --max-urls 5
```

可选参数：`--epg-icons` 额外生成带 `<icon>` 的 `dist/epg.xml`，`--quiet` 静默模式。只依赖 Python 标准库。

## 上游仓库

- 上游：[xisohi/CHINA-IPTV](https://github.com/xisohi/CHINA-IPTV)
- 本仓库 `README.upstream.md` 是上游 README 的副本，随同步更新，供查阅原始说明。
