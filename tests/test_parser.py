import unittest

from linked_jobs_monitor.parser import (
    extract_detail_jobs,
    extract_job_id_from_url,
    extract_jobs,
)


SEARCH_HTML = """
<li>
  <div class="base-card base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:4408953784">
    <a class="base-card__full-link" href="https://se.linkedin.com/jobs/view/infrastructure-engineer-%E2%80%93-hybrid-compute-amp-avd-at-vitrolife-group-4408953784?position=1"></a>
    <div class="base-search-card__info">
      <h3 class="base-search-card__title">Infrastructure Engineer – Hybrid Compute &amp;amp; AVD</h3>
      <h4 class="base-search-card__subtitle">
        <a href="https://se.linkedin.com/company/vitrolife">Vitrolife Group</a>
      </h4>
      <div class="base-search-card__metadata">
        <span class="job-search-card__location">Västra Frölunda, Västra Götaland County, Sweden</span>
        <div class="job-posting-benefits">
          <span class="job-posting-benefits__text">Be an early applicant</span>
        </div>
        <time class="job-search-card__listdate" datetime="2026-05-05">1 day ago</time>
      </div>
    </div>
  </div>
</li>
"""


DETAIL_HTML = """
<link rel="canonical" href="https://se.linkedin.com/jobs/view/infrastructure-engineer-at-vitrolife-group-4408953784">
<script type="application/ld+json">
{
  "@context": "http://schema.org",
  "@type": "JobPosting",
  "datePosted": "2026-05-05T02:53:51.000Z",
  "validThrough": "2026-05-20T14:54:44.000Z",
  "employmentType": "FULL_TIME",
  "industry": "Biotechnology Research",
  "title": "Infrastructure Engineer – Hybrid Compute &amp; AVD",
  "description": "Work with Azure Virtual Desktop&lt;br&gt;&lt;br&gt;Applications reviewed ongoing.",
  "hiringOrganization": {
    "@type": "Organization",
    "name": "Vitrolife Group",
    "sameAs": "https://se.linkedin.com/company/vitrolife"
  },
  "jobLocation": {
    "@type": "Place",
    "address": {
      "@type": "PostalAddress",
      "addressCountry": "SE",
      "addressLocality": "Västra Frölunda"
    }
  }
}
</script>
<h1 class="top-card-layout__title">Infrastructure Engineer – Hybrid Compute &amp;amp; AVD</h1>
<span class="posted-time-ago__text topcard__flavor--metadata">1 day ago</span>
<figcaption class="num-applicants__caption">Be among the first 25 applicants</figcaption>
<ul class="description__job-criteria-list">
  <li class="description__job-criteria-item">
    <h3 class="description__job-criteria-subheader">Seniority level</h3>
    <span class="description__job-criteria-text description__job-criteria-text--criteria">Entry level</span>
  </li>
  <li class="description__job-criteria-item">
    <h3 class="description__job-criteria-subheader">Job function</h3>
    <span class="description__job-criteria-text description__job-criteria-text--criteria">Information Technology</span>
  </li>
</ul>
"""


class ParserTests(unittest.TestCase):
    def test_extract_job_id_from_slug_url_uses_trailing_id(self):
        url = "https://se.linkedin.com/jobs/view/3-month-temporary-courier-at-fedex-4410565804/"

        self.assertEqual(extract_job_id_from_url(url), "4410565804")

    def test_extract_search_card_metadata(self):
        jobs = extract_jobs(SEARCH_HTML, keyword="Azure", source_url="https://example.test/search")

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.job_id, "4408953784")
        self.assertEqual(job.url, "https://www.linkedin.com/jobs/view/4408953784/")
        self.assertEqual(job.title, "Infrastructure Engineer – Hybrid Compute & AVD")
        self.assertEqual(job.company, "Vitrolife Group")
        self.assertEqual(job.company_url, "https://se.linkedin.com/company/vitrolife")
        self.assertEqual(job.location, "Västra Frölunda, Västra Götaland County, Sweden")
        self.assertEqual(job.posted_at, "2026-05-05")
        self.assertEqual(job.posted_text, "1 day ago")
        self.assertEqual(job.insight, "Be an early applicant")
        self.assertEqual(job.keyword, "Azure")
        self.assertEqual(job.source_url, "https://example.test/search")

    def test_extract_detail_json_ld_metadata(self):
        jobs = extract_detail_jobs(DETAIL_HTML, keyword="Azure")

        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.job_id, "4408953784")
        self.assertEqual(job.title, "Infrastructure Engineer – Hybrid Compute & AVD")
        self.assertEqual(job.company, "Vitrolife Group")
        self.assertEqual(job.location, "Västra Frölunda, SE")
        self.assertEqual(job.posted_at, "2026-05-05T02:53:51.000Z")
        self.assertEqual(job.application_deadline, "2026-05-20T14:54:44.000Z")
        self.assertEqual(job.employment_type, "FULL_TIME")
        self.assertEqual(job.industries, "Biotechnology Research")
        self.assertEqual(job.posted_text, "1 day ago")
        self.assertEqual(job.applicants, "Be among the first 25 applicants")
        self.assertEqual(job.seniority_level, "Entry level")
        self.assertEqual(job.job_function, "Information Technology")
        self.assertIn("Azure Virtual Desktop", job.description)


if __name__ == "__main__":
    unittest.main()
