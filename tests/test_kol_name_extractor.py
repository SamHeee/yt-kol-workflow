#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from filter.kol_name_extractor import MANUAL_CONFIRMATION, extract_kol_name


class ExtractKolNameTests(unittest.TestCase):
    def test_explicit_im_is_strong_evidence(self):
        result = extract_kol_name(
            channel_name="Sarah's Camping World",
            description="Welcome! I'm Sarah, and I review camping equipment.",
            email="business@example.com",
        )
        self.assertEqual(result, "Sarah")

    def test_explicit_my_name_is_preserves_full_name(self):
        result = extract_kol_name(
            channel_name="",
            description="My name is John Doe and I make weekly videos.",
            email="",
        )
        self.assertEqual(result, "John Doe")

    def test_lowercase_explicit_name_is_normalized_for_greeting(self):
        self.assertEqual(
            extract_kol_name("", "Hi! I am sarah.", ""),
            "Sarah",
        )

    def test_chinese_explicit_introduction_is_strong(self):
        self.assertEqual(
            extract_kol_name("露营日记", "大家好，我是小明，分享户外装备。", ""),
            "小明",
        )

    def test_channel_name_and_dotted_email_corrobate(self):
        self.assertEqual(
            extract_kol_name("Sarah Jones", "Outdoor videos.", "sarah.jones@example.com"),
            "Sarah Jones",
        )

    def test_channel_name_and_compact_email_corrobate(self):
        self.assertEqual(
            extract_kol_name("John Doe", "Outdoor videos.", "johndoe@example.com"),
            "John Doe",
        )

    def test_first_name_email_can_support_full_channel_name(self):
        self.assertEqual(
            extract_kol_name("Sarah Jones", "Outdoor videos.", "sarah@example.com"),
            "Sarah Jones",
        )

    def test_single_weak_channel_evidence_requires_manual_review(self):
        self.assertEqual(
            extract_kol_name("Sarah Jones", "Outdoor videos.", ""),
            MANUAL_CONFIRMATION,
        )

    def test_functional_mailbox_does_not_count_as_person_evidence(self):
        for prefix in ("info", "contact", "business", "support"):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    extract_kol_name(
                        "Sarah Jones",
                        "Outdoor videos.",
                        f"{prefix}@example.com",
                    ),
                    MANUAL_CONFIRMATION,
                )

    def test_conflicting_weak_sources_require_manual_review(self):
        self.assertEqual(
            extract_kol_name("Sarah Jones", "Outdoor videos.", "alice.smith@example.com"),
            MANUAL_CONFIRMATION,
        )

    def test_conflict_with_explicit_introduction_requires_manual_review(self):
        self.assertEqual(
            extract_kol_name(
                "Alice Smith",
                "I'm Sarah and I make outdoor videos.",
                "alice.smith@example.com",
            ),
            MANUAL_CONFIRMATION,
        )

    def test_multiple_conflicting_introductions_require_manual_review(self):
        self.assertEqual(
            extract_kol_name(
                "",
                "I'm Sarah. In another section, my name is Alice.",
                "",
            ),
            MANUAL_CONFIRMATION,
        )

    def test_first_person_adjective_is_not_mistaken_for_name(self):
        self.assertEqual(
            extract_kol_name(
                "Outdoor Learning",
                "I am passionate about helping people enjoy camping.",
                "business@example.com",
            ),
            MANUAL_CONFIRMATION,
        )

    def test_descriptive_this_is_phrase_is_not_mistaken_for_name(self):
        self.assertEqual(
            extract_kol_name(
                "Outdoor Learning",
                "This is where I share my newest camping videos.",
                "business@example.com",
            ),
            MANUAL_CONFIRMATION,
        )

    def test_brand_shaped_channel_is_not_person_evidence(self):
        self.assertEqual(
            extract_kol_name("Trail Gear Reviews", "Camping reviews.", "trailgear@example.com"),
            MANUAL_CONFIRMATION,
        )

    def test_alias_is_preferred_over_camelcase_channel_handle(self):
        self.assertEqual(
            extract_kol_name(
                "ShinySaffichu",
                "I'm ShinySaffichu or you could just call me Saffie.",
                "",
            ),
            "Saffie",
        )

    def test_acronym_and_camelcase_introductions_require_review(self):
        for channel, description in (
            ("LSQ", "Hey I'm LSQ!"),
            ("MowerOfTheLawn", "I'm MowerOfTheLawn and I make videos."),
            ("PokeCT", "Hey, TOP Pokemon TCG content right here!"),
        ):
            with self.subTest(channel=channel):
                self.assertEqual(
                    extract_kol_name(channel, description, ""),
                    MANUAL_CONFIRMATION,
                )

    def test_descriptor_channel_and_email_agreement_is_not_a_person(self):
        for channel, email in (
            ("Yalan App", "yalanapp@example.com"),
            ("SHIVAM SQUAD", "shivamsquad@example.com"),
            ("Ara's Easy Art", "araseasyart@example.com"),
            ("Jess Wang Pastry", "jesswangpastry@example.com"),
        ):
            with self.subTest(channel=channel):
                self.assertEqual(
                    extract_kol_name(channel, "Product videos.", email),
                    MANUAL_CONFIRMATION,
                )


if __name__ == "__main__":
    unittest.main()
