# iptv-auto

每天自动采集 → 合并去重 → 分类 → 可用性探测 → 生成可直接订阅的 IPTV 播放列表。

## 订阅地址（2026-10-06 实测可用）

结果发布在 **`output` 分支**，每天北京时间 **01:20** 自动更新。

| 用途 | 地址 |
|---|---|
| **主用**（jsDelivr，国内快，有缓存） | `https://cdn.jsdelivr.net/gh/FORGETL1/iptv-auto@output/直播源-精选.m3u` |
| **要最新**（ghproxy，不缓存） | `https://ghproxy.net/https://raw.githubusercontent.com/FORGETL1/iptv-auto/output/直播源-精选.m3u` |
| TXT 版（只吃 TXT 的播放器） | 把文件名换成 `直播源-精选.txt` |
| 频道清单 | 把文件名换成 `频道清单.csv` |

实测：两条地址均返回 HTTP 200，约 389 KB，含 1330 个频道 / 1655 条线路。

> jsDelivr 对 `@分支` 地址有缓存（最长约 12 小时），刚更新完可能拿到的还是上一版；
> 想立刻拿到最新结果就用 ghproxy 那条。

导入 APTV / 途播 / CarTV：源管理 → 添加订阅 → 粘贴地址。
想手动跑一次：**Actions** 页面 → 选「每日更新直播源」→ Run workflow。

---

## ⚠️ 一个必须知道的限制

**GitHub Actions 的运行机器在微软 Azure 机房，不在你家网络里。**

所以结果里「本机可达」这个标记反映的是**云端机房能不能连通**，和你家里的宽带/手机流量并不一致。
同一时刻对比：

| 跑在哪 | 探测到可达的线路 |
|---|---|
| GitHub Actions（Azure 机房） | 2152 条 |
| 本机（国内家宽） | 2308 条 |

想让结果真正贴合你自己的网络，请在本机跑同一个脚本（`python build.py --outdir out`），
本机跑出来的「本机可达」才是你自己网络下的真实结果。

两种用法可以叠加：**云端负责每天采集和初筛，本地负责按你的网络再精筛。**

## 为什么不用 Guovin/iptv-api 的 fork？

Guovin/iptv-api 官方已把 GitHub Actions 改成**仅支持手动触发**，其 `gd` 分支的自动输出停在 **2026-08-27**
就没有再更新过，官方说明是「需要定时执行时请使用 Docker、命令行或 GUI」。所以 fork 它拿不到每日自动更新。

本项目是一个轻量替代：只做「采集 + 合并 + 探测」，不需要 Docker、不需要配置，跑完直接产出 m3u。

## 目录结构

```
.
├── .github/workflows/daily.yml   # 每日定时任务
├── build.py                      # 全部逻辑都在这里
├── README.md
└── .gitignore
```

## 常用改动

| 想做什么 | 怎么做 |
|---|---|
| 改运行时间 | 改 `daily.yml` 里的 `cron`（注意是 **UTC**，北京时间 = UTC+8） |
| 增减采集源 | 改 `build.py` 顶部的 `SOURCES` 列表 |
| 每个频道保留几条线路 | 改 `build.py` 里 `best_of(v, n=2)` 的 `n` |
| 换 EPG 节目单地址 | 改 `build.py` 顶部的 `EPG_URLS` |
| 本地试跑（不推 GitHub） | `python build.py --outdir out` |

## 参数

```bash
python build.py \
  --outdir out \        # 输出目录
  --engine urllib \     # 探测引擎：urllib（默认，快）/ curl
  --max-probe 3000 \    # 只探测前 N 条，加快调试
  --workers 80          # 并发数
```

## 重新部署（换账号或重建时）

**方式一：网页拖拽上传（最简单）**

1. GitHub 新建一个**公开**空仓库（不要勾选 Add README）。
2. 打开仓库页面，点 **uploading an existing file**。
3. 把这个项目里的**所有内容**（含 `.github` 文件夹）拖进去，点 Commit。
4. 到 **Actions** 页面确认「每日更新直播源」跑起来（约 3–8 分钟）。
5. 跑完自动生成 `output` 分支，用上面的订阅地址导入播放器即可。

**方式二：命令行**

```bash
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/<你的用户名>/iptv-auto.git
git push -u origin main
```

## 实现要点（踩过的坑）

1. **探测不能跟随 302 跳转**。大量 IPTV 源返回 302，跟随跳转会把活源误判成死链。
   所以 `301/302/303/307/308` 一律算「可达」。实测这一条能救回约 1/3 的线路。
2. **`raw.githubusercontent.com` 在国内常被墙**，每个 GitHub 源都带 `ghproxy.net` / `gh-proxy.com` 兜底，
   按顺序自动重试。
3. **源文件要并发下载**。串行下载 15 个源时，只要有一个卡在超时上，整体就会被拖几分钟。
4. **组播地址（`rtp://` / `udp://` / `igmp://`）必须剔除**，播放器无法直接播，留着只会占位置。
5. **频道名归一化是质量关键**：`CCTV-1 综合` / `CCTV1` / `央视一套` 必须归到同一个频道，
   否则列表里会出现十几个「CCTV1」。注意央视的归一化要排在「纯英文名判垃圾」规则**之前**，
   否则 `CCTV1` 会被当成乱码名删掉。
6. 过滤掉虎牙/斗鱼主播房间、历年春晚这类噪音条目。

## 免责声明

所有源均采集自互联网公开分享，**仅供个人学习与播放器功能测试**，请勿商用或公开传播。
直播源天然易失效，本项目只负责让列表尽量新鲜，不对内容可用性做任何保证。
