# TextBasedMCPAgent

TextBasedMCPAgent 是一个专为不支持 function calling 的模型设计的 MCP Agent。它使用文本解析的方式来识别和执行工具调用，而不依赖于 OpenAI 的 function calling API。

## 工作原理

TextBasedMCPAgent 通过以下方式工作：

1. **文本解析**：使用正则表达式从模型的文本输出中提取工具调用
2. **提示工程**：使用特定的提示词引导模型生成格式化的工具调用
3. **MCP 集成**：将解析后的工具调用转发到 MCP 服务器执行

## 工具调用格式

模型需要使用以下格式来调用工具：

```
<tool>
name: [工具名称]
args: {
  "参数1": "值1",
  "参数2": "值2"
}
</tool>
```

例如：

```
<tool>
name: browser
args: {
  "url": "https://example.com"
}
</tool>
```

## 使用方法

### 1. 配置 Ollama 或其他不支持 function calling 的模型

在 `config/config.toml` 中配置你的模型：

```toml
[llm]
api_type = 'ollama'
model = "llama3.2"  # 或其他模型
base_url = "http://localhost:11434/v1"
api_key = "ollama"
max_tokens = 4096
temperature = 0.0
```

### 2. 运行 TextBasedMCPAgent

使用提供的运行脚本启动 Agent：

```bash
python run_text_mcp.py --interactive
```

或者使用单个提示：

```bash
python run_text_mcp.py --prompt "查找关于人工智能的最新信息"
```

## 支持的工具

TextBasedMCPAgent 支持所有 MCP 服务器提供的工具，包括：

- **bash**：执行 shell 命令
- **browser**：浏览网页
- **editor**：编辑文件
- **terminate**：终止 Agent 执行

## 优势

- 可以使用不支持 function calling 的本地模型
- 减少对特定 API 功能的依赖
- 可以与各种 LLM 提供商一起使用
- 保持与原始 MCP 功能的兼容性

## 限制

- 依赖于模型正确格式化工具调用
- 可能需要更多的提示和引导
- 解析可能不如原生 function calling 可靠

## 示例

```
用户: 请帮我查找关于人工智能的最新信息

Agent: 我会帮你查找关于人工智能的最新信息。让我使用浏览器工具来搜索。

<tool>
name: browser
args: {
  "url": "https://www.google.com/search?q=latest+artificial+intelligence+news"
}
</tool>

现在我正在浏览搜索结果，让我查看一些最新的文章...
```
