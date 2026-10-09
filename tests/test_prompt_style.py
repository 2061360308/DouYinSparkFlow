import unittest

from core.ai.config import AIConfig, DEFAULT_PROMPT
from core.ai.engine import ReplyEngine


class PromptStyleTests(unittest.TestCase):
    def test_default_style_is_brief_natural_and_honest(self):
        self.assertIn('自然、日常', DEFAULT_PROMPT)
        self.assertIn('少用语气助词', DEFAULT_PROMPT)
        self.assertIn('不主动提及 AI', DEFAULT_PROMPT)
        self.assertIn('简短如实回答', DEFAULT_PROMPT)
        self.assertIn('不冒充账号本人', DEFAULT_PROMPT)
        self.assertEqual(AIConfig().system_prompt, DEFAULT_PROMPT)

    def test_style_and_media_guard_reach_model_for_group_and_private_chat(self):
        for group in (False, True):
            with self.subTest(group=group):
                engine = ReplyEngine(AIConfig(cooldown=0), started_at=100)
                pending = engine.prepare('a', [{'id':'1', 'created_at':101, 'from_me':False,
                                              'text':'😂', 'sender_id':'friend'}], now=102, is_group=group)
                messages = engine.messages(pending)
                self.assertIn(DEFAULT_PROMPT, messages[0]['content'])
                self.assertIn('不要声称看过视频', messages[0]['content'])
