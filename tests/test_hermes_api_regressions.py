from pathlib import Path
import unittest


HERMES_API_PATH = Path(__file__).resolve().parents[1] / "src" / "lib" / "hermes-api.ts"


class HermesApiRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = HERMES_API_PATH.read_text(encoding="utf-8")

    def test_local_fetches_use_configurable_local_api_url_helper(self):
        self.assertIn("fetch(localApiUrl('/system')", self.source)
        self.assertIn("maybeFetchLocalJson<Partial<MissionControlKnowledgeSnapshot>>('/knowledge'", self.source)
        self.assertIn("maybeFetchLocalJson<MissionControlKnowledgeFilePayload>(\n      `/knowledge/file?path=${encodeURIComponent(sourcePath)}`", self.source)
        self.assertNotIn("fetch('/api/local/system'", self.source)
        self.assertNotIn("fetch('/api/local/knowledge'", self.source)
        self.assertNotIn("fetch(`/api/local/knowledge/file?path=${encodeURIComponent(sourcePath)}`", self.source)

    def test_knowledge_file_loader_catches_local_fetch_errors_before_core_fallback(self):
        marker = "export async function loadMissionControlKnowledgeFile("
        start = self.source.index(marker)
        end = self.source.index("export async function loadMissionControlTools(", start)
        function_body = self.source[start:end]

        self.assertIn("try {", function_body)
        self.assertIn("catch (error)", function_body)
        self.assertIn("if (error instanceof MissionControlAuthError)", function_body)
        self.assertIn("const response = await fetch(apiUrl(`/knowledge/file?path=${encodeURIComponent(sourcePath)}`)", function_body)


if __name__ == "__main__":
    unittest.main()
