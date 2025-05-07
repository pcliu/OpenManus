"""
TextBasedMCPAgent - An MCP Agent that works with models that don't support function calling.
"""
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple, Union

from pydantic import Field

from app.agent.mcp import MCPAgent
from app.logger import logger
from app.schema import AgentState, Function, Message, ToolCall
from app.tool.base import ToolResult


# 改进的系统提示，增加错误处理指导
TEXT_SYSTEM_PROMPT = """You are an AI assistant with access to tools.

To use a tool, format your response like this:

<tool>
name: [tool name]
args: {
  "param1": "value1"
}
</tool>

Example:
<tool>
name: bash
args: {
  "command": "ls -la"
}
</tool>

IMPORTANT ERROR HANDLING:
1. If a tool returns an error, DO NOT repeat the same call with identical parameters.
2. For path-related errors, always use absolute paths like "/Users/username/path" instead of relative paths like "." or "..".
3. If str_replace_editor fails, try using bash tool instead.
4. After completing a task or if you encounter persistent errors, summarize what you've learned and consider the task complete.

Keep your responses short and focused. Use tools when needed to complete tasks.
"""


class TextBasedMCPAgent(MCPAgent):
    """MCP Agent for models without function calling support.

    This agent uses text-based parsing to extract tool calls from model responses,
    rather than relying on the function calling API.
    """

    name: str = "text_mcp_agent"
    description: str = "An agent that connects to an MCP server and uses text-based tool calling."

    # Override system prompt with our text-based version
    system_prompt: str = TEXT_SYSTEM_PROMPT

    # Tool call extraction pattern
    tool_pattern: re.Pattern = re.compile(
        r"<tool>\s*name:\s*([^\n]+)\s*args:\s*(\{[^<]+\})\s*</tool>",
        re.DOTALL
    )

    # Maximum number of messages to keep in history (显著增加历史消息数量)
    max_history_messages: int = 20

    async def think(self) -> bool:
        """Process current state and decide next action using text-based tool calling."""
        # Check MCP session and tools availability (same as parent)
        if not self.mcp_clients.session or not self.mcp_clients.tool_map:
            logger.info("MCP service is no longer available, ending interaction")
            self.state = AgentState.FINISHED
            return False

        # Refresh tools periodically (same as parent)
        if self.current_step % self._refresh_tools_interval == 0:
            await self._refresh_tools()
            # All tools removed indicates shutdown
            if not self.mcp_clients.tool_map:
                logger.info("MCP service has shut down, ending interaction")
                self.state = AgentState.FINISHED
                return False

        # 激进清理消息历史，减少上下文长度
        self._manage_message_history()

        # 生成简化的工具描述
        tool_descriptions = self._generate_simplified_tool_descriptions()

        # 创建简化的系统消息
        custom_system_prompt = f"{self.system_prompt}\n\nTools:\n{tool_descriptions}"

        try:
            # 使用非流式响应并增加重试间隔
            import time
            retry_count = 0
            max_retries = 3
            retry_delay = 2  # 初始重试间隔（秒）

            while retry_count < max_retries:
                try:
                    # 使用非流式响应并减小模型负担
                    response_text = await self.llm.ask(
                        messages=self.messages,
                        system_msgs=[Message.system_message(custom_system_prompt)],
                        stream=False,  # 禁用流式响应以提高稳定性
                    )
                    break  # 成功获取响应，跳出循环
                except ValueError as e:
                    # 处理空响应错误
                    retry_count += 1
                    logger.warning(f"Error getting response from LLM (attempt {retry_count}/{max_retries}): {e}")
                    if retry_count < max_retries:
                        # 指数退避，每次重试间隔翻倍
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        # 进一步清理消息历史
                        self._manage_message_history()
                        continue
                    # 所有重试失败，返回简单响应
                    # 检查是否是空响应错误
                    if "Empty or invalid response" in str(e) or "empty content" in str(e).lower():
                        # 对于空响应，提供一个默认的工具调用
                        if len(self.messages) > 0 and hasattr(self.messages[-1], 'content') and \
                           "list" in self.messages[-1].content.lower() and "file" in self.messages[-1].content.lower():
                            # 如果用户请求列出文件，默认使用 bash 工具
                            logger.info("检测到用户请求列出文件，提供默认的 bash 工具调用")
                            response_text = "<tool>\nname: bash\nargs: {\n  \"command\": \"ls -la\"\n}\n</tool>"
                        else:
                            # 其他情况下提供一个默认响应
                            response_text = "I'm having trouble processing that. Let me try a simpler approach."
                except Exception as e:
                    logger.error(f"Unexpected error from LLM: {e}")
                    response_text = "I encountered an error. Let's try again with a simpler approach."
                    break
        except Exception as e:
            logger.error(f"Critical error in think method: {e}")
            response_text = "I'm experiencing technical difficulties. Please try a simpler request."

        # Extract tool calls from the text response
        extracted_tool_calls = self._extract_tool_calls(response_text)
        self.tool_calls = extracted_tool_calls

        # 检测任务是否已完成
        if self._is_task_completed(response_text):
            logger.info("任务已完成，结束交互")
            self.state = AgentState.FINISHED
            return False  # 返回 False 停止循环

        # Log the extracted tool calls
        if extracted_tool_calls:
            logger.info(
                f"🛠️ {self.name} extracted {len(extracted_tool_calls)} tools to use"
            )
            logger.info(
                f"🧰 Tools being prepared: {[call.function.name for call in extracted_tool_calls]}"
            )
            for call in extracted_tool_calls:
                logger.info(f"🔧 Tool arguments: {call.function.arguments}")

        # Create and add assistant message
        assistant_msg = (
            Message.from_tool_calls(content=response_text, tool_calls=self.tool_calls)
            if self.tool_calls
            else Message.assistant_message(content=response_text)
        )
        self.memory.add_message(assistant_msg)

        # Return True if we have tool calls or content
        return bool(self.tool_calls or response_text)

    def _manage_message_history(self) -> None:
        """Manage message history to prevent context overflow.
        Keeps only the most recent messages up to max_history_messages.
        """
        # 改进的消息历史管理，保留错误信息
        if len(self.messages) > self.max_history_messages:
            # 分类消息
            user_messages = [msg for msg in self.messages if msg.role == "user"]
            system_messages = [msg for msg in self.messages if msg.role == "system"]
            tool_messages = [msg for msg in self.messages if msg.role == "tool"]
            assistant_messages = [msg for msg in self.messages if msg.role == "assistant"]

            # 找出包含错误的工具消息
            error_messages = [msg for msg in tool_messages if "Error executing tool" in msg.content]

            # 保留所有系统消息
            latest_system_msg = system_messages if system_messages else []

            # 保留最新的用户消息（最多 8 条）
            latest_user_msgs = user_messages[-8:] if user_messages else []

            # 保留最新的助手消息（最多 8 条）
            latest_assistant_msgs = assistant_messages[-8:] if assistant_messages else []

            # 保留所有工具调用错误消息
            error_messages = [msg for msg in tool_messages if "Error executing tool" in msg.content]

            # 保留最新的成功工具调用结果（最多 8 条）
            success_tool_msgs = [msg for msg in tool_messages if "Error executing tool" not in msg.content]
            latest_success_tool_msgs = success_tool_msgs[-8:] if success_tool_msgs else []

            # 计算删除的消息数量
            old_count = len(self.messages)

            # 组合保留的消息，保持顺序
            kept_messages = []
            for msg in self.messages:
                if (msg in latest_system_msg or msg in latest_user_msgs or
                    msg in error_messages or msg in latest_assistant_msgs or
                    msg in latest_success_tool_msgs):
                    kept_messages.append(msg)

            self.messages = kept_messages
            removed = old_count - len(self.messages)

            if removed > 0:
                logger.info(f"增强的历史管理: 从历史中删除了 {removed} 条消息，保留所有系统消息、最新用户消息、最新助手消息、所有错误信息和最新成功工具调用")

    def _extract_tool_calls(self, text: str) -> List[ToolCall]:
        """Extract tool calls from text using regex pattern matching."""
        tool_calls = []

        # Find all tool call patterns in the text
        matches = self.tool_pattern.findall(text)

        for i, (tool_name, args_str) in enumerate(matches):
            try:
                # Clean up the tool name and args
                tool_name = tool_name.strip()
                args_str = args_str.strip()

                # Parse the JSON arguments
                try:
                    args = json.loads(args_str)
                except json.JSONDecodeError:
                    # Try to fix common JSON formatting issues
                    fixed_args_str = self._fix_json_string(args_str)
                    args = json.loads(fixed_args_str)

                # Create a ToolCall object
                function = Function(name=tool_name, arguments=json.dumps(args))
                tool_call = ToolCall(
                    id=f"call_{uuid.uuid4()}",
                    type="function",
                    function=function
                )
                tool_calls.append(tool_call)

            except Exception as e:
                logger.error(f"Error parsing tool call: {e}")
                # Continue with other tool calls even if one fails
                continue

        return tool_calls

    def _fix_json_string(self, json_str: str) -> str:
        """Attempt to fix common JSON formatting issues."""
        # Replace single quotes with double quotes
        json_str = re.sub(r"'([^']*)'", r'"\1"', json_str)

        # Add quotes around unquoted keys
        json_str = re.sub(r'([{,])\s*([a-zA-Z0-9_]+)\s*:', r'\1"\2":', json_str)

        # Remove trailing commas
        json_str = re.sub(r',\s*}', '}', json_str)

        return json_str

    def _generate_simplified_tool_descriptions(self) -> str:
        """Generate simplified tool descriptions for the system prompt."""
        descriptions = []

        for tool_name, tool in self.mcp_clients.tool_map.items():
            # Get the tool schema
            schema = self.tool_schemas.get(tool_name, {})

            # 简化参数描述
            params = []
            if schema and "properties" in schema:
                for param_name, param_info in schema.get("properties", {}).items():
                    required = "*" if param_name in schema.get("required", []) else ""
                    params.append(f"{param_name}{required}")

            # 添加简化的工具描述
            param_str = ", ".join(params)
            descriptions.append(f"{tool_name}({param_str}) - {tool.description}")

        return "\n".join(descriptions)

    def _generate_tool_descriptions(self) -> str:
        """Generate formatted tool descriptions for the system prompt."""
        descriptions = []

        for tool_name, tool in self.mcp_clients.tool_map.items():
            # Get the tool schema
            schema = self.tool_schemas.get(tool_name, {})

            # Format the parameters
            params_desc = ""
            if schema and "properties" in schema:
                params_desc = "Parameters:\n"
                for param_name, param_info in schema.get("properties", {}).items():
                    required = "required" if param_name in schema.get("required", []) else "optional"
                    param_type = param_info.get("type", "any")
                    param_desc = param_info.get("description", "")
                    params_desc += f"  - {param_name} ({param_type}, {required}): {param_desc}\n"

            # Add the tool description
            descriptions.append(
                f"Tool: {tool_name}\n"
                f"Description: {tool.description}\n"
                f"{params_desc}"
            )

        return "\n\n".join(descriptions)
    def _is_task_completed(self, response_text: str) -> bool:
        """检测任务是否已完成

        检测标准：
        1. 响应中包含表示完成的关键词
        2. 连续两次响应没有工具调用
        3. 当前消息数量超过阈值且最近没有工具调用
        """
        # 检查是否包含表示完成的关键词
        completion_keywords = [
            "任务完成", "已完成", "完成了", "已经完成",
            "task completed", "completed the task", "finished", "done",
            "I've completed", "I have completed", "that's all", "that is all"
        ]

        for keyword in completion_keywords:
            if keyword.lower() in response_text.lower():
                logger.info(f"检测到任务完成关键词: '{keyword}'")
                return True

        # 检查工具调用后的响应
        if len(self.messages) >= 3:
            # 获取所有消息
            all_messages = self.messages

            # 检查是否有工具调用结果消息
            tool_result_messages = [msg for msg in all_messages if msg.role == "tool"]

            # 如果有工具调用结果，并且当前没有新的工具调用
            if tool_result_messages and not self.tool_calls:
                # 获取最后一条工具结果消息和当前助手消息
                last_tool_result = tool_result_messages[-1]
                assistant_messages = [msg for msg in all_messages if msg.role == "assistant"]

                # 如果工具调用后有助手消息，且没有新的工具调用
                if assistant_messages and assistant_messages[-1].content and not getattr(assistant_messages[-1], "tool_calls", None):
                    # 如果助手消息内容超过 50 个字符，认为是总结性回复
                    if len(assistant_messages[-1].content) > 50:
                        logger.info("工具调用后有详细回复且没有新的工具调用，认为任务已完成")
                        return True

        # 检查是否连续两次没有工具调用
        if not self.tool_calls and len(self.messages) >= 4:
            # 获取最后两条助手消息
            assistant_messages = [msg for msg in self.messages if msg.role == "assistant"]

            if len(assistant_messages) >= 2:
                # 检查最后两条助手消息是否都没有工具调用
                last_two = assistant_messages[-2:]
                no_tools = all(not getattr(msg, "tool_calls", None) for msg in last_two)

                if no_tools:
                    logger.info("连续两次响应没有工具调用，认为任务已完成")
                    return True

        # 检查是否有工具调用错误
        tool_messages = [msg for msg in self.messages if msg.role == "tool"]
        error_messages = [msg for msg in tool_messages if "Error executing tool" in msg.content]

        # 如果连续出现相同的工具调用错误，认为任务已完成
        if len(error_messages) >= 2:
            # 检查最近两次错误是否相同
            if error_messages[-1].content == error_messages[-2].content:
                logger.info("连续出现相同的工具调用错误，认为任务已完成")
                return True

        # 检查是否有空响应
        assistant_messages = [msg for msg in self.messages if msg.role == "assistant"]
        empty_responses = [msg for msg in assistant_messages if hasattr(msg, 'content') and
                          (not msg.content or msg.content.strip() == "" or
                           "having trouble" in msg.content or
                           "encountered an error" in msg.content)]

        # 如果有连续的空响应，认为任务已完成
        if len(empty_responses) >= 2:
            logger.info("检测到连续的空响应或错误响应，认为任务已完成")
            return True

        # 检查消息数量是否超过阈值
        if len(self.messages) > 6:
            # 如果消息数量超过阈值且当前没有工具调用
            if not self.tool_calls:
                logger.info("消息数量超过阈值且当前没有工具调用，认为任务已完成")
                return True

        # 检查步骤数是否超过阈值
        if self.current_step > 5:
            # 如果步骤数超过 5 且没有有效的工具调用结果，认为任务已完成
            successful_tool_results = [msg for msg in tool_messages if "Error executing tool" not in msg.content]
            if not successful_tool_results:
                logger.info("步骤数超过阈值且没有有效的工具调用结果，认为任务已完成")
                return True

        return False
