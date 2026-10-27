"""
XXXClaw Agent 评测脚本

量化 Agent 四个指标：任务成功率、平均工具调用轮次、token 消耗、端到端延迟。
用途：给简历上的 "XX%" 提供真数字，而不是拍脑袋。

用法（在项目根目录执行）：
    python eval/bench.py --mock              # 零成本：工具真实干活，LLM 决策走预设脚本
    python eval/bench.py                     # 真实调用 Kimi（会花几分钱）
    python eval/bench.py --case 7 11         # 只跑第 7、11 个用例
    python eval/bench.py --verbose           # 打印每一轮的工具入参和返回
    python eval/bench.py --compare           # 跑完自动和上次 results.json 对比

两种模式的区别，先说清楚：
    --mock  : LLM 的"决策"是脚本写死的，工具却是真的在读你磁盘。
              所以它量出来的是【工具链路可用率】——
              "解析 tool_call → 执行真实工具 → 结果回灌 → 再推理"这条闭环通不通。
              不能拿它当模型能力写进简历。
    真实模式: 决策和执行都是真的，量出来的才是【端到端任务成功率】。
              简历上要写的数字，必须来自这个模式。

结果写入 eval/results.json，方便你改完代码后跨版本对比。

依赖：openai、项目本身的 tools 包（内含 ddgs，缺了就 uv add ddgs）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # 用例里全是相对路径，强制落到项目根目录，避免换个地方跑结果就变

from tools.executor import ToolExecutor  # noqa: E402


def load_tools():
    """
    兼容两种 import 路径：
      1) tools/__init__.py 导出了 get_tools        → 用它（可能含 MCP 扩展工具）
      2) 只导出了 tools.builtins.get_builtin_tools → 退回到内置 8 个
    这样你那份仓库和我这份有一点差异也不会一上来就 ImportError。
    """
    try:
        from tools import get_tools  # type: ignore

        got = get_tools()
        if got:
            return got
    except Exception:
        pass

    try:
        from tools.builtins import get_builtin_tools  # type: ignore

        return get_builtin_tools()
    except ImportError as exc:
        print(f"无法加载工具：{exc}\n"
              "大概率是 ddgs 没装（tools/builtins/search.py 顶层 import 了它）。\n"
              "先执行一次： uv add ddgs")
        sys.exit(1)

SYSTEM_PROMPT = (
    "你是一个会调用工具的助手。"
    "涉及本地文件/代码的问题，优先使用 read/grep/find/ls 等本地工具，"
    "拿到工具结果后再回答，不要凭空猜测。"
    "工具报错时如实转述错误信息，不要编造内容。"
)

MAX_TURNS = 10         # 硬上限：防止 LLM 卡进死循环烧钱
MAX_TOOL_TURNS = 6     # 单个任务最多允许多少轮工具调用

# 跨平台：Windows 没有 wc/tail，换成 PowerShell
IS_WIN = os.name == "nt"
LINE_COUNT_CMD = (
    'powershell -Command "(Get-ChildItem tools\\builtins\\*.py | '
    'ForEach-Object { (Get-Content $_.FullName).Count }) | Measure-Object -Sum).Sum"'
    if IS_WIN
    else "wc -l tools/builtins/*.py | tail -1"
)


# ---------------------------------------------------------------------------
# 测试用例
#   expect_tools      : 必须调用到的工具（考查路由能力）
#   expect_in_answer  : 最终回答必须包含的关键词，小写比对（考查是否真的读了工具结果）
#   mock_tool_calls   : mock 模式下依次发起的工具调用，用来验证多步闭环
# ---------------------------------------------------------------------------
@dataclass
class Case:
    name: str
    prompt: str
    expect_tools: list[str] = field(default_factory=list)
    expect_in_answer: list[str] = field(default_factory=list)
    mock_tool_calls: list[dict] = field(default_factory=list)


CASES: list[Case] = [
    Case(
        name="列出指定目录内容",
        prompt="请列出 tools/builtins 目录下有哪些文件",
        expect_tools=["ls"],
        expect_in_answer=["tool_def.py"],
        mock_tool_calls=[{"name": "ls", "arguments": {"path": "tools/builtins"}}],
    ),
    Case(
        name="读取文件指定行",
        prompt="读取 tools/builtins/ls.py 的前 15 行",
        expect_tools=["read"],
        expect_in_answer=["def ls"],
        mock_tool_calls=[{"name": "read", "arguments": {"path": "tools/builtins/ls.py", "limit": 15}}],
    ),
    Case(
        name="搜索代码内容",
        prompt="在 core 目录里搜索 'Node' 出现在哪些文件的哪些行",
        expect_tools=["grep"],
        expect_in_answer=["node"],
        mock_tool_calls=[{"name": "grep", "arguments": {"pattern": "Node", "path": "core", "glob": "*.py"}}],
    ),
    Case(
        name="按模式查找文件",
        prompt="找出 examples 目录下所有的 py 文件",
        expect_tools=["find"],
        expect_in_answer=["main.py"],
        mock_tool_calls=[{"name": "find", "arguments": {"pattern": "*.py", "path": "examples"}}],
    ),
    Case(
        name="执行命令拿到版本号",
        prompt="用命令行查看当前 Python 版本号",
        expect_tools=["bash"],
        expect_in_answer=["python", "3."],
        mock_tool_calls=[{"name": "bash", "arguments": {"command": "python --version"}}],
    ),
    Case(
        name="两步任务：写入再读回验证",
        prompt="往 eval/_tmp_test.txt 写入 'agent-eval-42'，然后读回来确认写成功了吗",
        expect_tools=["write", "read"],
        expect_in_answer=["agent-eval-42"],
        mock_tool_calls=[
            {"name": "write", "arguments": {"path": "eval/_tmp_test.txt", "content": "agent-eval-42"}},
            {"name": "read", "arguments": {"path": "eval/_tmp_test.txt"}},
        ],
    ),
    Case(
        name="两步任务：先找再读",
        prompt="先找出 tools 目录下的所有 Python 文件，再看 tool_def.py 里到底注册了哪些工具",
        expect_tools=["find", "read"],
        expect_in_answer=["read_file"],
        mock_tool_calls=[
            {"name": "find", "arguments": {"pattern": "*.py", "path": "tools"}},
            {"name": "read", "arguments": {"path": "tools/builtins/tool_def.py"}},
        ],
    ),
    Case(
        name="统计项目代码行数",
        prompt="统计 tools/builtins 目录下所有 py 文件一共有多少行",
        expect_tools=["bash"],
        expect_in_answer=[],
        mock_tool_calls=[{"name": "bash", "arguments": {"command": LINE_COUNT_CMD}}],
    ),
    Case(
        name="读取不存在的文件（容错：不许编造）",
        prompt="读取 definitely_not_exists_123.txt 这个文件的内容",
        expect_tools=["read"],
        expect_in_answer=["not found"],
        mock_tool_calls=[{"name": "read", "arguments": {"path": "definitely_not_exists_123.txt"}}],
    ),
    Case(
        name="查看当前工作目录",
        prompt="我现在在哪个目录？列出当前目录的内容",
        expect_tools=["ls"],
        expect_in_answer=[],
        mock_tool_calls=[{"name": "ls", "arguments": {"path": "."}}],
    ),
    Case(
        name="三步任务：写入→改写→复读",
        prompt="往 eval/_tmp_test.txt 写入 hello，然后把 hello 改成 goodbye，最后读回确认",
        expect_tools=["write", "edit", "read"],
        expect_in_answer=["goodbye"],
        mock_tool_calls=[
            {"name": "write", "arguments": {"path": "eval/_tmp_test.txt", "content": "hello"}},
            {"name": "edit", "arguments": {"path": "eval/_tmp_test.txt", "old_text": "hello", "new_text": "goodbye"}},
            {"name": "read", "arguments": {"path": "eval/_tmp_test.txt"}},
        ],
    ),
]


# ---------------------------------------------------------------------------
# LLM 调用层：统一约定返回 (message, usage)
#   message 必须是 {"role":"assistant","content":str,"tool_calls":[...]} 形状
# ---------------------------------------------------------------------------
def call_llm_real(messages: list[dict], tools: list[dict], system_prompt: str) -> tuple[dict, dict]:
    """真实调用 OpenAI 兼容接口。比 core.llm.call_llm 多返回 usage，方便算钱。"""
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
    )
    msgs = [{"role": "system", "content": system_prompt}, *messages]
    resp = client.chat.completions.create(
        model=_model_name(),
        messages=msgs,
        tools=tools,
        tool_choice="auto",
    )
    msg = resp.choices[0].message

    result: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        result["tool_calls"] = [tc.model_dump() for tc in msg.tool_calls]

    usage: dict[str, int] = {}
    if resp.usage:
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens": resp.usage.total_tokens,
        }
    return result, usage


def _model_name() -> str:
    return os.environ.get("EVAL_MODEL") or os.environ.get("OPENAI_MODEL") or "kimi-k2.6"


class MockLLM:
    """
    假 LLM：按用例脚本决定每一轮调什么工具，最后一步把所有工具结果汇总成回答。

    工具是真实执行的（真的读写你的磁盘），只有"该调哪个工具"这个决策是假的。
    所以它能验证闭环通不通，验证不了模型聪不聪明。
    """

    def __init__(self, case: Case) -> None:
        self.case = case
        self.step = 0

    def __call__(self, messages: list[dict], tools: list[dict], system_prompt: str):
        self.step += 1
        script = self.case.mock_tool_calls

        # 还没走完脚本 → 继续发工具调用
        if self.step <= len(script):
            call = script[self.step - 1]
            return (
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": f"call_{self.step}",
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call["arguments"], ensure_ascii=False),
                            },
                        }
                    ],
                },
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            )

        # 脚本走完 → 汇总。这里刻意引用最近一次工具结果，模拟"看着结果作答"
        last_tool = ""
        for m in reversed(messages):
            if m.get("role") == "tool":
                last_tool = m.get("content", "")
                break
        return (
            {"role": "assistant", "content": last_tool.strip()[:1500]},
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )


# ---------------------------------------------------------------------------
# 核心：跑一个任务。这就是 Agent Loop 本体
#   while True: LLM决策 → 有tool_call? 执行并回灌 : 结束
# ---------------------------------------------------------------------------
def run_agent(case: Case, tools: list[dict], call_fn, verbose: bool = False) -> dict:
    executor = ToolExecutor()
    messages: list[dict] = [{"role": "user", "content": case.prompt}]

    tools_used: list[str] = []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    tool_turns = 0
    guard_hit = False

    started = time.time()
    for turn in range(1, MAX_TURNS + 1):
        assistant, usage = call_fn(messages, tools, SYSTEM_PROMPT)
        for k in total_usage:
            total_usage[k] += usage.get(k, 0)
        messages.append(assistant)

        calls = assistant.get("tool_calls") or []
        if not calls:
            break  # 没要调工具 → 说明它认为可以作答了

        if tool_turns >= MAX_TOOL_TURNS:
            guard_hit = True  # 熔断：再往下就该烧钱了
            break

        tool_turns += 1
        parsed = executor.parse_tool_calls(assistant)
        results = executor.execute_all(parsed)

        for tc, res in zip(parsed, results):
            tools_used.append(tc.name)
            if verbose:
                print(f"    [turn {tool_turns}] {tc.name}({tc.arguments})")
                preview = res.content[:160].replace("\n", " ")
                print(f"             → err={res.is_error}  {preview}")
            messages.append(res.to_message())  # ★ 结果回灌，Loop 的关键一行

    elapsed = time.time() - started

    answer = ""
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("content"):
            answer = m["content"]
            break

    error = f"触发熔断：工具调用超过 {MAX_TOOL_TURNS} 轮" if guard_hit else None
    return {
        "answer": answer,
        "tools_used": tools_used,
        "tool_turns": tool_turns,
        "usage": total_usage,
        "elapsed_sec": round(elapsed, 3),
        "error": error,
    }


def evaluate(case: Case, result: dict) -> tuple[bool, str]:
    """双重判定：既看你调没调对工具，也看你有没有真的用了工具结果。"""
    if result["error"]:
        return False, result["error"]

    used = result["tools_used"]
    missing = [t for t in case.expect_tools if t not in used]
    if missing:
        return False, f"未调用必需工具 {missing}"

    answer_lower = result["answer"].lower()
    absent = [k for k in case.expect_in_answer if k.lower() not in answer_lower]
    if absent:
        return False, f"回答缺少关键信息 {absent}"

    return True, "OK"


# ---------------------------------------------------------------------------
def _print_summary(rows: list[dict], mock: bool) -> None:
    n = len(rows)
    passed = sum(r["passed"] for r in rows)
    turns = [r["tool_turns"] for r in rows]
    tokens = [r["usage"]["total_tokens"] for r in rows]
    latency = [r["elapsed_sec"] for r in rows]

    headline = "工具链路可用率" if mock else "端到端任务成功率"
    print("\n" + "=" * 70)
    print(f"评测结果汇总（{'链路自检，不可写入简历' if mock else '可直接写入简历'}）")
    print("=" * 70)
    print(f"  {headline:<16}: {passed}/{n} = {passed / n * 100:.1f}%")
    print(f"  平均工具调用轮次  : {sum(turns) / n:.2f}   (最大 {max(turns)})")
    if any(tokens):
        print(f"  平均 token 消耗   : {sum(tokens) / n:.0f}")
        print(f"  总 token 消耗     : {sum(tokens)}")
    else:
        print("  平均 token 消耗   : mock 模式不计量")
    print(f"  平均端到端延迟    : {sum(latency) / n:.2f} 秒")

    tool_freq: dict[str, int] = {}
    for r in rows:
        for t in r["tools_used"]:
            tool_freq[t] = tool_freq.get(t, 0) + 1
    if tool_freq:
        print("\n  工具调用频次：")
        for t, c in sorted(tool_freq.items(), key=lambda x: -x[1]):
            print(f"    {t:<8} {c} 次")

    failed = [r for r in rows if not r["passed"]]
    if failed:
        print("\n  失败用例：")
        for r in failed:
            print(f"    - {r['case']}: {r['reason']}")


def _compare(prev: list[dict], cur: list[dict], mock: bool) -> None:
    old = {r["case"]: r for r in prev}
    label = "成功率" if not mock else "链路可用率"
    op = sum(r["passed"] for r in prev) / len(prev) * 100
    np_ = sum(r["passed"] for r in cur) / len(cur) * 100
    print(f"\n  与上次对比（{label}）： {op:.1f}%  →  {np_:.1f}%")
    for r in cur:
        before = old.get(r["case"])
        if not before:
            continue
        if before["passed"] != r["passed"]:
            arrow = "↗ 修好了" if r["passed"] else "↘ 退化了"
            print(f"    {arrow}  {r['case']}")


def main() -> None:
    ap = argparse.ArgumentParser(description="XXXClaw Agent 评测")
    ap.add_argument("--mock", action="store_true", help="不调用真实 API，零成本自检链路")
    ap.add_argument("--case", type=int, nargs="*", help="只跑指定编号用例（从 1 开始）")
    ap.add_argument("--verbose", action="store_true", help="打印每轮工具入参和返回")
    ap.add_argument("--compare", action="store_true", help="跑完与上一次 results.json 对比")
    args = ap.parse_args()

    if not args.mock and not os.environ.get("OPENAI_API_KEY"):
        print("未检测到 OPENAI_API_KEY。\n"
              "  想真跑：先 $env:OPENAI_API_KEY=\"sk-...\" 再重试\n"
              "  想白嫖：python eval/bench.py --mock")
        sys.exit(1)

    tools = [t.to_llm_format() for t in load_tools()]
    cases = CASES if not args.case else [CASES[i - 1] for i in args.case]

    print("=" * 70)
    print(f"XXXClaw Agent 评测   模式={'mock（链路自检）' if args.mock else '真实 API'}"
          f"   工具数={len(tools)}   用例数={len(cases)}")
    if not args.mock:
        print(f"  模型={_model_name()}   base_url={os.environ.get('OPENAI_BASE_URL')}")
    print("=" * 70)

    out = ROOT / "eval" / "results.json"
    prev: list[dict] = []
    if args.compare and out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            prev = []

    rows = []
    for idx, case in enumerate(cases, 1):
        print(f"\n[{idx}] {case.name}")
        call_fn: Callable = MockLLM(case) if args.mock else call_llm_real
        res = run_agent(case, tools, call_fn, verbose=args.verbose)
        ok, reason = evaluate(case, res)

        rows.append({"case": case.name, **res, "passed": ok, "reason": reason})
        flag = "PASS" if ok else "FAIL"
        print(f"    {flag}  工具={res['tools_used'] or '无'}  轮次={res['tool_turns']}  "
              f"token={res['usage']['total_tokens']}  {res['elapsed_sec']}s  "
              f"{'' if ok else reason}")

    _print_summary(rows, mock=args.mock)

    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  详细结果已写入 {out}")

    if prev:
        _compare(prev, rows, mock=args.mock)

    tmp = ROOT / "eval" / "_tmp_test.txt"
    if tmp.exists():
        tmp.unlink()
        print("  已清理临时文件 eval/_tmp_test.txt")


if __name__ == "__main__":
    main()