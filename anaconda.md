**✅ Windows 11 安装 Anaconda 完整指南（2026 最新）**

以下是目前最推荐的安装和配置流程，适合大多数用户（尤其是做数据科学、机器学习、科研的用户）。

### 1. 下载 Anaconda

1. 打开浏览器，访问官网：  
   **[https://www.anaconda.com/download](https://www.anaconda.com/download)**

2. 页面会自动识别 Windows 系统，点击 **Download** 下载 **64-bit** 版本（推荐）。

   - 文件名类似：`Anaconda3-2025.06-Windows-x86_64.exe`（版本号会不断更新）
   - 文件较大（约 600MB~1GB），建议用稳定网络下载。

---

### 2. 安装 Anaconda（关键步骤）

1. **双击运行** 下载的 `.exe` 文件（**建议右键 → 以管理员身份运行**）。
2. 点击 **Next** → 同意 License Agreement。
3. **选择安装类型**（重要）：
   - **Just Me (recommended)** ← **推荐大多数人选择**
   - All Users（需要管理员权限，安装到系统目录）
4. **选择安装位置**：
   - 默认路径通常是 `C:\Users\你的用户名\anaconda3`
   - **建议**：路径尽量不要包含中文或空格（虽然现在兼容性好了很多，但保险起见）。
5. **Add Anaconda to my PATH environment variable**（最重要的一步）：
   - **强烈建议不要勾选**！
   - Anaconda 官方也不推荐勾选（尤其是 All Users 安装），容易和系统自带 Python 冲突。
   - 后面我们用专门的 **Anaconda Prompt** 就行。
6. 点击 **Install**，等待安装完成（大概 5~15 分钟，取决于电脑性能）。

安装完成后可以选择打开 **Anaconda Navigator**（图形界面）。

---

### 3. 验证安装是否成功

按 `Win` 键搜索并打开 **Anaconda Prompt**（这是 Anaconda 自带的命令行）。

在里面依次输入：

```bash
conda --version
python --version
```

看到版本号就说明安装成功了（目前一般是 Python 3.12 或 3.13）。

---

### 4. 更新 conda（安装后立即执行）

在 **Anaconda Prompt** 中运行：

```bash
conda update conda
conda update --all
```

---

### 5. 配置国内镜像源（中国用户强烈推荐）

官方源在国内访问经常很慢，配置**清华大学镜像**能大幅加速。

#### 方法一：命令行配置（推荐）

在 Anaconda Prompt 中依次执行以下命令：

```bash
conda config --set show_channel_urls yes

conda config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main
conda config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r
conda config --add channels https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge
```

#### 方法二：手动编辑 `.condarc` 文件

1. 按 `Win + R`，输入 `%USERPROFILE%` 回车，打开用户主目录。
2. 找到 `.condarc` 文件（没有就先运行上面 `conda config --set show_channel_urls yes` 生成）。
3. 用记事本打开，**替换**为以下内容：

```yaml
channels:
  - conda-forge
  - defaults
show_channel_urls: true
default_channels:
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r
  - https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/msys2
custom_channels:
  conda-forge: https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge
```

保存后，在 Anaconda Prompt 测试是否生效：

```bash
conda config --show channels
```

---

### 6. 常用操作

| 操作 | 命令 |
|------|------|
| **创建新环境**（强烈推荐） | `conda create -n myenv python=3.12` |
| **激活环境** | `conda activate myenv` |
| **退出环境** | `conda deactivate` |
| **查看所有环境** | `conda env list` |
| **安装包** | `conda install numpy pandas matplotlib jupyter` |
| **用 pip 安装**（在 conda 环境里） | `pip install torch torchvision torchaudio` |
| **删除环境** | `conda remove -n myenv --all` |

---

### 7. 其他实用建议

- **Anaconda Navigator**：开始菜单搜索打开，可图形化管理环境、安装包、启动 Jupyter Lab / Spyder。
- **想在 PowerShell 里使用 conda**：
  ```bash
  conda init powershell
  ```
  然后**重启** PowerShell。
- **VS Code / PyCharm 配置**：安装 Python 扩展后，选择对应的 conda 环境解释器即可。
- **磁盘空间**：完整安装大概占用 3~6GB。
- **如果已经安装了其他 Python**：Anaconda 可以共存，只要不把 Anaconda 加到系统 PATH 就基本没问题。

---

**安装过程中遇到问题？**

请把**具体错误信息**（截图或文字）发给我，我帮你排查。

需要我再给你：
- 创建常用科研环境的推荐命令（PyTorch + Jupyter 等）？
- 配置 Jupyter Lab 的进阶设置？
- 或者 Miniconda（更轻量版）的安装方法？

随时说！