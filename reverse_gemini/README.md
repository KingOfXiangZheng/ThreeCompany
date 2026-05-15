# Google Gemini Chat Interface Reverse Engineering

逆向分析 Google Gemini (gemini.google.com) 聊天接口的 Node.js 实现。

## ⚠️ 重要说明

**本项目仅供学习和研究使用。** Google Gemini 的接口可能随时变化，并且使用这些接口可能违反 Google 的服务条款。

## 📋 逆向分析结论

### 接口信息

- **API 端点**: `https://gemini.google.com/_/BardChatUi/data/batchexecute`
- **请求方法**: POST
- **数据格式**: JSPSB (Google Protocol Buffer JSON)
- **认证方式**: Cookie (`__Secure-ENID`)

### 请求参数

| 参数 | 说明 |
|------|------|
| `rpcids` | RPC 方法 ID（如 `aPya6c` 发送消息） |
| `source-path` | 固定值 `/app` |
| `bl` | 后端版本标识 |
| `f.sid` | 会话 ID |
| `hl` | 语言（zh-CN） |
| `_reqid` | 请求序号 |
| `rt` | 固定值 `c` |

### 关键 Header

- `x-goog-ext-73010989-jspb`: `[0]`
- `x-goog-ext-525001261-jspb`: 会话追踪数据
- `cookie`: `__Secure-ENID=...`

## 🔧 安装和使用

### 1. 安装依赖

```bash
npm install
```

### 2. 获取 Cookie

从浏览器登录 https://gemini.google.com 后：

1. 按 F12 打开开发者工具
2. 切换到 Network（网络）标签
3. 刷新页面
4. 找到任意一个 `batchexecute` 请求
5. 在 Request Headers 中复制 `cookie` 值
6. 提取 `__Secure-ENID=...` 部分填入 `config/cookies.json`

### 3. 配置 Cookie

编辑 `config/cookies.json`:

```json
{
  "__Secure-ENID": "你的cookie值"
}
```

### 4. 发送消息

```bash
node main.js "你好，请介绍一下你自己"
```

## 📁 项目结构

```
gemini-reverse/
├── config/
│   ├── cookies.json    # Cookie 配置（需手动填写）
│   ├── headers.json     # 请求头模板
│   └── config.json      # 基础配置
├── utils/
│   └── request.js       # 请求构建工具
├── main.js             # 主程序
├── package.json
└── README.md
```

## 🔍 已知问题

1. **Cookie 过期**: Google Cookie 有有效期限制，过期后需要重新获取
2. **会话追踪**: `x-goog-ext-525001261-jspb` Header 包含动态数据，可能需要更新
3. **请求频率**: 频繁请求可能被限制

## 🛠️ 调试

如需调试，可以：

1. 使用浏览器开发者工具查看网络请求
2. 检查 `config/headers.json` 的 header 配置
3. 查看 `utils/request.js` 的请求构建逻辑

## 📝 免责声明

本项目仅用于教育目的。使用本项目代码即表示您同意：

1. 仅将本项目用于学习研究目的
2. 遵守 Google 的服务条款
3. 不使用本项目进行任何滥用或非法活动

**作者不对因使用本项目造成的任何后果负责。**

---

## 逆向分析摘要

基于浏览器抓包分析，Google Gemini 使用以下技术：

1. **内部 RPC 架构**: 使用 `batchexecute` 接口处理所有请求
2. **JSPSB 协议**: Google 自定义的 Protocol Buffer JSON 格式
3. **Cookie 认证**: 基于 `__Secure-ENID` 的会话认证
4. **WebSocket 推送**: 使用 `signaler-pa.clients6.google.com` 进行实时响应推送

如需进一步分析，建议使用浏览器开发者工具进行深度抓包分析。

---

## 2026-05-12 更新：Gemini 凭证刷新与历史记录写入

当前 Python 纯 HTTP 实现不再只依赖 `__Secure-ENID`。如果希望 API 调用生成的会话能在 Gemini 浏览器页面中看到，需要使用浏览器同账号的完整 Gemini/Google 登录态 cookie，并在 `StreamGenerate` 成功后执行浏览器同款确认请求。

### 更新 Gemini 凭证

在项目根目录运行：

```powershell
python core/refresh_browser_auth.py --target gemini
```

流程：

1. 脚本会打开或连接 Chrome。
2. 在浏览器中登录 `https://gemini.google.com`。
3. 登录完成后回到终端按 Enter。
4. 脚本会更新：
   - `reverse_gemini/config/cookies.json`
   - `reverse_gemini/config/headers.json`

也可以通过 Camoufox 抓包/导出当前登录态后写入 `cookies.json`，但要注意只保留 Gemini 请求实际使用的 Google/Gemini cookie，避免混入 `.youtube.com`、`.google.de`、`.google.com.co` 等同名 cookie。

### 验证凭证

```powershell
python reverse_gemini/main.py "Reply exactly: credential_test" --model gemini-3-pro
```

成功标准：

- `StreamGenerate` 返回 200。
- 返回文本中包含 `credential_test`。
- 返回 metadata 中包含 `conversation_id` 和 `response_id`。
- 内部后置 `PCck7e(response_id)` 返回 200。

如果需要验证浏览器历史记录，使用返回的 `conversation_id`，去掉前缀 `c_` 后打开：

```text
https://gemini.google.com/app/<conversation_id without c_>
```

页面能以 200 打开并显示刚生成的对话，说明会话已经写入浏览器账号历史。

### Pro 模型字段

浏览器抓包确认 Gemini Pro 当前关键字段为：

- mode id: `e6fa609c3fa255c0`
- mode code: `3`
- body: `inner[17]=[[0]]`, `inner[79]=3`
- 响应 metadata: `3.1 Pro`

`gemini-pro` 会被规范化为 `gemini-3-pro`。如果怀疑模型不对，优先看请求字段和响应 metadata，不要只依赖模型自报。

### 历史记录写入链路

只调用 `StreamGenerate` 能拿到回复，但不一定会出现在 Gemini 浏览器历史记录里。浏览器真实链路中，生成后还会调用：

```text
PCck7e([response_id])
```

这个请求必须带同一个 bootstrap 页面里的 `at` 参数；缺失时会返回 400，并在响应里出现 `xsrf` 标记。

### Cookie 注意事项

不要把完整 cookie 同时种到 `.google.com` 和 `gemini.google.com`。这样会导致请求头里出现重复的 `SID`、`__Secure-*PSID` 等 cookie，完整登录态下可能直接返回 401。

正确做法是构造一个浏览器同款的单一 `Cookie` header。当前 Python 实现已经按这个方式发送。

常见需要保留的 Gemini 请求 cookie 包括：

- `SOCS`
- `__Secure-ENID`
- `SID`
- `__Secure-1PSID`
- `__Secure-3PSID`
- `HSID`
- `SSID`
- `APISID`
- `SAPISID`
- `__Secure-1PAPISID`
- `__Secure-3PAPISID`
- `SIDCC`
- `__Secure-1PSIDCC`
- `__Secure-3PSIDCC`
- `COMPASS`
- `__Secure-1PSIDTS`
- `__Secure-3PSIDTS`
- `NID`

更详细的逆向记录见：

```text
reverse_gemini/GEMINI_REVERSE_NOTES.md
```
