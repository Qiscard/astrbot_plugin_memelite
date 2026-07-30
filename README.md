<div align="center">

# astrbot_plugin_memelite

> 维护仓库：[Qiscard/astrbot_plugin_memelite](https://github.com/Qiscard/astrbot_plugin_memelite)  
> Gitee 镜像：[qiscard/astrbot_plugin_memelite](https://gitee.com/qiscard/astrbot_plugin_memelite)  
> 原作者 / 上游：[Zhalslar/astrbot_plugin_memelite](https://github.com/Zhalslar/astrbot_plugin_memelite)

_✨ [AstrBot](https://github.com/AstrBotDevs/AstrBot) 表情包制作插件 ✨_

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

</div>

## 简介

- 基于原作者 Python 版继续维护，补充资源增量安装、代理测速、列表美化、全局热度等能力
- 插件本体不含大体量资源；安装后需执行表情/字体修复
- 代码含 AI 辅助生成，请以实际环境测试为准

## 安装

### 1. 安装插件

AstrBot 插件市场搜索 `astrbot_plugin_memelite`，或克隆到 `data/plugins`。

依赖（`requirements.txt`）：

```text
meme_generator>=0.1.14,<0.2.0
```

- 若环境 Pillow≥12，可装兼容分支：

```bash
pip install "git+https://github.com/Qiscard/meme-generator.git@pillow-12-compat"
```

- 需要 Rust 版请使用原作者插件。

### 2. 系统依赖（Linux / Docker）

```bash
apt-get update && apt-get install -y \
  libegl1 libgl1 libgles2 libglib2.0-0 fontconfig \
  fonts-noto-cjk fonts-noto-color-emoji
```

Windows 一般无需额外系统库。可用 `/meme检查` 查看缺失项。

### 3. 下载资源（安装后必须）

管理员发送：

```text
/meme检查
/meme表情修复
/meme字体修复
```

| 命令 | 作用 | 默认落盘 |
|------|------|----------|
| `/meme表情修复` | 安装默认表情 + 配置额外链接 | `site-packages/meme_generator/memes/` |
| `/meme字体修复` | 安装默认字体 | Linux: `~/.local/share/fonts/meme-generator`<br>Windows: `%LOCALAPPDATA%\Microsoft\Windows\Fonts` |
| `/meme检查` | 环境与资源状态 | - |

下载包 / 临时文件 / 清单：`data/plugin_data/astrbot_plugin_memelite/`（`packages/` `temp/` `index/`）。  
表情与字体安装位置不变。无需再配置仓库名、Release 标签或本地 zip 路径。

## 配置

面板：插件管理 → astrbot_plugin_memelite → 插件配置

| 配置项 | 说明 | 默认 |
|--------|------|------|
| `need_prefix` | 需要前缀或 @bot 才触发 | `true` |
| `is_compress_image` | 压缩超过 512px 静态图 | `true` |
| `is_check_resources` | 启动时官方在线资源检查 | `false` |
| `auto_fix_resources_on_start` | 启动时自动修复表情/字体 | `false` |
| `meme_resource_urls` | 额外 meme 资源直链（zip/tar.gz） | `[]` |
| `download_timeout` | 下载超时秒数（30-300） | `180` |
| `use_github_proxy` | 启用 GitHub 代理（默认仍直链；测速合格后才自动选用） | `true` |
| `github_proxy` | 固定 GitHub 代理；留空=测速自动选 | `""` |
| `proxy_probe_on_fix` | 修复时自动测速（缓存 12h） | `true` |
| `meme_timeout` | 生成超时（秒） | `15` |
| `meme_list_style` | `standard` 三列 / `compact` 四列 | `standard` |
| `meme_list_page_size` | 每页数量，`0`=默认（90/160） | `0` |
| `meme_new_days` | ⭐ 天数，0-7，0=不显示 | `3` |
| `meme_hot_min_count` | 🔥 最低次数，0-100，0=不显示 | `3` |

### 额外资源 `meme_resource_urls`

- 支持：`zip`、`tar.gz` / `tgz`
- 不支持：`rar` / `7z`、仓库主页链接（请用压缩包直链）

`/meme表情修复` 顺序：默认包 → 配置中的额外链接。自动识别套一层文件夹的第三方结构，最终落到：

```text
meme_generator/memes/<表情名>/逻辑文件 + 图片
```

### 增量与缓存

1. 读清单 `data/plugin_data/astrbot_plugin_memelite/index/resource_index.json`
2. url/hash 有效且表情仍在 → **跳过下载**
3. 清单不全但本地 `packages/` 仍有包 → **优先本地安装**
4. 仅本地缺失或 `/meme表情修复 强制` 才重新下载
5. 超时终止当前源并删除 `.part` 临时文件，继续尝试下一候选源

| 路径 | 用途 |
|------|------|
| `.../packages/` | 已下载资源包 |
| `.../temp/` | 解压临时目录 |
| `.../index/` | 资源清单 / 代理测速缓存 |
| `meme_generator/memes/` | 表情安装位置 |
| 用户字体目录 | 字体安装位置 |

## 命令

| 命令 | 说明 | 权限 |
|------|------|------|
| `{关键词}` | 触发 meme | 普通 |
| `/meme列表` | 列表图（0-9→a-z→其他；详情见日志） | 普通 |
| `/meme排行` | 全局热门 TOP20 | 普通 |
| `/meme详情 xxx` | 查看参数 | 普通 |
| `/meme检查` | 环境与资源摘要 | 普通 |
| `/meme表情修复` | 增量安装；加“强制”全量；完成后热重载 | 管理员 |
| `/meme字体修复` | 安装默认字体 | 管理员 |
| `/meme代理测速` | 测速 GitHub 代理（≤3000ms 才自动选用） | 管理员 |
| `/meme重置` | 清空全局计数 / hot / new | 管理员 |
| `禁用meme` / `启用meme` / `meme黑名单` | 黑名单 | 管理员 |
| `添加保护` / `移除保护` / `保护名单` | 保护名单 | 管理员 |
| `添加反弹表情` / `移除反弹表情` / `反弹表情列表` | 反弹 | 管理员 |

## 头像获取

配置项 `avatar_fetch_mode`：

| 值 | 说明 | 适用 |
|------|------|------|
| `auto`（默认） | 按平台/用户ID自动选择 | 多平台、省心，推荐 |
| `onebot` | 强制数字 QQ 号 `qlogo` | 仅 OneBot/数字QQ号，自动判错时 |
| `qq_official` | 强制 `qqapp/{appid}/{openid}` | 仅 QQ 官方机器人，自动判错时 |

- **OneBot / 数字 QQ 号**：`qlogo.cn/headimg_dl?dst_uin={qq}`
- **QQ 官方机器人（openid）**：`http://q.qlogo.cn/qqapp/{appid}/{openid}/640`
  - `appid` 优先自动读取适配器（`event.bot` / platform config）
  - 自动失败时可在插件配置填写 `qq_official_appid`
- 同时会尝试事件自带 `avatar`、以及 `bot.getUser` / `get_stranger_info` 等通用接口
- 保护名单 / 用户黑名单：OneBot 填 QQ 号，官方机器人填 **openid 字符串**
## 使用

- 参数空格分隔，如：`喜报 nmsl`
- 支持引用消息、`@某人` / `@qq号` 作为图片参数
- 参数不足时自动补发送者 / bot 头像与默认文本
- 保护名单用户被作为目标时，表情反弹到触发者

### 列表图

`/meme列表` 只发图片：

- 标题：`meme表情列表` + `共N个`
- 排序：首个关键词 **0-9 → a-z → 其他**
- 页脚：`第x页/共x页`；`默认+x组额外表情包，最近更新：年.月.日`
- 资源路径/失败原因只写日志

### 下载与代理

- **默认直链**：Gitee 始终直链；GitHub 无合格代理时也直链
- **测速后代理**：`/meme代理测速` 后，仅当最低延迟 **≤3000ms** 才自动选用；否则提示网络不佳并保持直链
- 已选用代理时：GitHub **代理优先，失败回退直链**
- 面板 `github_proxy` 可固定代理；`use_github_proxy=false` 则永不走代理
- 超时（`download_timeout`，默认 180s）会清临时缓存并尝试下一源

## 注意事项

1. 安装后通常还要：系统依赖 + `/meme表情修复` + `/meme字体修复`
2. 重建 venv 后 `site-packages` 内表情会丢，需重新修复
3. 中文缺字：`/meme字体修复`；Linux 建议装 `fonts-noto-cjk`
4. 第三方包请用 zip/tar.gz 直链
5. `/meme表情修复` 完成后会热重载，一般无需重启；仍异常可再修一次或重载插件

## 致谢与链接

基于 [Zhalslar/astrbot_plugin_memelite](https://github.com/Zhalslar/astrbot_plugin_memelite) 继续维护。

- GitHub：[Qiscard/astrbot_plugin_memelite](https://github.com/Qiscard/astrbot_plugin_memelite)
- Gitee：[qiscard/astrbot_plugin_memelite](https://gitee.com/qiscard/astrbot_plugin_memelite)
- [meme-generator](https://github.com/MemeCrafters/meme-generator)
- [AstrBot](https://astrbot.app/)
