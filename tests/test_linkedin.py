import unittest

from linked_jobs_monitor.config import SearchConfig
from linked_jobs_monitor.database import SearchSource
from linked_jobs_monitor.linkedin import (
    build_search_url,
    build_search_urls_for_source,
    km_to_linkedin_distance,
    posted_within_filter,
)


class LinkedInUrlTests(unittest.TestCase):
    def test_km_to_linkedin_distance_converts_300_km_to_miles(self):
        self.assertEqual(km_to_linkedin_distance(300), 187)

    def test_posted_within_filter_uses_seconds(self):
        self.assertEqual(posted_within_filter(7), "r604800")

    def test_build_search_url_contains_expected_filters(self):
        config = SearchConfig(
            keywords=["Azure"],
            location="Gothenburg, Västra Götaland County, Sweden",
            geo_id="90009607",
            ai_search_url="",
            radius_km=300,
            posted_within_days=7,
            sort_by="DD",
        )

        url = build_search_url(config, "Azure")

        self.assertTrue(url.startswith("https://www.linkedin.com/jobs/search/?"))
        self.assertIn("keywords=Azure", url)
        self.assertIn("geoId=90009607", url)
        self.assertIn("distance=187", url)
        self.assertIn("f_TPR=r604800", url)
        self.assertIn("sortBy=DD", url)

    def test_source_urls_are_generated_from_keywords_not_manual_url(self):
        source = SearchSource(
            id=7,
            name="Azure",
            keywords="Azure",
            location="Gothenburg, Sweden",
            geo_id="90009607",
            ai_search_url="https://www.linkedin.com/jobs/search-results/?keywords=AI+manual",
            radius_km=300,
            posted_within_days=7,
            sort_by="DD",
            is_active=True,
            last_run_at="",
            created_at="",
            updated_at="",
        )

        urls = build_search_urls_for_source(source)

        self.assertEqual(len(urls), 1)
        self.assertIn("/jobs/search/?", urls[0].url)
        self.assertIn("keywords=Azure", urls[0].url)
        self.assertNotEqual(urls[0].url, source.ai_search_url)


if __name__ == "__main__":
    unittest.main()
