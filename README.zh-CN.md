# <img src="static/icon.svg" width="28" align="top" alt="logo"> anime-rss-auto

[English](README.md) | **简体中文**

面向 Windows 的全自动季度番剧流水线：

```
bangumi.tv（在看列表）
     │  每 5 分钟轮询
     ▼
mikanani.me（解析番剧 → 单一字幕组的 RSS feed）
     │
     ▼
qBittorrent（RSS 自动下载规则，套用你的命名约定）
     │  硬链接镜像
     ▼
Jellyfin（按季度自动建库、季节封面、倒序排列）
     │  webhook：看完一集
     ▼
停止该集做种 + 在 bangumi.tv 上标记该集看过
```

只要在 bangumi.tv 上把一部番标成**在看**，其余的一切——订阅、下载、
Jellyfin 建库、看过状态回写——全部自动完成。或者只标成**想看**：它会在
开播当天自动提升为在看（先行版会被过滤掉）。

## 功能一览

| 模块 | 作用 | 开关 |
|---|---|---|
| **sync / watch** | bgm 在看列表 → mikan feed + qB 规则（保存路径 `<库>\<YYYY.MM>\<番名>`、季度标签） | 核心 |
| **开播自动追（premiere watch）** | 想看列表里的番在开播当天自动提升为在看（以 bgm 第一集放送日为门槛；可用 `premiere_times.json` 覆盖），并在面板弹开播横幅。先行版按名字（先行/予告）、大小（> 2 GB）、开播前发布日期三重剔除 | `premiere_watch_enabled` |
| **字幕组优先级** | 按你排好的优先级为每部番选一个字幕组；绝不重复下多个组 | `group_priority` |
| **ANi 宽限期保险丝** | 番剧首次出现在 mikan 时若最高优先组还没发布，先等 N 小时再锁定次优组（等待期漏掉的剧集会从 feed 补抓回来） | `ani_grace_hours` |
| **对账（reconcile）** | 番剧转为 看过/抛弃 → 删 qB 规则（保留文件）；彻底移出收藏 → 退订 + 删文件 | `purge_dropped_files` |
| **旧番分界线** | 早于分界季度的番永不触碰——不增、不删 | `skip_before_season` |
| **mark-watched** | 你在 qB 里暂停一个已完成的种子 → 该集在 bgm 上标看过（基于状态跳变，绝不批量误标） | `mark_watched_enabled` |
| **Jellyfin 镜像** | 把新剧集硬链接进 `<镜像>\<季度>\<番名>\Season 01\`（0 额外占空间，不碰做种） | `jellyfin_mirror_enabled` |
| **Jellyfin 自动建库** | 新增季度文件夹 → 自动建库、生成封面、倒序排列 | `jellyfin_autolib_enabled` |
| **Jellyfin 联动删除** | 源库删掉某季度 → 镜像 + Jellyfin 库一并删除（多重安全闸） | `jellyfin_mirror_delete_enabled` |
| **Jellyfin 空系列自愈** | 镜像重建竞态导致某系列在 Jellyfin 里变空壳、点播报「Unable to find a valid media source」→ 每轮 1 次调用查出「0 集但磁盘有视频」的系列并递归刷新修好（正常番零开销，带扫描进行中安全闸） | `jellyfin_heal_empty_enabled` |
| **jfhook** | Jellyfin Webhook 插件 → 看完一集 → 停该集做种 + bgm 标看过 | `jfhook_port` |
| **Web 控制面板** | 本地仪表盘：按收藏类型分组显示所有 bgm 标记过的番（在看/想看/看过/搁置/抛弃）并可按类型筛选 + 番名实时搜索框、光标悬停时卡片轮廓发出对应收藏类型颜色的光晕、番名是指向对应 bangumi 条目页的真实链接（支持中键/键盘打开）且跟随界面语言（英文界面经 AniList 显示英文/罗马字名）、所有列表按开播时间从早到晚排序（看过页除外：按季度从新到旧分块、每个季度有整行彩色分割线、块内刚标记看过的排前面）、手动同步时日志面板实时显示同步输出（结束后仍可回看；失败会弹提示并自动展开日志）、周内时刻表页（含在看+想看、开播状态用时刻颜色区分、可按开播状态筛选、每个周几一根竖列、从今天开始往后排、今天整列高亮、按**本地时区**播出时刻排列）、深色/浅色配色（默认跟随系统）、开播横幅保留一周（可手动关闭，多条时有「全部知道了」按钮）、每部番显示**你本地时区**的开播时间（经 AniList）、宽限倒计时、切换字幕组（会删除该番旧字幕组已下载的文件、再从新组逐集重下整季——不可撤销，受季度分界线保护并有确认弹窗）、每番「n/m 集准备完成」汇总、季度彩色徽章（8 色两年一循环）、每卡 bangumi 社区评分徽章、所选标签页记忆并支持深链（`?tab=schedule&theme=light&lang=zh`）、全屏品牌启动加载页（进度条按真实加载阶段推进；后端未就绪时变琥珀色并每 5 秒快速重试；`?boothold` 可冻结用于截图）、所有时间戳本地化、页头离线 / qBittorrent 掉线指示、手机友好布局与触控目标、键盘/读屏可用、遵循系统减少动效设置（支持中英切换） | `webui.py` |

每个模块都可在配置里独立开关——各取所需。

## 文件

- `anime_rss.py` —— 除面板外的全部功能；纯标准库，单文件。
  子命令：`list`、`plan`、`apply`、`prune`、`sync`、`watch`、`mark`、`premiere`、`auth`、`jfhook`。
- `webui.py` + `static/index.html` —— FastAPI 控制面板，`http://127.0.0.1:8767`。
- `run_watch*.bat/vbs`、`run_webui*.bat/vbs` —— 隐藏窗口自启动脚本
  （把指向 `.vbs` 的快捷方式放进 `shell:startup` 即可开机自启）。

## 部署

1. 环境要求：Windows、Python 3.11+、开启 Web UI 的 qBittorrent（localhost、
   免密），以及可选的 Jellyfin + Webhook 插件。
   面板需要 `pip install fastapi uvicorn`。
2. 复制 `config.example.json` → `config.local.json`，填入你的值
   （bgm 用户 id、mikan cookie、Jellyfin API key、各路径）。
3. 单次运行：`set PYTHONUTF8=1 && python anime_rss.py sync`
   守护进程：`python anime_rss.py watch`（每 5 分钟同步 + jfhook 监听）。
4. 面板：`python webui.py` → 浏览器打开 http://127.0.0.1:8767。

bgm token：用 365 天个人令牌（`bgm_access_token`），或用可自动续期的 OAuth——
在 https://bgm.tv/dev/app 建应用，填 `bgm_client_id`/`bgm_client_secret`，
执行一次 `python anime_rss.py auth`。

## 本工具自动套用的约定

- qB 保存路径 `<bangumi_library>\<YYYY.MM>\<英文番名>`，标签 `<YYYY.MM>`。
- RSS feed 挂在 `<YYYY.MM>` 文件夹下，订阅前会先显式建好该文件夹——
  qBittorrent 5.x 的 `addFeed` 不会自动创建父文件夹（季度文件夹不存在会 409，
  留下一条没有 feed 的空转规则）。
- 每部番只用一个字幕组——mikan 的 RSS 地址本身就是按组区分的。
- 季度：01 / 04 / 07 / 10；季度字符串按字典序比较（`2026.04 < 2026.07`）。
- 破坏性操作（删文件/删规则）只对 `skip_before_season` 及之后的番生效；
  更早的番对本工具严格只读。

## 安全说明

- `config.local.json` 存放全部密钥，已 gitignore；代码里不硬编码任何敏感信息。
- Jellyfin 联动删除在源库缺失或为空时拒绝执行（防盘未挂载），删除数量异常时中止。
- Web 面板默认只绑 127.0.0.1；只有在可信局域网内才建议设 `webui_host: "0.0.0.0"`
  （面板本身没有鉴权）。
