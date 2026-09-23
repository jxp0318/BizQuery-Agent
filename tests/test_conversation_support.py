"""P1 历史会话基础能力的无外部依赖测试。"""

import asyncio
import unittest
from datetime import datetime

from pydantic import ValidationError

from app.api.schemas.conversation_schema import (
    ConversationCreateSchema,
    ConversationMessageSchema,
    ConversationSummarySchema,
)
from app.services.conversation_lock_manager import ConversationLockManager


class ConversationSchemaTests(unittest.TestCase):
    def test_title_is_normalized(self):
        payload = ConversationCreateSchema(title="  一季度   GMV  ")
        self.assertEqual(payload.title, "一季度 GMV")

    def test_blank_title_is_rejected(self):
        with self.assertRaises(ValidationError):
            ConversationCreateSchema(title="   ")

    def test_api_response_uses_camel_case(self):
        now = datetime(2026, 9, 19, 12, 0, 0)
        summary = ConversationSummarySchema(
            id="conversation-id",
            title="测试",
            created_at=now,
            updated_at=now,
            message_count=2,
        )
        payload = summary.model_dump(by_alias=True)
        self.assertIn("createdAt", payload)
        self.assertIn("updatedAt", payload)
        self.assertEqual(payload["messageCount"], 2)

    def test_null_message_steps_are_restored_as_empty_list(self):
        now = datetime(2026, 9, 19, 12, 0, 0)
        message = ConversationMessageSchema.model_validate(
            {
                "id": "message-id",
                "conversation_id": "conversation-id",
                "role": "user",
                "content": "统计 GMV",
                "status": "done",
                "steps": None,
                "created_at": now,
            }
        )
        self.assertEqual(message.steps, [])


class ConversationLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_conversation_reuses_lock(self):
        manager = ConversationLockManager()
        first = await manager.get_lock("conversation-1")
        second = await manager.get_lock("conversation-1")
        other = await manager.get_lock("conversation-2")
        self.assertIs(first, second)
        self.assertIsNot(first, other)

    async def test_same_conversation_is_serial_and_other_conversation_is_parallel(self):
        manager = ConversationLockManager()
        first = await manager.get_lock("conversation-1")
        same = await manager.get_lock("conversation-1")
        other = await manager.get_lock("conversation-2")

        await first.acquire()
        try:
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(same.acquire(), timeout=0.01)
            await asyncio.wait_for(other.acquire(), timeout=0.1)
            other.release()
        finally:
            first.release()


if __name__ == "__main__":
    unittest.main()
