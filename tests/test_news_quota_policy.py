from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_builder():
    path = PROJECT_ROOT / "scripts" / "14_seal_news_bundle.py"
    spec = importlib.util.spec_from_file_location(
        "integrated_news_bundle_builder",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import news bundle builder: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _article(category: str, ordinal: int) -> dict[str, str]:
    return {
        "article_id": f"news_20260302_{category}_{ordinal:08d}",
        "observed_at": f"2026-03-02T08:{ordinal:02d}:00+09:00",
    }


class NewsQuotaPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.builder = _load_builder()

    def test_missing_category_is_not_backfilled_from_another_category(self) -> None:
        articles = [
            *(_article("종목", ordinal) for ordinal in range(1, 5)),
            *(_article("섹터", ordinal) for ordinal in range(1, 11)),
            *(_article("경제", ordinal) for ordinal in range(1, 6)),
        ]

        selected = self.builder.select_slots(articles)
        categories = [
            self.builder._cat_of(article["article_id"])
            for article in selected
        ]

        self.assertEqual(len(selected), 9)
        self.assertEqual(categories.count("종목"), 4)
        self.assertEqual(categories.count("섹터"), 3)
        self.assertEqual(categories.count("경제"), 2)


if __name__ == "__main__":
    unittest.main()
