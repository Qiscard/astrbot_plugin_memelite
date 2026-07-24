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

- 对接本地 [meme-generator](https://github.com/MemeCrafters/meme-generator)（Python 0.1.x / Rust 0.2.x），**插件默认为python版本**，RS版请使用作者原插件。
- **插件本体只含框架代码**，不附带大体量表情图片与字体。
- 安装后执行命令，按**插件内置默认源**自动下载资源（无需再配置仓库/标签/压缩包路径）：
  - `/meme表情修复`
  - `/meme字体修复`
- 支持在配置中**批量添加**第三方 meme 资源直链；安装时自动识别目录层级。
- 资源采用**清单增量安装**：链接 → 压缩包 hash → 表情列表；未变更则跳过下载。
- **本地包优先**：已下载的资源包（zip/tar.gz）缓存在 `plugin_data/.../packages/`，中断后再次修复优先用本地包，不必整包重下。
- 可配置**下载最大延时**（默认 180 秒，范围 30-300）；超时终止并清理未完成缓存。
- `/meme资源列表` 发送按名称排序的表情列表图（资源详情见日志）。
- 启动时自动识别系统依赖；若缺少 OpenGL/EGL/fontconfig 等，会给出可直接复制的安装命令。

## 致谢与声明

本插件基于 [Zhalslar/astrbot_plugin_memelite](https://github.com/Zhalslar/astrbot_plugin_memelite) 继续维护与增强。  
当前维护者：Qiscard

## 安装

### 1. 安装插件

在 AstrBot 插件市场搜索 `astrbot_plugin_memelite` 安装，或克隆到 `data/plugins`。

默认 Python 依赖（`requirements.txt`）：

```text
meme_generator>=0.1.14,<0.2.0
```

- 官方 0.1.x 声明 `Pillow<11`。若环境已是 Pillow 12，可手动安装兼容分支：

```bash
pip install "git+https://github.com/Qiscard/meme-generator.git@pillow-12-compat"
```

- 需要 Rust 版时，将 requirements 改为 `meme_generator>=0.2.0,<0.3.0`。

### 2. 安装系统依赖（Linux / Docker 必看）

meme-generator 依赖 `skia-python`，Linux 上需要 EGL/OpenGL/fontconfig。  
插件启动或执行 `/meme检查` 时会自动检测。

```bash
# Debian / Ubuntu / 多数 Docker 镜像
apt-get update && apt-get install -y \
  libegl1 libgl1 libgles2 libglib2.0-0 fontconfig \
  fonts-noto-cjk fonts-noto-color-emoji
```

Windows 一般无需额外系统库；字体通过 `/meme字体修复` 安装到用户字体目录。

### 3. 下载表情与字体资源（安装后必须）

插件安装完成后，**管理员**在机器人中发送：

```text
/meme检查
/meme表情修复
/meme字体修复
```

说明：

| 命令 | 作用 | 资源来源 | 默认落盘位置 |
|------|------|----------|----------------|
| `/meme表情修复` | 下载默认表情包 + 配置中的额外链接，并自动识别目录层级 | 插件内置默认源 | `site-packages/meme_generator/memes/` |
| `/meme字体修复` | 下载默认字体包并安装 | 插件内置默认源 | Linux: `~/.local/share/fonts/meme-generator`<br>Windows: `%LOCALAPPDATA%\Microsoft\Windows\Fonts` |
| `/meme检查` | 系统依赖 + 资源状态 | - | - |

> 兼容：旧命令 `/meme表情修复2`、`/meme字体修复2` 仍可用。

> 下载包/临时文件/资源清单保存在框架 `data/plugin_data/astrbot_plugin_memelite/`（`packages/`、`temp/`、`index/`）。表情与字体安装位置不变。

> 不再需要在配置里填写 GitHub/Gitee 仓库、Release 标签或本地 zip 路径。

## 配置

AstrBot 面板：插件管理 -> astrbot_plugin_memelite -> 插件配置

| 配置项 | 描述 | 默认 |
|--------|------|------|
| `need_prefix` | 需要前缀或 @bot 才触发 | `true` |
| `extra_prefix` | 额外前缀 | `""` |
| `is_compress_image` | 压缩超过 512px 静态图 | `true` |
| `is_check_resources` | 启动时官方在线资源检查（旧逻辑） | `false` |
| `auto_fix_resources_on_start` | 启动时自动修复表情/字体 | `false` |
| `meme_resource_urls` | **meme 资源添加**（批量 zip/tar.gz 直链） | `[]` |
| `download_timeout` | 资源下载最大延时（秒，30-300） | `180` |
| `meme_timeout` | 生成超时（秒） | `15` |
| `memes_disabled_list` | meme 黑名单 | `[]` |
| `user_blacklist` | 用户黑名单 | `""` |
| `protected_users` | 保护名单 | `""` |
| `bounce_back_memes` | 反弹表情列表 | `""` |

### meme 资源添加（`meme_resource_urls`）

支持的压缩格式：

- ✅ `zip`
- ✅ `tar.gz` / `tgz`
- ❌ `rar` / `7z`（请先转换）
- ❌ 仓库主页链接（请用 Release/archive 的压缩包直链）

在面板中可批量填写第三方 meme 资源包直链（**zip / tar.gz**）。  
执行 `/meme表情修复` 时顺序为：

1. 下载并安装**默认表情资源包**
2. 依次安装配置中的额外链接

插件会自动识别资源目录层级，兼容以下常见结构：

```text
# 标准
memes/
  摸/
    __init__.py
    images/...

# 第三方套一层文件夹
xxx_release/
  memes/
    摸/
      __init__.py
      images/...

# 直接是多个表情文件夹
摸/
  __init__.py
  images/...
拍/
  ...
```

最终只保留基础样式：

```text
meme_generator/memes/<表情名称>/逻辑文件 + 图片资源
```


### 资源清单与增量安装

执行 `/meme表情修复` 时：

1. 读取配置 `meme_resource_urls` + 本地 `data/plugin_data/astrbot_plugin_memelite/index/resource_index.json`
2. 每个资源源（默认源 + 额外链接）若 **url 未变、hash 有效、表情目录仍在** → 跳过下载
3. 否则下载 zip/tar.gz → 自动识别目录层级 → 安装 → 扫描表情名写回清单
4. 汇总：安装 / 跳过 / 失败

查看：

```text
/meme资源列表
```

强制全量重装：

```text
/meme表情修复 强制
```


### 增量与缓存策略

执行 `/meme表情修复` 时：

1. 读取资源清单 `data/plugin_data/astrbot_plugin_memelite/index/resource_index.json`
2. 若某资源源 **hash 未变且表情仍在位** → **跳过**（不会重新下载）
3. 若清单缺失/表情不全，但本地 `packages/{source_id}.zip|.tar.gz` 仍在 → **优先本地安装**，不走网络
4. 仅当本地包不存在，或使用 `/meme表情修复 强制` 时才重新下载
5. 下载超时（`download_timeout`）会终止当前下载并删除 `.part` 临时文件

目录约定：

| 路径 | 用途 |
|------|------|
| `data/plugin_data/astrbot_plugin_memelite/packages/` | 已下载的资源包 |
| `data/plugin_data/astrbot_plugin_memelite/temp/` | 解压/组装临时目录 |
| `data/plugin_data/astrbot_plugin_memelite/index/` | 资源清单 |
| `site-packages/meme_generator/memes/` | 表情安装位置（虚拟环境中） |
| 用户字体目录 | 字体安装位置 |

> 说明：头像请求走 QQ CDN 且体积很小，当前版本**不额外做头像磁盘缓存**（收益有限）。

## 命令

| 命令 | 说明 | 权限 |
|------|------|------|
| `{关键词}` | 触发 meme | 普通 |
| `/meme资源列表` | 按名称排序发送表情列表图（详情见日志） | 普通 |
| `/meme详情 xxx` | 查看参数 | 普通 |
| `/meme检查` | 环境依赖与资源状态摘要 | 普通 |
| `/meme表情修复` | 增量安装默认 + 额外资源；加“强制”全量重装；完成后热重载 | 管理员 |
| `/meme字体修复` | 安装默认字体资源 | 管理员 |
| `禁用meme xxx` / `启用meme xxx` | 黑名单 | 管理员 |
| `meme黑名单` | 查看禁用列表 | 管理员 |
| `添加保护` / `移除保护` / `保护名单` | 保护名单 | 管理员 |
| `添加反弹表情` / `移除反弹表情` / `反弹表情列表` | 反弹 | 管理员 |

## 使用说明

- 参数用空格分隔，如：`喜报 nmsl`
- 支持引用消息、`@某人` / `@qq号` 作为图片参数
- 参数不足时自动补发送者 / bot 头像与默认文本
- 保护名单用户被作为目标时，表情会反弹到触发者

## 注意事项

1. **插件安装完毕后**：可能还需系统依赖 + `/meme表情修复` + `/meme字体修复`。
2. 重建 AstrBot 虚拟环境后，`site-packages` 内表情会丢失，需重新执行修复命令。
3. 中文乱码/缺字：执行 `/meme字体修复`；Linux 建议再装 `fonts-noto-cjk`。
4. 第三方资源包请使用 **zip 或 tar.gz**（不支持 rar/7z、不支持仓库主页链接）；若套了外层文件夹，插件会自动识别，无需手动改结构。
5. Docker 请把系统依赖写入镜像或启动脚本。

## 相关链接

- 原作者仓库：[Zhalslar/astrbot_plugin_memelite](https://github.com/Zhalslar/astrbot_plugin_memelite)
- 本仓库 GitHub：[Qiscard/astrbot_plugin_memelite](https://github.com/Qiscard/astrbot_plugin_memelite)
- 本仓库 Gitee：[qiscard/astrbot_plugin_memelite](https://gitee.com/qiscard/astrbot_plugin_memelite)
- [meme-generator](https://github.com/MemeCrafters/meme-generator)
- [AstrBot](https://astrbot.app/)


## 热重载说明

`/meme表情修复` 完成后会自动热重载 `meme_generator` 表情注册表，**通常无需重启 AstrBot** 即可使用新安装的表情。

若个别表情仍不生效，可再执行一次修复，或重载插件/重启框架。

`/meme资源列表` 仅发送按名称排序（0-9 → a-z → 其他）的表情列表图；资源源、路径、失败原因等详情写入日志。

`/meme表情修复` 聊天回执仅保留简要结果；详细路径与跳过/失败原因见日志。
