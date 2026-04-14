"""
WeChat Insight - 微信群聊洞察工具
基于 wechat-cli 的本地网页应用，支持群消息总结、智能问答、分组管理
"""

import subprocess
import json
import os
import re
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, send_file
from openai import OpenAI


def _get_base_dir():
    """获取基础目录：打包后为 exe 所在目录，开发时为脚本所在目录"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _get_bundle_dir():
    """获取资源目录：PyInstaller 解压临时目录或脚本目录"""
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS)
    return Path(__file__).parent


BASE_DIR = _get_base_dir()
BUNDLE_DIR = _get_bundle_dir()

app = Flask(__name__,
            static_folder=str(BUNDLE_DIR / "static"),
            template_folder=str(BUNDLE_DIR / "templates"))
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

# ── 配置 ──────────────────────────────────────────────
API_BASE_URL = os.environ.get("API_BASE_URL", "https://poloai.top/v1")
API_KEY = os.environ.get("API_KEY", "sk-")
MODEL_NAME = os.environ.get("MODEL_NAME", "gpt-4o-mini")

DEFAULT_SYSTEM_PROMPT = """你是一位专业的HIS（医院信息系统）工程师助手。请分析以下聊天记录，帮助我快速掌握工作动态。重点关注：

1. **需求梳理**：有哪些需求或任务被提出？分别是谁提出的？提出的时间和背景是什么？
2. **优先级判断**：根据对话语气和上下文，哪些事项比较紧急或重要？
3. **技术问题**：是否有需要关注或处理的技术问题、Bug反馈、系统异常？
4. **决策与结论**：是否有已达成的决策、确认的方案或结论？
5. **待办事项**：需要跟进、回复或处理的具体事项有哪些？

请用清晰的结构化格式回答，按重要程度排序，帮我快速了解工作重点和待办事项。如果某些类别没有相关内容可以跳过。"""

SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)

# ── Prompt 模板 ─────────────────────────────────────
PROMPT_TEMPLATES = [
    {
        "id": "chat_summary",
        "name": "大家在聊什么",
        "icon": "💬",
        "prompt": "简要概括大家最近在聊什么，按话题分类列出，每个话题用一两句话说明核心内容和参与人。"
    },
    {
        "id": "needs",
        "name": "需求提取",
        "icon": "📋",
        "prompt": "从聊天记录中提取所有需求和任务。对每条需求列出：提出人、提出时间、具体内容、紧急程度（高/中/低）。按紧急度排序。"
    },
    {
        "id": "bugs",
        "name": "Bug 汇总",
        "icon": "🐛",
        "prompt": "从聊天记录中提取所有系统异常、Bug反馈、报错信息。对每个问题列出：反馈人、时间、问题描述、影响范围、当前状态（已解决/待处理/处理中）。"
    },
    {
        "id": "deploy",
        "name": "部署追踪",
        "icon": "🚀",
        "prompt": "从聊天记录中梳理版本发布和部署相关信息。列出：发布时间、版本号、发布内容、是否有异常反馈、回滚情况。"
    },
    {
        "id": "todo",
        "name": "待办清单",
        "icon": "✅",
        "prompt": "从聊天记录中提取需要我（HIS工程师）跟进处理的事项。列出：事项内容、相关人员、紧急程度、截止时间（如有）。按紧急度排序，给出建议处理顺序。"
    },
    {
        "id": "weekly",
        "name": "周报素材",
        "icon": "📝",
        "prompt": "从聊天记录中提取本周工作成果和进展，整理为周报素材。分类：已完成事项、进行中事项、待解决问题、下周计划。用简洁的条目格式输出，可以直接用于工作周报。"
    },
]

BRIEFING_PROMPT = """你是一位HIS工程师的智能助手。请对以下来自多个微信群/联系人的聊天记录生成【今日早报】。

要求：
1. **紧急事项**（标记 🔴）：系统故障、Bug、阻塞性问题，需要立即处理的
2. **重要事项**（标记 🟡）：新需求、待确认事项、需要跟进的
3. **一般动态**（标记 🟢）：日常沟通、已解决的问题、知会类信息
4. **待办清单**：从所有对话中提取需要我回复或处理的事项，列出具体行动

每个事项标注来源群/联系人。先紧急后一般，简洁明了，帮我5分钟内掌握全局。"""

# 分组数据存储路径（exe 所在目录 / 脚本所在目录）
DATA_DIR = BASE_DIR / "data"
GROUPS_FILE = DATA_DIR / "groups.json"

client = None


def get_ai_client():
    global client
    if client is None and API_KEY and API_BASE_URL:
        client = OpenAI(api_key=API_KEY, base_url=API_BASE_URL)
    return client


# ── 分组管理 ──────────────────────────────────────────

def load_groups() -> list:
    """加载所有分组"""
    if not GROUPS_FILE.exists():
        return []
    try:
        with open(GROUPS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return []


def save_groups(groups: list):
    """保存分组数据"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)


# ── wechat-cli 封装 ───────────────────────────────────

def _find_wechat_cli():
    """查找 wechat-cli 可执行文件"""
    import shutil
    # 打包后优先找 exe 同目录下的 wechat-cli
    if getattr(sys, 'frozen', False):
        bundled = BUNDLE_DIR / "wechat-cli.exe"
        if bundled.exists():
            return str(bundled)
    return shutil.which("wechat-cli") or "wechat-cli"


WECHAT_CLI = _find_wechat_cli()


def run_wechat_init(force: bool = False, db_dir: str = None) -> dict:
    """运行 wechat-cli init，返回 {ok, output} 或 {error, output}"""
    cmd = [WECHAT_CLI, "init"]
    if force:
        cmd.append("--force")
    if db_dir:
        cmd += ["--db-dir", db_dir]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=120, env=env)
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        output = (stdout + "\n" + stderr).strip()
        if result.returncode == 0:
            return {"ok": True, "output": output}
        return {"error": stderr.strip() or "初始化失败", "output": output}
    except subprocess.TimeoutExpired:
        return {"error": "初始化超时（密钥提取可能需要较长时间）"}
    except FileNotFoundError:
        return {"error": "未找到 wechat-cli"}


def check_wechat_cli_ready() -> dict:
    """检查 wechat-cli 是否已初始化"""
    config_dir = os.path.expanduser("~/.wechat-cli")
    config_file = os.path.join(config_dir, "config.json")
    keys_file = os.path.join(config_dir, "all_keys.json")
    initialized = os.path.exists(config_file) and os.path.exists(keys_file)
    # 试一下能否正常调用
    usable = False
    if initialized:
        result = run_wechat_cli(["sessions", "--limit", "1"])
        usable = isinstance(result, list) or (isinstance(result, dict) and "error" not in result)
    return {
        "initialized": initialized,
        "usable": usable,
        "config_file": config_file if initialized else None,
        "keys_file": keys_file if initialized else None,
    }


def run_wechat_cli(args: list) -> dict | list | str:
    """调用 wechat-cli 命令并返回解析后的结果"""
    cmd = [WECHAT_CLI] + args + ["--format", "json"]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=60, env=env
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        if result.returncode != 0:
            return {"error": stderr.strip() or "命令执行失败"}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            return stdout.strip()
    except subprocess.TimeoutExpired:
        return {"error": "命令超时"}
    except FileNotFoundError:
        return {"error": "未找到 wechat-cli，请确认已安装"}


def get_sessions(limit: int = 500):
    """获取最近会话列表"""
    return run_wechat_cli(["sessions", "--limit", str(limit)])


def get_history(session_name: str, limit: int = 50, start_time: str = None, end_time: str = None):
    """获取聊天记录，支持按条数或时间范围"""
    args = ["history", session_name, "--media"]
    if start_time:
        args += ["--start-time", start_time]
    if end_time:
        args += ["--end-time", end_time]
        # wechat-cli 按时间范围查也需要 limit，给一个足够大的值
        args += ["--limit", "1000"]
    if not start_time:
        args += ["--limit", str(limit)]
    data = run_wechat_cli(args)
    if isinstance(data, dict) and "error" not in data:
        raw_messages = data.get("messages", [])
        parsed = [parse_message_line(msg) for msg in raw_messages]
        data["parsed_messages"] = parsed
    return data


def get_today_str():
    """返回今天的日期字符串 YYYY-MM-DD"""
    return datetime.now().strftime("%Y-%m-%d")


def get_tomorrow_str():
    """返回明天的日期字符串 YYYY-MM-DD"""
    return (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")


def parse_message_line(line: str) -> dict:
    """解析 wechat-cli 的消息字符串"""
    m = re.match(r'^\[([^\]]+)\]\s*(.+?):\s*(.*)$', line, re.DOTALL)
    if m:
        return {"time": m.group(1), "sender": m.group(2).strip(), "content": m.group(3).strip()}
    m2 = re.match(r'^\[([^\]]+)\]\s*(.*)$', line, re.DOTALL)
    if m2:
        return {"time": m2.group(1), "sender": "", "content": m2.group(2).strip()}
    return {"time": "", "sender": "", "content": line}


def decode_dat_file(file_path: str) -> tuple[bytes, str]:
    """解码微信 .dat 图片文件（XOR 加密），返回 (解码数据, mime_type)"""
    with open(file_path, "rb") as f:
        data = f.read()
    if not data:
        return data, "application/octet-stream"
    # 尝试常见图片格式的 magic bytes 来推断 XOR key
    magics = [
        (b'\xff\xd8\xff', "image/jpeg"),
        (b'\x89\x50\x4e\x47', "image/png"),
        (b'\x47\x49\x46', "image/gif"),
        (b'\x42\x4d', "image/bmp"),
    ]
    for magic, mime in magics:
        key = data[0] ^ magic[0]
        if all(data[i] ^ key == magic[i] for i in range(len(magic))):
            decoded = bytes(b ^ key for b in data)
            return decoded, mime
    return data, "application/octet-stream"


def get_stats(session_name: str):
    return run_wechat_cli(["stats", session_name])


def search_messages(keyword: str, session_name: str = None):
    args = ["search", keyword]
    if session_name:
        args += ["--chat", session_name]
    return run_wechat_cli(args)


def search_contacts(query: str, limit: int = 50):
    """搜索联系人（匹配昵称、备注、wxid）"""
    args = ["contacts", "--query", query, "--limit", str(limit)]
    return run_wechat_cli(args)


# ── AI 能力 ───────────────────────────────────────────

def format_messages_for_ai(data) -> str:
    if isinstance(data, dict) and "error" in data:
        return ""
    messages = data.get("messages", []) if isinstance(data, dict) else []
    return "\n".join(messages)


# 单次 AI 调用的文本上限（字符数），超过则分段处理
# 约 8000 字符 ≈ 4000 token，留足空间给 prompt 和回复
CHUNK_CHAR_LIMIT = 8000


def _split_text_to_chunks(text: str, limit: int = CHUNK_CHAR_LIMIT) -> list[str]:
    """按行拆分文本为多个不超过 limit 字符的分段"""
    lines = text.split("\n")
    chunks = []
    current = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > limit and current:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def _ai_call(system_msg: str, user_content: str, max_tokens: int = 4000) -> str:
    """单次 AI 调用"""
    ai = get_ai_client()
    if not ai:
        return "未配置 AI API，请点击左下角「AI 设置」进行配置。"
    try:
        resp = ai.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
            max_tokens=max_tokens,
        )
        return resp.choices[0].message.content
    except Exception as e:
        return f"AI 调用失败: {str(e)}"


def ai_summarize(messages_text: str, context_label: str = "") -> str:
    ai = get_ai_client()
    if not ai:
        return "未配置 AI API，请点击左下角「AI 设置」进行配置。"

    ctx = f"（数据来源：{context_label}）\n" if context_label else ""
    system_msg = SYSTEM_PROMPT or "你是一个微信群聊分析助手。请对聊天记录进行总结分析。"

    chunks = _split_text_to_chunks(messages_text)

    if len(chunks) <= 1:
        # 文本不大，直接分析
        user_content = f"""{ctx}请对以下聊天记录进行分析总结。
如果数据来自多个群/联系人，请按来源分别总结后再给出综合洞察。
请用中文回答，简洁有条理。

聊天记录：
{messages_text}"""
        return _ai_call(system_msg, user_content)

    # 分段摘要
    part_summaries = []
    for i, chunk in enumerate(chunks):
        prompt = f"""{ctx}这是聊天记录的第 {i+1}/{len(chunks)} 部分。
请提取这部分中的关键信息，包括：重要讨论话题、提出的需求/问题、关键决策、待办事项。
尽量保留具体的人名、时间、数据等细节。简洁输出，不遗漏要点。

聊天记录片段：
{chunk}"""
        result = _ai_call(system_msg, prompt, max_tokens=2000)
        part_summaries.append(f"【第{i+1}部分摘要】\n{result}")

    # 汇总所有分段摘要
    combined = "\n\n".join(part_summaries)
    final_prompt = f"""{ctx}以下是对一段较长聊天记录分 {len(chunks)} 部分提取的摘要。
请将这些摘要合并为一份完整的分析报告，去重整合，按重要程度排序。
保留所有关键细节（人名、时间、具体事项），不要遗漏任何重要信息。

各部分摘要：
{combined}"""
    return _ai_call(system_msg, final_prompt, max_tokens=4000)


def ai_ask(messages_text: str, question: str, context_label: str = "") -> str:
    ai = get_ai_client()
    if not ai:
        return "未配置 AI API，请点击左下角「AI 设置」进行配置。"

    ctx = f"（数据来源：{context_label}）\n" if context_label else ""
    system_msg = SYSTEM_PROMPT or "你是一个微信群聊分析助手。根据聊天记录回答用户的问题。"

    chunks = _split_text_to_chunks(messages_text)

    if len(chunks) <= 1:
        user_content = f"""{ctx}根据以下聊天记录回答我的问题。
如果聊天记录中没有相关信息，请如实说明。请用中文回答。

聊天记录：
{messages_text}

我的问题：{question}"""
        return _ai_call(system_msg, user_content)

    # 分段提取与问题相关的信息
    part_extracts = []
    for i, chunk in enumerate(chunks):
        prompt = f"""{ctx}这是聊天记录的第 {i+1}/{len(chunks)} 部分。
用户的问题/指令是：{question}

请从这部分聊天记录中提取所有相关的信息和要点。保留具体的人名、时间、数据等细节。

聊天记录片段：
{chunk}"""
        result = _ai_call(system_msg, prompt, max_tokens=2000)
        part_extracts.append(f"【第{i+1}部分】\n{result}")

    # 基于所有分段提取的信息回答问题
    combined = "\n\n".join(part_extracts)
    final_prompt = f"""{ctx}以下是从一段较长聊天记录中分 {len(chunks)} 部分提取的信息。
请基于这些信息，完整回答用户的问题/执行用户的指令。去重整合，不遗漏要点。请用中文回答。

各部分提取的信息：
{combined}

用户的问题/指令：{question}"""
    return _ai_call(system_msg, final_prompt, max_tokens=4000)


def get_days_ago_str(days: int) -> str:
    """返回 N 天前的日期字符串 YYYY-MM-DD"""
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def collect_group_messages(group: dict, limit: int = 50, mode: str = "today", days: int = 0) -> tuple[str, int, list]:
    """收集一个分组下所有会话的消息
    mode: "today" 取当天数据, "days" 取最近N天, "limit" 按条数取
    返回 (合并文本, 总消息数, 各会话详情)
    """
    members = group.get("members", [])
    all_text_parts = []
    total_count = 0
    details = []

    if mode == "today":
        start_time = get_today_str()
        end_time = get_tomorrow_str()
    elif mode == "days" and days > 0:
        start_time = get_days_ago_str(days)
        end_time = get_tomorrow_str()
    else:
        start_time = end_time = None

    for member in members:
        username = member.get("username", "")
        chat_name = member.get("chat", username)
        if not username:
            continue
        if mode in ("today", "days") and start_time:
            data = get_history(username, start_time=start_time, end_time=end_time)
        else:
            data = get_history(username, limit=limit)
        if isinstance(data, dict) and "error" not in data:
            msgs = data.get("messages", [])
            count = len(msgs)
            total_count += count
            if msgs:
                header = f"\n--- {chat_name} ({count} 条消息) ---"
                all_text_parts.append(header)
                all_text_parts.extend(msgs)
            details.append({"chat": chat_name, "username": username, "count": count})
        else:
            details.append({"chat": chat_name, "username": username, "count": 0,
                            "error": data.get("error", "") if isinstance(data, dict) else ""})

    return "\n".join(all_text_parts), total_count, details


# ── 路由：页面 ────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── 路由：初始化 ────────────────────────────────────────

@app.route("/api/init/status")
def api_init_status():
    """检查 wechat-cli 初始化状态"""
    return jsonify(check_wechat_cli_ready())


@app.route("/api/init/run", methods=["POST"])
def api_init_run():
    """执行 wechat-cli init"""
    body = request.json or {}
    force = body.get("force", False)
    db_dir = body.get("db_dir", "").strip() or None
    return jsonify(run_wechat_init(force=force, db_dir=db_dir))


# ── 路由：会话 ────────────────────────────────────────

@app.route("/api/sessions")
def api_sessions():
    return jsonify(get_sessions())


@app.route("/api/history")
def api_history():
    name = request.args.get("name", "")
    mode = request.args.get("mode", "limit")
    days = int(request.args.get("days", "0"))
    limit = int(request.args.get("limit", "50"))
    if not name:
        return jsonify({"error": "缺少会话名称"}), 400
    if mode == "today":
        return jsonify(get_history(name, start_time=get_today_str(), end_time=get_tomorrow_str()))
    elif mode == "days" and days > 0:
        return jsonify(get_history(name, start_time=get_days_ago_str(days), end_time=get_tomorrow_str()))
    return jsonify(get_history(name, limit=limit))


@app.route("/api/stats")
def api_stats():
    name = request.args.get("name", "")
    if not name:
        return jsonify({"error": "缺少会话名称"}), 400
    return jsonify(get_stats(name))


@app.route("/api/search")
def api_search():
    keyword = request.args.get("q", "")
    chat = request.args.get("chat", "")
    if not keyword:
        return jsonify({"error": "缺少搜索关键词"}), 400
    return jsonify(search_messages(keyword, chat or None))


@app.route("/api/contacts")
def api_contacts():
    query = request.args.get("q", "")
    limit = request.args.get("limit", "50")
    if not query:
        return jsonify({"error": "缺少搜索关键词"}), 400
    return jsonify(search_contacts(query, int(limit)))


def _find_dat_file(file_path: str) -> str | None:
    """查找 .dat 文件，如果精确路径不存在则在 attach 目录下模糊搜索"""
    if os.path.isfile(file_path):
        return file_path
    # wechat-cli 的 MD5 目录名可能不精确，尝试用文件名在同级目录搜索
    filename = os.path.basename(file_path)
    # 路径格式: .../attach/<hash>/YYYY-MM/Img/<filename>.dat
    # 尝试在所有 attach/<hash>/YYYY-MM/Img/ 下找同名文件
    parts = Path(file_path).parts
    try:
        attach_idx = parts.index("attach")
        attach_dir = str(Path(*parts[:attach_idx + 1]))
        sub_parts = parts[attach_idx + 2:]  # 跳过 <hash>，取 YYYY-MM/Img/filename
        if os.path.isdir(attach_dir) and len(sub_parts) >= 2:
            for d in os.listdir(attach_dir):
                candidate = os.path.join(attach_dir, d, *sub_parts)
                if os.path.isfile(candidate):
                    return candidate
    except (ValueError, OSError):
        pass
    return None


@app.route("/api/media")
def api_media():
    """解码并返回微信 .dat 图片文件"""
    file_path = request.args.get("path", "")
    if not file_path or not file_path.endswith(".dat"):
        return jsonify({"error": "不支持的文件类型"}), 400
    resolved = _find_dat_file(file_path)
    if not resolved:
        return jsonify({"error": "文件不存在"}), 404
    try:
        decoded, mime = decode_dat_file(resolved)
        return Response(decoded, mimetype=mime)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/summarize", methods=["POST"])
def api_summarize():
    body = request.json
    name = body.get("name", "")
    limit = body.get("limit", 100)
    mode = body.get("mode", "limit")
    days = body.get("days", 0)
    if not name:
        return jsonify({"error": "缺少会话名称"}), 400
    if mode == "today":
        data = get_history(name, start_time=get_today_str(), end_time=get_tomorrow_str())
    elif mode == "days" and days > 0:
        data = get_history(name, start_time=get_days_ago_str(days), end_time=get_tomorrow_str())
    else:
        data = get_history(name, limit=limit)
    text = format_messages_for_ai(data)
    if not text:
        return jsonify({"error": "未获取到消息记录"})
    count = data.get("count", 0) if isinstance(data, dict) else 0
    chat_name = data.get("chat", name) if isinstance(data, dict) else name
    summary = ai_summarize(text, context_label=chat_name)
    return jsonify({"summary": summary, "message_count": count})


@app.route("/api/ask", methods=["POST"])
def api_ask():
    body = request.json
    name = body.get("name", "")
    question = body.get("question", "")
    limit = body.get("limit", 100)
    mode = body.get("mode", "limit")
    days = body.get("days", 0)
    if not name or not question:
        return jsonify({"error": "缺少会话名称或问题"}), 400
    if mode == "today":
        data = get_history(name, start_time=get_today_str(), end_time=get_tomorrow_str())
    elif mode == "days" and days > 0:
        data = get_history(name, start_time=get_days_ago_str(days), end_time=get_tomorrow_str())
    else:
        data = get_history(name, limit=limit)
    text = format_messages_for_ai(data)
    if not text:
        return jsonify({"error": "未获取到消息记录"})
    chat_name = data.get("chat", name) if isinstance(data, dict) else name
    answer = ai_ask(text, question, context_label=chat_name)
    return jsonify({"answer": answer})


# ── 路由：分组 CRUD ───────────────────────────────────

@app.route("/api/groups", methods=["GET"])
def api_groups_list():
    return jsonify(load_groups())


@app.route("/api/groups/message-counts")
def api_groups_message_counts():
    """获取所有分组的当天消息总数"""
    groups = load_groups()
    result = {}
    start = get_today_str()
    end = get_tomorrow_str()
    for group in groups:
        total = 0
        for member in group.get("members", []):
            username = member.get("username", "")
            if not username:
                continue
            data = get_history(username, start_time=start, end_time=end)
            if isinstance(data, dict) and "error" not in data:
                total += len(data.get("messages", []))
        result[group["id"]] = total
    return jsonify(result)


@app.route("/api/groups", methods=["POST"])
def api_groups_create():
    body = request.json
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "分组名称不能为空"}), 400
    groups = load_groups()
    group = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "members": body.get("members", []),  # [{username, chat}]
    }
    groups.append(group)
    save_groups(groups)
    return jsonify(group)


@app.route("/api/groups/<group_id>", methods=["PUT"])
def api_groups_update(group_id):
    body = request.json
    groups = load_groups()
    for g in groups:
        if g["id"] == group_id:
            if "name" in body:
                g["name"] = body["name"]
            if "members" in body:
                g["members"] = body["members"]
            save_groups(groups)
            return jsonify(g)
    return jsonify({"error": "分组不存在"}), 404


@app.route("/api/groups/<group_id>", methods=["DELETE"])
def api_groups_delete(group_id):
    groups = load_groups()
    groups = [g for g in groups if g["id"] != group_id]
    save_groups(groups)
    return jsonify({"ok": True})


@app.route("/api/groups/<group_id>/add", methods=["POST"])
def api_groups_add_member(group_id):
    """向分组添加成员"""
    body = request.json
    username = body.get("username", "")
    chat = body.get("chat", "")
    if not username:
        return jsonify({"error": "缺少 username"}), 400
    groups = load_groups()
    for g in groups:
        if g["id"] == group_id:
            # 避免重复
            existing = {m["username"] for m in g["members"]}
            if username not in existing:
                g["members"].append({"username": username, "chat": chat})
                save_groups(groups)
            return jsonify(g)
    return jsonify({"error": "分组不存在"}), 404


@app.route("/api/groups/<group_id>/remove", methods=["POST"])
def api_groups_remove_member(group_id):
    """从分组移除成员"""
    body = request.json
    username = body.get("username", "")
    groups = load_groups()
    for g in groups:
        if g["id"] == group_id:
            g["members"] = [m for m in g["members"] if m["username"] != username]
            save_groups(groups)
            return jsonify(g)
    return jsonify({"error": "分组不存在"}), 404


@app.route("/api/groups/<group_id>/counts", methods=["POST"])
def api_groups_counts(group_id):
    """获取分组内各成员的消息条数"""
    body = request.json or {}
    mode = body.get("mode", "today")
    days = body.get("days", 0)
    groups = load_groups()
    group = next((g for g in groups if g["id"] == group_id), None)
    if not group:
        return jsonify({"error": "分组不存在"}), 404
    counts = {}
    for member in group.get("members", []):
        username = member.get("username", "")
        if not username:
            continue
        if mode == "today":
            data = get_history(username, start_time=get_today_str(), end_time=get_tomorrow_str())
        elif mode == "days" and days > 0:
            data = get_history(username, start_time=get_days_ago_str(days), end_time=get_tomorrow_str())
        else:
            data = get_history(username, limit=500)
        if isinstance(data, dict) and "error" not in data:
            counts[username] = len(data.get("messages", []))
        else:
            counts[username] = 0
    return jsonify(counts)


# ── 路由：分组 AI ─────────────────────────────────────

@app.route("/api/groups/<group_id>/context", methods=["POST"])
def api_groups_context(group_id):
    """预览分组上下文，不调用 AI"""
    body = request.json or {}
    limit = body.get("limit", 50)
    mode = body.get("mode", "today")  # "today", "days", "limit"
    days = body.get("days", 0)
    question = body.get("question", "")
    groups = load_groups()
    group = next((g for g in groups if g["id"] == group_id), None)
    if not group:
        return jsonify({"error": "分组不存在"}), 404
    if not group["members"]:
        return jsonify({"error": "分组内没有成员"}), 400

    text, total_count, details = collect_group_messages(group, limit, mode=mode, days=days)
    if not text:
        mode_labels = {"today": "当天", "days": f"最近 {days} 天", "limit": "按条数"}
        return jsonify({"error": f"未获取到消息记录（模式：{mode_labels.get(mode, mode)}）"})

    if mode == "today":
        mode_label = f"当天 ({get_today_str()})"
    elif mode == "days":
        mode_label = f"最近 {days} 天 ({get_days_ago_str(days)} ~ {get_today_str()})"
    else:
        mode_label = f"每会话最近 {limit} 条"
    label = f"分组「{group['name']}」{len(group['members'])} 个会话 · {mode_label}"
    return jsonify({
        "context": text,
        "context_label": label,
        "question": question,
        "group_name": group["name"],
        "total_messages": total_count,
        "details": details,
        "mode": mode,
        "date": get_today_str() if mode == "today" else None,
    })


@app.route("/api/context", methods=["POST"])
def api_single_context():
    """预览单会话上下文，不调用 AI"""
    body = request.json
    name = body.get("name", "")
    limit = body.get("limit", 100)
    mode = body.get("mode", "limit")  # 单会话默认按条数
    days = body.get("days", 0)
    question = body.get("question", "")
    if not name:
        return jsonify({"error": "缺少会话名称"}), 400
    if mode == "today":
        data = get_history(name, start_time=get_today_str(), end_time=get_tomorrow_str())
    elif mode == "days" and days > 0:
        data = get_history(name, start_time=get_days_ago_str(days), end_time=get_tomorrow_str())
    else:
        data = get_history(name, limit=limit)
    text = format_messages_for_ai(data)
    if not text:
        return jsonify({"error": "未获取到消息记录"})
    count = data.get("count", 0) if isinstance(data, dict) else 0
    chat_name = data.get("chat", name) if isinstance(data, dict) else name
    return jsonify({
        "context": text,
        "question": question,
        "chat_name": chat_name,
        "message_count": count,
        "mode": mode,
    })


@app.route("/api/groups/<group_id>/summarize", methods=["POST"])
def api_groups_summarize(group_id):
    """对整个分组的所有会话消息做 AI 总结"""
    body = request.json or {}
    limit = body.get("limit", 50)
    mode = body.get("mode", "today")
    days = body.get("days", 0)
    groups = load_groups()
    group = next((g for g in groups if g["id"] == group_id), None)
    if not group:
        return jsonify({"error": "分组不存在"}), 404
    if not group["members"]:
        return jsonify({"error": "分组内没有成员"}), 400

    text, total_count, details = collect_group_messages(group, limit, mode=mode, days=days)
    if not text:
        return jsonify({"error": "未获取到任何消息记录"})

    label = f"分组「{group['name']}」包含 {len(group['members'])} 个会话"
    summary = ai_summarize(text, label)
    return jsonify({
        "summary": summary,
        "group_name": group["name"],
        "total_messages": total_count,
        "details": details,
    })


@app.route("/api/groups/<group_id>/ask", methods=["POST"])
def api_groups_ask(group_id):
    """基于分组所有会话消息回答问题"""
    body = request.json or {}
    question = body.get("question", "")
    limit = body.get("limit", 50)
    mode = body.get("mode", "today")
    days = body.get("days", 0)
    if not question:
        return jsonify({"error": "缺少问题"}), 400
    groups = load_groups()
    group = next((g for g in groups if g["id"] == group_id), None)
    if not group:
        return jsonify({"error": "分组不存在"}), 404

    text, total_count, details = collect_group_messages(group, limit, mode=mode, days=days)
    if not text:
        return jsonify({"error": "未获取到任何消息记录"})

    label = f"分组「{group['name']}」包含 {len(group['members'])} 个会话"
    answer = ai_ask(text, question, label)
    return jsonify({"answer": answer, "total_messages": total_count})


# ── 路由：模板 & 早报 ────────────────────────────────

@app.route("/api/templates")
def api_templates():
    return jsonify(PROMPT_TEMPLATES)


@app.route("/api/briefing", methods=["POST"])
def api_briefing():
    """一键早报：汇总所有分组的当日消息"""
    body = request.json or {}
    mode = body.get("mode", "today")
    days = body.get("days", 1)
    groups = load_groups()
    if not groups:
        return jsonify({"error": "还没有创建分组，请先创建分组并添加会话"})

    all_parts = []
    total_count = 0
    group_details = []

    for group in groups:
        if not group.get("members"):
            continue
        if mode == "today":
            text, count, details = collect_group_messages(group, mode="today")
        else:
            text, count, details = collect_group_messages(group, mode="days", days=days)
        if text:
            all_parts.append(f"\n===== 分组：{group['name']} =====")
            all_parts.append(text)
            total_count += count
        group_details.append({
            "group": group["name"],
            "count": count,
            "details": details,
        })

    combined = "\n".join(all_parts)
    if not combined.strip():
        return jsonify({"error": f"所有分组暂无{'当天' if mode == 'today' else f'最近{days}天的'}消息"})

    ai = get_ai_client()
    if not ai:
        return jsonify({"error": "未配置 AI，请先在 AI 设置中配置"})

    try:
        resp = ai.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": BRIEFING_PROMPT},
                {"role": "user", "content": f"以下是来自 {len(groups)} 个分组的聊天记录，请生成早报：\n{combined}"},
            ],
            temperature=0.3,
            max_tokens=4000,
        )
        return jsonify({
            "briefing": resp.choices[0].message.content,
            "total_messages": total_count,
            "group_details": group_details,
        })
    except Exception as e:
        return jsonify({"error": f"AI 调用失败: {str(e)}"})


# ── 路由：配置 ────────────────────────────────────────

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify({
        "api_base_url": API_BASE_URL,
        "model_name": MODEL_NAME,
        "system_prompt": SYSTEM_PROMPT,
        "configured": bool(API_KEY and API_BASE_URL and MODEL_NAME),
    })


@app.route("/api/config", methods=["POST"])
def api_set_config():
    global API_BASE_URL, API_KEY, MODEL_NAME, SYSTEM_PROMPT, client
    body = request.json
    API_BASE_URL = body.get("api_base_url", API_BASE_URL)
    API_KEY = body.get("api_key", API_KEY)
    MODEL_NAME = body.get("model_name", MODEL_NAME)
    if "system_prompt" in body:
        SYSTEM_PROMPT = body["system_prompt"]
    client = None
    return jsonify({"ok": True, "configured": bool(API_KEY and API_BASE_URL and MODEL_NAME)})


# ── 启动 ──────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  WeChat Insight - 微信群聊洞察工具")
    print("=" * 50)
    print(f"  打开浏览器访问: http://localhost:5678")
    print(f"  按 Ctrl+C 退出")
    print("=" * 50 + "\n")

    import webbrowser
    webbrowser.open("http://localhost:5678")
    is_frozen = getattr(sys, 'frozen', False)
    app.run(host="127.0.0.1", port=5678, debug=not is_frozen)
