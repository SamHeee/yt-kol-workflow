#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import unittest
from pathlib import Path

from filter.channel_classifier import FALLBACK_ASSESSMENT, classify_channel


class ClassifyChannelTests(unittest.TestCase):
    def test_outdoor_personal_channel_with_suspected_brand(self):
        result = classify_channel(
            channel_name="Sarah Outdoors",
            description=(
                "I'm Sarah. I review camping gear and share beginner tutorials. "
                "You can also visit my shop."
            ),
            recent_videos=[
                {"title": "Best Tent Review", "tags": ["outdoor", "camping"]},
                {"title": "How to Pitch a Tent", "tags": "tutorial,hiking"},
            ],
        )
        self.assertEqual(
            result,
            "领域=户外/露营; 内容=产品测评,教程; 主体=个人创作者; 自有品牌=疑似",
        )

    def test_explicit_founder_language_means_owned_brand(self):
        result = classify_channel(
            "John Doe",
            "I am John, founder of TrailLight. I make flashlight reviews.",
            [{"title": "Trail flashlight review", "tags": ["edc", "torch"]}],
        )
        self.assertIn("领域=EDC/照明", result)
        self.assertIn("主体=个人创作者", result)
        self.assertTrue(result.endswith("自有品牌=明确有"))

    def test_sponsor_or_affiliate_language_is_not_brand_ownership(self):
        result = classify_channel(
            "Camp Guide",
            "Camping tutorials. Sponsored by Acme; affiliate links below.",
            [{"title": "How to camp", "tags": "camping,tutorial"}],
        )
        self.assertTrue(result.endswith("自有品牌=未发现"))

    def test_generic_merch_store_is_not_brand_ownership(self):
        result = classify_channel(
            "Camp Guide",
            "Camping tutorials and reviews. Visit the merch store below.",
            [{"title": "How to camp", "tags": "camping,tutorial"}],
        )
        self.assertTrue(result.endswith("自有品牌=未发现"))

    def test_source_keyword_is_only_a_domain_signal(self):
        result = classify_channel("Unknown", "", [], source_keyword="户外露营")
        self.assertEqual(
            result,
            "领域=户外/露营; 内容=待确认; 主体=待确认; 自有品牌=未发现",
        )

    def test_official_brand_channel_entity(self):
        result = classify_channel(
            "Acme Official",
            "The official channel for Acme. Shop our camping products.",
            [{"title": "New tent first look", "tags": "camping,unboxing"}],
        )
        self.assertIn("主体=品牌官方", result)
        self.assertIn("自有品牌=疑似", result)

    def test_content_is_limited_to_three_whitelisted_labels(self):
        result = classify_channel(
            "Tech Channel",
            "Reviews, tutorials, unboxing, comparisons and news.",
            [
                {
                    "title": "Top 10 Phone Review: Unboxing and How To Guide News",
                    "tags": ["tech", "comparison", "tutorial", "vlog"],
                }
            ],
        )
        content_segment = result.split("; ")[1].removeprefix("内容=")
        labels = content_segment.split(",")
        self.assertLessEqual(len(labels), 3)

        taxonomy_path = Path(__file__).resolve().parents[1] / "channel_taxonomy.json"
        taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
        allowed = {item["label"] for item in taxonomy["content_types"]}
        self.assertTrue(set(labels).issubset(allowed))

    def test_blank_metadata_uses_unknown_labels_except_no_brand_evidence(self):
        self.assertEqual(
            classify_channel("", "", []),
            "领域=待确认; 内容=待确认; 主体=待确认; 自有品牌=未发现",
        )

    def test_bad_recent_video_shape_fails_safely(self):
        self.assertEqual(
            classify_channel("Sarah", "I'm Sarah", ["not-a-dict"]),
            FALLBACK_ASSESSMENT,
        )

    def test_output_always_has_fixed_four_part_format(self):
        result = classify_channel(
            "Travel Couple",
            "We are a couple sharing travel vlogs.",
            [{"title": "A day in my life in Paris", "tags": []}],
        )
        parts = result.split("; ")
        self.assertEqual(len(parts), 4)
        self.assertEqual(
            [part.split("=", 1)[0] for part in parts],
            ["领域", "内容", "主体", "自有品牌"],
        )


if __name__ == "__main__":
    unittest.main()
