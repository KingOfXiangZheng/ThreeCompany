/**
 * Google Gemini Chat Interface - Node.js Client
 * 逆向分析接口实现 v2.0
 *
 * 使用说明:
 * 1. npm install
 * 2. 从浏览器登录 gemini.google.com 后导出 __Secure-ENID cookie
 * 3. 将 cookie 值填入 config/cookies.json
 * 4. node main.js "你的问题"
 */

const axios = require('axios');
const https = require('https');
const fs = require('fs');
const path = require('path');

// 加载配置
const configDir = path.join(__dirname, 'config');
const cookies = JSON.parse(fs.readFileSync(path.join(configDir, 'cookies.json'), 'utf8'));
const headers = JSON.parse(fs.readFileSync(path.join(configDir, 'headers.json'), 'utf8'));
const config = JSON.parse(fs.readFileSync(path.join(configDir, 'config.json'), 'utf8'));

// HTTPS Agent
const httpsAgent = new https.Agent({ rejectUnauthorized: false });

// Axios 实例
const client = axios.create({
  baseURL: config.api_base,
  timeout: 30000,
  httpsAgent
});

let conversationId = null;
let extData = null;

// ============== 工具函数 ==============

function generateSessionId() {
  const timestamp = Date.now();
  const random = Math.floor(Math.random() * 9999999999999);
  const combined = BigInt(timestamp) * BigInt(10000000000000) + BigInt(random);
  return combined.toString();
}

function buildQueryParams(rpcId) {
  const params = new URLSearchParams({
    rpcids: rpcId,
    'source-path': '/app',
    bl: config.version,
    'f.sid': generateSessionId(),
    hl: config.hl,
    _reqid: Math.floor(Math.random() * 9000000 + 1000000),
    rt: 'c'
  });
  return params.toString();
}

function buildRequestBody(rpcId, paramsArray) {
  // Google 的 JSPSB 格式: [[[rpcId, paramsString, null, "generic"]]]
  const reqData = [[[rpcId, JSON.stringify(paramsArray), null, 'generic']]];
  return new URLSearchParams({ 'f.req': JSON.stringify(reqData) }).toString();
}

function buildHeaders() {
  const allHeaders = { ...headers };

  // Google 扩展头
  allHeaders['x-goog-ext-73010989-jspb'] = '[0]';
  allHeaders['x-goog-ext-525001261-jspb'] = '[1,null,null,null,null,null,null,null,[4],null,null,null,null,null,1,null,"225CDC15-DF99-4545-A619-74C720D374A7"]';

  // 添加 Cookie
  const enidValue = cookies['__Secure-ENID'];
  const fullCookie = enidValue.startsWith('33.SE=')
    ? `__Secure-ENID=${enidValue}`
    : `__Secure-ENID=33.SE=${enidValue}`;
  allHeaders['cookie'] = fullCookie;

  return allHeaders;
}

// ============== 主要功能 =============

/**
 * 发送消息到 Gemini
 * Google 使用流式响应，实际内容通过 WebSocket 推送
 * 这里我们模拟完整的请求流程
 */
async function sendMessage(message, convId = null) {
  try {
    // 构建发送参数 - Google 需要完整的参数结构
    const sendParams = [
      convId,                              // conversation ID
      message,                             // 消息内容
      null,                                // 附加参数1
      null,                                // 附加参数2
      [0]                                  // 安全标记
    ];

    console.log('正在发送消息...');

    // 1. 发送消息请求
    const queryParams = buildQueryParams('aPya6c');
    const body = buildRequestBody('aPya6c', sendParams);
    const reqHeaders = buildHeaders();

    const sendResponse = await client.post(
      `${config.batchexecute_path}?${queryParams}`,
      body,
      { headers: reqHeaders }
    );

    console.log('发送请求完成, 状态:', sendResponse.status);
    console.log('响应:', sendResponse.data.substring(0, 300));

    // 2. 等待并轮询响应
    console.log('\n等待响应生成...');

    // 提取响应中的关键信息
    const sendData = sendResponse.data;
    let conversationIdMatch = sendData.match(/"([^"]*c2c7f529[^"]*)"/);
    if (conversationIdMatch) {
      conversationId = conversationIdMatch[1];
      console.log('会话 ID:', conversationId);
    }

    // 3. 轮询获取完整响应 (模拟 WebSocket 行为)
    let responseReceived = false;
    let attempts = 0;
    const maxAttempts = 5;

    while (!responseReceived && attempts < maxAttempts) {
      await new Promise(resolve => setTimeout(resolve, 1500));
      attempts++;

      try {
        // 使用不同的 RPC 获取更新
        const pollParams = buildQueryParams('CNgdBe');
        const pollBody = buildRequestBody('CNgdBe', []);
        const pollHeaders = buildHeaders();

        const pollResponse = await client.post(
          `${config.batchexecute_path}?${pollParams}`,
          pollBody,
          { headers: pollHeaders }
        );

        const pollData = pollResponse.data;
        console.log(`轮询 ${attempts}:`, pollData.substring(0, 200));

        // 检查是否有内容
        if (pollData.includes('"wrb.fr"') && pollData.includes('text')) {
          const result = extractTextFromResponse(pollData);
          if (result) {
            return result;
          }
        }
      } catch (e) {
        console.log(`轮询 ${attempts} 出错:`, e.message);
      }
    }

    return {
      status: 'partial',
      message: '流式响应需要 WebSocket 支持',
      conversationId: conversationId,
      note: '在浏览器中可以正常获取完整响应，纯 HTTP 协议需要 WebSocket 支持'
    };

  } catch (error) {
    console.error('请求失败:', error.message);
    if (error.response) {
      console.error('响应状态:', error.response.status);
      console.error('响应数据:', error.response.data);
    }
    return { error: error.message };
  }
}

/**
 * 从响应中提取文本内容
 */
function extractTextFromResponse(data) {
  try {
    // 解析 JSPSB 格式
    const lines = data.split('\n').filter(l => l.trim());

    for (const line of lines) {
      if (line.startsWith('[[') && !line.includes('"e",4')) {
        try {
          const parsed = JSON.parse(line);

          // 遍历数组元素
          for (const item of parsed) {
            if (Array.isArray(item) && item[0] === 'wrb.fr') {
              // wrb.fr 是响应帧
              // 格式: ["wrb.fr", rpcId, content, ...]
              for (let i = 2; i < item.length; i++) {
                if (typeof item[i] === 'string' && item[i].length > 10) {
                  return {
                    success: true,
                    message: item[i],
                    type: 'text'
                  };
                }
              }
            }
          }
        } catch (e) {
          // 跳过无效 JSON
        }
      }
    }
  } catch (error) {
    console.log('解析响应出错:', error.message);
  }
  return null;
}

/**
 * 获取对话历史
 */
async function getHistory(convId) {
  if (!convId) {
    return { error: '需要会话 ID' };
  }

  try {
    const queryParams = buildQueryParams('L5adhe');
    const body = buildRequestBody('L5adhe', [convId]);
    const reqHeaders = buildHeaders();

    const response = await client.post(
      `${config.batchexecute_path}?${queryParams}`,
      body,
      { headers: reqHeaders }
    );

    return { data: response.data };
  } catch (error) {
    return { error: error.message };
  }
}

// ============== 主函数 ==============

async function main() {
  const args = process.argv.slice(2);

  if (args.length === 0) {
    console.log(`
╔═══════════════════════════════════════════════════════════╗
║       Google Gemini Chat Client - v2.0                     ║
╚═══════════════════════════════════════════════════════════╝

使用方法:
  node main.js "你的问题"

示例:
  node main.js "你好，请介绍一下你自己"

注意:
  - 需要在 config/cookies.json 中配置有效的登录 Cookie
  - Google 使用流式响应，实际内容通过 WebSocket 推送
  - 纯 HTTP 协议无法获取完整响应内容
    `);
    return;
  }

  const message = args.join(' ');
  console.log(`\n发送消息: ${message}\n`);

  const result = await sendMessage(message, conversationId);

  console.log('\n========== 结果 ==========');
  console.log(JSON.stringify(result, null, 2));
  console.log('===========================\n');
}

// 导出模块
module.exports = { sendMessage, getHistory };

// 运行
if (require.main === module) {
  main();
}