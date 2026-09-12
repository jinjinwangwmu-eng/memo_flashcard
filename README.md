# 记忆卡片·每日待办（安卓 APK 云端构建）

本仓库是「每日任务小程序」的安卓端源码，配合 GitHub Actions **在云端免费编译出 APK**，
不需要你本机装 WSL / Android Studio / Java。

编译成功后你会得到一个 `memo_flashcard-...-debug.apk`，拷到华为手机安装即可。

---

## 一、你需要准备的东西
1. 一个 **GitHub 账号**（免费，去 https://github.com 注册即可）。
2. 本文件夹 `memo_flashcard/` 已经全部就位：
   ```
   memo_flashcard/
   ├── main.py              # 手机端界面（待办/卡片/同步三页）
   ├── task_store.py        # 待办数据层
   ├── flashcard_store.py   # 卡片数据层
   ├── sync_core.py         # 同步合并核心
   ├── buildozer.spec       # 打包配置
   ├── 构建说明.txt          # 手机端使用/同步说明
   └── .github/workflows/build.yml   # 云端构建工作流（关键）
   ```

---

## 二、把代码传到 GitHub（推荐用 GitHub Desktop，最省心）

> 为什么用 GitHub Desktop：它能完整保留 `.github` 这种隐藏文件夹，网页拖拽上传可能丢这个文件夹，导致构建不触发。

1. 安装 GitHub Desktop：https://desktop.github.com/ ，登录你的 GitHub 账号。
2. 菜单 **File → New Repository…**
   - Name 填 `memo_flashcard`（随便起也行）
   - 勾选 **Initialize with README**（不勾也行，本文件夹已有 README）
   - 点 **Create Repository**
3. 把本文件夹 `memo_flashcard/` 里的**所有文件和子文件夹**复制进 GitHub Desktop 刚创建的那个本地仓库目录。
   - 重点确认 `.github` 文件夹也一起复制过去了（带点的隐藏文件夹别漏）。
4. GitHub Desktop 左侧会列出改动，Summary 随便写一句（如 `init`），点 **Commit to main**。
5. 点顶部 **Publish repository**（第一次会让你确认），保持默认公开（Public），点 **Publish**。

> 不想装 GitHub Desktop？也可以去 github.com 新建空仓库，用网页「Upload files」把整个文件夹拖进去。
> **但上传后请务必检查仓库里有没有 `.github/workflows/build.yml`**；没有就说明隐藏文件夹被漏掉了，改用 GitHub Desktop。

---

## 三、等云端编译（约 10–25 分钟）

1. 打开 https://github.com ，进入你刚建的仓库。
2. 点顶部 **Actions** 标签 → 应该能看到一个名叫 **Build Android APK** 的工作流正在运行（黄点=进行中）。
3. 点进去可以看到实时日志。首次构建会下载依赖、编译，约 10–25 分钟。
4. 看到绿色对勾 = 成功。

> 如果没自动开始：进 Actions 标签，点 **Build Android APK** → 右边 **Run workflow** 手动点一次。

---

## 四、下载 APK 并装到手机

1. 构建成功后，在刚才的 Actions 运行页面底部，有一个 **Artifacts** 区域，名叫 **memo_flashcard-apk**。
2. 点它下载，得到一个 zip，解压出 `memo_flashcard-1.0.0-arm64-v8a-debug.apk`。
3. 把 apk 传到华为手机（微信文件传输/数据线/U盘都行）。
4. 手机上点 apk → 若提示「禁止安装未知来源应用」，按提示去设置里**允许**本次安装 → 完成安装。

---

## 五、之后怎么用 / 怎么和电脑互传词库

看仓库里的 **`构建说明.txt`**（也存在于本文件夹），里面有：
- 手机 App 三页用法（待办 / 记忆卡片 / 同步）
- 电脑端双击 `启动同步服务.bat`、手机切「同步」页点「发现并同步」即可同一 Wi‑Fi 互传词库和待办

---

## 六、改了代码想重新出包

把改动重新复制进 GitHub Desktop 的仓库目录 → Commit → Push，
Actions 会自动重新构建，去 Artifacts 下载新 APK 即可。
