# Learn-OpenClaw-main 源码逐行讲解

> 基于你上传的 `all_code.txt`（30 个文件，2408 行）。
> 中文注释在 PowerShell 导出时编码损坏了，我已还原其内容。

## 推荐阅读顺序

按这条路线读，可以只用最少的代码量建立完整认知：

```
① core/llm.py                    LLM 怎么调
② core/node.py                   编排引擎（已讲过，回顾即可）
③ examples/workflow/main.py      最简流程
④ examples/chatbot/main.py       + 循环
⑤ examples/chatbot_with_tools/   + 工具 ← 真正的 Agent，核心
⑥ tools/builtins/tool_def.py     Tool 的本质
⑦ tools/executor.py              怎么执行工具、怎么回灌结果
⑧ tools/builtins/ls.py、bash.py   单个工具怎么写（含防御措施）
⑨ tools/skill_loader.py          Skill（半成品）
⑩ tools/mcp/                     MCP（半成品）
```

跳过不看：`tools/skills/pdf/scripts/` 下 8 个脚本是 PDF 处理的参考资料，
和 Agent 框架本身无关，是 Skill 的示例内容。

---

## ① core/llm.py —— 整个项目只有 69 行

### `call_llm_simple(prompt)` —— 第 10–22 行

```python
def call_llm_simple(prompt: str) -> str:
    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),   # 从环境变量读
        base_url=os.environ.get("OPENAI_BASE_URL"),
    )
    response = client.chat.completions.create(
        model="kimi-k2.6",
        messages=[{"role": "user", "content": prompt}],
    )
    message = response.choices[0].message
    return message.content or ""
```

最简单的一问一答。**每次调用都新建一个 client**——小规模没事，
高并发场景应该复用（这是个小小的浪费）。

`return message.content or ""` 的 `or ""`：模型有时会返回 `None`，
这里兜底成空字符串，避免下游拿到 `None` 报错。

### `call_llm(messages, tools, system_prompt)` —— 第 25–64 行

这是**带工具支持**的版本，也是 Agent 真正用的：

```python
msgs = list(messages)                      # 拷贝，避免污染外部列表
if system_prompt:
    msgs = [{"role": "system", "content": system_prompt}, *msgs]   # 系统提示插到最前

kwargs = {"model": "kimi-k2.6", "messages": msgs}
if tools:
    kwargs["tools"] = tools                # 工具说明书塞进去
    kwargs["tool_choice"] = "auto"         # 让模型自己决定要不要用
```

第 57–62 行处理返回值：

```python
result = {"role": "assistant", "content": message.content or ""}

reasoning_content = getattr(message, "reasoning_content", None)   # Kimi 的思维链
if reasoning_content:
    result["reasoning_content"] = reasoning_content

if message.tool_calls:                     # ★ 模型要求调工具
    result["tool_calls"] = [tc.model_dump() for tc in message.tool_calls]
```

**这一句是 Agent 的开关**：如果有 `tool_calls`，上层就知道要执行工具；没有就是普通回复。

### 第 67–68 行

```python
if __name__ == "__main__":
    print("Basic:", call_llm_simple("一加一等于几"))
```

你最早跑的 `python core/llm.py` 就是跑这里。

---

## ② core/node.py —— 回顾

56 行的编排引擎，说过的核心三点：

- `successors` 字典 = 通讯录，记录「我之后找谁」
- `>>` 连线、`- "名字"` 给连线打标签
- `Flow.run()` 用 action 查通讯录决定下一步

```python
while curr:
    last_action, payload = curr._exec(payload)
    curr = curr.successors.get(last_action)   # 查不到 → None → 结束
```

---

## ③ examples/workflow/main.py —— 一条直线

三个节点，三段式：

```python
class QueryNode(Node):
    def exec(self, payload):
        return "search", str(payload)          # 原样传下去

class SearchNode(Node):
    def exec(self, payload):
        results = search_ddgs(str(payload), max_results=3)   # 调 DuckDuckGo
        titles = [r.get("title") or r.get("body") or "" for r in results]
        summary_input = " | ".join([t for t in titles if t])
        return "summarize", summary_input      # 拼成一串标题字符串

class SummarizeNode(Node):
    def exec(self, payload):
        prompt = f"基于以下要点写一句话摘要：{payload}"
        text = call_llm_simple(prompt)
        return "default", text                 # default 没注册后继 → 结束
```

连线（第 387–388 行）：

```python
query - "search"    >> search
search - "summarize" >> summarize
```

注意第 391 行：

```python
_, result = flow.run("asyncio python best practices")
```

**payload 是从外部传进去的**，不是 `input()`。这就是 `workflow` 和 `chatbot` 的本质差别——
前者是一次性的批处理，后者是交互式循环。

> 这也解释了为什么你跑 workflow 时看到输出是 `asyncio python best practices` 的搜索摘要，
> 而不是等你输入。

---

## ④ examples/chatbot/main.py —— 加个 while

### 第 138 行：路径技巧

```python
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
```

`main.py` 在 `examples/chatbot/` 下，往上三级才是项目根。
把这行加进 `sys.path`，才能写 `from core.llm import call_llm`。

**三种运行方式的区别就在这**：

| 命令 | 为什么能/不能工作 |
|---|---|
| `python -m examples.chatbot.main`（根目录） | ✅ Python 自动把根目录加入 path |
| `python main.py`（chatbot 目录内） | ⚠️ 靠上面那行手动补 path 才能工作 |
| `uv run chatbot` | ❌ 命令没安装（缺 build-system） |

### 第 149–153 行：ChatNode

```python
def exec(self, payload):
    messages = shared["messages"]                     # 从全局字典拿历史
    assistant_message = call_llm(messages=messages, system_prompt=SYSTEM_PROMPT)
    messages.append(assistant_message)                # 把回复写回历史
    return "output", assistant_message
```

**这里是记忆的实现**：`messages` 列表每轮都在增长，
下次调 LLM 时整个历史一起发过去，模型才「记得」你前面说了什么。

### 第 181 行：连线

```python
chat - "output" >> output
```

只有一条边：**ChatNode 永远走 output**，没有分支。

### 第 183–199 行：外层循环

```python
while True:
    user_input = input("👤 You: ").strip()
    if user_input.lower() in ("quit", "exit", "q"):
        break
    shared["messages"].append({"role": "user", "content": user_input})
    flow = Flow(chat)
    flow.run(None)                    # ★ 每轮都新建一个 Flow
```

注意 `flow.run()` 在 while **里面**：每问一句，跑一次完整的 Flow。

### 第 173–174 行：初始化

```python
shared.clear()                    # 清空
shared["messages"] = []           # 历史从空开始
```

`shared` 是模块级全局字典（定义在 `core/node.py` 第 4 行），
每次启动要手动清空，否则上一次的数据会残留——**这是全局变量模式的典型代价**。

### 第 202–205 行：前置检查

```python
def main():
    if not os.environ.get("OPENAI_API_KEY") or not os.environ.get("OPENAI_BASE_URL"):
        print("⚠️ 提示：请先设置环境变量 OPENAI_API_KEY 和 OPENAI_BASE_URL")
        return
```

友好的报错提示，比让程序崩在 `OpenAIError` 上好得多。

---

## ⑤ examples/chatbot_with_tools/ —— ★ 真正的 Agent

这是整个教程的终点，也是最值得细读的文件。

### 第 231–235 行：系统提示词变了

```python
SYSTEM_PROMPT = (
    "你是一个会调用工具的助手。"
    "当问题涉及最新信息、模型版本、产品发布时间或事实核验时，优先先调用 search 工具，再基于搜索结果回答。"
    "如果问题是本地文件/代码相关，优先使用 read/grep/find/ls 等本地工具。"
)
```

**用自然语言告诉模型"什么时候该用什么工具"**。
这看起来不起眼，但工程上极其重要——工具选择很大程度靠这段提示词引导。

### 第 241–251 行：ChatNode 有了分支

```python
assistant_message = call_llm(messages=messages, tools=tools, system_prompt=SYSTEM_PROMPT)
messages.append(assistant_message)

if assistant_message.get("tool_calls"):
    return "tool_call", assistant_message     # ★ 走工具节点
return "output", assistant_message           # 直接回答
```

**这就是节点的"决策"**：看 LLM 返回里有没有 `tool_calls`，决定走哪条边。

### 第 257–270 行：ToolCallNode

```python
tool_calls = executor.parse_tool_calls(response)    # 解析出要调什么
results = executor.execute_all(tool_calls)          # 执行

for tc, result in zip(tool_calls, results):
    print(f"  [Tool] 执行: {tc.name}({tc.arguments})")     # ← 你截图里看到的那行
    print(f"  [Tool] 结果: {result.content[:100]}...")
    messages.append(result.to_message())            # ★ 结果写回历史

return "chat", None                                 # ★ 回到 ChatNode
```

最后一行是整个 Loop 的灵魂：**回到 ChatNode，让 LLM 拿着工具结果再想一次**。

### 第 300–302 行：三条边构成循环

```python
chat - "tool_call" >> tool_call
tool_call - "chat"  >> chat        # ★ 回边！循环靠这条
chat - "output"     >> output
```

画成图：

```
        ┌──────────────────────┐
        │                      │  "chat"（回边）
        ▼                      │
    [ChatNode] ──"tool_call"──► [ToolCallNode]
        │
        │ "output"
        ▼
    [OutputNode] ──► 结束
```

**外层 `while True` 是等你打字的；内层的工具循环是图自己转的。**
所以你问"有哪些文件夹"时，它能在你一次提问里完成「想→做→再想→答」的全过程。

### 第 293–294 行：初始化多了两样

```python
shared["tools"] = [t.to_llm_format() for t in get_tools()]   # 工具说明书列表
shared["tool_executor"] = ToolExecutor()                      # 执行器
```

---

## ⑥ tools/builtins/tool_def.py —— Tool 的本质

### `Tool` 类（第 1449–1477 行）

只有 4 个字段：`name`、`description`、`parameters`、`fn`。

```python
def to_llm_format(self) -> dict:
    return {
        "type": "function",
        "function": {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,   # JSON Schema
        },
    }
```

**给模型看的就是这一段 JSON。** 没有魔法，就是一份说明书：
工具叫什么、干什么用、要哪些参数、哪些必填。

```python
def execute(self, **kwargs):
    return self.fn(**kwargs)        # 直接把参数丢给绑定的函数
```

### 8 个内置工具（第 1481–1598 行）

| 工具 | 必填参数 | 说明 |
|---|---|---|
| `read` | `path` | 支持 `offset`/`limit` 分页读大文件 |
| `write` | `path`, `content` | 自动创建父目录 |
| `edit` | `path`, `old_text`, `new_text` | 精确文本替换 |
| `bash` | `command` | 可选 `timeout`，输出截断到 2000 行 / 30KB |
| `grep` | `pattern` | 可选 `path`/`glob`，正则搜索 |
| `find` | `pattern` | glob 匹配找文件 |
| `ls` | 无 | 目录后加 `/` 区分，`ls` 的 `path` 是可选的 |
| `search` | `query` | DuckDuckGo 联网搜索 |

仔细体会每个 `description` 的写法——**它们决定了模型会不会用、会不会用错**。
比如 `edit` 强调 "old_text must match exactly"，是在提前防止模型瞎猜。

---

## ⑦ tools/executor.py —— 解析与执行

### `ToolCall`（第 421–442 行）

```python
@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    @classmethod
    def from_openai_item(cls, item):
        function = item.get("function", {})
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            arguments = _safe_json_loads(arguments)     # ★ JSON 字符串 → dict
        ...
```

`@dataclass(slots=True)`：`slots` 省内存、属性访问更快。`slots=True` 是 Python 3.10+ 特性。

`arguments` 为啥可能是字符串？因为 **LLM 返回的 JSON 是在字符串里**，需要解析。
（有的 API 直接返回 dict，所以代码两种都兼容。）

### `ToolResult.to_message()`（第 453–459 行）

```python
return {
    "role": "tool",
    "tool_call_id": self.tool_call_id,
    "content": self.content,
}
```

**这 5 行就是「结果回灌」**。生成一条 tool 消息塞进 `messages`，
下一轮 LLM 就能看到工具返回了什么。

### `ToolExecutor`（第 462–509 行）

```python
def __init__(self):
    self.tools = get_builtin_tools()
    self.tool_map = {tool.name: tool for tool in self.tools}   # 名字 → 工具对象
```

用**字典**做查找表，O(1) 找到工具。比遍历列表快。

```python
def execute(self, tool_call):
    tool = self.tool_map.get(tool_call.name)
    if not tool:
        return ToolResult(..., content=f"Tool '{name}' not found", is_error=True)
    try:
        raw_result = tool.execute(**tool_call.arguments)
    except Exception as exc:
        return ToolResult(..., content=f"Error: {exc}", is_error=True)
```

**两处防御**：

1. 工具不存在 → 返回错误消息，**而不是抛异常**
2. 工具执行出错 → 捕获异常，转成错误消息

为什么必须这样？因为错误要**告诉模型**（让它换个方法），而不是让程序崩溃。
这是 Agent 工程的关键思维。

```python
def execute_all(self, tool_calls):
    return [self.execute(tc) for tc in tool_calls]     # 串行，不是并发
```

> 注意：这里是**串行执行**。多个工具之间无依赖时，并行能显著提速（XingClaw
> 就支持 `parallel` / `sequential` 两种模式）。这是个可优化点。

### `demo()`（第 527–580 行）

手动构造一个假的 LLM 回复，演示完整流程。**这是很好的学习材料**：

```powershell
uv run python -m tools.executor
```

会打印：LLM 返回什么 → 怎么解析 → 怎么执行 → 怎么转 message。

---

## ⑧ 工具实现：以 ls 和 bash 为例

### `ls.py`（第 1262–1319 行）

```python
if cwd:
    dir_path = Path(cwd) / (path or ".")
else:
    dir_path = Path(path or ".")
dir_path = dir_path.resolve()          # 转成绝对路径

if not dir_path.exists():
    raise FileNotFoundError(...)       # 提前报错，给清晰信息
if not dir_path.is_dir():
    raise ValueError(...)
```

**先校验再干活**，异常信息还带上具体路径——模型拿到报错就知道该怎么调整。

```python
entries.sort(key=lambda e: e.name.lower())    # 排序，保证输出稳定
for entry in entries:
    suffix = "/" if entry.is_dir() else ""    # 目录加 / 后缀
    results.append(entry.name + suffix)
```

`/` 后缀是给模型看的视觉线索：**让它能一眼分清哪个是目录哪个是文件**。这种细节很见功力。

```python
if len(output.encode("utf-8")) > DEFAULT_MAX_BYTES:
    output = output_bytes[:DEFAULT_MAX_BYTES].decode("utf-8", errors="ignore")
    output += f"\n\n[30KB limit reached]"
```

**截断！** 工具输出要塞进 LLM 上下文，不限制就爆 token。

`errors="ignore"` 是因为强行截断可能切到半个 UTF-8 字符，忽略解码错误比崩溃好。

### `bash.py`（第 733–793 行）

```python
result = subprocess.run(
    command, shell=True, cwd=work_dir,
    capture_output=True, text=True, timeout=timeout,
)
output = result.stdout
if result.stderr:
    output += "\n" + result.stderr if output else result.stderr
```

stdout 和 stderr 合并返回——模型需要看到完整信息才能判断命令是否成功。

返回 dict 而不是裸字符串：

```python
return {"stdout": output, "stderr": ..., "exit_code": result.returncode}
```

`exit_code` 让模型知道命令**是不是真的成功了**（有些命令失败但照样输出内容）。

超时处理：

```python
except subprocess.TimeoutExpired:
    return {"stdout": "", "stderr": f"Command timed out after {timeout}s", "exit_code": -1}
```

⚠️ **但这个项目没有任何危险命令拦截**。`shell=True` + 无黑名单意味着
`rm -rf` 之类会直接执行。正经实现（XingClaw、Claude Code）都有：
危险命令黑名单、白名单模式、用户二次确认。你自己测试时务必小心。

---

## ⑨ tools/skill_loader.py —— 半成品

```python
def load(path: str) -> tuple[dict, str]:
    content = f.read()
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)        # 切出 YAML frontmatter
    metadata = yaml.safe_load(parts[1])    # 解析 metadata
    body = parts[2].strip()                # 正文
    return metadata or {}, body
```

只做了一件事：**解析 SKILL.md 的 YAML 头**。

但我们说过 Skill 的核心是「渐进式加载」——先只给模型看 metadata（名字+简介），
它说要用才加载 body。**这个逻辑在这里完全没有实现**，而且 `skill_loader` 
没有被任何地方 import。

所以这个项目里 **Skill 是未完成状态**，只是预留了文件结构。

`tools/skills/pdf/` 下的 8 个脚本是 Skill 的**内容示例**（处理 PDF 表单），
和框架本身无关，初学可以跳过。

---

## ⑩ tools/mcp/ —— 同样是半成品

### `server.py`（第 1770–1803 行）

```python
mcp = FastMCP("agent-tools")

@mcp.tool()
def search(query: str, max_results: int = 5) -> list[dict]:
    """使用 DuckDuckGo 搜索网页"""
    from tools.builtins.search import search as search_impl
    return search_impl(query, max_results)

@mcp.tool()
def add(a: float, b: float) -> float:
    return a + b
```

用装饰器把普通函数变成 MCP 工具——**这就是「远程 Tool」**。
函数签名和 docstring 会自动变成工具说明书。

### `client.py`（第 1688–1767 行）

```python
async def connect_stdio(self, command, args=None):
    server_params = StdioServerParameters(command=command, args=args or [], env=None)
    async with stdio_client(server_params) as (read, write):
        self.session = ClientSession(read, write)
        await self.session.initialize()
        tools_result = await self.session.list_tools()
        self.tools = [tool.dict() for tool in tools_result.tools]
```

🐛 **这里有 bug**：`async with` 块结束后连接就关闭了，
但 `self.session` 还留着引用。之后再调 `list_tools()` / `call_tool()` 会失败。
正确做法是把 `stdio_client` 的上下文生命周期提到类级别管理。

而且和 Skill 一样，**MCP 也没有被接入主循环**——`chatbot_with_tools` 
只用 builtin tools，MCP 工具没有出现在 `get_tools()` 里。

---

## 完整数据流（一次工具调用）

以你问「列出当前目录」为例，串起所有代码：

```
① 你在终端输入
   ↓
② main.py while 循环：shared["messages"].append({"role":"user", ...})
   ↓
③ Flow(chat).run() 进入 ChatNode.exec()
   ↓
④ core/llm.py call_llm()：把 8 个工具的 JSON Schema + 历史消息发给 Kimi
   ↓
⑤ Kimi 返回 {"tool_calls": [{"function":{"name":"ls","arguments":'{"path":"."}'}}]}
   ↓
⑥ ChatNode 看到 tool_calls → return "tool_call"
   ↓
⑦ Flow 查 successors["tool_call"] → 跳到 ToolCallNode
   ↓
⑧ executor.parse_tool_calls()：JSON 字符串 → ToolCall 对象
   ↓
⑨ executor.execute()：tool_map["ls"] 找到函数 → ls(path=".")
   ↓
⑩ ls 读磁盘，返回带 / 后缀的字符串，超过 30KB 会截断
   ↓
⑪ ToolResult.to_message() 生成 {"role":"tool", "content":"..."}
   ↓
⑫ messages.append(这条结果)  ← ★ 回灌完成
   ↓
⑬ ToolCallNode return "chat" → Flow 跳回 ChatNode（回边起作用了）
   ↓
⑭ 再次 call_llm()，这次历史里多了工具结果
   ↓
⑮ Kimi 基于真实结果生成回答，没有 tool_calls
   ↓
⑯ ChatNode return "output" → OutputNode 打印 → 结束
```

**整个链路 16 步，核心代码不到 300 行。** 这就是教程想告诉你的：Agent 不复杂。

---

## 我发现的问题清单

不是你的锅，是项目本身的粗糙之处：

| 问题 | 位置 | 影响 |
|---|---|---|
| 4 个注册命令全废 | `pyproject.toml` 缺 `build-system` | `uv run workflow` 报错 |
| 注册了不存在的文件 | 同上，`node_examples`/`search_agent` | 按文档跑必失败 |
| **工具循环无上限** | `chatbot_with_tools` 图结构 | LLM 若反复调工具会无限烧钱 |
| bash 无危险命令拦截 | `builtins/bash.py` | 可能误删文件，测试需小心 |
| 工具串行执行 | `executor.execute_all` | 多工具场景偏慢 |
| 每次调用新建 client | `core/llm.py` | 重复开销 |
| Skill 未实现加载逻辑 | `skill_loader.py` 无人调用 | 只有文件结构，功能缺失 |
| MCP client 生命周期 bug | `mcp/client.py` `async with` | 连接会提前关闭 |
| `chat_with_tools` 是假的 | `tools/__init__.py` | 关键字匹配假装调 LLM，别当真 |
| 依赖缺 chromadb | `pyproject.toml` | 教程的 RAG 章节跑不起来 |

---

## 学习清单

读完这些代码，你应该能回答：

- [ ] Tool 到底是什么？（答：一段 JSON Schema + 一个 Python 函数）
- [ ] 模型怎么知道要调工具？（答：看 `description`，由你写的系统提示词引导）
- [ ] Agent Loop 是 while 写的吗？（答：这个项目里是**图的回边**）
- [ ] 工具执行出错怎么办？（答：转成错误消息告诉模型，让它自己想办法）
- [ ] 为什么工具输出要截断？（答：防止撑爆上下文 token）
- [ ] 模型为什么能"记住"前文？（答：`messages` 列表每轮都在增长）

答得上来这 6 个，教程第二节你就真的懂了。
