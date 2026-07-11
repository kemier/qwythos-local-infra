# Qwythos-9B Local Inference

在 Apple Silicon Mac 上通过 llama.cpp 运行 Qwythos-9B Claude Mythos 模型，并通过代理让 Codex CLI 能够使用本地模型执行工具调用。

> 当前推荐配置：`--ctx-size 262144`、`--reasoning-budget 0`、`model_reasoning_effort = "high"`。

## 架构

```
Codex CLI → codex_proxy.py (8090) → llama-server (8007) → Qwythos-9B Q8 (Metal)
```

## 文件结构

| 文件 | 说明 |
|------|------|
| `models/Qwythos-9B-Claude-Mythos-5-1M-Q8_0.gguf` | Q8 量化模型文件 |
| `src-latest/` | llama.cpp 源码和编译产物 |
| `src-latest/build/bin/llama-server` | 编译后可执行的 server 二进制 |
| `codex_proxy.py` | Codex ↔ llama-server 协议转换代理 |
| `start_server.sh` | llama-server 一键启动脚本（写入 PID 文件） |
| `stop_server.sh` | llama-server 停止脚本（读 PID 文件 + 端口回退） |
| `think_strip.py` | （可选）思考块过滤代理 |

## 编译 llama.cpp

### 依赖

- Xcode (Command Line Tools 不够，需要完整 Xcode，含 Metal/AppKit 等 framework)
- [brew 安装的 LLVM/Clang](https://formulae.brew.sh/formula/llvm)（提供 libc++ 和 Modern CMake）
- OpenSSL (`brew install openssl`)
- macOS 14+（Metal shader 要求）

> **macOS 26+ 注意**：Apple 从 macOS 26 (Sequoia) 开始不再在 `/usr/lib` 下提供 `libc++.1.dylib`。CMake 默认会把 rpath 设为 build 目录，因此编译时需要把 `/usr/local/lib`（Homebrew LLVM 自带的 libc++）加进 rpath；否则编译出的二进制启动会报 `dyld: Library not loaded: @rpath/libc++.1.dylib`。这个修复在 `src-latest/tools/ui/CMakeLists.txt` 里（给 `llama-ui-embed` 加 `-Wl,-rpath,/usr/local/lib`）。

### 配置

```bash
cd src-latest

cmake -B build \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=ON \
  -DGGML_METAL=ON \
  -DGGML_BLAS=OFF \
  -DGGML_CPU=ON \
  -DCMAKE_OSX_ARCHITECTURES=arm64
```

关键选项说明：

| 选项 | 值 | 作用 |
|------|-----|------|
| `CMAKE_BUILD_TYPE` | `Release` | 启用优化 |
| `BUILD_SHARED_LIBS` | `ON` | ggml/llama 编译为 `.dylib`（`llama-server` 动态加载） |
| `GGML_METAL` | `ON` | 启用 Apple Silicon GPU 加速 |
| `GGML_BLAS` | `OFF` | 不使用 Accelerate BLAS（Metal 已经够用） |
| `GGML_CPU` | `ON` | 保留 CPU 后备后端 |
| `CMAKE_OSX_ARCHITECTURES` | `arm64` | 仅编译 ARM（Apple Silicon） |

### 编译

```bash
cmake --build build -j$(sysctl -n hw.ncpu) --target llama-server
```

产物位置：

- `src-latest/build/bin/llama-server` — 主可执行文件
- `src-latest/build/bin/libllama-server-impl.dylib` — server 实现动态库
- `src-latest/build/bin/libllama.*.dylib` / `libggml*` — 底层依赖

### macOS 26+ 运行期 libc++ 缺失的解决办法

编译完成后，如果启动报 `dyld: Library not loaded: @rpath/libc++.1.dylib`，把 Homebrew LLVM 的运行时库拷到 `build/bin/`：

```bash
cp -a /usr/local/lib/libc++.1.0.dylib /usr/local/lib/libc++abi.1.0.dylib /usr/local/lib/libunwind.1.0.dylib src-latest/build/bin/
cd src-latest/build/bin
ln -sf libc++.1.0.dylib libc++.1.dylib
ln -sf libc++abi.1.0.dylib libc++abi.1.dylib
ln -sf libunwind.1.0.dylib libunwind.1.dylib
```

这些文件和 `llama-server` 放在一起，就能带在身边分发，不依赖系统 libc++。

## 快速启动

### 1. llama-server

```bash
cd ~/projects/llama-cpp-project
./start_server.sh
```

`start_server.sh` 会在 `exec` 前把当前 PID 写入 `.llama-server.pid`，退出时自动清理；重复启动若发现已有存活进程会被拒绝。

停止：

```bash
./stop_server.sh        # 默认 8007 端口
./stop_server.sh 8080   # 自定义端口
```

停止逻辑：先读 PID 文件（精确）；若 PID 文件不存在（手动启动过），回退到 `lsof -ti tcp:<port>` 按端口查找。

或手动启动：

```bash
./src-latest/build/bin/llama-server \
  --model models/Qwythos-9B-Claude-Mythos-5-1M-Q8_0.gguf \
  --alias qwythos \
  --host 0.0.0.0 --port 8007 \
  --ctx-size 262144 \
  --parallel 1 --batch-size 2048 --ubatch-size 512 \
  --reasoning-format deepseek --reasoning-budget 0 \
  --temp 0.6 --top-p 0.9 --top-k 40 \
  --repeat-penalty 1.2 \
  --dry-multiplier 1.0 --dry-base 2 --dry-penalty-last-n 2048 \
  --n-gpu-layers 99 --flash-attn on --fit on
```

**重要**：不要加 `--chat-template chatml` 或 `--jinja` 参数——使用 GGUF 内置的 jinja 模板（支持工具调用）。

### 2. 代理

```bash
python3 ~/projects/llama-cpp-project/codex_proxy.py 8090
```

代理解决的问题：
- Codex 发送 `developer` role → 改为 `system`（匹配 Qwythos 模板）
- Codex 发送顶级 `instructions` 字段 → 合并到 developer 消息内容（避免双重 system 消息导致模板报错）
- 合并重复的 `system` 消息（MCP 场景下 Codex 可能同时发 system + developer，需去重才能通过模板校验）
- 正确处理 Content-Length
- 劫持 `GET /v1/models` 并在响应中注入 `context_length: 262144`，让 Codex 知道模型的真实上下文窗口，从而在接近上限时自动触发 compact（详见下文）

### 3. Codex 配置 (`~/.codex/config.toml`)

```toml
model_provider = "custom"
model = "qwythos"
model_reasoning_effort = "high"
disable_response_storage = true
model_auto_compact_token_limit = 200000

[model_providers.custom]
name = "custom"
base_url = "http://127.0.0.1:8090/v1"
wire_api = "responses"
requires_openai_auth = false
stream_idle_timeout_ms = 600000
stream_max_retries = 3
```

`model_auto_compact_token_limit` 是自定义 provider 接入本地模型时**唯一需要多填的一行**。没有它的话，Codex 只能靠 `/v1/models` 返回的 `context_length` 来判断 —— 而之前那个字段是空的，所以 compact 永远不触发。现在 proxy 虽然已经注入了 `context_length`，但显式写上更稳妥：即使哪天 proxy 没跑，Codex 也有 200K 压缩阈值兜底，不会撞到 llama-server 的 262K 硬上限。

### 4. 使用

```bash
cd ~/projects/some-project
codex exec --model qwythos "你的问题"
```

## 性能

| 指标 | 数值 |
|------|------|
| 模型 | Qwythos-9B Q8_0 (9B 参数) |
| 推理速度 | ~18 tokens/s |
| 首 token 延迟 (TTFT) | ~0.14s |
| GPU 加速 | Apple Silicon Metal (`-ngl 99`) |
| 上下文窗口 | 262144 tokens (32GB Mac 实测稳定) |

## 防重复 / 防无限循环

默认配置已启用 DRY sampler 和重复惩罚，防止模型陷入重复循环：

```bash
--repeat-penalty 1.2
--dry-multiplier 1.0 --dry-base 2 --dry-penalty-last-n 2048
```

如果仍出现重复，可调整：
- 增大 `--dry-multiplier`（如 `1.5`）— 更强抑制重复
- 增大 `--repeat-penalty`（如 `1.3`）— 惩罚最近 64 个 token 内的重复
- 增大 `--dry-penalty-last-n`（如 `4096`）— 扩大防重复窗口
- 提高 `--temp`（如 `0.7`）— 增加随机性

```bash
# 更强防重复配置
--temp 0.7 --repeat-penalty 1.3 --dry-multiplier 1.5 --dry-penalty-last-n 4096
```

## Auto-Compact（自动历史压缩）

Codex 通过读取模型的 `context_window` 来决定何时触发 compact（压缩历史）。
`codex_proxy.py` 已经在 `GET /v1/models` 返回里注入 `context_length: 262144`，
所以 Codex 会自动算出阈值并在累计 token 接近上下文窗口的 90% 时触发压缩。

阈值规则（来源：`codex-rs/protocol/src/openai_models.rs`）：

```
auto_compact_threshold = min(
    config.model_auto_compact_token_limit,  # 用户可选设置
    context_window * 90%                     # 来自 /v1/models
)
```

如果两项都未知（比如没跑 proxy 直接连 llama-server），阈值退化为 `i64::MAX`，
compact 永远不会触发，直到 llama-server 报 `request exceeds context size`。

如有需要，也可以显式写在配置里覆盖：

```toml
# ~/.codex/config.toml
model_auto_compact_token_limit = 200000   # 约 200K 就触发，留出余量
```

## 故障排除

### "System message must be at the beginning" 错误

原因：Codex 发送的 `instructions` 字段被 llama.cpp 转为 system 消息，与 developer 消息冲突。

解决：确保 `codex_proxy.py` 在运行，它会合并 instructions 到 developer 消息。

### 模型无限循环重复输出

原因：默认 sampler 允许 token 重复。

解决：启用 DRY sampler（默认已开启）。如仍重复，增大 `--dry-multiplier`。

### 只输出纯文本不执行工具

原因：用了 `chatml` 模板（不支持工具调用语法）。

解决：去掉 `--chat-template chatml` 参数，使用 GGUF 内置模板。

### 流式断开（stream disconnected）

原因：HTTP/1.0 不支持 chunked transfer encoding。

解决：确保代理正确转发 HTTP/1.1 响应。

### 请求超出上下文限制

```
request (17111 tokens) exceeds the available context size (16384 tokens)
```

原因：Codex 请求（工具定义 + 系统提示 + 用户消息）超出 ctx-size。

解决：增大 `--ctx-size`（最大 262144，32GB Mac）。

### `dyld: Library not loaded: @rpath/libc++.1.dylib`

原因：macOS 26+ 不再在 `/usr/lib` 下提供 `libc++.1.dylib`。Clang 编译的动态 C++ 二进制在运行时找不到它。

解决：把 `/usr/local/lib`（Homebrew LLVM）下的 `libc++.1.0.dylib`、`libc++abi.1.0.dylib`、`libunwind.1.0.dylib` 复制到 `build/bin/` 下即可。详细命令见本文「编译 llama.cpp」章节。

## 已知限制

- macOS 27 Tahoe Metal 内存限制：`-ngl 999` 会 OOM，需用 `-ngl 99`
- 上下文窗口最大 262144（32GB Mac 实测稳定；192K 在旧配置下触发 Metal compute error，256K 可稳定运行）
- `--reasoning-budget 0` 禁用思考块输出；设为正数可保留 `<think>` 推理（但可能耗尽 token 预算导致无输出，不建议目前开启）
- 32GB Mac 推荐 ctx-size ≤ 256K
- 9B 模型工具调用稳定性有限：Responses API 格式下 tool_call 触发不完全可靠，偶尔会直接用文本回答
