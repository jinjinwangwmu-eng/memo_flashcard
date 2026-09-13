[app]

# 应用信息
title = 记忆卡片·每日待办
package.name = memo_flashcard
package.domain = org.memo

# 入口：本目录下的 main.py
source.dir = .
source.include_exts = py,png,jpg,kv,json,txt,ttf,otf

version = 1.0.1
version.code = 2

# 启动入口
app.main = main.py

# 依赖
requirements = python3,kivy,jnius

# 安卓设置
android.permissions = INTERNET, ACCESS_WIFI_STATE, ACCESS_NETWORK_STATE
android.api = 31
android.minapi = 21
android.ndk = 25b
android.accept_sdk_license = True
android.arch = arm64-v8a
# 中文字体：随包打入，供 main.py 注册为默认字体
android.add_fonts = assets/font.ttf

# 方向：竖屏
orientation = portrait

# 日志
log_level = 2
warn_on_root = 0

[buildozer]
# 默认目标 android
target = android
