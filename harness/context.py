"""上下文组装：system 提示 + 记忆注入 + 历史裁剪。

提示词的**文本**在 `harness/prompts/{zh,en}.py`（按语言一套，ADR-085），
这里只负责"怎么拼"。

性能考量（直接对应省钱）：
  DeepSeek 的 prompt cache 按 **token 前缀**命中，所以消息顺序是经过设计的 ——

      [ 身份与规则 + 联网说明 + 背景档案 README + 记忆条目 ]   稳定（写记忆/整理时才变）
      [ 对话摘要（发生过压缩时才有）]                          偶有变化
      [ 历史对话 … ]                                          大头，纯追加 → 全额命中缓存
      [ 当前用户消息 ]
      [ 当前时间 + 裁剪提示 ]                                  每轮都变，所以放在**最后**

  两条硬约束：
  1. **记忆与背景档案都排在历史之前**。它们一变（写入一条记忆、跑完一次整理）
     会让历史的前缀失效一次 —— 这是必要的代价，因为记忆必须每一轮都在场。
     真正要守住的是下一条：**每轮都变的东西绝不能进 system**。
     （M2 时代还有第三条约束"检索块不能放最前面"，那套检索机制已删除，见 ADR-058。）
  2. **精确到分钟的时间也绝不能放进 system** —— system 之后还有摘要与全部历史，
     时间一变，那些全都要按原价重算。缓存命中价是未命中的 1/50，这笔账很大。
     所以时间与"已省略 N 段"这类每轮都变的信息统一放在最后一条消息里。
"""

from __future__ import annotations

from datetime import datetime

from .config import Settings
from .llm_types import Message, estimate_messages_tokens, split_into_groups
from .prompts import bundle

# 提示词的**文本**已经搬到 `harness/prompts/{zh,en}.py`（ADR-085）：
# 它们是几十行一段的"代码"，按语言各放一个模块；这里只保留"怎么拼"的逻辑。
# 取文本一律 `bundle()`（当前语言）—— **不要** import 具体某个语言模块，
# 否则英文模式会拿到中文。


def web_tools_text(
    enabled: bool, search_available: bool, search_on: bool = True
) -> str:
    """按当前配置生成"联网工具"那一段系统提示。

    三种情形分开写清楚，而不是留空 —— 免得模型以为工具存在却调不到：
      * 联网整个关着；
      * 只能开链接（会话没开搜索，或后端不可用）；
      * 搜索 + 开链接都能用。
    """
    if not enabled:
        return bundle().WEB_DISABLED
    if not search_available or not search_on:
        return bundle().OPEN_ONLY_PROMPT
    return bundle().WEB_TOOLS_PROMPT


def vault_prompt_text(readme: str, *, available: bool) -> str:
    """生成"背景档案"那一段。

    `available` 与"有没有注册那三个工具"必须是**同一个判断**（都由
    `VaultStore.has_content()` 决定）：提示词里说有、工具却没注册，
    模型会去调一个不存在的工具，白烧一步。
    """
    if not available:
        return bundle().VAULT_EMPTY_PROMPT
    if not readme.strip():
        return bundle().VAULT_NO_README_PROMPT
    return bundle().VAULT_PROMPT.format(readme=readme)


def build_system_message(
    settings: Settings,
    memory_text: str,
    *,
    web_tools: str = "",
    vault_text: str = "",
) -> Message:
    """主 system 消息。**刻意不含任何每轮都变的内容**（时间、裁剪提示）。

    它一变，排在它后面的摘要与全部历史都会失去缓存命中。

    README 排在这里（记忆条目**之前**）是按"变得多快"排的：
    记忆条目每调用一次 mem_create 就变一次，而 README 一天最多变一次 ——
    变得慢的放前面，缓存前缀才长。
    """
    content = bundle().SYSTEM_PROMPT.format(
        memory_text=memory_text,
        vault_text=vault_text or bundle().VAULT_EMPTY_PROMPT,
        web_tools_text=web_tools or web_tools_text(enabled=False, search_available=False),
    )
    return Message(role="system", content=content)


def build_tail_message(*, now: datetime, omitted_groups: int = 0) -> Message:
    """每轮末尾的易变信息：当前时间 + 裁剪提示。

    为什么放在**最后一条消息**而不是 system 里：
    prompt cache 按 token 前缀命中，而 system 之后还有摘要与全部历史。
    把精确到分钟的时间放进 system，等于每跨一分钟就让摘要和整段历史按原价重算 ——
    缓存命中价是未命中的 1/50，这笔账比它自己那几十个 token 大几个数量级。
    放在末尾，则只有它自己是新的，前面全部复用缓存。
    """
    lines: list[str] = []
    if omitted_groups > 0:
        lines.append(bundle().TAIL_OMITTED_NOTICE.format(omitted=omitted_groups))
    lines.append(
        bundle().TAIL_NOW.format(
            stamp=now.strftime("%Y-%m-%d %H:%M"),
            weekday=now.strftime("%A"),
            offset=now.strftime("%z")[:3],
        )
    )
    return Message(role="system", content="\n\n".join(lines) + "\n")


def trim_history(
    history: list[Message], budget_tokens: int
) -> tuple[list[Message], int]:
    """从最新往旧保留整组历史，返回 (保留的消息, 被省略的组数)。

    最新一组无论多大都保留 —— 宁可这一轮超预算，也不能让当前问题消失。
    """
    groups = split_into_groups(history)
    if not groups:
        return [], 0

    kept: list[list[Message]] = []
    used = 0
    for group in reversed(groups):
        cost = estimate_messages_tokens(group)
        if kept and used + cost > budget_tokens:
            break
        kept.append(group)
        used += cost
    kept.reverse()

    omitted = len(groups) - len(kept)
    return [message for group in kept for message in group], omitted


def _image_marker(message: Message) -> str:
    """把消息里的图片渲染成 `[图片 名字 宽×高] ` 前缀（图片本身不进摘要）。

    为什么压缩路径必须留下这个标记：摘要会**替代**原始消息，如果带图的那条被
    压成一句没有图的文本，模型之后会以为自己凭空少了一轮；而"用户发过一张图"
    这件事往往正是当时在讨论什么的关键。图片本身不进摘要（那是几 MB 的
    base64，而且摘要调用本身也不该再花一次图片 token）。
    """
    if not message.images:
        return ""
    return "".join(
        bundle().RENDER_IMAGE.format(
            name=image.name or "image", width=image.width, height=image.height
        )
        for image in message.images
    )


def render_for_summary(messages: list[Message], *, max_chars: int | None = 40000) -> str:
    """把要压缩的消息渲染成纯文本，供摘要调用使用。

    `max_chars=None` 表示**不截断**，用于"先量一量这段有多长"。
    截断是最后手段：调用方应当按**整组**累积到预算以内再送进来，
    否则会出现"被截掉的部分既没进摘要、又被标记为已覆盖"的信息永久丢失。
    """
    lines: list[str] = []
    for message in messages:
        if message.role == "user":
            lines.append(
                bundle().RENDER_USER.format(
                    text=f"{_image_marker(message)}{message.content}"
                )
            )
        elif message.role == "assistant":
            if message.content:
                lines.append(bundle().RENDER_ASSISTANT.format(text=message.content))
            for call in message.tool_calls:
                lines.append(
                    bundle().RENDER_CALL.format(name=call.name, arguments=call.arguments)
                )
        elif message.role == "tool":
            preview = message.content[:160]
            lines.append(
                bundle().RENDER_TOOL_RESULT.format(name=message.name or "", preview=preview)
            )
    text = "\n".join(lines)
    if max_chars is not None and len(text) > max_chars:
        # 超长时保留**开头**：较早的信息更需要被摘要固化下来
        text = text[:max_chars] + bundle().RENDER_TRUNCATED_SUFFIX
    return text


def build_messages(
    settings: Settings,
    history: list[Message],
    memory_text: str,
    *,
    summary_text: str = "",
    web_tools: str = "",
    vault_text: str = "",
    now: datetime | None = None,
) -> tuple[list[Message], int]:
    trimmed, omitted = trim_history(history, settings.history_budget())
    system = build_system_message(
        settings, memory_text, web_tools=web_tools, vault_text=vault_text
    )
    messages = [system]
    if summary_text:
        # 位置讲究：紧跟主 system。摘要不常变，放在前缀里对缓存最友好；
        # 而它一变（发生压缩）本来也只能作废一次。
        messages.append(Message(role="system", content=bundle().SUMMARY_HEADER + summary_text))
    messages += trimmed
    # 易变信息放最末：时间 / 裁剪提示一变，只有它们自己失效（见 build_tail_message）
    messages.append(
        build_tail_message(now=now or datetime.now().astimezone(), omitted_groups=omitted)
    )
    return messages, omitted
