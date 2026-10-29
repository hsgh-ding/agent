import unittest

from rtl_agent.knowledge import search


class KnowledgeTests(unittest.TestCase):
    def test_chinese_query_retrieves_relevant_entry(self):
        entries = search("仿真失败错误日志", limit=2)
        self.assertTrue(entries)
        self.assertEqual(entries[0]["id"], "vivado-diagnostics")

    def test_ollama_tool_calling_entry_is_searchable(self):
        entries = search("Ollama Qwen3 原生工具调用", limit=5)
        self.assertIn("ollama-qwen3", [entry["id"] for entry in entries])

    def test_verification_skill_is_indexed_and_searchable(self):
        entries = search("Vivado compile simulation workflow", limit=10)
        self.assertIn("skill:rtl-verification", [entry["id"] for entry in entries])


if __name__ == "__main__":
    unittest.main()
