"""
LiteLLM 커스텀 pre-call 훅: Bedrock Converse 비호환 콘텐츠 블록 정리.

Claude Desktop이 도구 호출(tool_use) 턴을 이력에 담을 때 빈 text 블록과
빈 thinking 블록(thinking="", signature="")을 함께 넣는데, Bedrock Converse는
"text content blocks must be non-empty" / "Invalid signature in thinking block"으로
거부한다. modify_params로는 이 조합이 정리되지 않아 훅에서 직접 제거한다.

제거 규칙 (assistant/user 메시지의 content 배열에 적용):
- type=text 이고 text가 비어있음(공백만 포함 포함) → 제거
- type=thinking 이고 thinking 또는 signature가 비어있음 → 제거
- 블록 제거 후 content 배열이 비면, 메시지가 유실되지 않도록 최소 처리

LiteLLM custom pre-call hook: clean up content blocks incompatible with Bedrock Converse.

When Claude Desktop includes tool_use turns in the history, it puts in empty text
blocks and empty thinking blocks (thinking="", signature="") together, which Bedrock
Converse rejects with "text content blocks must be non-empty" / "Invalid signature in
thinking block". modify_params does not clean up this combination, so we remove them
directly in the hook.

Removal rules (applied to the content array of assistant/user messages):
- type=text and text is empty (including whitespace-only) -> remove
- type=thinking and thinking or signature is empty -> remove
- If the content array becomes empty after block removal, apply minimal handling so the message is not lost
"""

from typing import Any, Optional

from litellm.integrations.custom_logger import CustomLogger


def _clean_content_blocks(content: list) -> list:
    cleaned = []
    for block in content:
        if not isinstance(block, dict):
            cleaned.append(block)
            continue
        btype = block.get("type")
        if btype == "text":
            if (block.get("text") or "").strip() == "":
                continue  # 빈/공백 text 블록 제거 / Remove empty/whitespace-only text blocks
        elif btype == "thinking":
            # 빈 thinking 또는 서명 없는 thinking은 Bedrock이 거부
            # Bedrock rejects empty thinking blocks or thinking blocks without a signature
            if not (block.get("thinking") or "").strip() or not (block.get("signature") or "").strip():
                continue
        elif btype == "redacted_thinking":
            if not block.get("data"):
                continue
        cleaned.append(block)
    return cleaned


class SanitizeBedrockBlocks(CustomLogger):
    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> Optional[dict]:
        messages = data.get("messages")
        if not isinstance(messages, list):
            return data

        for msg in messages:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            cleaned = _clean_content_blocks(content)
            # content 배열이 통째로 비면 tool_use 등 유효 블록이 없었다는 뜻이 아니라
            # 빈 블록만 있었던 경우다. 빈 배열은 Bedrock이 거부하므로 최소 텍스트를 넣는다.
            # If the content array becomes entirely empty, it does not mean there were no valid
            # blocks like tool_use — it means there were only empty blocks. Bedrock rejects an
            # empty array, so insert minimal text.
            if not cleaned:
                cleaned = [{"type": "text", "text": "(continue)"}]
            msg["content"] = cleaned

        return data


sanitize_bedrock_blocks_instance = SanitizeBedrockBlocks()
