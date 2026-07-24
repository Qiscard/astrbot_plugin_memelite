<div align="center">

# astrbot_plugin_memelite

> 维护仓库：[Qiscard/astrbot_plugin_memelite](https://github.com/Qiscard/astrbot_plugin_memelite)

_✨ [AstrBot](https://github.com/AstrBotDevs/AstrBot) 表情包制作插件 ✨_

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

</div>

## 简介

- 对接本地 [meme-generator](https://github.com/MemeCrafters/meme-generator)（Python 0.1.x / Rust 0.2.x）。
- **插件本体只含框架代码**，不附带大体量表情图片与字体。
- 安装插件后，通过命令按需下载本仓库 Release 中的精简资源包：
  - `/meme表情修复`
  - `/meme字体修复`
- 启动时自动识别系统依赖；若缺少 OpenGL/EGL/fontconfig 等，会给出可直接复制的安装命令。

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
插件启动或执行 `/meme检查` 时会自动检测；若缺失会提示类似：

```text
当前环境缺少系统库 libEGL (OpenGL EGL)，请执行命令“apt-get update && apt-get install -y libegl1 libgl1 libgles2”
```

常用安装命令：

```bash
# Debian / Ubuntu / 多数 Docker 镜像
apt-get update && apt-get install -y \
  libegl1 libgl1 libgles2 libglib2.0-0 fontconfig \
  fonts-noto-cjk fonts-noto-color-emoji

# 若仍报错可补装
apt-get install -y libxrender1 libxcursor1 libxkbcommon0 libdbus-1-3

# 桌面环境备选
sudo apt install -y libegl1-mesa libgles2-mesa libgl1-mesa-dev
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

| 命令 | 作用 | 默认落盘位置 |
|------|------|----------------|
| `/meme表情修复` | 下载 `memes.zip` 并解压 | `site-packages/meme_generator/memes/` |
| `/meme字体修复` | 下载 `fonts.zip` 并安装 | Linux: `~/.local/share/fonts/meme-generator`<br>Windows: `%LOCALAPPDATA%\Microsoft\Windows\Fonts` |
| `/meme检查` | 系统依赖 + 资源状态 | - |

资源默认从 GitHub Release 标签 `assets-v1` 下载：

- `https://github.com/Qiscard/astrbot_plugin_memelite/releases/tag/assets-v1`

也可在插件配置中填写：

- `memes_url` / `fonts_url`：自定义直链
- `local_memes_dir` / `local_fonts_dir`：本地已解压目录
- `local_memes_zip` / `local_fonts_zip`：本地 zip 路径

> 不再默认调用官方 `meme download` 拉全量上游图床。若仍想用官方在线检查，可打开配置 `is_check_resources`。

## 配置

AstrBot 面板：插件管理 -> astrbot_plugin_memelite -> 插件配置

| 配置项 | 描述 | 默认 |
|--------|------|------|
| `need_prefix` | 需要前缀或 @bot 才触发 | `true` |
| `extra_prefix` | 额外前缀 | `""` |
| `is_compress_image` | 压缩超过 512px 静态图 | `true` |
| `is_check_resources` | 启动时官方在线资源检查（旧逻辑） | `false` |
| `auto_fix_resources_on_start` | 启动时自动修复表情/字体 | `false` |
| `resource_repo` | 资源仓库 | `Qiscard/astrbot_plugin_memelite` |
| `resource_release_tag` | Release 标签 | `assets-v1` |
| `memes_url` / `fonts_url` | 自定义下载直链 | `""` |
| `local_memes_dir` / `local_fonts_dir` | 本地目录优先 | `""` |
| `local_memes_zip` / `local_fonts_zip` | 本地 zip 优先 | `""` |
| `meme_timeout` | 生成超时（秒） | `15` |
| `memes_disabled_list` | meme 黑名单 | `[]` |
| `user_blacklist` | 用户黑名单 | `""` |
| `protected_users` | 保护名单 | `""` |
| `bounce_back_memes` | 反弹表情列表 | `""` |

## 命令

| 命令 | 说明 | 权限 |
|------|------|------|
| `{关键词}` | 触发 meme | 普通 |
| `/meme帮助` | meme 列表图 | 普通 |
| `/meme详情 xxx` | 查看参数 | 普通 |
| `/meme检查` | 环境依赖与资源状态 | 普通 |
| `/meme表情修复` | 下载/安装表情资源包 | 管理员 |
| `/meme字体修复` | 下载/安装字体资源包 | 管理员 |
| `禁用meme xxx` / `启用meme xxx` | 黑名单 | 管理员 |
| `meme黑名单` | 查看禁用列表 | 管理员 |
| `添加保护` / `移除保护` / `保护名单` | 保护名单 | 管理员 |
| `添加反弹表情` / `移除反弹表情` / `反弹表情列表` | 反弹 | 管理员 |

## 使用说明

- 参数用空格分隔，如：`喜报 nmsl`
- 支持引用消息、`@某人` / `@qq号` 作为图片参数
- 参数不足时自动补发送者 / bot 头像与默认文本

## 维护资源包

本仓库 Release `assets-v1` 应包含：

- `memes.zip`：表情图片资源（解压到 `meme_generator/memes`）
- `fonts.zip`：字体文件（安装到用户字体目录）

本地打包示例（开发用）：

```bash
python _pack_assets.py
# 输出: assets/dist/memes.zip  assets/dist/fonts.zip
```

上传到 Release：

```bash
gh release create assets-v1 assets/dist/memes.zip assets/dist/fonts.zip \
  --title "Assets v1" \
  --notes "精简表情与字体资源包，供 /meme表情修复 与 /meme字体修复 使用"
```

## 注意事项

1. **装插件 ≠ 立即可用**：还需系统依赖 + `/meme表情修复` + `/meme字体修复`。
2. 重建 AstrBot 虚拟环境后，`site-packages` 内表情会丢失，需重新执行 `/meme表情修复`。
3. 中文乱码/缺字：执行 `/meme字体修复`；Linux 建议再装 `fonts-noto-cjk`。
4. Docker 请把系统依赖写入镜像或启动脚本，避免每次容器重建后重复踩坑。

## 相关链接

- [meme-generator](https://github.com/MemeCrafters/meme-generator)
- [meme-generator-rs](https://github.com/MemeCrafters/meme-generator-rs)
- [AstrBot](https://astrbot.app/)
