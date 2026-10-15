import pytest

from jobscraper.scraping.robots import Rules


@pytest.mark.parametrize(
    ("rules", "path", "allowed"),
    [
        ("Disallow: /\nAllow: /careers/", "/careers/sc", True),
        ("Allow: /\nDisallow: /private", "/private/job", False),
        ("Disallow: /careers/*/apply$", "/careers/sc/apply", False),
        ("Disallow: /careers/*/apply$", "/careers/sc/apply/help", True),
        ("Disallow: /jobs?*secret=", "/jobs?q=dev&secret=x", False),
        ("Disallow: /same\nAllow: /same", "/same", True),
        ("Disallow:", "/jobs", True),
        ("Disallow: /caf%C3%A9", "/café", False),
        ("Disallow: /foo/%62ar", "/foo/bar", False),
        ("Disallow: /foo%2Fbar", "/foo/bar", True),
        ("Disallow: /foo%2Fbar", "/foo%2fbar", False),
    ],
)
def test_rules(rules, path, allowed):
    policy = Rules("User-agent: *\n" + rules, "jobscraper/0.1")
    result, reason = policy.check("https://example.com" + path)
    assert result is allowed
    assert reason


def test_specific_groups_merge_and_override_wildcard():
    policy = Rules(
        """User-agent: *
Disallow: /
User-agent: jobscraper
Disallow: /private
Crawl-delay: 2.5
User-agent: JOBSCRAPER
Allow: /private/public
Crawl-delay: 3
""",
        "jobscraper/0.1",
    )
    assert policy.check("https://example.com/jobs")[0]
    assert policy.check("https://example.com/private/public")[0]
    assert not policy.check("https://example.com/private")[0]
    assert policy.delay == 3
